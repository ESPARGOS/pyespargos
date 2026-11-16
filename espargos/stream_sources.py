"""Host-side encoders for low-bandwidth IQ transmission."""

import numpy as np

# ITU-R M.2034 (2013), Annex 2, ASCII Varicode code points:
# https://www.itu.int/rec/R-REC-M.2034/en
_VARICODE = (
    '1010101011 1011011011 1011101101 1101110111 1011101011 1101011111 1011101111 1011111101 '
    '1011111111 11101111 11101 1101101111 1011011101 11111 1101110101 1110101011 '
    '1011110111 1011110101 1110101101 1110101111 1101011011 1101101011 1101101101 1101010111 '
    '1101111011 1101111101 1110110111 1101010101 1101011101 1110111011 1011111011 1101111111 '
    '1 111111111 101011111 111110101 111011011 1011010101 1010111011 101111111 '
    '11111011 11110111 101101111 111011111 1110101 110101 1010111 110101111 '
    '10110111 10111101 11101101 11111111 101110111 101011011 101101011 110101101 '
    '110101011 110110111 11110101 110111101 111101101 1010101 111010111 1010101111 '
    '1010111101 1111101 11101011 10101101 10110101 1110111 11011011 11111101 '
    '101010101 1111111 111111101 101111101 11010111 10111011 11011101 10101011 '
    '11010101 111011101 10101111 1101111 1101101 101010111 110110101 101011101 '
    '101110101 101111011 1010101101 111110111 111101111 111111011 1010111111 101101101 '
    '1011011111 1011 1011111 101111 101101 11 111101 1011011 '
    '101011 1101 111101011 10111111 11011 111011 1111 111 '
    '111111 110111111 10101 10111 101 110111 1111011 1101011 '
    '11011111 1011101 111010101 1010110111 110111011 1010110101 1011010111 1110110101 '
).split()


def psk31_bits(text, *, preamble=32, postamble=32):
    """Encode ASCII text as BPSK31 bits for ``IQStream.write``.

    Zero bits reverse phase. The default idle preamble permits acquisition;
    the steady-carrier postamble lets decoders squelch. For successive pieces
    of a conversation, use a preamble only on the first piece and a postamble
    only on the last; character separators are included in every piece.
    """
    if not isinstance(preamble, int) or not isinstance(postamble, int) or min(preamble, postamble) < 0:
        raise ValueError("preamble and postamble must be nonnegative bit counts")
    data = text.encode("ascii") if isinstance(text, str) else bytes(text)
    if any(value > 127 for value in data):
        raise ValueError("BPSK31 text encoder supports ASCII")
    symbols = "0" * preamble + "".join(_VARICODE[value] + "00" for value in data) + "1" * postamble
    return bytes(int(bit) for bit in symbols)


class SSBEncoder:
    """Stateful audio-to-IQ conversion for the raw streaming mode.

    Feed normalized, zero-mean real audio at the stream's 8/16 kSa/s rate.
    A 257-tap windowed Hilbert filter suppresses the other sideband without
    discontinuities between calls. The useful voice band starts around
    300 Hz; allow margin below Nyquist. Group delay is 128 audio samples.
    The default gain reserves headroom for the quadrature filter's overshoot.
    """

    def __init__(self, *, sideband="upper", gain=.25):
        if sideband not in ("upper", "lower"):
            raise ValueError("sideband must be upper or lower")
        if not np.isfinite(gain) or not 0 < gain <= 1:
            raise ValueError("gain must be finite and in (0, 1]")
        n = np.arange(-128, 129)
        h = np.zeros(257)
        odd = n % 2 != 0
        h[odd] = 2 / (np.pi * n[odd])
        self._filter = h * np.blackman(257)
        self._history = np.zeros(256)
        self._sign = 1 if sideband == "upper" else -1
        self.gain = gain

    def encode(self, audio):
        """Return one complex IQ sample per audio sample, retaining FIR history."""
        a = np.asarray(audio)
        if a.ndim != 1 or np.iscomplexobj(a) or not np.all(np.isfinite(a)) or np.any(abs(a) > 1):
            raise ValueError("audio must be a finite real vector within [-1, 1]")
        if not len(a):
            return np.empty(0, complex)
        joined = np.concatenate((self._history, a))
        q = np.convolve(joined, self._filter, mode="valid")
        iq = self.gain * (joined[128:128+len(a)] + 1j*self._sign*q)
        if np.any(abs(iq.imag) > 1):
            raise ValueError("quadrature audio exceeds IQ8 range; reduce encoder gain")
        self._history = joined[-256:].copy()
        return iq

    def flush(self):
        """Return the filter tail; feed it before finishing the IQ stream."""
        return self.encode(np.zeros(256))
