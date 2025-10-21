#!/usr/bin/env python3

import websockets.sync.client
import http.client
import threading
import logging
import ctypes
import json
from . import csi


class EspargosHTTPStatusError(Exception):
    "Raised when the ESPARGOS HTTP API returns an invalid status code"
    pass


class EspargosUnexpectedResponseError(Exception):
    "Raised when the server (ESPARGOS controller) provides unexpected response. Is the server really ESPARGOS?"
    pass


class Board(object):
    _csistream_timeout = 5

    def __init__(self, host: str):
        """
        Constructor for the Board class. Tries to connect to the ESPARGOS controller at the given host and fetches configuration information.

        :param host: The IP address or hostname of the ESPARGOS controller

        :raises TimeoutError: If the connection to the ESPARGOS controller times out
        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller
        """
        self.logger = logging.getLogger("pyespargos.board")

        self.host = host
        try:
            identification = self._fetch("identify")
        except TimeoutError:
            self.logger.error(f"Could not connect to {self.host}")
            raise TimeoutError

        if identification != "ESPARGOS":
            raise EspargosUnexpectedResponseError

        self.netconf = json.loads(self._fetch("get_netconf"))
        self.ip_info = json.loads(self._fetch("get_ip_info"))
        self.wificonf = json.loads(self._fetch("get_wificonf"))

        self.logger.info(
            f"Identified ESPARGOS at {self.ip_info['ip']} as {self.get_name()}")

        self.csistream_connected = True
        self.consumers = []

    def get_name(self):
        """
        Returns the hostname of the ESPARGOS controller as configured in the web interface.

        :return: The hostname of the ESPARGOS controller
        """
        return self.netconf["hostname"]

    def start(self):
        """
        Starts the CSI stream thread for the ESPARGOS controller. The thread will run indefinitely until the stop() method is called.
        """
        self.csistream_thread = threading.Thread(target=self._csistream_loop)
        self.csistream_thread.start()
        self.logger.info(f"Started CSI stream for {self.get_name()}")

    def stop(self):
        """
        Stops the CSI stream thread for the ESPARGOS controller. The thread will stop after the current packet has been processed, or after a short timeout.
        """
        if self.csistream_connected:
            self.csistream_connected = False
            self.csistream_thread.join()
            self.logger.info(f"Stopped CSI stream for {self.get_name()}")

    def set_calib(self, calibrate: bool):
        """
        Enables or disables calibration mode on the ESPARGOS controller.

        :param calibrate: True to enable calibration mode, False to disable it

        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller
        """
        res = self._fetch("set_calib", "1" if calibrate else "0")
        if res != "ok":
            self.logger.error(f"Invalid response: {res}")
            raise EspargosUnexpectedResponseError

    def set_mac_filter(self, mac_filter: str):
        """
        Tell ESPARGOS board to only receive packets from transmitters with this sender MAC.

        :param mac_filter: The MAC address filter to set (as string, e.g. "00:11:22:33:44:55")

        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller
        """
        data = json.dumps({"enable": True, "mac": mac_filter})
        res = self._fetch("set_mac_filter", data)
        if res != "ok":
            self.logger.error(f"Invalid response: {res}")
            raise EspargosUnexpectedResponseError

    def clear_mac_filter(self):
        """
        Tell ESPARGOS board to receive packets from all transmitters.

        :raises EspargosUnexpectedResponseError: If the server at the given host is not an ESPARGOS controller
        """
        data = json.dumps({"enable": False})
        res = self._fetch("set_mac_filter", data)
        if res != "ok":
            self.logger.error(f"Invalid response: {res}")
            raise EspargosUnexpectedResponseError

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
        pktsize = ctypes.sizeof(csi.csistream_pkt_t)
        assert(len(message) % pktsize == 0)
        for i in range(0, len(message), pktsize):
            packet = csi.csistream_pkt_t(message[i:i + pktsize])
            serialized_csi = csi.deserialize_packet_buffer(packet.buf)

            for clist, cv, args in self.consumers:
                with cv:
                    clist.append((packet.esp_num, serialized_csi, *args))
                    cv.notify()

    def _csistream_loop(self):
        with websockets.sync.client.connect("ws://" + self.host + "/csi", close_timeout = 0.5) as websocket:
            self.csistream_connected = True
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
                    self.logger.warn("Websockets timeout, disconnecting")
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