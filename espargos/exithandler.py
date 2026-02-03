#!/usr/bin/env python3

import signal


class ExitHandler:
    running = True

    def __init__(self):
        signal.signal(signal.SIGINT, self.handler)
        signal.signal(signal.SIGTERM, self.handler)

    def handler(self, *args):
        self.running = False

    def kill(self, *args, **kwargs):
        self.running = False
