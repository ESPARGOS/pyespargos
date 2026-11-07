"""LE Coded FEC regression against Bluetooth Core Vol 6 Part C section 2.2.

https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core-54/out/en/low-energy-controller/sample-data.html
"""
import numpy as np
from espargos.ble_decoder import _viterbi


def test_sig_access_address_and_termination():
    # The SIG's encoder output for AA 0x8e89bed6, followed by CI and TERM1.
    # Kept grouped as byte-pair output so the transmission order is explicit.
    aa = '00110101 11010010 01111010 01011011 10010000 10111111 10001010 10001111'
    for coding, suffix in ((2, '0101001100'), (8, '1011110000')):
        encoder_bits = np.array([int(c) for c in aa.replace(' ', '') + suffix])
        soft = encoder_bits * 2. - 1
        for error in (None, 5, 22):
            damaged = soft.copy()
            if error is not None:
                damaged[error] *= -1
            decoded = _viterbi(damaged, 2, 37)
            assert int.from_bytes(np.packbits(decoded[:32], bitorder='little').tobytes(), 'little') == 0x8e89bed6
            assert list(decoded[32:]) == [int(coding == 2), 0, 0, 0, 0]


def test_coded_frame_must_fit_capture():
    from ble_waveforms import coded_waveform
    from espargos.ble_decoder import decode
    z, _ = coded_waveform(coding=8, length=80)
    raw = ((z.real.astype(np.int32)&1023)|((z.imag.astype(np.int32)&1023)<<10)).astype(np.uint32)
    assert len(raw) > 16384
    assert decode(raw[:16384], 8, 37, 0) is None
