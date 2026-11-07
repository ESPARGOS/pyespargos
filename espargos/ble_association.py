"""Conservative event association for repeated BLE PDUs.

The PDU prefix is not unique. Timestamp windows, board-local clock/configuration
context and per-sensor occupancy all participate in choosing an event. Multiple
candidates are rejected rather than selecting the closest timestamp.
"""


def same_context(cluster, board_index, packet):
    anchor = cluster.packet
    return cluster.board_index == board_index and (anchor.clock_epoch, anchor.generation, anchor.settings) == (
        packet.clock_epoch,
        packet.generation,
        packet.settings,
    )


def nearby(cluster, packet, window_us):
    times = cluster.time_us[cluster.completion]
    return bool(len(times) and all(abs(t - packet.time_us) <= window_us for t in times))


def packet_candidates(clusters, board_index, message):
    p = message.payload
    return [
        c
        for c in clusters
        if same_context(c, board_index, p)
        and c.packet.identity == p.identity
        and nearby(c, p, 2000)
        and c.get_packet(board_index, message.antenna_id) is None
    ]


