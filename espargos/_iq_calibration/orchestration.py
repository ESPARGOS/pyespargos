"""Common entry point for controller-monitor and sensor-reciprocity calibration."""

import numpy as np
from ..calibration import Antenna, AntennaCorrection, Calibration, CalibrationGroup, CalibrationState
from ..iq_packet import DECIM_TO_FS
from .reference import _map
from ..iq_tx_control import _rpc


def snapshot(pool, tx_result=None, method="rx", scope=None):
    """Bind measured corrections to the restored, currently running sensors."""
    from .scope import CalibrationScope

    scope = scope or CalibrationScope(pool)
    configs = _map(lambda b: b.iq.get_config(), pool.boards)
    states = _map(lambda b: _rpc(b, "get_iq_tx"), pool.boards)
    rx, tx = [], []
    if tx_result is not None:
        selected = {(bi, a) for bi, c in enumerate(configs) for a, mode in enumerate(c["modes"]) if mode == "iq_tx" and a in scope.ids[bi]}
        if selected != set(tx_result["_tx_states"]):
            raise RuntimeError("Sensor roles changed while publishing calibration")
    model = pool.calibration
    epochs = None if pool._calibration_epoch is None else np.frombuffer(pool._calibration_epoch, np.uint64).reshape((len(pool.boards), 2, 4))
    for bi, board in enumerate(pool.boards):
        for a, mode in enumerate(configs[bi]["modes"]):
            antenna = Antenna(board.host, a)
            if antenna not in scope.antennas:
                continue
            sensor = states[bi]["sensors"][a]
            pos = (bi, *board.revision.antenna_id_to_row_col(a))
            if mode == "iq_rx" and model is not None and epochs is not None:
                center = configs[bi]["receivers"][a]["rf_freq_hz"]
                rate = DECIM_TO_FS[configs[bi]["adc_decimation"]]
                if center != model.phase_reference_frequency or rate != pool.calibration_sample_rate:
                    continue
                origin = None if pool._calibration_grid_origin is None else int(pool._calibration_grid_origin[pos])
                state = CalibrationState(antenna, "rx", center, rate, sensor["boot_id"], int(epochs[pos]), sample_origin_ns=origin)
                phase = -model.phase_offsets[pos] - 2 * np.pi * center * model.reference_path_delays[pos]
                delay = model.reference_path_delays[pos] - model.timing_offsets[pos]
                rx.append(AntennaCorrection(state, float(phase), float(delay)))
            elif mode == "iq_tx" and tx_result is not None:
                if not sensor.get("origin_ns") or not sensor["enabled"]:
                    raise RuntimeError(f"{antenna}: transmitter has no replay timestamp")
                if (sensor["boot_id"], sensor["epoch"]) != tx_result["_tx_states"][bi, a] or sensor["frequency_hz"] != tx_result["_center_hz"]:
                    raise RuntimeError(f"{antenna}: configuration changed while publishing calibration")
                state = CalibrationState.from_tx_status(board.host, sensor)
                correction = tx_result["calibration"][bi]
                tx.append(AntennaCorrection(state, np.deg2rad(correction["phase_deg"][a]), correction["delay_samples"][a] / 80e6))
    groups = tuple(CalibrationGroup(tuple(corrections)) for corrections in (rx, tx) if corrections)
    diagnostics = {k: v for k, v in (tx_result or {}).items() if k != "calibration" and not k.startswith("_")}
    result = Calibration(groups, {"method": method, **diagnostics})
    return result


def calibrate(pool, *, method="monitor", path="reference", scope=None, **kwargs):
    """Calibrate current IQ TX sensors, preserving waveforms and receiver roles.

    ``monitor`` needs a bidirectional RF path back to the master controller.
    ``reciprocity`` measures two-way sensor links; ``path`` can be ``reference``
    (default), ``antenna_l`` or ``antenna_r``. Both methods require a shared
    reference clock and forward RX reference. Each run uses one frequency.
    Returns a scoped :class:`Calibration`; corrections are already applied.
    """
    if method not in ("monitor", "reciprocity"):
        raise ValueError("method must be monitor or reciprocity")
    if path not in ("reference", "antenna_l", "antenna_r") or method == "monitor" and path != "reference":
        raise ValueError("Monitor calibration requires reference; reciprocity also supports antenna_l/antenna_r")
    if method == "monitor":
        from .monitor import calibrate as run

        result = run(pool, scope=scope, **kwargs)
    else:
        from .reciprocity import calibrate as run

        result = run(pool, pilot_output=path, scope=scope, **kwargs)
    try:
        result["path"] = path
        return snapshot(pool, result, method, scope)
    except Exception:
        # A result without verified hardware scope must never leave TX active.
        _map(lambda b: _rpc(b, "set_iq_tx", {"muted": True}), pool.boards if scope is None else [pool.boards[bi] for bi in scope.active])
        raise
