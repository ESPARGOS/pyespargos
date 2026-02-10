#!/usr/bin/env python3

import websockets.sync.client
import http.client
import threading
import logging
import socket
import ctypes
import json

from . import revisions
from . import csi


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
SUPPORTED_API_MAJOR = 1


class Board(object):
    _csistream_timeout = 5

    # Defaults for controller configuration
    DEFAULT_CSI_ACQUIRE_CONFIG = {
        "enable": True,
        "acquire_csi_legacy": True,
        "acquire_csi_force_lltf": False,
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
            self.logger.warning(f"ESPARGOS at {self.host} runs older firmware with no API version information. " f"Please update the firmware.")
            api_info = {"device": "espargos", "revision": "densiflorus", "api-major": 0, "api-minor": 0}

        if "api-major" not in api_info or "api-minor" not in api_info:
            raise EspargosUnexpectedResponseError(f"Server at {self.host} did not provide API version information in api_info response.")

        api_major = api_info["api-major"]
        api_minor = api_info.get("api-minor", 0)

        if api_major > SUPPORTED_API_MAJOR:
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

        self.csistream_connected = True
        self.consumers = []

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
            - "udp": The controller will send CSI packets to a local UDP socket. This transport is lower-latency and more efficient (higher throughput), but requires API version 1 or higher and may not work in all network environments.
            - "websocket": The controller will send CSI packets over a WebSocket connection. This transport is more widely compatible but may have higher latency and overhead.

        :param transports: Optional list of transports to try, in order of preference. Valid values are "udp" and "websocket". If None (default), tries UDP first (if supported by API version) and then WebSocket.

        :raises EspargosCsiStreamConnectionError: If neither UDP nor WebSocket CSI stream could be established
        """
        if transports is None:
            transports = ["udp", "websocket"] if self.api_version[0] > 0 else ["websocket"]

        for transport in transports:
            if transport == "udp":
                if self.api_version[0] == 0:
                    raise EspargosAPIVersionError(f"ESPARGOS controller at {self.host} runs API version {self.api_version[0]}.{self.api_version[1]}, which does not support UDP CSI streaming. Please update the controller firmware.")
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

    def _try_start_udp(self) -> str | None:
        """
        Try to start the CSI stream via UDP.
        Returns None on success, or an error message string on failure.
        """
        self.logger.info(f"Trying UDP CSI stream for {self.get_name()}")

        # Open a local UDP socket on an ephemeral port
        try:
            udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_sock.bind(("", 0))
            local_port = udp_sock.getsockname()[1]
        except OSError as e:
            return f"Could not create UDP socket: {e}"

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
        self._csistream_transport = "udp"
        self.csistream_connected = True
        self.csistream_thread = threading.Thread(target=self._csistream_loop_udp)
        self.csistream_thread.start()
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
            self.csistream_thread.join()

            if getattr(self, "_csistream_transport", None) == "udp":
                if hasattr(self, "_udp_sock"):
                    self._udp_sock.close()
                self._disable_udp_stream()

            self.logger.info(f"Stopped CSI stream for {self.get_name()}")

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
        Format (inferred from setter payloads)::

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
        pktsize = ctypes.sizeof(self.revision.csistream_pkt_t)
        assert len(message) % pktsize == 0
        for i in range(0, len(message), pktsize):
            packet = self.revision.csistream_pkt_t(message[i : i + pktsize])
            serialized_csi = csi.deserialize_packet_buffer(self.revision, packet.buf)

            for clist, cv, args in self.consumers:
                with cv:
                    clist.append((packet.esp_num, serialized_csi, *args))
                    cv.notify()

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
            # For major API version 1 or greater, wait for magic packet that confirms valid CSI stream connection
            if self.api_version[0] >= 1:
                try:
                    magic = websocket.recv(timeout=3)
                except TimeoutError:
                    self._csistream_error = EspargosCsiStreamConnectionError(f"Timeout waiting for CSI stream magic packet from {self.host}")
                    self._csistream_magic_event.set()
                    return
                except Exception as e:
                    self._csistream_error = EspargosCsiStreamConnectionError(f"Error receiving CSI stream magic packet from {self.host}: {e}")
                    self._csistream_magic_event.set()
                    return

                if magic != CSISTREAM_MAGIC:
                    self._csistream_error = EspargosCsiStreamConnectionError(f"Invalid CSI stream magic packet from {self.host}: expected {CSISTREAM_MAGIC.hex()}, got {magic.hex() if isinstance(magic, bytes) else repr(magic)}")
                    self._csistream_magic_event.set()
                    return
            else:
                self._csistream_magic_event.set()

            self.csistream_connected = True
            self._csistream_magic_event.set()

            timeout_total = 0
            timeout_once = 0.2
            while self.csistream_connected:
                try:
                    message = websocket.recv(timeout_once)
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
