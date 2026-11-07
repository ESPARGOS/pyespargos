"""Replay measured IQ covariances against independently measured WiFi CSI.

Fixture: greenspargel 2x4, 2472 MHz, 2026-10-30, robot positions A/B/C,
R and L feeds held separately. Each IQ covariance is the recording nearest
its feed/position's median dominant direction among strong captures. CSI
references are averages of robot-MAC packets at the same position/feed.
Recording filenames are embedded in the fixture for traceability; raw data
and comparison script are in ../measurements/iq-camera-debug.

This checks agreement of two measurement paths, NOT line-of-sight accuracy:
both can follow the same reflection (particularly at position C). No packet
identity or robot position is supplied to the production IQ estimator.
"""

from pathlib import Path

import numpy as np

from espargos.array_processing import root_music_2d


def test_recorded_iq_directions_agree_with_same_feed_csi_across_positions():
    with np.load(Path(__file__).parent / "fixtures/iq_camera_room_covariances.npz") as data:
        assert set(data["labels"]) == {"A-R", "A-L", "B-R", "B-L", "C-R", "C-L"}
        for label, covariance, snapshots, expected in zip(data["labels"], data["covariances"], data["snapshots"], data["csi_directions"]):
            directions, powers, _ = root_music_2d(covariance, 2, 4, frequency_hz=2472e6, snapshots=int(snapshots), dynamic_range_db=12)
            assert len(powers), label
            np.testing.assert_allclose(directions[np.argmax(powers)], expected, atol=0.06, err_msg=str(label))
            # Near-coincident roots must not manufacture large source powers.
            assert np.all(np.isfinite(powers)) and np.all(powers >= 0)
            assert powers.sum() * 8 <= np.trace(covariance).real * 1.05
