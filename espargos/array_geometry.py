"""Physical geometry and reference cabling shared by RX and TX."""

import copy
from pathlib import Path
import numpy as np
import yaml
from .combined_array import parse_combined_array_config
from .constants import ANTENNA_SEPARATION, ANTENNAS_PER_BOARD, ANTENNAS_PER_ROW, ROWS_PER_BOARD


class ArrayGeometry:
    def __init__(self, hosts=None, config=None):
        self.cables = self.velocity_factors = None
        self.orientations = None
        if config:
            data = config if isinstance(config, dict) else yaml.safe_load(Path(config).read_text())["combined-array"]
            data = copy.deepcopy(data)
            for board in data["boards"].values():
                cable = board.setdefault("cable", {})
                cable.setdefault("length", 0.0)
                cable.setdefault("velocity_factor", 1.0)
            self.indices, names, self.cables, self.velocity_factors, _, _, self.orientations = parse_combined_array_config(data)
            self.hosts = list(names.values())
            self.names = list(names)
            if hosts:
                if len(hosts) != len(self.hosts):
                    raise ValueError("Host overrides must match the number of boards in the layout")
                self.hosts = hosts
        else:
            self.hosts = hosts or []
            self.names = list(self.hosts)
            self.indices = np.arange(ANTENNAS_PER_BOARD).reshape(ROWS_PER_BOARD, ANTENNAS_PER_ROW) if len(self.hosts) == 1 else None
        if not self.hosts or any(not isinstance(h, str) or not h.strip() for h in self.hosts) or len(set(self.hosts)) != len(self.hosts):
            raise ValueError("Provide distinct, nonempty board addresses")

    def positions(self, boards):
        """Outward-looking x/y coordinates, indexed by board and antenna ID."""
        if self.indices is None:
            raise ValueError("Beam steering across boards requires antenna positions; use Pool.from_config()")
        rows, cols = self.indices.shape
        result = np.zeros((len(boards), ANTENNAS_PER_BOARD, 2))
        for row, col in np.ndindex(self.indices.shape):
            board, local = divmod(int(self.indices[row, col]), ANTENNAS_PER_BOARD)
            for a in range(ANTENNAS_PER_BOARD):
                if boards[board].revision.antenna_id_to_row_col(a) == divmod(local, ANTENNAS_PER_ROW):
                    result[board, a] = ((cols - 1) / 2 - col, (rows - 1) / 2 - row)
        return result * ANTENNA_SEPARATION
