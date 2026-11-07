"""Exercise Python host decoding with independent GFSK fixtures."""
from pathlib import Path
import struct
import sys
import numpy as np

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from ble_waveforms import interop_cases
from espargos.ble_packet import BLEPacket
from espargos import BLECluster, SensorMessage, BoardRevisionDensiflorus

for case in interop_cases():
    raw, phy, divider = case['raw'], case['phy'], case['divider']
    raw = np.pad(raw, (0, -len(raw) % 8))
    h = [0x35514c42, 1, 0, 0, 0, 0, 100000, 0, 1, 2,
         37 | 2 << 8 | 40 << 16, 3, 15, divider, len(raw), len(raw), 1, 0]
    data = struct.pack('<18I', *h) + struct.pack('<Ii3I4B32s',
        32000000 // divider, 2000000, 0, 0, 16, phy, 0, 2, 0, b'') + raw.tobytes()
    packet = BLEPacket(data)
    assert packet.qualified == case['valid'], case
    assert packet.host_decoded == case['valid']
    assert packet.raw_capture
    assert bytes(packet) == data
    if case['valid']:
        assert packet.coding == case.get('coding', 0)
        assert packet.capture_phy == phy
        assert packet.phy == (2 if packet.coding else phy)
        assert packet.access_address == case['address'] and packet.prefix == case['pdu'][:32]
        assert packet.crc_valid == (case['address'] == 0x8e89bed6)
        cluster = BLECluster('test', 0, packet, [BoardRevisionDensiflorus()])
        for antenna in range(8):
            cluster.add_message(0, SensorMessage(antenna << 29 | 1, antenna, BLEPacket(data)))
        assert cluster.is_complete
        if packet.coding:
            from espargos.ble_calibration import BLECalibration
            calibration = BLECalibration(None, 0, packet.generation, packet.settings, phy,
                {a: (packet.clock_epoch, packet.receiver_epoch) for a in range(8)})
            assert calibration.matches(cluster)
print('Python host decoder: waveform fixtures, provenance and complete clusters passed')
