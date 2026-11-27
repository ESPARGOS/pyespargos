#!/usr/bin/env python

"""IQ-sampling Board capability: IQ capture configuration, reference tone
control, sync anchors, and the IQ chunk stream subscription.

``board.iq`` groups the controller operations of the IQ-sampling mode.
"""

import json
import math
import time
from typing import Any, Callable

from espargos.board import BoardCapability, SensorMessageSubscription
from espargos.sensor import SensorMessage

from .iq_rx_stream import IQStreamPacket, IQ_RX_STREAM_TYPE_HEADER
from .iq_packet import (
    IQ_ACCUM_TYPE_HEADER,
    IQ_CHUNK_TYPE_HEADER,
    IQAccumPacket,
    IQChunkPacket,
    IQ_CHUNK_FLAG_REFERENCE,
    IQ_CHUNK_FLAG_REFERENCE_READY,
)

__all__ = ["IQCapability"]


class IQCapability(BoardCapability):
    """Board capability for IQ-sampling capture control and chunk reception."""

    def __init__(self, board):
        if board.api_version[0] < 4:
            from espargos.board import EspargosAPIVersionError

            raise EspargosAPIVersionError("IQ mode requires controller API 4 or later and matching sensor firmware.")
        super().__init__(board)

    def set_config(self, config: dict):
        """Apply a partial IQ-capture configuration.

        Receiver updates use the canonical ``receivers`` list. Each entry must
        contain an ``antid`` and may contain ``rf_freq_hz``, ``gain_mode``
        (``"auto"``, ``"manual"`` or ``"expert"``), ``rx_gain``, and
        ``expert_gain_words``. Use a scalar ``rf_switch`` to set all inputs
        or a mapping by antenna ID to change selected inputs on API 6.
        Capture-wide settings such as ``adc_decimation`` and ``trigger``
        apply to the whole board.

        ``trigger`` is a named object, e.g. ``{"mode": "interval",
        "interval_chunks": 16384, "burst_chunks": 4}``. On API 6, changing one field retains
        the other settings for that trigger mode. Changing modes starts from
        the new mode's defaults. Older controllers start from defaults on
        every trigger write. ``get_config()`` returns all applied
        defaults. The controller validates field names, types, and ranges.
        """
        self._board._resources.write_radio("iq/rx", config)

    def set_config_and_wait(self, config: dict, timeout: float = 5.0) -> int:
        """Apply settings and wait for every selected IQ receiver to report the generation.

        This also works for a quiet trigger that produces no samples. Requires
        firmware supporting generation receipts and sensor acknowledgements.
        Raises if a newer configuration supersedes the requested generation.
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        response = self._board._resources.write_radio("iq/rx", {**config, "return_generation": True})
        if response is None:
            raise RuntimeError("Controller accepted IQ settings but does not support generation receipts")
        generation = response["config_generation"]
        deadline = time.monotonic() + timeout
        while True:
            current = self.get_rx_status()
            if current["config_generation"] != generation:
                raise RuntimeError(f"IQ generation {generation} was superseded by {current['config_generation']}")
            pending = [a for a, applied in enumerate(current["applied_generations"]) if current["receiver_mask"] & (1 << a) and applied != generation]
            if not pending:
                return generation
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"IQ generation {generation} unconfirmed on sensors {pending}")
            time.sleep(min(0.1, remaining))

    def get_rx_status(self) -> dict:
        """Return the selected receiver mask and last generation reported by each antenna.

        An entry is null until the controller receives status from a sensor
        supporting RX acknowledgements. Zero denotes an inactive receiver.
        Controllers exposing report_age_ms give the age of the corresponding
        acknowledgement in milliseconds, indexed by antenna ID.
        """
        return self._board._resources.read("iq/rx/status")

    def get_config(self) -> dict:
        """Return the controller's canonical eight-receiver IQ configuration."""
        return self._board._resources.read_radio("iq/rx")

    def get_config_json(self) -> str:
        """Return the raw IQ configuration JSON string (e.g. for QML consumers)."""
        return json.dumps(self.get_config())

    def rearm_signal_capture(self):
        """Atomically discard retained Signal state on all eight sensors.

        Starts a new capture generation and resumes acquisition together on
        this array. Other arrays are unaffected. This does not synchronize
        Signal triggers across a combined multi-array setup.
        """
        self._board._resources.write_radio("iq/rx", {"rearm_signal": True})

    def acknowledge_signal_capture(self, config_generation: int, capture_id: int):
        """Acknowledge one fully validated Signal event to all array sensors.

        Call this after validating a complete capture to release its retained
        samples. The acknowledgement applies only to this array.
        """

        self._board._resources.command(
            "iq/rx/signal/ack",
            {
                "config_generation": int(config_generation),
                "capture_id": int(capture_id),
            },
        )

    def set_receiver(self, antenna_id: int, **settings):
        """Apply a partial update to one IQ receiver."""
        if not isinstance(antenna_id, int) or not 0 <= antenna_id < 8:
            raise ValueError("antenna_id must be an integer in the range 0..7")
        self.set_config({"receivers": [{"antid": antenna_id, **settings}]})

    def set_all_receivers(self, **settings):
        """Apply the same receiver settings to all eight IQ receivers."""
        self.set_config({"receivers": [{"antid": antenna_id, **settings} for antenna_id in range(8)]})

    def set_sync_anchor(self, anchor_ns: list, seq=None, *, boot_ids=None) -> str:
        """Post CPU-cycle reference timestamps (one per antenna ID, encoded in ns).

        The controller schedules a coordinated IQ engine (re)start based on the
        anchors. Pass the eight ``boot_ids`` from RX status before acquiring
        the reference to reject a sensor restart during the measurement.
        Returns the controller response (the new sync sequence).
        """
        if len(anchor_ns) != 8:
            raise ValueError("anchor_ns must contain one entry per antenna ID (8)")
        payload = {"anchor_ns": [int(value) for value in anchor_ns]}
        if boot_ids is not None:
            if len(boot_ids) != 8:
                raise ValueError("boot_ids must contain one entry per antenna ID (8)")
            payload["boot_ids"] = [None if boot is None else int(boot) for boot in boot_ids]
        if seq is not None:
            payload["seq"] = int(seq)
        return self._board._resources.post("iq/rx/sync-anchor", payload)

    def set_reference(self, mode="off", freq_hz=None, backoff_qdb=30, cw_tuning=None):
        """Set the controller reference source: off, cw, or broadband iq.

        Reference generation is independent of sensor modes.
        WiFi reference packets pause while CW/noise owns the radio and resume
        when disabled. Stop reference monitoring before enabling this source.
        """
        payload = {"mode": mode}
        if mode != "off":
            payload.update(freq_khz=round(freq_hz / 1000), backoff_qdb=backoff_qdb)
            if cw_tuning is not None:
                payload["cw_tuning"] = cw_tuning
        return self._board._resources.post("reference/iq", payload)

    def get_reference(self):
        """Return the reference source state."""
        return self._board._resources.read("reference/iq")

    def subscribe_chunks(
        self,
        callback: Callable[[SensorMessage[IQChunkPacket]], None],
    ) -> SensorMessageSubscription:
        """Subscribe to decoded IQ chunk messages while preserving sensor metadata."""
        return self._subscribe_decoded_sensor_messages(
            IQ_CHUNK_TYPE_HEADER,
            IQChunkPacket,
            lambda message: callback(message) if not message.payload.flags & (IQ_CHUNK_FLAG_REFERENCE | IQ_CHUNK_FLAG_REFERENCE_READY) else None,
        )

    def subscribe_accumulations(
        self,
        callback: Callable[[SensorMessage[IQAccumPacket]], None],
    ) -> SensorMessageSubscription:
        """Subscribe to decoded ``IQA1`` accumulated-vector sections."""

        return self._subscribe_decoded_sensor_messages(
            IQ_ACCUM_TYPE_HEADER,
            IQAccumPacket,
            callback,
        )

    def subscribe_stream(self, callback: Callable[[SensorMessage[IQStreamPacket]], None]) -> SensorMessageSubscription:
        """Receive each antenna's narrowband IQ blocks, including its absolute output sample index."""
        return self._subscribe_decoded_sensor_messages(IQ_RX_STREAM_TYPE_HEADER, IQStreamPacket, callback)
