"""Reference acquisition and validity for narrowband BLE phase measurements."""

from contextlib import ExitStack
from dataclasses import dataclass
import threading
import re
import numpy as np

from .ble_packet import channel_frequency
from .ble_iq import align_packet_iq, channel_response
from .sensor_calibration import SensorCalibration, ClockReferenceScope, compute_reference_path_delays


@dataclass(frozen=True)
class BLECalibration:
    """A single-board reference result bound to its receiver context.

    The common SensorCalibration supplies phase correction including PCB reference
    trace delays. Residual phase and delay are fitted only over the occupied BLE
    band after per-packet capture timing and IF-origin correction.
    """

    model: SensorCalibration
    board_index: int
    generation: int
    settings: int
    phy: int
    epochs: dict

    def matches(self, cluster):
        if cluster.board_index != self.board_index or cluster.is_calibration:
            return False
        for a in range(8):
            p = cluster.get_packet(self.board_index, a)
            if p is not None and (p.generation != self.generation or p.settings != self.settings or getattr(p, "capture_phy", p.phy) != self.phy or (p.clock_epoch, p.receiver_epoch) != self.epochs[a]):
                return False
        return True

    def correction(self, cluster):
        return self.model.phase_time_correction(np.array([channel_frequency(cluster.packet.channel)]))[..., 0]


