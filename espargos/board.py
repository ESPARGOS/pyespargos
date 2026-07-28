#!/usr/bin/env python

from __future__ import annotations

import websockets.sync.client
import http.client
import threading
import logging
import socket
import json
from dataclasses import dataclass
from typing import Any, Callable

from . import revisions
from . import sensor
from . import uart

# Port used by the controller as the source port for UDP sensor packets
STREAM_CONTROLLER_SRC_PORT = 53330
WEBSOCKET_STREAM_PATHS = ("stream", "csi")
UDP_STREAM_CONTROL_PATHS = ("stream_udp", "csi_udp")


class EspargosHTTPStatusError(Exception):
    "Raised when the ESPARGOS HTTP API returns an invalid status code"

    def __init__(self, status=None, path=None, body=None):
        self.status = status
        self.path = path
        self.body = body

        message = "ESPARGOS HTTP API returned an invalid status code"
        if status is not None:
            message += f" {status}"
        if path is not None:
            message += f" for /{path}"
        if body:
            message += f": {body}"
        super().__init__(message)


class EspargosUnexpectedResponseError(Exception):
    "Raised when the server (ESPARGOS controller) provides unexpected response. Is the server really ESPARGOS?"

    pass


class EspargosStreamConnectionError(Exception):
    "Raised when the sensor-message stream connection could not be established."

    pass


class EspargosAPIVersionError(Exception):
    "Raised when the ESPARGOS controller runs an unsupported API version"

    pass


@dataclass(frozen=True, eq=False)
class SensorMessageSubscription:
    """Handle for one sensor-message callback subscription."""

    type_header: int
    callback: Callable[[sensor.SensorMessage[bytes]], None]


# Magic bytes sent by the controller to confirm a valid sensor stream
STREAM_MAGIC = bytes([0xE5, 0xA7, 0x60, 0x00])

# Only this major API version is supported
SUPPORTED_API_MAJOR = 3


class BoardControl:
    """Public, message-agnostic access to the controller request API."""

    def __init__(self, board: Board):
        self._board = board

    def fetch(self, path: str, data: str | bytes | None = None) -> str:
        """Fetch one controller endpoint and return its response as text.

        Supplying ``data`` sends a POST request; otherwise a GET request is
        used.
        """

        path = path.lstrip("/")
        if not path:
            raise ValueError("path must not be empty")
        return self._board._fetch(path, data)

    def get_json(self, path: str) -> Any:
        """GET a controller endpoint and decode its JSON response."""

        response = self.fetch(path)
        try:
            return json.loads(response)
        except (json.JSONDecodeError, TypeError):
            self._board.logger.error(f"Invalid response: {response}")
            raise EspargosUnexpectedResponseError(str(response))

    def post_json(self, path: str, payload: Any) -> str:
        """POST a JSON value and return the controller response as text."""

        return self.fetch(path, json.dumps(payload))

    def command(
        self,
        path: str,
        payload: Any | None = None,
        expected_response: str = "ok",
    ) -> str:
        """Call a command endpoint and require its expected text response."""

        response = self.fetch(path) if payload is None else self.post_json(path, payload)
        if response != expected_response:
            self._board.logger.error(f"Invalid response: {response}")
            raise EspargosUnexpectedResponseError(str(response))
        return response


class BoardCapability:
    """Base class for a lazily instantiated Board capability.

    Extensions normally subclass this class, put controller operations on that
    subclass, and register it with :meth:`Board.register_capability`.  The
    protected subscription helper decodes feature-specific sensor messages and
    ensures that :meth:`close` can detach them as a group.
    """

    def __init__(self, board: Board):
        self.board = board
        self._subscriptions: list[SensorMessageSubscription] = []
        self._subscriptions_lock = threading.Lock()

    def _subscribe_decoded_sensor_messages(
        self,
        type_header: int,
        decoder: Callable[[bytes], Any],
        callback: Callable[[sensor.SensorMessage[Any]], None],
    ) -> SensorMessageSubscription:
        """Subscribe to and decode one sensor-message type for this capability."""

        def decode_and_dispatch(message: sensor.SensorMessage[bytes]):
            try:
                decoded_message = sensor.decode_sensor_message(message, decoder)
            except (AssertionError, ValueError) as exc:
                self.board.logger.debug(f"Ignoring malformed sensor message: {exc}")
                return
            callback(decoded_message)

        subscription = self.board.subscribe_sensor_messages(
            type_header,
            decode_and_dispatch,
        )
        with self._subscriptions_lock:
            self._subscriptions.append(subscription)
        return subscription

    def close(self):
        """Remove every sensor-message subscription owned by this capability."""

        with self._subscriptions_lock:
            subscriptions = self._subscriptions
            self._subscriptions = []
        for subscription in subscriptions:
            self.board.unsubscribe_sensor_messages(subscription)

    def unsubscribe(self, subscription: SensorMessageSubscription) -> bool:
        """Remove one subscription previously returned by this capability."""

        with self._subscriptions_lock:
            try:
                self._subscriptions.remove(subscription)
            except ValueError:
                return False
        return self.board.unsubscribe_sensor_messages(subscription)


