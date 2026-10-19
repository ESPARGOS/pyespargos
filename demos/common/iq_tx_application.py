"""Shared Qt Quick application setup for single and combined-array TX demos."""

from PyQt6.QtCore import QTimer
from PyQt6.QtQml import qmlRegisterType
from .espargos_application import ESPARGOSApplication
from .iq_tx_controller import IQTxController
from espargos.iq_tx import TXLayout as Layout
from .iq_tx_plot import IQTxPlot


class ESPARGOSTxApplication(ESPARGOSApplication):
    def _add_argparse_arguments(self, parser):
        parser.add_argument("hosts", nargs="?", default="", help="Host or comma-separated layout host overrides")
        parser.add_argument("--spacing", type=float, default=0.06, help="Antenna grid spacing in metres")

    def __init__(self, argv):
        super().__init__(argv)
        layout = Layout(self.get_initial_config("pool", "hosts") or None, self.get_initial_config("combined-array") or None, self.args.spacing)
        self.txcontrol = IQTxController(layout, self, all_tx=True)
        self.txcontrol.closed.connect(self.quit)
        self.txcontrol.changed.connect(self._ready)
        qmlRegisterType(IQTxPlot, "ESPARGOS.Tx", 1, 0, "IQTxPlot")
        self.setQuitOnLastWindowClosed(False)

    def _ready(self):
        # Failed connections must leave the reconnect control accessible.
        ready = not self.txcontrol.data["busy"] or self.txcontrol.data["connected"]
        if ready != self.ready:
            self.ready = ready
            self.initComplete.emit()

    def initialize_qml(self, qml_file, context_props=None):
        super().initialize_qml(qml_file, {"txcontrol": self.txcontrol, **(context_props or {})})
        self._qml_ok = bool(self.engine.rootObjects())
        if self._qml_ok:
            QTimer.singleShot(0, self.txcontrol.connectArray)
        else:
            self.txcontrol.executor.shutdown(wait=False)

    def onAboutToQuit(self):
        # Normal window close waits asynchronously for mute acknowledgements.
        # Also protect Qt.quit()/kiosk/application shutdown paths.
        if hasattr(self, "txcontrol") and not self.txcontrol.shutdown_started:
            control = self.txcontrol
            control.executor.shutdown(wait=True)
            if control.session:
                try:
                    control.session.close()
                except Exception:
                    self.logger.exception("Could not confirm TX shutdown")
        super().onAboutToQuit()
