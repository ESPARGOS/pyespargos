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
)

__all__ = ["IQCapability"]


class IQCapability(BoardCapability):
    """Board capability for IQ-sampling capture control and chunk reception."""

    def set_config(self, config: dict):
        """Apply a partial IQ-capture configuration.

        Receiver updates use the canonical ``receivers`` list. Each entry must
        contain an ``antid`` and may contain ``rf_freq_hz``, ``gain_mode``
        (``"auto"``, ``"manual"`` or ``"expert"``), ``rx_gain``, and
        ``expert_gain_words``. Capture-wide fields (e.g. ``mode``,
        ``adc_decimation``, ``rf_switch``, ``trigger_mode``,
        ``trigger_config``) remain at the top level.
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

        Event validation remains a host decision. The controller only holds
        the shared BOOT barrier while it broadcasts a fresh configuration
        generation, then lets all sensors resume on one common boundary.
        """
        self._board.control.command("set_iq_control", {"rearm_signal": True})

    def acknowledge_signal_capture(self, config_generation: int, capture_id: int):
        """Acknowledge one fully validated Signal event to all array sensors.

        The controller is only a stateless UART relay. Event completeness is
        deliberately decided here on the host so future coherent multi-board
        coordinators can delay each board's acknowledgement until their joint
        event is complete.
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

    def set_sync_anchor(self, anchor_ns: list) -> str:
        """Post per-sensor sync anchor timestamps (one per antenna ID, in nanoseconds).

        The controller schedules a coordinated IQ engine (re)start based on the
        anchors. Returns the controller response (the new sync sequence).
        """
        if len(anchor_ns) != 8:
            raise ValueError("anchor_ns must contain one entry per antenna ID (8)")
        return self._board.control.post_json("set_iq_sync_anchor", {"anchor_ns": [int(value) for value in anchor_ns]})

    def set_reftx_tone(self, payload: dict):
        """Configure the reference CW tone generator (``enable``, ``freq_khz``, ``backoff_qdb``)."""
        self._board.control.command("set_reftx_tone", payload)

    def set_reftx_tone_freq(self, payload: dict):
        """Configure a forced-VCO-capacitor tone (``freq_khz``, ``cbw``, ``cap``)."""
        self._board.control.command("set_reftx_tone_freq", payload)

    def get_reftx_tone(self) -> dict:
        """Return the reference CW tone generator state."""
        return self._board.control.get_json("get_reftx_tone")

    def get_reftx_tone_json(self) -> str:
        """Return the raw reference tone state JSON string (e.g. for QML consumers)."""
        return self._board.control.fetch("get_reftx_tone")

    def subscribe_chunks(
        self,
        callback: Callable[[SensorMessage[IQChunkPacket]], None],
    ) -> SensorMessageSubscription:
        """Subscribe to decoded IQ chunk messages while preserving sensor metadata."""
        return self._subscribe_decoded_sensor_messages(
            IQ_CHUNK_TYPE_HEADER,
            IQChunkPacket,
            callback,
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