def calibrate(receiver, board_index, samples, timeout, reference_address):
    from .ble_receiver import BLECalibrationError

    if not isinstance(samples, int) or samples < 2 or not np.isfinite(timeout) or timeout <= 0:
        raise ValueError("Calibration requires at least two samples and a positive timeout")
    if not receiver.processing:
        raise RuntimeError("Start pool processing before BLE reference acquisition")
    board = receiver.boards[board_index]
    with receiver._control_lock, ExitStack() as restore:
        config = board.ble.get_config()
        if config["phy"] not in ("LE1M", "LE2M"):
            raise BLECalibrationError("BLE phase calibration requires LE1M or LE2M")
        if not np.all(receiver._expected[board_index]):
            raise BLECalibrationError("BLE phase calibration currently requires all eight sensors on the board")
        controller = getattr(receiver._owner, "reference_controller", None)
        if reference_address is None and controller is not None:
            from .controller_reference import prepare_packet_reference

            if config["phy"] != "LE1M":
                raise BLECalibrationError("The controller reference transmitter uses LE1M")
            reference_address = prepare_packet_reference(restore, controller, "ble", config["channel"])
        if reference_address is None:
            if config["reference_source"] not in (0, 1):  # Only internal and master controllers generate REF.
                raise BLECalibrationError("Specify reference_address for an external BLE reference transmitter")
            reference_address = config.get("reference_address")
        if not isinstance(reference_address, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", reference_address):
            raise ValueError("A BLE reference advertiser address is required (xx:xx:xx:xx:xx:xx)")
        reference_address = reference_address.lower()
        if config["phy"] == "LE1M" and config["reference_source"] in (0, 1) and config["reference_mode"]:
            # Selecting REF starts the local generator and can switch the
            # controller radio. Return it to the caller's role after capture.
            from .controller_reference import select_reference_mode

            select_reference_mode(restore, board, "ble")
        ready = threading.Event()
        lock = threading.Lock()
        epochs, responses = {}, []
        phase_sum = np.zeros(receiver.shape[1:], complex)
        counts = np.zeros(receiver.shape[1:], int)
        generation = None

        def collect(cluster):
            if ready.is_set() or cluster.board_index != board_index or not cluster.is_calibration or cluster.packet.generation != generation:
                return
            # REF routing does not identify the source. Antenna leakage still
            # delivers valid packets from unrelated nearby BLE transmitters.
            packet = cluster.packet
            if not packet.is_advertisement or packet.advertiser != reference_address:
                return
            iq = align_packet_iq(cluster)
            phase = iq.phase()
            valid = phase.valid[board_index]
            if not valid[0, 0]:
                return
            context = {a: (p.clock_epoch, p.receiver_epoch) for a in range(8) if (p := cluster.get_packet(board_index, a)) is not None}
            with lock:
                if ready.is_set():
                    return
                if any(a in epochs and epochs[a] != epoch for a, epoch in context.items()):
                    counts.fill(0)
                    phase_sum.fill(0)
                    responses.clear()
                    epochs.clear()
                if not np.any(valid & (counts < samples)):
                    return
                epochs.update(context)
                vector = phase.vector[board_index]
                phase_sum[valid] += vector[valid] * vector[0, 0].conj()
                counts[valid] += 1
                responses.append(channel_response(iq))
                if np.all(counts >= samples):
                    ready.set()

        # Expert words can select a different analog signal path.
        # Measure it with those same words on the reference input.
        gain_mode = "expert" if config["gain_mode"] == "expert" else "manual"
        probe = {"rf_switch": 1, "gain_mode": gain_mode}
        if gain_mode == "manual" and config["reference_source"] in (1, 2):
            # A cabled master drives the splitter well above typical antenna levels.
            probe["rx_gain"] = 24
        handle = receiver.add_callback(collect, include_partial=True)
        try:
            with receiver._state_lock:
                receiver._invalidate_board(board_index)
                state = board.ble.set_config(probe)
                generation = state["generation"]
                receiver._generations[board_index] = generation
                receiver._epochs = {k: v for k, v in receiver._epochs.items() if k[0] != board_index}
            if not ready.wait(timeout):
                with lock:
                    observed = counts.tolist()
                raise BLECalibrationError(f"Insufficient BLE reference measurements on {board.host} " f"(usable packets by row: {observed}, need {samples} per antenna). " "Check reference generation and routing")
            with lock:
                mean = phase_sum / counts
                if np.min(abs(mean)) < 0.85:
                    raise BLECalibrationError(f"BLE reference phase is unstable on {board.host}. Check the reference path and receiver gain")
                timing, phases = fit_reference_response(responses, board_index)
        finally:
            receiver.remove_callback(handle)
            with receiver._state_lock:
                receiver._invalidate_board(board_index)
                restored = board.ble.set_config({key: config[key] for key in probe})
                receiver._generations[board_index] = restored["generation"]
                receiver._epochs = {k: v for k, v in receiver._epochs.items() if k[0] != board_index}
        if (restored["channel"], restored["phy"]) != (state["channel"], state["phy"]):
            raise BLECalibrationError("BLE receiver tuning changed during calibration")
        restore.close()
        model = SensorCalibration(
            sensor_shape=receiver.shape,
            timing_offsets=timing,
            phase_offsets=phases,
            phase_reference_frequency=channel_frequency(config["channel"]),
            clock_scope=ClockReferenceScope.PER_BOARD,
            reference_path_delays=compute_reference_path_delays(receiver.boards),
        )
        settings = board.ble.packet_settings(restored)
        result = BLECalibration(model, board_index, restored["generation"], settings, 0 if config["phy"] == "LE1M" else 1, epochs)
        with receiver._state_lock:
            receiver._calibrations[board_index] = result
        return result


def fit_reference_response(responses, board_index):
    """Fit the residual REF phase and slope after packet timing alignment.

    Like WiFi calibration, remove the timing slope before estimating constant
    phase. Unlike WiFi, BLE event timestamps are not FFT/sample-origin timestamps
    and cannot anchor timing. This model therefore describes only the residual
    aligned response within this BLE channel, not a reusable wideband delay.
    """
    offsets = responses[0].offsets_hz
    shape = responses[0].response.shape[:-1]
    timing, phases = np.zeros(shape), np.zeros(shape)
    for position in np.ndindex(shape):
        if position[0] != board_index:
            continue
        values, weights = [], []
        usable_packets = 0
        for response in responses:
            if not np.array_equal(response.offsets_hz, offsets):
                raise ValueError("Reference frequency grids must agree")
            # Remove the packet gauge even if its reference antenna changed.
            anchor = response.response[board_index, 0, 0]
            h = np.divide(response.response[position], anchor, out=np.full(anchor.shape, np.nan + 1j * np.nan), where=np.isfinite(anchor) & (abs(anchor) > 0))
            weight = response.coherence[position] * response.coherence[board_index, 0, 0]
            valid = np.isfinite(h) & (abs(h) > 0) & (weight > 0.8)
            usable_packets += bool(np.any(valid))
            values.append(np.divide(h, abs(h), out=np.zeros_like(h), where=valid))
            weights.append(np.where(valid, weight, 0))
        weight = np.sum(weights, axis=0)
        mean = np.sum(np.asarray(values) * weights, axis=0)
        valid = (weight > usable_packets * 0.5) & (abs(mean) > weight * 0.8)
        if np.count_nonzero(valid) < 4:
            raise ValueError("Insufficient coherent BLE reference bandwidth")
        f, w = offsets[valid], weight[valid]
        phase = np.unwrap(np.angle(mean[valid]))
        center = np.average(f, weights=w)
        slope = np.sum(w * (f - center) * (phase - np.average(phase, weights=w))) / np.sum(w * (f - center) ** 2)
        timing[position] = -slope / (2 * np.pi)
        phases[position] = np.angle(np.sum(mean[valid] * np.exp(-1j * slope * f)))
    return timing, phases
