#!/usr/bin/env python

"""IQ-sampling Board capability: IQ capture configuration, reference tone
control, sync anchors, and the IQ chunk stream subscription.

``board.iq`` groups the controller operations of the IQ-sampling mode.
"""

from typing import Any, Callable

from espargos.board import BoardCapability, SensorMessageSubscription
from espargos.sensor import SensorMessage

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
        if board.api_version[0] != 4:
            from espargos.board import EspargosAPIVersionError

            raise EspargosAPIVersionError("IQ mode requires controller API major 4 and matching sensor firmware. Update the array firmware!")
        super().__init__(board)

    def set_config(self, config: dict):
        """Apply a partial IQ-capture configuration.

        Receiver updates use the canonical ``receivers`` list. Each entry must
        contain an ``antid`` and may contain ``rf_freq_hz``, ``gain_mode``
        (``"auto"``, ``"manual"`` or ``"expert"``), ``rx_gain``, and
        ``expert_gain_words``. Capture-wide fields (e.g. ``mode``,
        ``adc_decimation``, ``rf_switch``, ``trigger``) remain at the top level.

        ``trigger`` is a named object, e.g. ``{"mode": "interval",
        "interval_chunks": 16384, "burst_chunks": 4}``. Supplying it replaces
        the trigger settings with defaults for omitted fields. Omit it to
        leave the trigger unchanged. ``get_config()`` returns all applied
        defaults. The controller validates field names, types, and ranges.
        """
        self._board.control.command("set_iq_control", config)

    def get_config(self) -> dict:
        """Return the controller's canonical eight-receiver IQ configuration."""
        return self._board.control.get_json("get_iq_control")

    def get_config_json(self) -> str:
        """Return the raw IQ configuration JSON string (e.g. for QML consumers)."""
        return self._board.control.fetch("get_iq_control")

    def rearm_signal_capture(self):
        """Atomically discard retained Signal state on all eight sensors.

        Starts a new capture generation and resumes acquisition together on
        this array. Other arrays are unaffected. This does not synchronize
        Signal triggers across a combined multi-array setup.
        """
        self._board.control.command("set_iq_control", {"rearm_signal": True})

    def acknowledge_signal_capture(self, config_generation: int, capture_id: int):
        """Acknowledge one fully validated Signal event to all array sensors.

        Call this after validating a complete capture to release its retained
        samples. The acknowledgement applies only to this array.
        """

        self._board.control.command(
            "ack_iq_signal",
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

    def set_sync_anchor(self, anchor_ns: list, seq=None) -> str:
        """Post CPU-cycle reference timestamps (one per antenna ID, encoded in ns).

        The controller schedules a coordinated IQ engine (re)start based on the
        anchors. Returns the controller response (the new sync sequence).
        """
        if len(anchor_ns) != 8:
            raise ValueError("anchor_ns must contain one entry per antenna ID (8)")
        payload = {"anchor_ns": [int(value) for value in anchor_ns]}
        if seq is not None:
            payload["seq"] = int(seq)
        return self._board.control.post_json("set_iq_sync_anchor", payload)

    def set_reference(self, mode="off", freq_hz=None, backoff_qdb=30, cw_tuning=None):
        """Set the API-v4 reference source: off, cw, or broadband iq."""
        payload = {"mode": mode}
        if mode != "off":
            payload.update(freq_khz=round(freq_hz / 1000), backoff_qdb=backoff_qdb)
            if cw_tuning is not None:
                payload["cw_tuning"] = cw_tuning
        return self._board.control.post_json("set_iq_reference", payload)

    def get_reference(self):
        """Return the API-v4 reference source state."""
        return self._board.control.get_json("get_iq_reference")

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
