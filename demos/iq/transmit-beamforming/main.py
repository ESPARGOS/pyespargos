#!/usr/bin/env python
"""Interactive TX beamforming for one ESPARGOS array or a combined aperture."""

import pathlib
import signal
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
from application import ESPARGOSTxApplication

if __name__ == "__main__":
    app = ESPARGOSTxApplication(sys.argv)
    signal.signal(signal.SIGINT, lambda *_: app.txcontrol.close())
    app.initialize_qml(pathlib.Path(__file__).with_name("Main.qml"))
    sys.exit(app.exec())
