#!/usr/bin/env python

"""Central timestamp-anchored IQ chunk-grid synchronization.

In WiFi mode, every REFTX packet is hardware-timestamped by each sensor with
nanosecond precision. This module picks ONE reference packet that was received
by ALL sensors (a complete calibration-CSI cluster — central selection solves
the "did everyone sync to the same packet?" ambiguity), reads each sensor's
own local timestamp of that packet, and posts these per-antenna-ID 64-bit ns
anchors to the controller (set_iq_sync_anchor RPC). Each sensor's IQ chunk
grid is then anchored at anchor + k * chunk_period in its own local MAC-time
domain — since all anchors mark the SAME physical instant, the grids (and
chunk indices) coincide array-wide, without any shared trigger wiring.

The same holds ACROSS boards: on a coherent multi-board setup (e.g. the
Aperture Kit, where the master's REFTX feeds every board's sensors through a
length-matched star splitter) one reference packet is received by all boards'
sensors simultaneously. Passing a pool spanning several boards to
:func:`post_sync_anchors` therefore anchors ALL boards' chunk grids to the
same instant — equal grid index means the same instant array-of-arrays-wide.

A single packet is sufficient: the sensors' composed ns timestamps carry
modem time directly (the CPU-vs-modem clock offset is compensated in the
sensor firmware). Re-measured 2026-07-31 over 2000 reference packets: the
per-packet scatter of the inter-sensor timestamp offsets is <= 37.5 ns
(3 ticks of the 80 MHz timestamp clock) — at most a few samples even at
80 MSa/s, well inside the fine calibration's +-16-sample search window.
"""

import json
import time

import numpy as np

import espargos

__all__ = ["acquire_reference_anchors", "post_sync_anchor", "post_sync_anchors"]


def acquire_reference_anchors(pool, boards, timeout=20.0, log=None):
    """Wait for the FIRST reference packet received by EVERY sensor of EVERY
    board and return its per-board, per-antenna-ID 64-bit ns timestamps (each in
    the receiving sensor's own local MAC-time domain) — all marking the SAME
    physical instant.

    :param pool: A started :class:`espargos.CSIPool` spanning ``boards``,
        driven by this function via :meth:`run`.
    Returns anchors list[n_boards][8], or None on timeout."""
    first = []

    def cb(cluster):
        if first:
            return
        ts = cluster.sensor_timestamps  # (boards, 2, 4) seconds
        if np.any(np.isnan(ts.reshape(-1))):
            return  # not received by every sensor
        first.append(ts)

    callback_handle = pool.add_csi_callback(cb)
    was_emitting = pool.emit_calibration_csi
    was_rf_switch = pool.get_rf_switch()
    # Calibration clusters ride the reference path: REFTX -> power splitter ->
    # every sensor. That guaranteed-common path is exactly what makes them the
    # right sync reference packets.
    try:
        pool.set_rf_switch(espargos.RFSwitchState.SENSOR_RFSWITCH_REFERENCE)
        pool.emit_calibration_csi = True
        if log is not None:
            log(f"sync: waiting for one reference packet received by every sensor of {len(boards)} board(s) (timeout {timeout:.0f} s)")
        t0 = time.time()
        last_progress = t0
        while not first and time.time() - t0 < timeout:
            pool.run()
            time.sleep(0.001)
            if log is not None and time.time() - last_progress >= 3.0:
                last_progress = time.time()
                log(f"sync: no complete reference packet after {time.time() - t0:.1f} s (REFTX running? all sensors up?)")
    finally:
        pool.emit_calibration_csi = was_emitting
        pool.set_rf_switch(was_rf_switch)
        pool.remove_csi_callback(callback_handle)
    if not first:
        return None
    if log is not None:
        log(f"sync: reference packet acquired in {time.time() - t0:.2f} s")

    # Controller ctrl arrays are indexed by the sensors' SELF-IDENTIFIED
    # antenna id; the cluster grid is filled by the stream uid ids (the SPI
    # slot numbers). antenna_id_to_row_col composes both maps — verified
    # empirically: the esp_num_to_row_col variant permutes the anchors and
    # provably misaligns the grids (cross-correlation match collapses to
    # zero).
    ts = first[0]
    anchors = []
    for b, board in enumerate(boards):
        rev = board.revision
        anchors.append([int(round(float(ts[b][rev.antenna_id_to_row_col(antenna_id)]) * 1e9)) for antenna_id in range(8)])
    return anchors


def post_sync_anchors(pool, boards, timeout=15.0, log=None):
    """Acquire ONE common reference packet for all boards in the pool + post
    each board's per-antenna-ID anchors to its controller. All chunk grids then
    coincide across every sensor of every board (equal source_chunk_index =
    same instant). Returns (anchors list[n_boards][8], sync_seq list)."""
    anchors = acquire_reference_anchors(pool, boards, timeout=timeout, log=log)
    if anchors is None:
        raise RuntimeError("no calibration cluster complete across all boards within timeout " "(reference TX running? all sensors in WiFi mode? shared reference network?)")
    seqs = []
    for board, board_anchors in zip(boards, anchors):
        body = board.iq.set_sync_anchor(board_anchors)
        seqs.append(json.loads(body).get("sync_seq"))
        if log is not None:
            log(f"sync: anchors posted to {board.host} (sync_seq {seqs[-1]})")
    return anchors, seqs


def post_sync_anchor(pool, board, timeout=15.0, log=None):
    """Acquire a reference packet + post its per-antenna-ID anchors to one board.
    Returns (anchors, sync_seq)."""
    anchors, seqs = post_sync_anchors(pool, [board], timeout=timeout, log=log)
    return anchors[0], seqs[0]