class Board(object):
    _stream_timeout = 5
    _capability_factories: dict[str, Callable[[Board], BoardCapability]] = {}
    _capability_factories_lock = threading.Lock()

    @classmethod
    def register_capability(
        cls,
        name: str,
        factory: Callable[[Board], BoardCapability],
    ):
        """Register a capability factory used by existing and future boards.

        The factory receives the board instance and is called at most once per
        board, when ``board.<name>`` or :meth:`get_capability` first requests
        it. Registering a different factory under an existing name is rejected.
        """

        if not isinstance(name, str) or not name.isidentifier() or name.startswith("_"):
            raise ValueError("capability name must be a public Python identifier")
        if not callable(factory):
            raise TypeError("capability factory must be callable")
        with cls._capability_factories_lock:
            existing = cls._capability_factories.get(name)
            if existing is not None and existing is not factory:
                raise ValueError(f"Board capability {name!r} is already registered")
            cls._capability_factories[name] = factory

    @classmethod
    def registered_capabilities(cls) -> tuple[str, ...]:
        """Return the names of all registered Board capabilities."""

        with cls._capability_factories_lock:
            return tuple(sorted(cls._capability_factories))

    def has_capability(self, name: str) -> bool:
        """Return whether a capability with this name is registered."""

        return name in type(self).registered_capabilities()

    def get_capability(self, name: str) -> BoardCapability:
        """Return one cached capability, creating it on first access."""

        with self._capabilities_lock:
            capability = self._capabilities.get(name)
            if capability is not None:
                return capability
            with type(self)._capability_factories_lock:
                factory = type(self)._capability_factories.get(name)
            if factory is None:
                raise AttributeError(f"Board has no registered capability {name!r}")
            capability = factory(self)
            self._capabilities[name] = capability
            return capability

    def __getattr__(self, name: str):
        if self.has_capability(name):
            return self.get_capability(name)
        raise AttributeError(f"{type(self).__name__!s} has no attribute {name!r}")

    def __init__(self, host: str):
        """
        Connect to an ESPARGOS controller and identify its hardware and API.

        Feature-specific configuration and message decoding are provided by
        registered Board capabilities.

        :param host: The IP address or hostname of the ESPARGOS controller

        :raises TimeoutError: If the connection to the ESPARGOS controller times out
        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller or the request was invalid
        """
        self.logger = logging.getLogger("pyespargos.board")

        self.host = host
        self.control = BoardControl(self)
        self._capabilities: dict[str, BoardCapability] = {}
        self._capabilities_lock = threading.RLock()
        self._uart_client = None
        self._transport_kind = "network"
        if uart.is_uart_host(host):
            self._transport_kind = "uart"
            self._uart_client = uart.UARTClient(host)
            self._uart_client.add_log_callback(self._handle_uart_log)
            self._uart_client.connect()
        try:
            identification_raw = self.control.fetch("identify")
        except TimeoutError:
            self.logger.error(f"Could not connect to {self.host} to fetch identification information")
            raise TimeoutError

        if not "ESPARGOS-DENSIFLORUS" in identification_raw:
            raise EspargosUnexpectedResponseError(f"Server at {self.host} does not look like an ESPARGOS controller. Check if the host is correct.")

        try:
            api_info_raw = self.control.fetch("api_info")
            try:
                api_info = json.loads(api_info_raw)
            except (json.JSONDecodeError, TypeError):
                raise EspargosUnexpectedResponseError(f"Server at {self.host} did not provide valid API information. Check if the host is correct and the server is running ESPARGOS firmware.")
        except TimeoutError:
            self.logger.error(f"Could not connect to {self.host} to fetch API information")
            raise TimeoutError
        except EspargosHTTPStatusError:
            raise EspargosAPIVersionError(f"ESPARGOS controller at {self.host} did not provide API version information. " f"This version of pyespargos only supports API major version {SUPPORTED_API_MAJOR}. " "Please update the controller firmware.")

        if "api-major" not in api_info or "api-minor" not in api_info:
            raise EspargosUnexpectedResponseError(f"Server at {self.host} did not provide API version information in api_info response.")

        api_major = api_info["api-major"]
        api_minor = api_info.get("api-minor", 0)

        if api_major != SUPPORTED_API_MAJOR:
            raise EspargosAPIVersionError(
                f"ESPARGOS controller at {self.host} runs API version {api_major}.{api_minor}, "
                f"but this version of pyespargos only supports API major version {SUPPORTED_API_MAJOR}. " + ("Please update pyespargos." if api_major > SUPPORTED_API_MAJOR else "Please update the controller firmware.")
            )

        self.api_version = (api_major, api_minor)

        self.revision = None
        device = api_info.get("device", "")
        revision_name = api_info.get("revision", "")
        for rev in revisions.all_revisions:
            if (device, revision_name) == rev.identification:
                self.revision = rev
                break

        if self.revision is None:
            raise EspargosUnexpectedResponseError(f"Unknown ESPARGOS revision: device={device!r}, revision={revision_name!r}")

        self.netconf = self.control.get_json("get_netconf")
        self.ip_info = self.control.get_json("get_ip_info")

        self.logger.info(f"Identified ESPARGOS at {self.ip_info['ip']} as {self.get_name()}")

        self.stream_connected = False
        self._sensor_message_reassembler = sensor.SensorMessageReassembler(logger=self.logger)
        self._sensor_message_subscriptions: dict[int, list[SensorMessageSubscription]] = {}
        self._sensor_message_subscriptions_lock = threading.Lock()

    def get_name(self):
        """
        Returns the hostname of the ESPARGOS controller as configured in the web interface.

        :return: The hostname of the ESPARGOS controller
        """
        return self.netconf["hostname"]

    def start(self, transports=None):
        """
        Start receiving sensor messages from the ESPARGOS controller.

        The controller forwards transport packets containing fragments from its
        sensors. This board reassembles complete sensor messages and dispatches
        them to callbacks registered with :meth:`subscribe_sensor_messages`.
        Reception continues until :meth:`stop` is called.

        Supported transports:

        - "udp": The controller sends sensor packets to a local UDP socket. This transport is lower-latency and more efficient (higher throughput), but may not work in all network environments.
        - "websocket": The controller sends sensor packets over a WebSocket connection. This transport is more widely compatible but may have higher latency and overhead.
        - "uart": The controller streams sensor packets over the local serial/UART link. This transport is only available for hosts specified as ``uart:<port>``.

        :param transports: Optional list of transports to try, in order of
            preference. Network boards support ``"udp"`` and ``"websocket"``;
            UART boards use ``"uart"``. If omitted, network boards try UDP
            first and then WebSocket, while UART boards use UART.

        :raises EspargosStreamConnectionError: If none of the enabled
            sensor-message transports can be established.
        """
        if self._transport_kind == "uart":
            transports = ["uart"] if transports is None else transports
        elif transports is None:
            transports = ["udp", "websocket"]

        for transport in transports:
            if transport == "uart":
                uart_error = self._try_start_uart()
                if uart_error is None:
                    return

                self.logger.warning(f"UART sensor stream failed for {self.get_name()}: {uart_error}")
            elif transport == "udp":
                udp_error = self._try_start_udp()
                if udp_error is None:
                    return

                self.logger.warning(f"UDP sensor stream failed for {self.get_name()}: {udp_error}")
            elif transport == "websocket":
                ws_error = self._try_start_websocket()
                if ws_error is None:
                    return

                self.logger.warning(f"WebSocket sensor stream failed for {self.get_name()}: {ws_error}")
            else:
                self.logger.error(f"Unknown transport {transport} specified for {self.get_name()}, skipping")

        raise EspargosStreamConnectionError(f"Could not establish sensor stream to {self.host} via any of the enabled transports, tried transports: {transports}")

    def _try_start_uart(self) -> str | None:
        if self._uart_client is None:
            return f"Host {self.host!r} is not a UART host"

        self.logger.info(f"Trying UART sensor stream for {self.get_name()}")

        def _callback(payload: bytes):
            self._stream_handle_message(payload)

        self._uart_stream_callback = _callback
        self._uart_client.add_stream_callback(self._uart_stream_callback)
        try:
            self._uart_client.enable_sensor_stream()
        except Exception as e:
            self._uart_client.remove_stream_callback(self._uart_stream_callback)
            return f"Could not enable UART sensor stream: {e}"

        self._stream_transport = "uart"
        self.stream_connected = True
        self.logger.info(f"Started UART sensor stream for {self.get_name()} on {self.host}")
        return None

    def _try_start_udp(self) -> str | None:
        """
        Try to start the sensor stream via UDP.
        Returns None on success, or an error message string on failure.
        """
        self.logger.info(f"Trying UDP sensor stream for {self.get_name()}")

        # Resolve remote endpoint first so we can create a socket with the right family
        try:
            host_is_bracketed_ipv6 = self.host.startswith("[") and self.host.endswith("]")
            udp_host = self.host[1:-1] if host_is_bracketed_ipv6 else self.host
            host_for_ipv6_check = udp_host.split("%", 1)[0]
            try:
                socket.inet_pton(socket.AF_INET6, host_for_ipv6_check)
                host_is_ipv6_literal = True
            except OSError:
                host_is_ipv6_literal = False
            preferred_family = socket.AF_INET6 if (host_is_bracketed_ipv6 or host_is_ipv6_literal) else socket.AF_INET
            udp_info = socket.getaddrinfo(udp_host, STREAM_CONTROLLER_SRC_PORT, preferred_family, socket.SOCK_DGRAM)
            if len(udp_info) == 0:
                return f"Could not resolve UDP endpoint for host {self.host}"
            udp_family, udp_socktype, udp_proto, _, udp_remote_addr = udp_info[0]
        except OSError as e:
            return f"Could not resolve UDP endpoint for host {self.host}: {e}"

        # Open a local UDP socket on an ephemeral port
        try:
            udp_sock = socket.socket(udp_family, udp_socktype, udp_proto)
            if udp_family == socket.AF_INET6:
                udp_sock.bind(("::", 0, 0, 0))
            else:
                udp_sock.bind(("", 0))
            udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
            local_port = udp_sock.getsockname()[1]
        except OSError as e:
            return f"Could not create UDP socket: {e}"

        # Send an empty packet to the controller's source port to punch a hole
        # in the Windows firewall so that incoming UDP packets are allowed through
        try:
            udp_sock.sendto(b"", udp_remote_addr)
        except OSError as e:
            self.logger.warning(f"Could not send firewall-punch packet: {e}")

        # Tell the server to start streaming to us via UDP
        udp_control_error = self._enable_udp_stream(local_port)
        if udp_control_error is not None:
            udp_sock.close()
            return udp_control_error

        # Wait for the magic packet on the UDP socket
        udp_sock.settimeout(3)
        try:
            data, addr = udp_sock.recvfrom(1024)
        except socket.timeout:
            udp_sock.close()
            self._disable_udp_stream()
            return "Timeout waiting for UDP magic packet"
        except OSError as e:
            udp_sock.close()
            self._disable_udp_stream()
            return f"Error receiving UDP magic packet: {e}"

        if data != STREAM_MAGIC:
            udp_sock.close()
            self._disable_udp_stream()
            return f"Invalid UDP magic packet: expected {STREAM_MAGIC.hex()}, got {data.hex()}"

        # UDP stream established successfully
        self._udp_sock = udp_sock
        self._udp_remote_addr = udp_remote_addr
        self._stream_transport = "udp"
        self.stream_connected = True
        self._stream_thread = threading.Thread(target=self._stream_loop_udp)
        self._stream_thread.start()

        # Start keepalive thread that periodically sends empty packets to punch through the firewall
        self._udp_keepalive_stop = threading.Event()
        self._udp_keepalive_thread = threading.Thread(target=self._udp_keepalive_loop, daemon=True)
        self._udp_keepalive_thread.start()

        self.logger.info(f"Started UDP sensor stream for {self.get_name()} on local port {local_port}")
        return None

    def _enable_udp_stream(self, local_port: int) -> str | None:
        errors = []
        for path in UDP_STREAM_CONTROL_PATHS:
            try:
                self.control.command(path, {"enable": True, "port": local_port})
            except Exception as exc:
                errors.append(f"/{path}: {exc}")
                continue
            self._udp_stream_control_path = path
            return None
        return f"HTTP request to enable UDP stream failed ({'; '.join(errors)})"

    def _try_start_websocket(self) -> str | None:
        """Try the generic WebSocket endpoint, then its legacy alias."""

        errors = []
        for path in WEBSOCKET_STREAM_PATHS:
            error = self._try_start_websocket_path(path)
            if error is None:
                return None
            errors.append(f"/{path}: {error}")
        return "; ".join(errors)

    def _try_start_websocket_path(self, path: str) -> str | None:
        self.logger.info(f"Trying WebSocket sensor stream for {self.get_name()} via /{path}")

        self._stream_ready_event = threading.Event()
        self._stream_error = None
        self._stream_transport = "websocket"
        self._stream_websocket_path = path
        self._stream_thread = threading.Thread(target=self._stream_loop_websocket, args=(path,))
        self._stream_thread.start()

        if not self._stream_ready_event.wait(timeout=3):
            self.stream_connected = False
            self._stream_thread.join()
            return "Connection was not established within 3 seconds"

        if self._stream_error is not None:
            self.stream_connected = False
            self._stream_thread.join()
            return str(self._stream_error)

        self.logger.info(f"Started WebSocket sensor stream for {self.get_name()} via /{path}")
        return None

    def _disable_udp_stream(self):
        """Tell the server to stop the UDP stream (best-effort)."""
        try:
            path = getattr(self, "_udp_stream_control_path", UDP_STREAM_CONTROL_PATHS[-1])
            self.control.command(path, {"enable": False})
        except Exception:
            pass

    def stop(self):
        """
        Stop receiving sensor messages from the ESPARGOS controller.

        The receiver stops after the current packet has been processed, or
        after a short transport timeout.
        """
        if self.stream_connected:
            self.stream_connected = False
            if hasattr(self, "_stream_thread"):
                self._stream_thread.join()

            if getattr(self, "_stream_transport", None) == "udp":
                if hasattr(self, "_udp_keepalive_stop"):
                    self._udp_keepalive_stop.set()
                    self._udp_keepalive_thread.join()
                if hasattr(self, "_udp_sock"):
                    self._udp_sock.close()
                self._disable_udp_stream()
            elif getattr(self, "_stream_transport", None) == "uart":
                if hasattr(self, "_uart_stream_callback"):
                    self._uart_client.remove_stream_callback(self._uart_stream_callback)
                if self._uart_client is not None:
                    self._uart_client.disable_sensor_stream()

            self.logger.info(f"Stopped sensor stream for {self.get_name()}")

    def close(self):
        """
        Close capabilities and transport resources associated with this board.

        For UART-backed boards, this releases the serial port lock. Calling this on
        network-backed boards is harmless.
        """
        with self._capabilities_lock:
            capabilities = list(self._capabilities.values())
            self._capabilities = {}
        for capability in capabilities:
            capability.close()
        self.stop()
        if self._uart_client is not None:
            self._uart_client.close()

    def reboot(self):
        """
        Trigger a controller reboot.

        The controller responds with ``"ok"`` and then reboots shortly after
        sending the reply.

        :raises EspargosUnexpectedResponseError: If the server at the given host
            is not an ESPARGOS controller or the request was invalid
        """
        self.control.command("reboot")

    def subscribe_sensor_messages(
        self,
        type_header: int,
        callback: Callable[[sensor.SensorMessage[bytes]], None],
    ) -> SensorMessageSubscription:
        """Subscribe a callback to one raw sensor-message type.

        The callback receives a complete, reassembled
        :class:`sensor.SensorMessage` whose payload is still raw ``bytes``.
        Parsing belongs to the consumer, keeping message-specific knowledge out
        of the board transport. Callbacks run on the stream thread and must
        therefore return quickly without performing blocking or expensive work.

        :param type_header: Four-byte logical message type as an integer.
        :param callback: Callable accepting a raw, reassembled
            :class:`sensor.SensorMessage`.
        :return: Subscription handle accepted by
            :meth:`unsubscribe_sensor_messages`.
        :raises ValueError: If the type header is invalid.
        :raises TypeError: If ``callback`` is not callable.
        """
        if not isinstance(type_header, int) or not 0 <= type_header <= 0xFFFFFFFF:
            raise ValueError("type_header must be a 32-bit unsigned integer")
        if not callable(callback):
            raise TypeError("callback must be callable")
        subscription = SensorMessageSubscription(
            type_header=type_header,
            callback=callback,
        )
        with self._sensor_message_subscriptions_lock:
            subscriptions = self._sensor_message_subscriptions.setdefault(type_header, [])
            subscriptions.append(subscription)
        return subscription

    def unsubscribe_sensor_messages(self, subscription: SensorMessageSubscription) -> bool:
        """Remove a sensor-message subscription.

        The type-header entry is discarded automatically when its final
        callback unsubscribes.

        :param subscription: Handle returned by
            :meth:`subscribe_sensor_messages`.
        :return: ``True`` if the subscription was active.
        """
        if not isinstance(subscription, SensorMessageSubscription):
            return False
        with self._sensor_message_subscriptions_lock:
            subscriptions = self._sensor_message_subscriptions.get(subscription.type_header)
            if subscriptions is None:
                return False
            try:
                subscriptions.remove(subscription)
            except ValueError:
                return False
            if not subscriptions:
                del self._sensor_message_subscriptions[subscription.type_header]
            return True

    def _stream_handle_message(self, message):
        try:
            sensor_packet = sensor.SensorPacket.from_bytes(message)
        except ValueError as exc:
            self.logger.debug(f"Ignoring malformed sensor packet: {exc}")
            return

        completed_messages = self._sensor_message_reassembler.push(sensor_packet)

        for message in completed_messages:
            try:
                type_header = sensor.get_sensor_message_type_header(message)
            except ValueError:
                self.logger.debug("Ignoring sensor message without a logical type header")
                continue

            with self._sensor_message_subscriptions_lock:
                subscriptions = tuple(self._sensor_message_subscriptions.get(type_header, ()))
            if not subscriptions:
                continue

            for subscription in subscriptions:
                try:
                    subscription.callback(message)
                except Exception:
                    self.logger.exception(f"Sensor-message callback failed for type 0x{type_header:08x}")

    def _udp_keepalive_loop(self):
        """Periodically send empty UDP packets to the controller to keep the firewall hole open."""
        while not self._udp_keepalive_stop.wait(timeout=1.0):
            try:
                self._udp_sock.sendto(b"", self._udp_remote_addr)
            except OSError:
                break

    def _stream_loop_udp(self):
        self._udp_sock.settimeout(0.2)
        timeout_total = 0
        while self.stream_connected:
            try:
                data, addr = self._udp_sock.recvfrom(65535)
                timeout_total = 0
                self._stream_handle_message(data)
            except socket.timeout:
                timeout_total += 0.2
            except OSError as e:
                self.logger.error(f"Board {self.host} has error in UDP socket: {e}")
                self.stream_connected = False
                break

            if timeout_total > self._stream_timeout:
                self.logger.warning("UDP timeout, disconnecting")
                self.stream_connected = False

    def _stream_loop_websocket(self, path: str):
        try:
            ws = websockets.sync.client.connect(
                f"ws://{self.host}/{path}",
                open_timeout=3,
                close_timeout=0.5,
            )
        except Exception as e:
            self._stream_error = EspargosStreamConnectionError(f"Could not connect to sensor stream WebSocket on {self.host}: {e}")
            self._stream_ready_event.set()
            return

        with ws as websocket:
            self.stream_connected = True
            self._stream_ready_event.set()

            timeout_total = 0
            timeout_once = 0.2
            while self.stream_connected:
                try:
                    message = websocket.recv(timeout_once)
                    if message == STREAM_MAGIC:
                        # WebSocket connections do not need the UDP handshake.
                        continue
                    timeout_total = 0
                    self._stream_handle_message(message)
                except TimeoutError:
                    timeout_total = timeout_total + timeout_once
                except Exception as e:
                    self.logger.error(f"Board {self.host} has error in websocket: {e}")
                    self.stream_connected = False
                    break

                if timeout_total > self._stream_timeout:
                    self.logger.warning("WebSocket timeout, disconnecting")
                    self.stream_connected = False

    def _fetch(self, path: str, data: str | bytes | None = None) -> str:
        method = "GET" if data is None else "POST"
        if self._uart_client is not None:
            response = self._uart_client.request(method, path, data, timeout=5)
            if response.status != 200:
                raise EspargosHTTPStatusError(response.status, path, response.body_text())
            return response.body_text()

        conn = http.client.HTTPConnection(self.host, timeout=5)
        conn.request(method, "/" + path, data)

        try:
            res = conn.getresponse()
        except TimeoutError:
            self.logger.error(f"Timeout in HTTP request for {self.host}/{path}")
            raise TimeoutError

        if res.status != 200:
            body = res.read().decode("utf-8", errors="replace")
            raise EspargosHTTPStatusError(res.status, path, body)

        return res.read().decode("utf-8")

    def _handle_uart_log(self, message: str):
        self.logger.info(f"[device] {message.rstrip()}")
