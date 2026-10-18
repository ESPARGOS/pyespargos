"""Shared Jones correction for CSI polarization demos."""

import numpy as np

import espargos.csi_processing
from espargos.polarization import inverse_jones_response


class PolarizationMixin:
    def correct_polarization(self, csi, csi_format):
        """Correct (..., rows, columns, subcarriers, feeds) CSI into global H/V."""
        config = self.pooldrawer.configManager()
        channel = int(config.get("channel"))
        secondary = int(config.get("secondary_channel"))
        key = (csi_format, channel, secondary)
        if getattr(self, "_polarization_frequency_key", None) != key:
            if csi_format == "ht40":
                if secondary not in (1, 2):
                    raise ValueError("HT40 Jones correction requires an above/below secondary channel")
                secondary_channel = channel + (4 if secondary == 1 else -4)
                frequencies = espargos.csi_processing.get_frequencies_ht40(channel, secondary_channel)
            else:
                functions = {"lltf": espargos.csi_processing.get_frequencies_lltf, "ht20": espargos.csi_processing.get_frequencies_ht20, "he20": espargos.csi_processing.get_frequencies_he20}
                if csi_format not in functions:
                    raise ValueError(f"Unsupported CSI format: {csi_format}")
                frequencies = functions[csi_format](channel)
            self._polarization_frequency_inverse = inverse_jones_response(self.antenna_orientations, frequencies)
            self._polarization_frequency_key = key
        if self._polarization_frequency_inverse.shape[2] != csi.shape[-2]:
            raise ValueError("CSI subcarrier count does not match the antenna response grid")
        return np.einsum("...mnsf,mnspf->...mnsp", csi, self._polarization_frequency_inverse)
