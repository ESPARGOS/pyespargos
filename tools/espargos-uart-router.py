#!/usr/bin/env python3

import argparse
import asyncio
import logging
import pathlib
import sys

from aiohttp import web

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

from espargos.uart import UARTClient


class UARTRouter:
    def __init__(self, uart_host: str):
        self.logger = logging.getLogger("pyespargos.router")
        self.uart = UARTClient(uart_host)
        self._ws_clients = set()
        self._loop = None

    async def start(self):
        self._loop = asyncio.get_running_loop()
        self.uart.connect()
        self.uart.add_csi_callback(self._on_csi_frame)
        self.uart.add_log_callback(self._on_log_message)

    async def close(self):
        self.uart.close()

    async def handle_http(self, request: web.Request) -> web.StreamResponse:
        path = request.match_info.get("path", "")
        body = await request.read()
        response = self.uart.request(request.method.upper(), path, body)
        return web.Response(
            status=response.status,
            content_type=response.content_type or "text/plain",
            body=response.body,
        )

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        queue = asyncio.Queue()
        self._ws_clients.add(queue)
        self.logger.info("Local CSI WebSocket client connected")

        try:
            self.uart.enable_csi_stream()
            while True:
                payload = await queue.get()
                await ws.send_bytes(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._ws_clients.discard(queue)
            if not self._ws_clients:
                self.uart.disable_csi_stream()
            await ws.close()
            self.logger.info("Local CSI WebSocket client disconnected")

        return ws

    def _on_csi_frame(self, payload: bytes):
        if self._loop is None:
            return
        for q in list(self._ws_clients):
            self._loop.call_soon_threadsafe(q.put_nowait, payload)

    def _on_log_message(self, message: str):
        self.logger.info(f"[device] {message.rstrip()}")


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
    app.on_cleanup.append(on_cleanup)
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Expose an ESPARGOS UART link as a local HTTP/WebSocket endpoint")
    parser.add_argument("uart_host", help='UART host specifier, for example "uart:/dev/ttyUSB0"')
    parser.add_argument("--listen-host", default="127.0.0.1", help="Local listen address")
    parser.add_argument("--listen-port", type=int, default=8400, help="Local listen port")
    args = parser.parse_args(argv)

    router = UARTRouter(args.uart_host)
    app = build_app(router)
    web.run_app(app, host=args.listen_host, port=args.listen_port)


if __name__ == "__main__":
    main()
