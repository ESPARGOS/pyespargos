#!/usr/bin/env python3

import argparse
import asyncio
import contextlib
import logging
import pathlib
import sys

from aiohttp import web

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

from espargos.uart import UARTClient, UARTProtocolError, UARTTimeoutError, is_uart_host


class UARTRouter:
    def __init__(self, uart_host: str):
        self.logger = logging.getLogger("pyespargos.router")
        self.uart = UARTClient(uart_host)
        self._ws_clients = set()
        self._loop = None
        self._closing = False
        self._closed = False

    async def start(self):
        self._loop = asyncio.get_running_loop()
        self.uart.connect()
        self.uart.add_csi_callback(self._on_csi_frame)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self._closing = True

        clients = list(self._ws_clients)
        self._ws_clients.clear()
        for queue, ws in clients:
            with contextlib.suppress(Exception):
                queue.put_nowait(None)
            if not ws.closed:
                with contextlib.suppress(Exception):
                    await ws.close(code=1001, message=b"router shutting down")

        with contextlib.suppress(Exception):
            self.uart.remove_csi_callback(self._on_csi_frame)
        with contextlib.suppress(Exception):
            self.uart.close()

    async def handle_http(self, request: web.Request) -> web.StreamResponse:
        path = request.match_info.get("path", "")
        if request.method.upper() == "GET" and path == "get_transport":
            return web.json_response({"transport": "uart"})

        if request.method.upper() == "POST" and path in {
            "update_firmware",
            "update_sensor_application",
            "upload_partition",
            "trigger_webupdate",
            "flash_sensor_firmware",
        }:
            return web.Response(
                status=501,
                content_type="text/plain",
                text="updates are not supported over the UART router",
            )

        body = await request.read()
        try:
            response = self.uart.request(request.method.upper(), path, body)
        except UARTTimeoutError:
            return web.Response(status=504, text="")
        except (UARTProtocolError, OSError):
            return web.Response(status=502, text="")
        headers = {}
        if response.content_type:
            headers["Content-Type"] = response.content_type
        return web.Response(status=response.status, headers=headers, body=response.body)

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        queue = asyncio.Queue()
        client = (queue, ws)
        self._ws_clients.add(client)
        self.logger.info("Local CSI WebSocket client connected")

        try:
            self.uart.enable_csi_stream()
            while True:
                payload = await queue.get()
                if payload is None:
                    break
                await ws.send_bytes(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._ws_clients.discard(client)
            if not self._closing and not self._ws_clients:
                with contextlib.suppress(Exception):
                    self.uart.disable_csi_stream()
            if not ws.closed:
                with contextlib.suppress(Exception):
                    await ws.close()
            self.logger.info("Local CSI WebSocket client disconnected")

        return ws

    def _on_csi_frame(self, payload: bytes):
        if self._closing or self._loop is None:
            return
        for q, _ws in list(self._ws_clients):
            try:
                self._loop.call_soon_threadsafe(q.put_nowait, payload)
            except RuntimeError:
                return

def build_app(router: UARTRouter) -> web.Application:
    app = web.Application()
    app["router"] = router
    app.router.add_get("/csi", router.handle_ws)
    app.router.add_route("*", "/{path:.*}", router.handle_http)

    async def on_startup(app):
        await router.start()

    async def on_cleanup(app):
        await router.close()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_cleanup)
    app.on_cleanup.append(on_cleanup)
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Expose an ESPARGOS UART link as a local HTTP/WebSocket endpoint")
    parser.add_argument("uart_host", help='UART host specifier, for example "uart:/dev/ttyUSB0"')
    parser.add_argument("--listen-host", default="127.0.0.1", help="Local listen address")
    parser.add_argument("--listen-port", type=int, default=8400, help="Local listen port")
    args = parser.parse_args(argv)
    if not is_uart_host(args.uart_host):
        parser.error('uart_host must be a UART host specifier such as "uart:/dev/ttyUSB0"')

    router = UARTRouter(args.uart_host)
    app = build_app(router)
    web.run_app(app, host=args.listen_host, port=args.listen_port)


if __name__ == "__main__":
    main()
