#!/usr/bin/env python3
"""Analyze raw output from benchmark_ble_capture.py without using the array."""
import argparse
from collections import defaultdict
import gzip
import json
from pathlib import Path
import pickle

import numpy as np
from espargos import BLEPacket, BLECluster, BoardRevisionDensiflorus, SensorMessage
from espargos.ble_iq import align_packet_iq


def analyze(path):
    with gzip.open(path, 'rb') as file:
        data = pickle.load(file)
    stages = []
    for stage in data['stages']:
        advertisers = defaultdict(list)
        for event in stage['events']:
            for raw in event['packets']:
                if raw is not None and int.from_bytes(raw[48:52], 'little') != stage['source']:
                    raise ValueError('Effective source differs from the requested benchmark policy')
            packets = [BLEPacket(raw) if raw is not None else None for raw in event['packets']]
            if any(p is not None and p.sample_rate != 8_000_000 for p in packets):
                raise ValueError('Unexpected capture sample rate')
            first = next(p for p in packets if p is not None and p.crc_valid)
            cluster = BLECluster(0, 0, first, [BoardRevisionDensiflorus()])
            for antenna, packet in enumerate(packets):
                if packet is None:
                    continue
                message = SensorMessage(antenna << 29 | 1, antenna, packet)
                if packet.crc_valid:
                    cluster.add_message(0, message)
                else:
                    cluster._add_recovered_message(0, message)
            iq = align_packet_iq(cluster)
            phase = iq.phase()
            count = sum(p is not None for p in packets)
            crc_count = sum(p is not None and p.crc_valid for p in packets)
            rssis = [p.rssi for p in packets if p is not None and p.crc_valid]
            advertisers[event['advertiser']].append(dict(count=count, crc_count=crc_count,
                usable=int(phase.valid.sum()), rssi=float(np.median(rssis)),
                samples_per_sensor=float(np.isfinite(iq.samples).sum()/8),
                latency=event['latency'], vector=phase.vector.ravel(),
                routes=cluster.rf_switch_state.ravel()))
        summary = {key: stage[key] for key in ('source', 'route', 'repeat', 'seconds', 'records', 'record_bytes', 'crc_records')}
        if 'boot_enabled' in stage:
            summary['boot_enabled'] = stage['boot_enabled']
        summary['advertisers'] = {}
        for advertiser, events in advertisers.items():
            full = sum(e['count'] == 8 for e in events)
            usable = sum(e['usable'] == 8 for e in events)
            latency = [e['latency'] for e in events if e['latency'] is not None]
            # Compare phase stability within a trial and a fixed pair of feeds.
            # Never average Random-feed phases directly across different feeds.
            pairs = defaultdict(list)
            for event in events:
                vector = event['vector']
                for antenna in range(1, 8):
                    if np.isfinite(vector[0]) and np.isfinite(vector[antenna]):
                        key = (antenna, int(event['routes'][0]), int(event['routes'][antenna]))
                        pairs[key].append(vector[antenna]*vector[0].conjugate())
            deviations = []
            for values in pairs.values():
                if len(values) >= 5:
                    values = np.asarray(values)
                    resultant = abs(np.mean(values / np.maximum(abs(values), 1e-30)))
                    deviations.append(float(np.sqrt(-2*np.log(np.clip(resultant, 1e-30, 1)))))
            summary['advertisers'][advertiser or 'unidentified'] = dict(
                events=len(events), full=full, usable_full=usable,
                crc_full=sum(e['crc_count'] == 8 for e in events),
                peer_added_full=sum(e['usable'] == 8 and e['crc_count'] < 8 for e in events),
                usable_six=sum(e['usable'] >= 6 for e in events),
                median_rssi_dbm=float(np.median([e['rssi'] for e in events])),
                median_sensors=float(np.median([e['count'] for e in events])),
                median_usable_samples_per_sensor=float(np.median([e['samples_per_sensor'] for e in events if e['usable']==8])) if usable else None,
                latency_p50_ms=float(np.median(latency)*1000) if latency else None,
                latency_p95_ms=float(np.percentile(latency, 95)*1000) if latency else None,
                phase_pair_std_median_rad=float(np.median(deviations)) if deviations else None,
                phase_pair_std_groups=len(deviations))
        stages.append(summary)
        print('analyzed', stage['route'], stage['repeat'], stage['source'], len(stage['events']), flush=True)
    return dict(host=data['host'], rate_hz=8_000_000, capture_samples=3072,
                gain_mode='auto', channel=37, phy='LE1M', stages=stages)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(analyze(args.capture), indent=2)+'\n')
