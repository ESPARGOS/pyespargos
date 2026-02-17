#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

from demos.common import ESPARGOSApplication, BacklogMixin, CombinedArrayMixin, SingleCSIFormatMixin

import matplotlib.colors
import numpy as np
import espargos
import argparse

import PyQt6.QtCore
import PyQt6.QtGui
import PyQt6.QtQml
import PyQt6.QtQuick3D

# Resolution of the radiation pattern sphere mesh (front hemisphere; back is mirrored)
SPHERE_AZIMUTH_STEPS = 80
SPHERE_ELEVATION_STEPS = 40
ANTENNA_SPACING = 12.0
ANTENNA_SCALING = 200.0


class RadiationPatternGeometry(PyQt6.QtQuick3D.QQuick3DGeometry):
    """Radiation pattern sphere mesh with per-vertex color.
    Front hemisphere from FFT, back hemisphere mirrors front (or collapses when element pattern is on).
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setStride(7 * 4)  # 3 pos + 4 color RGBA = 28 bytes/vertex
        self.addAttribute(PyQt6.QtQuick3D.QQuick3DGeometry.Attribute.Semantic.PositionSemantic, 0, PyQt6.QtQuick3D.QQuick3DGeometry.Attribute.ComponentType.F32Type)
        self.addAttribute(PyQt6.QtQuick3D.QQuick3DGeometry.Attribute.Semantic.ColorSemantic, 3 * 4, PyQt6.QtQuick3D.QQuick3DGeometry.Attribute.ComponentType.F32Type)
        self.setPrimitiveType(PyQt6.QtQuick3D.QQuick3DGeometry.PrimitiveType.Triangles)

        az_steps, el_steps = SPHERE_AZIMUTH_STEPS, SPHERE_ELEVATION_STEPS

        # Sin-uniform grid; max |sin| = N/(N+1) to avoid grating null at sin = +/-1
        sin_max_az = az_steps / (az_steps + 1)
        sin_max_el = el_steps / (el_steps + 1)
        front_az = np.arcsin(np.linspace(-sin_max_az, sin_max_az, az_steps + 1))
        front_el = np.arcsin(np.linspace(-sin_max_el, sin_max_el, el_steps + 1))

        az_f, el_f = np.meshgrid(front_az, front_el)
        az_b, el_b = np.meshgrid(np.pi - front_az, front_el)

        self._az_grid = np.concatenate([az_f, az_b], axis=1)
        self._el_grid = np.concatenate([el_f, el_b], axis=1)

        self._unit_dirs = np.stack(
            [
                np.cos(self._el_grid) * np.sin(self._az_grid),
                np.sin(self._el_grid),
                np.cos(self._el_grid) * np.cos(self._az_grid),
            ],
            axis=-1,
        )

        total_az = self._az_grid.shape[1]
        ei_g, ai_g = np.meshgrid(np.arange(el_steps), np.arange(total_az - 1), indexing="ij")
        ei, ai = ei_g.ravel(), ai_g.ravel()
        self._tri_ei = np.column_stack([ei, ei + 1, ei + 1, ei, ei + 1, ei]).ravel()
        self._tri_ai = np.column_stack([ai, ai, ai + 1, ai, ai + 1, ai + 1]).ravel()

        # Default mesh
        self._update_mesh(
            np.ones_like(self._az_grid),
            np.full(self._az_grid.shape + (3,), 0.3),
        )

    def _update_mesh(self, radii, colors):
        positions = self._unit_dirs * radii[..., np.newaxis]
        tri_pos = positions[self._tri_ei, self._tri_ai]
        tri_col = colors[self._tri_ei, self._tri_ai]

        vdata = np.empty((tri_pos.shape[0], 7), dtype=np.float32)
        vdata[:, :3], vdata[:, 3:6], vdata[:, 6] = tri_pos, tri_col, 1.0

        self.setVertexData(PyQt6.QtCore.QByteArray(vdata.tobytes()))
        self.setBounds(
            PyQt6.QtGui.QVector3D(*(positions[..., i].min() for i in range(3))),
            PyQt6.QtGui.QVector3D(*(positions[..., i].max() for i in range(3))),
        )
        self.update()

    def updatePattern(self, front_radii, front_colors, show_back):
        """Update mesh. Back hemisphere mirrors front if show_back, else collapses to zero."""
        back_radii = front_radii if show_back else np.zeros_like(front_radii)
        back_colors = front_colors if show_back else np.zeros_like(front_colors)
        self._update_mesh(
            np.concatenate([front_radii, back_radii], axis=1),
            np.concatenate([front_colors, back_colors], axis=1),
        )


class EspargosDemoRadiationPattern3D(BacklogMixin, CombinedArrayMixin, SingleCSIFormatMixin, ESPARGOSApplication):
    configChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "color_mode": "power",
        "pattern_scale": 100.0,
        "element_pattern": True,
        "max_delay": 0.2,
        "polarization_mode": "ignore",
    }

    def __init__(self, argv):
        parser = argparse.ArgumentParser(description="ESPARGOS Demo: 3D Radiation Pattern", add_help=False)
        parser.add_argument("--no-calib", default=False, help="Do not calibrate", action="store_true")
        super().__init__(argv, argparse_parent=parser)
        self.initialize_pool(calibrate=not self.args.no_calib)

        # Element pattern on the front-hemisphere grid
        sin_max_az = SPHERE_AZIMUTH_STEPS / (SPHERE_AZIMUTH_STEPS + 1)
        sin_max_el = SPHERE_ELEVATION_STEPS / (SPHERE_ELEVATION_STEPS + 1)
        az_mesh, el_mesh = np.meshgrid(
            np.arcsin(np.linspace(-sin_max_az, sin_max_az, SPHERE_AZIMUTH_STEPS + 1)),
            np.arcsin(np.linspace(-sin_max_el, sin_max_el, SPHERE_ELEVATION_STEPS + 1)),
        )
        self.element_pattern = np.cos(el_mesh) ** 0.4 * np.cos(az_mesh) ** 0.4

        # Per-antenna inverse Jones matrices for polarization correction (R/L to H/V)
        self.jones_matrices_inv = espargos.util.build_jones_matrices(self.antenna_orientations)

        self.geometry = RadiationPatternGeometry()
        self._board_placements = self._compute_board_placements()

        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "radiation-pattern-3d-ui.qml",
            {"patternGeometry": self.geometry},
        )

    def _compute_board_placements(self):
        """Compute 3D position and rotation for each board. Returns list of {x, y, z_rot} dicts."""
        boards_seen = {}
        for r in range(self.n_rows):
            for c in range(self.n_cols):
                idx = int(self.indexing_matrix[r, c])
                board_offset = (idx // espargos.constants.ANTENNAS_PER_BOARD) * espargos.constants.ANTENNAS_PER_BOARD
                local_idx = idx - board_offset
                local_row = local_idx // espargos.constants.ANTENNAS_PER_ROW
                local_col = local_idx % espargos.constants.ANTENNAS_PER_ROW
                if local_row == 0 and local_col == 0:
                    boards_seen[board_offset] = (r, c, self.antenna_orientations[r, c])

        center_r = (self.n_rows - 1) / 2.0
        center_c = (self.n_cols - 1) / 2.0

        orientation_to_z_rot = {
            espargos.util.AntennaOrientation.N: 180,
            espargos.util.AntennaOrientation.E: -90,
            espargos.util.AntennaOrientation.S: 0,
            espargos.util.AntennaOrientation.W: 90,
        }

        board_center_local = np.array([0.5, 1.5])

        placements = []
        for board_offset, (origin_r, origin_c, orientation) in boards_seen.items():
            rot_mat = orientation.rotation_matrix()
            board_center_combined = np.array([origin_r, origin_c]) + rot_mat @ board_center_local
            x = (board_center_combined[1] - center_c) * ANTENNA_SPACING
            y = -(board_center_combined[0] - center_r) * ANTENNA_SPACING
            z_rot = orientation_to_z_rot.get(orientation, 0)
            placements.append({"x": float(x), "y": float(y), "z_rot": float(z_rot)})

        return placements

    def _on_update_app_state(self, newconfig):
        self.configChanged.emit()
        super()._on_update_app_state(newconfig)

    @PyQt6.QtCore.pyqtSlot()
    def updateRequest(self):
        pol_mode = self.appconfig.get("polarization_mode")

        if pol_mode == "incorporate":
            result = self.get_backlog_csi("rfswitch_state")
            if result is None:
                return
            csi_backlog, rfswitch_state_backlog = result
        else:
            if (csi_backlog := self.get_backlog_csi()) is None:
                return

        csi_largearray = espargos.util.build_combined_array_data(self.indexing_matrix, csi_backlog)

        if pol_mode == "incorporate":
            rfswitch_combined = espargos.util.build_combined_array_data(self.indexing_matrix, rfswitch_state_backlog)
            csi_largearray = espargos.util.separate_feeds(csi_largearray, rfswitch_combined)
            if csi_largearray is None:
                print("Need measurements for both R and L feeds for polarization mode")
                return
            # Apply per-antenna Jones correction (R/L to global H/V)
            csi_largearray = np.einsum("dmnsf,mnfp->dmnsp", csi_largearray, self.jones_matrices_inv)

        csi_avg = espargos.util.csi_interp_iterative(csi_largearray, iterations=5)

        az_steps, el_steps = SPHERE_AZIMUTH_STEPS, SPHERE_ELEVATION_STEPS
        use_elem = self.appconfig.get("element_pattern")
        scale = self.appconfig.get("pattern_scale")

        # Zero-pad and 2D-FFT to beamspace
        zp = np.zeros((az_steps + 1, el_steps + 1) + csi_avg.shape[2:], dtype=complex)
        r2, c2 = self.n_rows // 2, self.n_cols // 2
        zr, zc = (el_steps + 1) // 2, (az_steps + 1) // 2
        zp[zc - c2 : zc + c2, zr - r2 : zr + r2, ...] = np.moveaxis(csi_avg, [0, 1], [1, 0])

        bfs = np.fft.fftshift(np.fft.fft2(np.fft.ifftshift(zp, axes=(0, 1)), axes=(0, 1)), axes=(0, 1))
        bfs = np.moveaxis(bfs, [0, 1], [1, 0])[::-1, :, ...]  # (el, az, ...), flip el

        # Power: average over subcarriers (and polarization if present)
        bp = np.sqrt(np.mean(np.abs(bfs) ** 2, axis=tuple(range(2, bfs.ndim)))) / (self.n_rows * self.n_cols)
        if use_elem:
            bp *= self.element_pattern

        mx = np.max(bp)
        bp_n = bp / mx if mx > 0 else bp

        if self.appconfig.get("color_mode") == "delay":
            bfs_delay = np.moveaxis(bfs, -1, 0) if pol_mode == "incorporate" else bfs
            wdp = np.sum(bfs_delay[..., 1:] * np.conj(bfs_delay[..., :-1]), axis=-1)
            if wdp.ndim > 2:
                wdp = np.sum(wdp, axis=tuple(range(wdp.ndim - 2)))
            hsv = np.zeros((el_steps + 1, az_steps + 1, 3))
            hsv[..., 0] = (np.clip((np.angle(wdp) - np.angle(np.sum(wdp))) / self.appconfig.get("max_delay"), 0, 1) + 1 / 3) % 1
            hsv[..., 1], hsv[..., 2] = 0.8, bp_n
            colors = matplotlib.colors.hsv_to_rgb(hsv)
        else:
            colors = self._power_colors(bp_n)

        self.geometry.updatePattern(bp_n * scale, colors, show_back=not use_elem)

    @staticmethod
    def _power_colors(v):
        colors = np.zeros(v.shape + (3,))
        lo = v < 0.5
        colors[lo, 0], colors[lo, 1] = v[lo] * 2, 1.0
        colors[~lo, 0], colors[~lo, 1] = 1.0, 1 - (v[~lo] - 0.5) * 2
        return colors

    @PyQt6.QtCore.pyqtProperty(list, constant=True)
    def boardPlacements(self):
        return self._board_placements

    @PyQt6.QtCore.pyqtProperty(str, constant=True)
    def arrayModelSource(self):
        return pathlib.Path(__file__).resolve().parent.as_uri() + "/array-3dmodel.glb"

    @PyQt6.QtCore.pyqtProperty(float, constant=True)
    def arrayModelScale(self):
        return ANTENNA_SCALING

    @PyQt6.QtCore.pyqtProperty("QVariant", constant=True)
    def arrayModelOriginOffset(self):
        # GLB origin is at board corner; offset to align with computed center
        return {"x": -1.0 * ANTENNA_SPACING, "y": -2.0 * ANTENNA_SPACING}


app = EspargosDemoRadiationPattern3D(sys.argv)
sys.exit(app.exec())
