#!/usr/bin/env python
"""Interactive TX beams for one ESPARGOS array or a combined aperture."""

import pathlib
import signal
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from common.iq_tx_application import ESPARGOSTxApplication

if __name__ == "__main__":
    app = ESPARGOSTxApplication(sys.argv)
    signal.signal(signal.SIGINT, lambda *_: app.txcontrol.close())
    app.initialize_qml(pathlib.Path(__file__).with_name("iq-transmit-beams.qml"))
    sys.exit(app.exec())
