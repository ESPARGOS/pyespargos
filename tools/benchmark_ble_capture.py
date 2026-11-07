#!/usr/bin/env python3
"""Measure BLE captures, retaining raw events for offline analysis.

Runs against one array and restores its controller, sensor and BLE settings.
The transport is temporarily taken over. Raw captures may be large, so choose
an output location with sufficient space (for example ~/Downloads).
"""
import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
import pickle
import time

from espargos import Pool


def capture(host, seconds, rounds, output, routes):
    active = current = None
    stages = []
    with Pool(host) as pool:
        board = pool.boards[0]
        saved = (board.control.get_json('get_controller_mode'),
                 board.iq.get_config()['modes'], board.ble.get_config())
        receiver = pool.ble

        def discover(cluster):
            stage = active
            if stage is None or cluster.packet.generation != stage['generation'] or cluster.is_calibration:
                return
            stage['events'][id(cluster)] = [time.monotonic(), None, cluster]

        def complete(cluster):
            stage = current
            if stage is not None and id(cluster) in stage['events']:
                stage['events'][id(cluster)][1] = time.monotonic()

        def packet(message):
            stage = active
            if stage is not None and message.payload.generation == stage['generation']:
                stage['records'] += 1
                stage['record_bytes'] += len(bytes(message.payload))
                stage['crc_records'] += int(message.payload.crc_valid)

        receiver.add_callback(discover, lambda c: True)
        receiver.add_callback(complete)
        handle = board.ble.subscribe_packets(packet)
        try:
            board.control.post_json('set_controller_mode', {'mode': 'ble'})
            pool.configure(mode='ble')
            pool.start_processing()
            for route in routes:
                for repeat in range(rounds):
                    receiver.apply_config(dict(channel=37, phy='LE1M', gain_mode='auto',
                        rf_switch=route, decimation=4, capture_samples=3072))
                    time.sleep(1.5)
                    config = board.ble.get_config()
                    stage = dict(source=15, route=route, repeat=repeat,
                        generation=config['generation'], seconds=seconds, config=config,
                        events={}, records=0, record_bytes=0, crc_records=0)
                    active = current = stage
                    time.sleep(seconds)
                    active = None
                    if not pool._processing_thread.is_alive():
                        raise RuntimeError("Receiver worker stopped during the trial")
                    time.sleep(receiver.settle_timeout + .2)
                    events = []
                    current = None
                    for arrived, completed, cluster in stage['events'].values():
                        raw = [None if (p := cluster.get_packet(0, a)) is None else bytes(p) for a in range(8)]
                        events.append(dict(advertiser=cluster.packet.advertiser,
                            latency=None if completed is None else completed-arrived,
                            packets=raw))
                    snapshot = dict(stage, events=events)
                    stages.append(snapshot)
                    print(json.dumps(dict(source=15, route=route, repeat=repeat,
                        events=len(events), full=sum(all(p is not None for p in e['packets']) for e in events),
                        advertisers=dict(Counter(e['advertiser'] for e in events)))), flush=True)
                    temporary = output.with_suffix(output.suffix + '.tmp')
                    with gzip.open(temporary, 'wb', compresslevel=1) as file:
                        pickle.dump(dict(host=host, saved=saved, stages=stages), file)
                    temporary.replace(output)
        finally:
            active = None
            pool.stop_processing()
            board.ble.unsubscribe(handle)
            errors = []
            for restore in (
                lambda: board.ble.set_config({k: saved[2][k] for k in board.ble.CONFIG_KEYS}),
                lambda: board.iq.set_config({'modes': saved[1]}),
                lambda: board.control.post_json('set_controller_mode', saved[0]),
            ):
                try:
                    restore()
                except Exception as error:
                    errors.append(error)
            if errors:
                raise ExceptionGroup('Failed to restore array settings', errors)
    return stages


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('host')
    parser.add_argument('--seconds', type=float, default=12)
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--routes', type=int, nargs='+', choices=(2, 3, 4), default=[2])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.seconds <= 0 or args.rounds < 1:
        parser.error('seconds and rounds must be positive')
    capture(args.host, args.seconds, args.rounds, args.output, args.routes)
