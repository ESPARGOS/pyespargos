#!/usr/bin/env python
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from PyQt6.QtCore import QPointF, pyqtSlot
from espargos.wifi_iq_symbols import symbol_constellation
from demos.wifi.common.instantaneous_csi import InstantaneousCSI
from demos.wifi_iq.common.application import WiFiIQApplication


class WiFiIQInstantaneousCSI(InstantaneousCSI, WiFiIQApplication):
    DEFAULT_CONFIG = {**InstantaneousCSI.DEFAULT_CONFIG, "required_antennas": 1}
    qml_file = Path(__file__).with_name("Main.qml").resolve()

    def _cluster_predicate(self, cluster):
        # Give the other antennas time to report before displaying a partial packet.
        return cluster.is_complete or (cluster.age >= 0.1 and super()._cluster_predicate(cluster))

    def scale_channel(self, csi, levels):
        return self.adc_normalized_channel(csi)

    def _constellation_axis_limit(self, csi_key, csi_backlog, levels):
        # Same ADC-count display range as the WiFi IQ browser plot.
        return 2048

    def _extend_default_config(self, config):
        super()._extend_default_config(config)
        config["backlog"]["fields"]["iq_packets"] = True

    @pyqtSlot(list)
    def updateSymbols(self, series):
        backlog = getattr(self, "backlog", None)
        latest = backlog.get_latest("iq_packets") if backlog and "iq_packets" in backlog.fields else None
        feed = self.FEED_FILTER_MAP.get(self.appconfig.get("feed_filter"))
        packets = tuple(p for p in latest if feed is None or p.rf_switch == feed) if latest is not None else ()
        if packets == getattr(self, "_symbol_packets", None) and series[0] is getattr(self, "_symbol_series", None):
            return
        self._symbol_packets, self._symbol_series = packets, series[0]
        points = symbol_constellation(packets)
        for curve in series:
            curve.replace([QPointF(z.real, z.imag) for z in points[curve.name()]])


if __name__ == "__main__":
    sys.exit(WiFiIQInstantaneousCSI(sys.argv).exec())
