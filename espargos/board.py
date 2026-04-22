#!/usr/bin/env python

import websockets.sync.client
import http.client
import threading
import logging
import socket
import ctypes
import json
import binascii

import time

from . import revisions
from . import csi
from . import uart

# Port used by the controller as source port for UDP CSI packets
CSISTREAM_CONTROLLER_SRC_PORT = 53330


class EspargosHTTPStatusError(Exception):
    "Raised when the ESPARGOS HTTP API returns an invalid status code"

    pass


class EspargosUnexpectedResponseError(Exception):
    "Raised when the server (ESPARGOS controller) provides unexpected response. Is the server really ESPARGOS?"

    pass


class EspargosCsiStreamConnectionError(Exception):
    "Raised when the CSI stream connection could not be established (e.g. magic packet not received)"

    pass


class EspargosAPIVersionError(Exception):
    "Raised when the ESPARGOS controller runs an unsupported API version"

    pass


# Magic bytes sent by the controller as the first WebSocket frame to confirm a valid CSI stream connection
CSISTREAM_MAGIC = bytes([0xE5, 0xA7, 0x60, 0x00])

# Only this major API version is supported
SUPPORTED_API_MAJOR = 3


class Board(object):
    _csistream_timeout = 5

    # Defaults for controller configuration
    DEFAULT_CSI_ACQUIRE_CONFIG = {
        "enable": True,
        "acquire_csi_legacy": True,
        "acquire_csi_force_lltf": False,
        "compress_csi": False,
        "acquire_csi_ht20": True,
        "acquire_csi_ht40": True,
        "acquire_csi_vht": True,
        "acquire_csi_su": True,
        "acquire_csi_mu": True,
        "acquire_csi_dcm": True,
        "acquire_csi_beamformed": True,
        "acquire_csi_he_stbc_mode": 2,
        "val_scale_cfg": 0,
        "dump_ack_en": True,
    }

    DEFAULT_GAIN_SETTINGS = {
        "fft_scale_enable": False,
        "fft_scale_value": 0,
        "rx_gain_enable": False,
        "rx_gain_value": 0,
    }

    def __init__(self, host: str):
        """
        Constructor for the Board class. Tries to connect to the ESPARGOS controller at the given host and fetches configuration information.

        :param host: The IP address or hostname of the ESPARGOS controller

        :raises TimeoutError: If the connection to the ESPARGOS controller times out
        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller or the request was invalid
        """
        self.logger = logging.getLogger("pyespargos.board")

        self.host = host
        self._uart_client = None
        self._transport_kind = "network"
        if uart.is_uart_host(host):
            self._transport_kind = "uart"
            self._uart_client = uart.UARTClient(host)
            self._uart_client.add_log_callback(self._handle_uart_log)
            self._uart_client.connect()
        try:
            identification_raw = self._fetch("identify")
        except TimeoutError:
            self.logger.error(f"Could not connect to {self.host} to fetch identification information")
            raise TimeoutError

        if not "ESPARGOS-DENSIFLORUS" in identification_raw:
            raise EspargosUnexpectedResponseError(f"Server at {self.host} does not look like an ESPARGOS controller. Check if the host is correct.")

        try:
            api_info_raw = self._fetch("api_info")
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

        self.netconf = json.loads(self._fetch("get_netconf"))
        self.ip_info = json.loads(self._fetch("get_ip_info"))
        self.wificonf = json.loads(self._fetch("get_wificonf"))
        self.gain_settings = json.loads(self._fetch("get_gain_settings"))
        self.csi_acquire_config = json.loads(self._fetch("get_csi_acquire_config"))

        self.logger.info(f"Identified ESPARGOS at {self.ip_info['ip']} as {self.get_name()}")

        self.csistream_connected = False
        self.consumers = []
        self._fragment_reassembly = {}

    def get_name(self):
        """
        Returns the hostname of the ESPARGOS controller as configured in the web interface.

        :return: The hostname of the ESPARGOS controller
        """
        return self.netconf["hostname"]

    def start(self, transports=None):
        """
        Starts the CSI stream thread for the ESPARGOS controller. The thread will run indefinitely until the stop() method is called.

        Supported transports:

        - "udp": The controller will send CSI packets to a local UDP socket. This transport is lower-latency and more efficient (higher throughput), but may not work in all network environments.
        - "websocket": The controller will send CSI packets over a WebSocket connection. This transport is more widely compatible but may have higher latency and overhead.
        - "uart": The controller will stream CSI data over the local serial/UART link. This transport is only available for hosts specified as ``uart:<port>``.

        :param transports: Optional list of transports to try, in order of preference. Valid values are "udp" and "websocket". If None (default), tries UDP first (if supported by API version) and then WebSocket.

        :raises EspargosCsiStreamConnectionError: If neither UDP nor WebSocket CSI stream could be established
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

                self.logger.warning(f"UART CSI stream failed for {self.get_name()}: {uart_error}")
            elif transport == "udp":
                udp_error = self._try_start_udp()
                if udp_error is None:
                    return

                self.logger.warning(f"UDP CSI stream failed for {self.get_name()}: {udp_error}")
            elif transport == "websocket":
                ws_error = self._try_start_websocket()
                if ws_error is None:
                    return

                self.logger.warning(f"WebSocket CSI stream failed for {self.get_name()}: {ws_error}")
            else:
                self.logger.error(f"Unknown transport {transport} specified for {self.get_name()}, skipping")

        raise EspargosCsiStreamConnectionError(f"Could not establish CSI stream to {self.host} via any of the enabled transports, tried transports: {transports}")

    def _try_start_uart(self) -> str | None:
        if self._uart_client is None:
            return f"Host {self.host!r} is not a UART host"

        self.logger.info(f"Trying UART CSI stream for {self.get_name()}")

        def _callback(payload: bytes):
            self._csistream_handle_message(payload)

        self._uart_csi_callback = _callback
        self._uart_client.add_csi_callback(self._uart_csi_callback)
        try:
            self._uart_client.enable_csi_stream()
        except Exception as e:
            self._uart_client.remove_csi_callback(self._uart_csi_callback)
            return f"Could not enable UART CSI stream: {e}"

        self._csistream_transport = "uart"
        self.csistream_connected = True
        self.logger.info(f"Started UART CSI stream for {self.get_name()} on {self.host}")
        return None

    def _try_start_udp(self) -> str | None:
        """
        Try to start the CSI stream via UDP.
        Returns None on success, or an error message string on failure.
        """
        self.logger.info(f"Trying UDP CSI stream for {self.get_name()}")

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
            udp_info = socket.getaddrinfo(udp_host, CSISTREAM_CONTROLLER_SRC_PORT, preferred_family, socket.SOCK_DGRAM)
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
        try:
            res = self._fetch("csi_udp", json.dumps({"enable": True, "port": local_port}))
            if res != "ok":
                udp_sock.close()
                return f"Server rejected UDP stream request: {res}"
        except Exception as e:
            udp_sock.close()
            return f"HTTP request to enable UDP stream failed: {e}"

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

        if data != CSISTREAM_MAGIC:
            udp_sock.close()
            self._disable_udp_stream()
            return f"Invalid UDP magic packet: expected {CSISTREAM_MAGIC.hex()}, got {data.hex()}"

        # UDP stream established successfully
        self._udp_sock = udp_sock
        self._udp_remote_addr = udp_remote_addr
        self._csistream_transport = "udp"
        self.csistream_connected = True
        self.csistream_thread = threading.Thread(target=self._csistream_loop_udp)
        self.csistream_thread.start()

        # Start keepalive thread that periodically sends empty packets to punch through the firewall
        self._udp_keepalive_stop = threading.Event()
        self._udp_keepalive_thread = threading.Thread(target=self._udp_keepalive_loop, daemon=True)
        self._udp_keepalive_thread.start()

        self.logger.info(f"Started UDP CSI stream for {self.get_name()} on local port {local_port}")
        return None

    def _try_start_websocket(self) -> str | None:
        """
        Try to start the CSI stream via WebSocket.
        Returns None on success, or an error message string on failure.
        """
        self.logger.info(f"Trying WebSocket CSI stream for {self.get_name()}")

        self._csistream_magic_event = threading.Event()
        self._csistream_error = None
        self._csistream_transport = "websocket"
        self.csistream_thread = threading.Thread(target=self._csistream_loop_websocket)
        self.csistream_thread.start()

        # Only API version major 1 or greater sends magic packet
        if not self._csistream_magic_event.wait(timeout=3):
            self.csistream_connected = False
            self.csistream_thread.join()
            return "Did not receive WebSocket magic packet within 3 seconds"

        if self._csistream_error is not None:
            self.csistream_connected = False
            self.csistream_thread.join()
            return str(self._csistream_error)

        self.logger.info(f"Started WebSocket CSI stream for {self.get_name()}")
        return None

    def _disable_udp_stream(self):
        """Tell the server to stop the UDP stream (best-effort)."""
        try:
            self._fetch("csi_udp", json.dumps({"enable": False}))
        except Exception:
            pass

    def stop(self):
        """
        Stops the CSI stream thread for the ESPARGOS controller. The thread will stop after the current packet has been processed, or after a short timeout.
        """
        if self.csistream_connected:
            self.csistream_connected = False
            if hasattr(self, "csistream_thread"):
                self.csistream_thread.join()

            if getattr(self, "_csistream_transport", None) == "udp":
                if hasattr(self, "_udp_keepalive_stop"):
                    self._udp_keepalive_stop.set()
                    self._udp_keepalive_thread.join()
                if hasattr(self, "_udp_sock"):
                    self._udp_sock.close()
                self._disable_udp_stream()
            elif getattr(self, "_csistream_transport", None) == "uart":
                if hasattr(self, "_uart_csi_callback"):
                    self._uart_client.remove_csi_callback(self._uart_csi_callback)
                if self._uart_client is not None:
                    self._uart_client.disable_csi_stream()

            self.logger.info(f"Stopped CSI stream for {self.get_name()}")

    def close(self):
        """
        Close transport resources associated with this board.

        For UART-backed boards, this releases the serial port lock. Calling this on
        network-backed boards is harmless.
        """
        self.stop()
        if self._uart_client is not None:
            self._uart_client.close()

    def set_rfswitch(self, state: csi.rfswitch_state_t):
        """
        Sets the RF switch state on the ESPARGOS controller for reception mode.

        :param state: The RF switch state to set, must be one of :class:`csi.rfswitch_state_t`

        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller or the request was invalid
        """
        res = self._fetch("set_rfswitch", str(int(state)))
        if res != "ok":
            self.logger.error(f"Invalid response: {res}")
            raise EspargosUnexpectedResponseError

    def get_rfswitch(self) -> csi.rfswitch_state_t:
        """
        Fetches the current RF switch state from the ESPARGOS controller.

        :return: The current RF switch state as one of :class:`csi.rfswitch_state_t`

        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller or the request was invalid
        """
        res = self._fetch("get_rfswitch")
        try:
            state_int = int(res)
            # Check if valid enum value
            if state_int not in [e.value for e in csi.rfswitch_state_t]:
                raise EspargosUnexpectedResponseError("get_rfswitch returned invalid enum value")
            return csi.rfswitch_state_t(state_int)
        except (ValueError, KeyError):
            self.logger.error(f"Invalid response: {res}")
            raise EspargosUnexpectedResponseError

    def set_mac_filter(self, mac_filter: dict):
        """
        Tell ESPARGOS board to only receive packets from transmitters with this sender MAC.

        mac_filter is a dict with the following format::

            {
              "enable": true|false,
              "mac": "00:11:22:33:44:55"
              "mac_mask": "ff:ff:ff:ff:ff:ff"
            }
        The "enable" field toggles MAC filtering. When enabled, only packets from transmitters
        whose MAC address matches the given "mac" (applying the "mac_mask") will be received.
        "mac_mask" is a bitmask applied to both the configured MAC and the sender MAC before comparison.
        Only provided fields will be changed; others will remain as previously configured.

        :param mac_filter: MAC filter configuration dict

        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller or the request was invalid
        """
        self._post_json_ok("set_mac_filter", mac_filter)

    def get_mac_filter(self) -> dict:
        """
        Fetches the current MAC filter configuration from the ESPARGOS controller.

        The returned JSON/dict matches what is configured via :meth:`set_mac_filter` / :meth:`clear_mac_filter`.
        Format::

            {
              "enable": true|false,
              "mac": "00:11:22:33:44:55",
              "mac_mask": "ff:ff:ff:ff:ff:ff"
            }

        :return: MAC filter configuration dict
        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller or the request was invalid
        """
        return self._get_json("get_mac_filter")

    def clear_mac_filter(self):
        """
        Tell ESPARGOS board to receive packets from all transmitters.

        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller or the request was invalid
        """
        self._post_json_ok("set_mac_filter", {"enable": False})

    def set_wificonf(self, wificonf: dict):
        """
        Sets WiFi configuration on the ESPARGOS controller.

        The controller expects a Python dict with fixed field names
        using hyphenated keys. Expected format::

            {
              "calib-mode": 1,
              "calib-source": 0,
              "channel-primary": 13,
              "channel-secondary": 2,
              "country-code": "DE",
              "calib-txpower": 34,
              "calib-interval": 10
            }

        Field meanings / types (as used by the controller firmware):
          - "calib-mode" (int): When to generate phase reference packets:
            - 0: Never generate calibration packets
            - 1: Generate calibration packets if receiver RF switch is in reference channel configuration
            - 2: Always generate calibration packets
          - "calib-source" (int): Configures REFIN / REFOUT ports of controller:
            - 0: Use internal clock and phase reference for antennas
            - 1: Output clock and phase reference on REFOUT port, antennas expect clock and calibration from REFIN port (master mode)
            - 2: Antennas expect clock and calibration from REFIN port, do not output anything on REFOUT (slave mode)
          - "channel-primary" (int): Primary WiFi channel.
          - "channel-secondary" (int): Secondary channel selector (e.g. 0 = None, 1 = Above, 2 = Below).
          - "country-code" (str): Two-letter country code (e.g. "DE").
          - "calib-txpower" (int): TX power used for calibration packets (between 8 = 2dBm and 80 = 20dBm).
          - "calib-interval" (int): Calibration interval (milliseconds).

        :param wificonf: WiFi configuration dict
        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller or the request was invalid
        """
        self._post_json_ok("set_wificonf", wificonf)

    def get_wificonf(self) -> dict:
        """
        Fetches the current WiFi configuration from the ESPARGOS controller.

        The returned JSON/dict uses the same hyphenated keys as accepted by :meth:`set_wificonf`,
        e.g. contains fields like "channel-primary", "channel-secondary", "country-code", etc.

        :return: WiFi configuration dict
        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller or the request was invalid
        """
        return self._get_json("get_wificonf")

    def set_csi_acquire_config(self, config: dict):
        """
        Sets the CSI acquisition configuration on the ESPARGOS controller.

        The controller expects a JSON object (provided here as a Python dict) with integer
        fields (use 0/1 for booleans). Field names are fixed.

        Boolean toggles:
          - enable: Enable to acquire CSI.
          - acquire_csi_legacy: Enable to acquire L-LTF when receiving a 11g PPDU.
          - acquire_csi_force_lltf: Force receiver to acquire L-LTF, regardless of PPDU type.
          - compress_csi: Transform CSI to a time-domain CIR before transport.
          - acquire_csi_ht20: Enable to acquire HT-LTF when receiving an HT20 PPDU.
          - acquire_csi_ht40: Enable to acquire HT-LTF when receiving an HT40 PPDU.
          - acquire_csi_vht: Present in the HTTP API; semantics depend on firmware build / PHY mode support.
          - acquire_csi_su: Enable to acquire HE-LTF when receiving an HE20 SU PPDU.
          - acquire_csi_mu: Enable to acquire HE-LTF when receiving an HE20 MU PPDU.
          - acquire_csi_dcm: Enable to acquire HE-LTF when receiving an HE20 DCM applied PPDU.
          - acquire_csi_beamformed: Enable to acquire HE-LTF when receiving an HE20 Beamformed applied PPDU.
          - dump_ack_en: Enable to dump 802.11 ACK frame, default disabled.

        Integer / enum fields:
          - acquire_csi_he_stbc_mode: When receiving an STBC applied HE PPDU:
                0 = acquire the complete HE-LTF1
                1 = acquire the complete HE-LTF2
                2 = sample evenly among the HE-LTF1 and HE-LTF2.
          - val_scale_cfg: Value 0-3.

        Example payload::

            {
              "enable": true,
              "acquire_csi_legacy": true,
              "acquire_csi_force_lltf": false,
              "compress_csi": false,
              "acquire_csi_ht20": true,
              "acquire_csi_ht40": true,
              "acquire_csi_vht": true,
              "acquire_csi_su": true,
              "acquire_csi_mu": true,
              "acquire_csi_dcm": true,
              "acquire_csi_beamformed": true,
              "acquire_csi_he_stbc_mode": 2,
              "val_scale_cfg": 0,
              "dump_ack_en": true
            }

        :param config: CSI acquisition configuration dict (will be JSON-encoded and POSTed to /set_csi_acquire_config)
        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller or the request was invalid
        """
        self._post_json_ok("set_csi_acquire_config", config)

    def get_csi_acquire_config(self) -> dict:
        """
        Fetches the current CSI acquisition configuration from the ESPARGOS controller.

        :return: CSI acquisition configuration dict
        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller or the request was invalid
        """
        return self._get_json("get_csi_acquire_config")

    def set_gain_settings(self, settings: dict):
        """
        Sets the gain settings on the ESPARGOS controller.

        The gain settings are provided as a JSON object (here as a Python dict) with fixed field names:

          - fft_scale_enable (bool): Enable manual FFT scaling (false = automatic/firmware default).
          - fft_scale_value (int): FFT scale value (meaning/range depends on firmware; commonly 0 when disabled).
          - rx_gain_enable (bool): Enable manual RX gain (false = automatic/firmware default).
          - rx_gain_value (int): RX gain value (meaning/range depends on firmware; commonly 0 when disabled).

        Example payload::

            {
              "fft_scale_enable": false,
              "fft_scale_value": 0,
              "rx_gain_enable": false,
              "rx_gain_value": 0
            }

        :param settings: Gain settings dict (will be JSON-encoded and POSTed to /set_gain_settings)
        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller or the request was invalid
        """
        self._post_json_ok("set_gain_settings", settings)

    def get_gain_settings(self) -> dict:
        """
        Fetches the current gain settings from the ESPARGOS controller.

        :return: Gain settings dict
        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller or the request was invalid
        """
        return self._get_json("get_gain_settings")

    def set_radar_config(self, config: dict):
        """
        Sets the low-level radar TX configuration on the ESPARGOS controller.

        The payload mirrors the controller's ``/set_tx_control`` API. Supported fields are:

          - ``rfswitch_state`` (int)
          - ``active_by_antid`` (list[bool], length 8)
          - ``start_by_antid`` (list[int], length 8)
          - ``period_by_antid`` (list[int], length 8)
          - ``mac_by_antid`` (list[str], length 8, MAC addresses like ``"72:61:64:61:72:00"``)
          - ``tx_power`` (int)
          - ``tx_phymode`` (int)
          - ``tx_rate`` (int)

        Only provided fields are changed; others remain unchanged on the controller.

        :param config: Radar TX configuration dict
        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller or the request was invalid
        """
        self._post_json_ok("set_tx_control", config)

    def get_radar_config(self) -> dict:
        """
        Fetches the current low-level radar TX configuration from the ESPARGOS controller.

        The returned dict mirrors the controller's ``/get_tx_control`` response and contains fields such as
        ``rfswitch_state``, ``active_by_antid``, ``start_by_antid``, ``period_by_antid``, ``mac_by_antid``,
        ``tx_power``, ``tx_phymode``, and ``tx_rate``.

        :return: Radar TX configuration dict
        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller or the request was invalid
        """
        return self._get_json("get_tx_control")

    def add_consumer(self, clist: list, cv: threading.Condition, *args):
        """
        Adds a consumer to the CSI stream.
        A consumer is defined by a list, a condition variable and additional arguments.
        When a CSI packet is received, it will be appended to the list, and the condition variable will be notified.

        :param clist: A list to which the CSI packet will be appended. The entry added to the list is a tuple :code:`(esp_num, serialized_csi, *args)`,
                        where esp_num is the number of the sensor in the array, serialized_csi is the raw CSI packet and :code:`*args` are the additional arguments.
        :param cv: A condition variable that will be notified when a CSI packet is received
        :param args: Additional arguments that will be added to the list along with the CSI packet
        """
        self.consumers.append((clist, cv, args))

    def _csistream_handle_message(self, message):
        try:
            jumbo = csi.parse_csistream_jumbo_message(message)
            fragments = list(csi.iter_csistream_fragments(jumbo))
        except ValueError as exc:
            self.logger.debug(f"Ignoring malformed CSI stream message: {exc}")
            return

        now = time.monotonic()
        stale_keys = [key for key, entry in self._fragment_reassembly.items() if now - entry["timestamp"] > 5.0]
        for key in stale_keys:
            self._fragment_reassembly.pop(key, None)

        completed_packets = []
        for header, payload in fragments:
            packet_antid = csi.csistream_uid_to_antid(int(header.uid))
            key = int(header.uid)
            entry = self._fragment_reassembly.get(key)
            if entry is None or entry["total_fragments"] != int(header.total_fragments):
                entry = {
                    "timestamp": now,
                    "antid": packet_antid,
                    "total_fragments": int(header.total_fragments),
                    "parts": {},
                }
                self._fragment_reassembly[key] = entry
            elif entry["antid"] != packet_antid:
                self.logger.warning(f"Received jumbo fragments with inconsistent UID-derived antid for uid {int(header.uid)}")
                self._fragment_reassembly.pop(key, None)
                continue

            entry["timestamp"] = now
            entry["parts"][int(header.fragment_index)] = bytes(payload)

            if entry["total_fragments"] <= 0:
                self._fragment_reassembly.pop(key, None)
                continue

            if len(entry["parts"]) != entry["total_fragments"]:
                continue

            if any(index not in entry["parts"] for index in range(entry["total_fragments"])):
                continue

            completed_packets.append((entry["antid"], b"".join(entry["parts"][index] for index in range(entry["total_fragments"]))))
            self._fragment_reassembly.pop(key, None)

        for packet_antid, packet_payload in completed_packets:
            try:
                serialized_csi = csi.deserialize_packet_buffer(self.revision, packet_payload)
            except (AssertionError, ValueError):
                self.logger.debug("Ignoring CSI payload with unexpected logical type header")
                continue

            serialized_csi.antid = packet_antid

            packet_esp_num = self.revision.antid_to_esp_num[packet_antid]

            for clist, cv, args in self.consumers:
                with cv:
                    clist.append((packet_esp_num, serialized_csi, *args))
                    cv.notify()

    def _udp_keepalive_loop(self):
        """Periodically send empty UDP packets to the controller to keep the firewall hole open."""
        while not self._udp_keepalive_stop.wait(timeout=1.0):
            try:
                self._udp_sock.sendto(b"", self._udp_remote_addr)
            except OSError:
                break

    def _csistream_loop_udp(self):
        self._udp_sock.settimeout(0.2)
        timeout_total = 0
        while self.csistream_connected:
            try:
                data, addr = self._udp_sock.recvfrom(65535)
                timeout_total = 0
                self._csistream_handle_message(data)
            except socket.timeout:
                timeout_total += 0.2
            except OSError as e:
                self.logger.error(f"Board {self.host} has error in UDP socket: {e}")
                self.csistream_connected = False
                break

            if timeout_total > self._csistream_timeout:
                self.logger.warning("UDP timeout, disconnecting")
                self.csistream_connected = False

    def _csistream_loop_websocket(self):
        try:
            ws = websockets.sync.client.connect("ws://" + self.host + "/csi", close_timeout=0.5)
        except Exception as e:
            self._csistream_error = EspargosCsiStreamConnectionError(f"Could not connect to CSI stream WebSocket on {self.host}: {e}")
            self._csistream_magic_event.set()
            return

        with ws as websocket:
            # Do not wait for magic packet, no need for that when using websockets
            self.csistream_connected = True
            self._csistream_magic_event.set()

            timeout_total = 0
            timeout_once = 0.2
            while self.csistream_connected:
                try:
                    message = websocket.recv(timeout_once)
                    if message == CSISTREAM_MAGIC:
                        # Ignore magic packet, only relevant for UDP transport
                        continue
                    timeout_total = 0
                    self._csistream_handle_message(message)
                except TimeoutError:
                    timeout_total = timeout_total + timeout_once
                except Exception as e:
                    self.logger.error(f"Board {self.host} has error in websocket: {e}")
                    self.csistream_connected = False
                    break

                if timeout_total > self._csistream_timeout:
                    self.logger.warning("WebSocket timeout, disconnecting")
                    self.csistream_connected = False

    def _fetch(self, path, data=None):
        method = "GET" if data is None else "POST"

        if self._uart_client is not None:
            response = self._uart_client.request(method, path, data, timeout=5)
            if response.status != 200:
                raise EspargosHTTPStatusError
            return response.body_text()

        conn = http.client.HTTPConnection(self.host, timeout=5)
        conn.request(method, "/" + path, data)

        try:
            res = conn.getresponse()
        except TimeoutError:
            self.logger.error(f"Timeout in HTTP request for {self.host}/{path}")
            raise TimeoutError

        if res.status != 200:
            raise EspargosHTTPStatusError

        return res.read().decode("utf-8")

    def _post_json_ok(self, path: str, payload: dict):
        """
        POST JSON payload to `/<path>` and require literal response 'ok'.
        """
        res = self._fetch(path, json.dumps(payload))
        if res != "ok":
            self.logger.error(f"Invalid response: {res}")
            raise EspargosUnexpectedResponseError(str(res))

    def _get_json(self, path: str) -> dict:
        """
        GET `/<path>` and parse response as JSON.
        """
        res = self._fetch(path)
        try:
            return json.loads(res)
        except json.JSONDecodeError:
            self.logger.error(f"Invalid response: {res}")
            raise EspargosUnexpectedResponseError(str(res))

    def _handle_uart_log(self, message: str):
        self.logger.info(f"[device] {message.rstrip()}")
