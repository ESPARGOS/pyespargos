#!/usr/bin/env python

import errno
import logging
import os
import queue
import struct
import threading
import time
import termios

import serial
import serial.tools.list_ports

from . import csi

UART_PROTOCOL_VERSION = 1

FRAME_TYPE_HELLO_REQ = 0x01
FRAME_TYPE_HELLO_RESP = 0x02
FRAME_TYPE_RPC_REQ = 0x10
FRAME_TYPE_RPC_RESP = 0x11
FRAME_TYPE_CSI_DATA = 0x20
FRAME_TYPE_STREAM_CTRL = 0x21
FRAME_TYPE_LOG = 0x30
UART_ACTIVATION_TOKEN = b"ESPARGOS-UART-MODE\n"

STREAM_ID_CSI = 1
STREAM_ID_TRANSPORT = 0

RPC_METHOD_GET = 0
RPC_METHOD_POST = 1

DEFAULT_UART_BAUDRATE = 3000000
FT231XQ_UART_BAUDRATE = 2000000
CP2102C_UART_BAUDRATE = 3000000
DEFAULT_BOOT_BAUDRATE = 115200
DEFAULT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 0.001
DEFAULT_LATENCY_TIMER_MS = 1
DEFAULT_ACTIVATION_RETRY_INTERVAL = 0.05
DEFAULT_KEEPALIVE_INTERVAL = 1.0
DEFAULT_MODEM_IDLE_DTR = False
DEFAULT_MODEM_IDLE_RTS = False
USB_UART_BAUDRATES = {
    0x6015: FT231XQ_UART_BAUDRATE,  # FTDI FT231XQ USB UART
    0xEA64: CP2102C_UART_BAUDRATE,  # SiLabs CP2102C USB to UART Bridge Controller
}
NO_RESET_MODEM_STATES = {
    0x6015: (True, True),  # FTDI FT231X USB UART
    0xEA64: (True, True),  # SiLabs CP2102C USB to UART Bridge Controller
}


class UARTProtocolError(Exception):
    "Raised when the ESPARGOS UART protocol encounters malformed frames or unexpected responses."

    pass


class UARTTimeoutError(TimeoutError):
    "Raised when a UART request times out."

    pass


def is_uart_host(host: str) -> bool:
    return host.startswith("uart:")


def parse_uart_host(host: str) -> tuple[str, dict]:
    if not is_uart_host(host):
        raise ValueError(f"Unsupported UART host specifier: {host!r}")

    spec = host[len("uart:") :]
    if not spec:
        raise ValueError("UART host specifier is empty")

    device = spec
    params = {}

    if "@" in device:
        device, baud = device.rsplit("@", 1)
        if not baud or not baud.isdigit():
            raise ValueError(f"Invalid UART baudrate in host specifier: {host!r}")
        params["baud"] = baud

    if not device:
        raise ValueError("UART device path is empty")

    return device, params


def cobs_encode(data: bytes) -> bytes:
    if not data:
        return b"\x01"

    out = bytearray()
    code_index = 0
    out.append(0)
    code = 1

    for b in data:
        if b == 0:
            out[code_index] = code
            code_index = len(out)
            out.append(0)
            code = 1
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[code_index] = code
                code_index = len(out)
                out.append(0)
                code = 1

    out[code_index] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    if not data:
        raise UARTProtocolError("Received empty COBS frame")

    out = bytearray()
    idx = 0
    length = len(data)
    while idx < length:
        code = data[idx]
        if code == 0:
            raise UARTProtocolError("COBS frame contains zero byte")
        idx += 1
        end = idx + code - 1
        if end > length:
            raise UARTProtocolError("COBS frame is truncated")
        out.extend(data[idx:end])
        idx = end
        if code != 0xFF and idx < length:
            out.append(0)
    return bytes(out)


def _build_frame(frame_type: int, request_id: int, payload: bytes) -> bytes:
    header = struct.pack("<BBHI", UART_PROTOCOL_VERSION, frame_type, request_id & 0xFFFF, len(payload))
    return cobs_encode(header + payload) + b"\x00"


