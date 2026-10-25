"""Antenna selection for IQ calibration and its board-wide capture controls."""

import numpy as np

from ..calibration import Antenna


class CalibrationScope:
    def __init__(self, receiver, antennas=None):
        self.receiver = receiver
        self.boards = receiver.boards
        available = {Antenna(b.host, a) for b in self.boards for a in range(8)}
        self.antennas = available if antennas is None else set(antennas)
        if not self.antennas or not self.antennas <= available:
            raise ValueError("Select IQ antennas belonging to this pool")
        self.ids = [[a for a in range(8) if Antenna(b.host, a) in self.antennas] for b in self.boards]
        self.active = [bi for bi, ids in enumerate(self.ids) if ids]
        self.pairs = [(bi, a) for bi in self.active for a in self.ids[bi]]
        self.mask = np.zeros((len(self.boards), 2, 4), bool)
        for bi, a in self.pairs:
            self.mask[bi, *self.boards[bi].revision.antenna_id_to_row_col(a)] = True

    def modes(self, bi, saved, mode="iq_rx"):
        return [mode if a in self.ids[bi] else m for a, m in enumerate(saved)]

    def apply_rx(self, config):
        """Apply receiver settings only to selected sensors on participating boards."""
        config = dict(config)
        settings = {k: config.pop(k) for k in ("rf_freq_hz", "gain_mode", "rx_gain") if k in config}
        mode = config.pop("mode", None)
        for bi in self.active:
            payload = dict(config)
            if settings:
                payload["receivers"] = [dict(antid=a, **settings) for a in self.ids[bi]]
            if mode is not None:
                modes = self.boards[bi].iq.get_config()["modes"]
                payload["modes"] = self.modes(bi, modes)
            self.boards[bi].iq.set_config(payload)

    def check_sync(self, configs):
        """Reuse established grids when unrelated IQ sensors share a board.

        Firmware synchronization is board-wide and cannot run alongside TX.
        Do not silently recruit unselected sensors to acquire a new grid.
        """
        outside = any(c["modes"][a] != "wifi" for bi, c in enumerate(configs) if bi in self.active for a in range(8) if a not in self.ids[bi])
        if outside:
            if not all(configs[bi].get("coarse_synced", False) for bi in self.active):
                raise ValueError("Subgroup calibration requires an existing synchronized grid. Calibrate all IQ sensors on the participating boards once first.")
            if any(configs[bi]["adc_decimation"] != 1 for bi in self.active):
                raise ValueError("Subgroup calibration sharing a board with other IQ sensors requires 80 MSa/s to preserve their sample timing")
        return outside
