"""Shared Qt Quick application setup for single and combined-array TX demos."""

from PyQt6.QtCore import QTimer
from PyQt6.QtQml import qmlRegisterType
from demos.common.application import ESPARGOSApplication, CombinedArrayMixin
from controller import IQTxController
from espargos import Pool
from plot import IQTxPlot


class ESPARGOSTxApplication(CombinedArrayMixin, ESPARGOSApplication):
    def _add_config_arguments(self, parser):
        def single_host(value):
            if not value.strip() or "," in value:
                from argparse import ArgumentTypeError

                raise ArgumentTypeError("Use one host with -s, or a combined-array YAML file with -c")
            return value.strip()

        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("-s", "--single-array", type=single_host, metavar="HOST", help="Single ESPARGOS array")
        source.add_argument("-c", "--config", metavar="FILE", help="Combined-array YAML configuration")

    def _add_argparse_arguments(self, parser):
        parser.add_argument("--tx-calibration", choices=("monitor", "reciprocity"), default=None, help="Default: monitor for one board, reciprocity for combined arrays")
        parser.add_argument("--calibration-path", choices=("reference", "antenna_l", "antenna_r"), default="reference")

    def __init__(self, argv):
        super().__init__(argv)
        hosts = self.get_initial_config("pool", "hosts") or None
        config = self.get_initial_config("combined-array") or None
        self.pool = Pool.from_config(config, hosts=hosts) if config else Pool(hosts or [])
        self.txcontrol = IQTxController(self.pool, self, all_tx=True, calibration_method=self.args.tx_calibration or ("reciprocity" if len(self.pool.hosts) > 1 else "monitor"), calibration_path=self.args.calibration_path)
        self.txcontrol.closed.connect(self.quit)
        self.txcontrol.changed.connect(self._ready)
        qmlRegisterType(IQTxPlot, "ESPARGOS.Tx", 1, 0, "IQTxPlot")
        self.setQuitOnLastWindowClosed(False)

    def _ready(self):
        # Show connection errors even when initialization fails.
        ready = not self.txcontrol.data["busy"] or self.txcontrol.data["connected"]
        if ready != self.ready:
            self.ready = ready
            self.initComplete.emit()

    def initialize_qml(self, qml_file, context_props=None):
        super().initialize_qml(qml_file, {"txcontrol": self.txcontrol, **(context_props or {})})
        self._qml_ok = bool(self.engine.rootObjects())
        if self._qml_ok:
            QTimer.singleShot(0, self.txcontrol.connectPool)
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
                    self.pool.close()
                except Exception:
                    self.logger.exception("Could not confirm TX shutdown")
        super().onAboutToQuit()