def _parse_frame(frame: bytes) -> tuple[int, int, bytes]:
    decoded = cobs_decode(frame)
    if len(decoded) < 8:
        raise UARTProtocolError("Frame too short")

    version, frame_type, request_id, payload_len = struct.unpack("<BBHI", decoded[:8])
    if version != UART_PROTOCOL_VERSION:
        raise UARTProtocolError(f"Unsupported UART protocol version {version}")
    payload = decoded[8:]
    if len(payload) != payload_len:
        raise UARTProtocolError("Frame payload length mismatch")
    return frame_type, request_id, payload


class UARTControlResponse:
    def __init__(self, status: int, content_type: str, body: bytes):
        self.status = status
        self.content_type = content_type
        self.body = body

    def body_text(self) -> str:
        return self.body.decode("utf-8")


class UARTClient:
    def __init__(self, host: str, *, timeout: float = DEFAULT_TIMEOUT):
        self.logger = logging.getLogger("pyespargos.uart")
        self.timeout = timeout

        self.device, params = parse_uart_host(host)

        self.boot_baudrate = int(params.get("boot_baud", DEFAULT_BOOT_BAUDRATE))
        self.baudrate = int(params.get("baud", self._detect_default_baudrate()))
        self.read_timeout = float(params.get("read_timeout", DEFAULT_READ_TIMEOUT))
        self.latency_timer_ms = int(params.get("latency_ms", DEFAULT_LATENCY_TIMER_MS))
        self.activation_retry_interval = float(params.get("activation_interval", DEFAULT_ACTIVATION_RETRY_INTERVAL))
        self.keepalive_interval = float(params.get("keepalive_interval", DEFAULT_KEEPALIVE_INTERVAL))

        self._serial = None
        self._reader_thread = None
        self._keepalive_thread = None
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._reqid = 1
        self._reqid_lock = threading.Lock()
        self._csi_callbacks = []
        self._log_callbacks = []
        self._rx_buffer = bytearray()
        self._connected = False
        self._reader_running = False
        self._latency_timer_restore_path = None
        self._latency_timer_restore_value = None
        self._modem_idle_state = self._detect_modem_idle_state()

    def connect(self):
        if self._connected:
            return

        self._stop_event.clear()
        self._serial = self._open_serial(self.baudrate)

        try:
            self.hello(timeout=min(1.0, self.timeout))
        except (UARTProtocolError, UARTTimeoutError, serial.SerialException):
            self._activate_transport_mode()

        self.logger.info(f"Connected to ESPARGOS UART on {self.device}")
        self._connected = True
        self._start_keepalive_thread()

    def _activate_transport_mode(self) -> None:
        if self._serial is None:
            raise UARTProtocolError("UART is not connected")

        self._serial.baudrate = self.boot_baudrate
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

        self._serial.write(f"ESPARGOS-UART-MODE:{self.baudrate}\n".encode("ascii"))
        self._serial.flush()
        time.sleep(max(self.activation_retry_interval, 0.1))

        self._serial.baudrate = self.baudrate
        time.sleep(0.1)
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()
        self.hello()

    def close(self):
        if self._serial is None:
            return

        self._stop_event.set()
        if self._keepalive_thread is not None:
            self._keepalive_thread.join(timeout=1.0)
            self._keepalive_thread = None
        try:
            self.disable_csi_stream()
        except Exception:
            pass
        try:
            self.disable_transport_mode()
        except Exception:
            pass

        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None
        self._reader_running = False

        try:
            try:
                self._apply_modem_idle_state(self._serial)
            except Exception:
                pass
            self._serial.close()
        finally:
            self._restore_latency_timer()
            self._serial = None
            self._connected = False

    def add_csi_callback(self, callback):
        self._csi_callbacks.append(callback)

    def remove_csi_callback(self, callback):
        if callback in self._csi_callbacks:
            self._csi_callbacks.remove(callback)

    def add_log_callback(self, callback):
        self._log_callbacks.append(callback)

    def remove_log_callback(self, callback):
        if callback in self._log_callbacks:
            self._log_callbacks.remove(callback)

    def hello(self, timeout: float | None = None) -> dict:
        payload = self._request_frame(FRAME_TYPE_HELLO_REQ, b"", timeout=timeout)
        if len(payload) == 0:
            return {}
        if len(payload) < 8:
            raise UARTProtocolError("HELLO response too short")
        device_len, revision_len, api_major, api_minor = struct.unpack("<HHHH", payload[:8])
        expected = 8 + device_len + revision_len
        if len(payload) != expected:
            raise UARTProtocolError("HELLO response has invalid payload length")
        device = payload[8 : 8 + device_len].decode("utf-8")
        revision = payload[8 + device_len : expected].decode("utf-8")
        return {
            "device": device,
            "revision": revision,
            "api-major": api_major,
            "api-minor": api_minor,
        }

    def request(self, method: str, path: str, body: bytes | str | None = None, timeout: float | None = None) -> UARTControlResponse:
        if body is None:
            body_bytes = b""
        elif isinstance(body, bytes):
            body_bytes = body
        else:
            body_bytes = str(body).encode("utf-8")

        method_id = RPC_METHOD_GET if method.upper() == "GET" else RPC_METHOD_POST
        path_bytes = path.encode("utf-8")
        payload = struct.pack("<BHI", method_id, len(path_bytes), len(body_bytes)) + path_bytes + body_bytes
        response = self._request_frame(FRAME_TYPE_RPC_REQ, payload, timeout=timeout)
        if len(response) < 8:
            raise UARTProtocolError("RPC response too short")

        status, content_type_len, body_len = struct.unpack("<HHI", response[:8])
        expected = 8 + content_type_len + body_len
        if len(response) != expected:
            raise UARTProtocolError("RPC response length mismatch")

        content_type = response[8 : 8 + content_type_len].decode("utf-8")
        body = response[8 + content_type_len : expected]
        return UARTControlResponse(status, content_type, body)

    def enable_csi_stream(self):
        self._ensure_reader_thread()
        self._send_frame(FRAME_TYPE_STREAM_CTRL, 0, struct.pack("<BB", STREAM_ID_CSI, 1))

    def disable_csi_stream(self):
        self._send_frame(FRAME_TYPE_STREAM_CTRL, 0, struct.pack("<BB", STREAM_ID_CSI, 0))

    def disable_transport_mode(self):
        self._send_frame(FRAME_TYPE_STREAM_CTRL, 0, struct.pack("<BB", STREAM_ID_TRANSPORT, 0))
        if self._serial is not None:
            self._serial.flush()
            time.sleep(0.05)

    def _send_keepalive(self):
        self._send_frame(FRAME_TYPE_STREAM_CTRL, 0, struct.pack("<BB", STREAM_ID_TRANSPORT, 1))

    def _request_frame(self, frame_type: int, payload: bytes, timeout: float | None = None) -> bytes:
        if self._reader_running:
            return self._request_frame_async(frame_type, payload, timeout)

        return self._request_frame_sync(frame_type, payload, timeout)

    def _request_frame_async(self, frame_type: int, payload: bytes, timeout: float | None = None) -> bytes:
        reqid = self._allocate_request_id()
        q = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[reqid] = q
        try:
            self._send_frame(frame_type, reqid, payload)
            try:
                response_type, response_payload = q.get(timeout=self.timeout if timeout is None else timeout)
            except queue.Empty as exc:
                raise UARTTimeoutError(f"Timed out waiting for UART response to request {reqid}") from exc

            expected_type = FRAME_TYPE_HELLO_RESP if frame_type == FRAME_TYPE_HELLO_REQ else FRAME_TYPE_RPC_RESP
            if response_type != expected_type:
                raise UARTProtocolError(f"Unexpected response type 0x{response_type:02x} for request {reqid}")
            return response_payload
        finally:
            with self._pending_lock:
                self._pending.pop(reqid, None)

    def _request_frame_sync(self, frame_type: int, payload: bytes, timeout: float | None = None) -> bytes:
        reqid = self._allocate_request_id()
        expected_type = FRAME_TYPE_HELLO_RESP if frame_type == FRAME_TYPE_HELLO_REQ else FRAME_TYPE_RPC_RESP
        timeout = self.timeout if timeout is None else timeout

        with self._request_lock:
            self._send_frame(frame_type, reqid, payload)
            end_time = time.monotonic() + timeout
            while True:
                remaining = end_time - time.monotonic()
                if remaining <= 0:
                    raise UARTTimeoutError(f"Timed out waiting for UART response to request {reqid}")

                frame_type_rx, request_id_rx, payload_rx = self._read_one_frame(timeout=min(0.5, remaining))
                if frame_type_rx is None:
                    continue

                if request_id_rx == reqid and frame_type_rx == expected_type:
                    return payload_rx

                self._handle_frame(frame_type_rx, request_id_rx, payload_rx)

    def _allocate_request_id(self) -> int:
        with self._reqid_lock:
            reqid = self._reqid
            self._reqid = 1 if self._reqid == 0xFFFF else self._reqid + 1
            return reqid

    def _send_frame(self, frame_type: int, request_id: int, payload: bytes):
        if self._serial is None:
            raise UARTProtocolError("UART is not connected")
        data = _build_frame(frame_type, request_id, payload)
        with self._write_lock:
            self._serial.write(data)
            self._serial.flush()

    def _ensure_reader_thread(self):
        if self._reader_running:
            return
        self._stop_event.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, name=f"uart-reader-{self.device}", daemon=True)
        self._reader_thread.start()
        self._reader_running = True

    def _start_keepalive_thread(self):
        if self.keepalive_interval <= 0 or self._keepalive_thread is not None:
            return
        self._keepalive_thread = threading.Thread(target=self._keepalive_loop, name=f"uart-keepalive-{self.device}", daemon=True)
        self._keepalive_thread.start()

    def _keepalive_loop(self):
        while not self._stop_event.wait(self.keepalive_interval):
            try:
                self._send_keepalive()
            except Exception as exc:
                self.logger.debug(f"Stopping UART keepalive for {self.device}: {exc}")
                break

    def _open_serial(self, baudrate: int) -> serial.Serial:
        # Known ESPARGOS USB-UART adapters need specific modem-control levels
        # applied before opening the port, otherwise the standard ESP32 auto-
        # reset circuitry reboots the board as soon as the port is opened.
        ser = serial.Serial(
            port=None,
            baudrate=baudrate,
            timeout=self.read_timeout,
            exclusive=True,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        self._apply_modem_idle_state(ser)
        ser.port = self.device
        ser.open()
        self._configure_termios(ser)
        self._apply_modem_idle_state(ser)
        self._apply_low_latency_tuning()
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        return ser

    def _configure_termios(self, ser: serial.Serial):
        if os.name != "posix":
            return

        try:
            attrs = termios.tcgetattr(ser.fileno())
        except Exception as exc:
            self.logger.debug(f"Could not read termios state for {self.device}: {exc}")
            return

        attrs[2] |= termios.CLOCAL
        if hasattr(termios, "HUPCL"):
            attrs[2] &= ~termios.HUPCL

        try:
            termios.tcsetattr(ser.fileno(), termios.TCSANOW, attrs)
        except Exception as exc:
            self.logger.debug(f"Could not update termios state for {self.device}: {exc}")

    def _apply_modem_idle_state(self, ser: serial.Serial):
        ser.dtr, ser.rts = self._modem_idle_state

    def _matching_port_info(self):
        device_realpath = os.path.realpath(self.device)
        for portinfo in serial.tools.list_ports.comports():
            if os.path.realpath(portinfo.device) != device_realpath:
                continue
            return portinfo
        return None

    def _detect_default_baudrate(self) -> int:
        portinfo = self._matching_port_info()
        if portinfo is not None and portinfo.pid in USB_UART_BAUDRATES:
            baudrate = USB_UART_BAUDRATES[portinfo.pid]
            self.logger.debug(f"Using {baudrate} baud for USB-UART adapter PID 0x{portinfo.pid:04x}")
            return baudrate
        return DEFAULT_UART_BAUDRATE

    def _detect_modem_idle_state(self) -> tuple[bool, bool]:
        portinfo = self._matching_port_info()
        if portinfo is not None:
            if portinfo.pid in NO_RESET_MODEM_STATES:
                return NO_RESET_MODEM_STATES[portinfo.pid]
        return DEFAULT_MODEM_IDLE_DTR, DEFAULT_MODEM_IDLE_RTS

    def _apply_low_latency_tuning(self):
        if os.name != "posix":
            return

        devname = os.path.basename(os.path.realpath(self.device))
        latency_path = f"/sys/class/tty/{devname}/device/latency_timer"
        if not os.path.exists(latency_path):
            return

        try:
            current = open(latency_path, "r", encoding="ascii").read().strip()
        except OSError as exc:
            self.logger.debug(f"Could not read UART latency timer for {self.device}: {exc}")
            return

        if current == str(self.latency_timer_ms):
            return

        try:
            with open(latency_path, "w", encoding="ascii") as f:
                f.write(str(self.latency_timer_ms))
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EROFS, errno.EPERM):
                self.logger.debug(f"Could not set UART latency timer for {self.device}: {exc}")
            return

        self._latency_timer_restore_path = latency_path
        self._latency_timer_restore_value = current
        self.logger.info(f"Set UART latency timer for {self.device} to {self.latency_timer_ms} ms")

    def _restore_latency_timer(self):
        if self._latency_timer_restore_path is None or self._latency_timer_restore_value is None:
            return
        try:
            with open(self._latency_timer_restore_path, "w", encoding="ascii") as f:
                f.write(self._latency_timer_restore_value)
        except OSError:
            pass
        finally:
            self._latency_timer_restore_path = None
            self._latency_timer_restore_value = None

    def _read_one_frame(self, timeout: float) -> tuple[int | None, int | None, bytes | None]:
        end_time = time.monotonic() + timeout
        while True:
            try:
                delimiter = self._rx_buffer.index(0)
            except ValueError:
                delimiter = -1

            if delimiter >= 0:
                raw_frame = bytes(self._rx_buffer[:delimiter])
                del self._rx_buffer[: delimiter + 1]
                if not raw_frame:
                    continue
                try:
                    return _parse_frame(raw_frame)
                except UARTProtocolError as exc:
                    self.logger.debug(f"Ignoring invalid UART frame while resynchronizing: {exc}")
                    continue

            remaining = end_time - time.monotonic()
            if remaining <= 0:
                return None, None, None

            chunk = self._serial.read(self._serial.in_waiting or 4096)
            if not chunk:
                continue
            self._rx_buffer.extend(chunk)

    def _reader_loop(self):
        while not self._stop_event.is_set():
            try:
                frame_type, request_id, payload = self._read_one_frame(timeout=0.2)
            except serial.SerialException as exc:
                self.logger.error(f"UART read error on {self.device}: {exc}")
                break
            if frame_type is None:
                continue
            self._handle_frame(frame_type, request_id, payload)
        self._reader_running = False

    def _handle_frame(self, frame_type: int, request_id: int, payload: bytes):
        if frame_type in (FRAME_TYPE_HELLO_RESP, FRAME_TYPE_RPC_RESP):
            with self._pending_lock:
                q = self._pending.get(request_id)
            if q is not None:
                q.put((frame_type, payload))
            return

        if frame_type == FRAME_TYPE_CSI_DATA:
            for callback in list(self._csi_callbacks):
                callback(payload)
            return

        if frame_type == FRAME_TYPE_LOG:
            text = payload.decode("utf-8", errors="replace")
            for callback in list(self._log_callbacks):
                callback(text)
            return

        self.logger.debug(f"Ignoring UART frame type 0x{frame_type:02x}")


def validate_csistream_payload(payload: bytes, revision) -> bool:
    del revision
    if len(payload) < 4:
        return False
    try:
        jumbo = csi.parse_csistream_jumbo_message(payload)
        for _header, _fragment in csi.iter_csistream_fragments(jumbo):
            pass
    except ValueError:
        return False
    return True
