"""Aligned packet IQ and relative channel response across the BLE bandwidth.

BLE carries no OFDM subcarriers. Frequency bins here estimate the channel ratio
against a received copy of the same packet. They are not decoded WiFi-like CSI.
"""
from dataclasses import dataclass
import numpy as np
from .ble_processing import waveform, align_waveforms, _interpolate
from .ble_packet import channel_frequency
from . import gain_phase_calibration


@dataclass(frozen=True)
class BLEPacketIQ:
    """Full usable packet samples in (board, row, column, sample) order.

    Missing/nonoverlapping samples are NaN. Capture origins are aligned before
    correcting their IF phase, then conjugated to the common receive convention.
    Absolute phase between packets is intentionally unspecified.
    """
    samples: np.ndarray
    sample_rate: float
    frequency: float
    if_hz: float
    delays: np.ndarray
    coherence: np.ndarray
    board_index: int
    reference_position: tuple
    bandwidth: float = 1e6

    @property
    def valid(self):
        return np.any(np.isfinite(self.samples), axis=-1)

    def corrected(self, model):
        """Apply the shared receiver phase/delay and PCB reference-path model.

        The native taps were conjugated, so an FFT bin at f corresponds to RF
        frequency center-f. Zero padding avoids wrapping delayed edge samples.
        """
        from dataclasses import replace
        samples = np.full_like(self.samples, np.nan+1j*np.nan)
        for position in np.ndindex(self.samples.shape[:-1]):
            indices = np.flatnonzero(np.isfinite(self.samples[position]))
            if not len(indices):
                continue
            start, end = indices[0], indices[-1]+1
            nfft = 1 << int(end-start+32).bit_length()
            frequencies = self.frequency - np.fft.fftfreq(nfft, 1/self.sample_rate)
            correction = model.phase_time_correction(frequencies)[position]
            padded = np.pad(self.samples[position][start:end], (16, nfft-(end-start)-16))
            values = np.fft.ifft(np.fft.fft(padded) * correction)[16:16+end-start]
            samples[position][start:end] = values
        return replace(self, samples=samples)

    def phase(self):
        """Full-packet relative field summary, sharing the IQ alignment."""
        from .ble_processing import BLEPhase
        reference = self.samples[self.reference_position]
        overlap = np.isfinite(self.samples) & np.isfinite(reference)
        products = np.sum(np.where(overlap, self.samples * reference.conj(), 0), axis=-1)
        valid = self.valid & (abs(products) > 0)
        vector = np.divide(products, abs(products), out=np.full(products.shape, np.nan+1j*np.nan), where=valid)
        if np.any(valid):
            vector *= np.conj(vector[valid][0])
        power = np.sum(np.where(np.isfinite(self.samples), abs(self.samples)**2, 0), axis=-1)
        count = np.sum(np.isfinite(self.samples), axis=-1)
        amplitude = np.sqrt(np.divide(power, count, out=np.full(count.shape, np.nan), where=count > 0))
        return BLEPhase(vector, self.coherence, valid, amplitude)

    def covariance(self):
        """Spatial second moments and sample counts, retaining every usable pair."""
        samples = self.samples[self.board_index].reshape(np.prod(self.samples.shape[1:-1]), self.samples.shape[-1])
        valid = np.isfinite(samples)
        x = np.where(valid, samples, 0)
        counts = valid.astype(np.int64) @ valid.astype(np.int64).T
        total = x @ x.conj().T
        result = np.divide(total, counts, out=np.zeros(counts.shape, complex), where=counts > 0)
        return result, counts


@dataclass(frozen=True)
class BLEChannelResponse:
    offsets_hz: np.ndarray
    response: np.ndarray
    coherence: np.ndarray
    sample_counts: np.ndarray

    def group_delay(self, *, minimum_coherence=.8, window=5):
        """Relative group delay in seconds from a local phase-slope fit.

        Unwrap only contiguous supported bins. Missing/low-coherence bins stay
        NaN rather than joining unrelated phase branches across spectral nulls.
        The received reference defines zero delay, not absolute time of flight.
        """
        if window < 3 or window % 2 != 1:
            raise ValueError("Group-delay window must be odd and at least three")
        result = np.full(self.response.shape, np.nan)
        for position in np.ndindex(self.response.shape[:-1]):
            h, quality = self.response[position], self.coherence[position]
            valid = np.isfinite(h) & (abs(h) > 0) & (quality >= minimum_coherence)
            indices = np.flatnonzero(valid)
            for run in np.split(indices, np.flatnonzero(np.diff(indices) != 1)+1):
                if len(run) < 3:
                    continue
                phase = np.unwrap(np.angle(h[run]))
                for j, index in enumerate(run):
                    section = slice(max(0,j-window//2),min(len(run),j+window//2+1))
                    bins = run[section]
                    if len(bins) < 3:
                        continue
                    f, w = self.offsets_hz[bins], quality[bins]
                    f = f - np.average(f,weights=w)
                    result[position][index] = -np.sum(w*f*phase[section]) / (2*np.pi*np.sum(w*f*f))
        return result


def align_packet_iq(cluster):
    """Align each capture once and retain its full valid packet interval."""
    b = cluster.board_index
    anchors = [a for a in range(8) if (p := cluster.get_packet(b, a)) is not None and p.qualified and p.rx_gain is not None]
    if not anchors:
        packet = cluster.packet
        return BLEPacketIQ(np.full((*cluster.shape,0), np.nan+1j*np.nan, np.complex64),
                           packet.sample_rate, channel_frequency(packet.channel), packet.if_hz,
                           np.full(cluster.shape,np.nan), np.full(cluster.shape,np.nan),
                           b, (b,0,0), 2e6 if packet.phy == 1 else 1e6)
    anchor = min(anchors)
    packet = cluster.get_packet(b, anchor)
    if packet.phy not in (0, 1, 2):
        raise ValueError('BLE packet IQ processing supports LE1M, LE2M and LE Coded')
    length = packet.prefix[1]
    waves = {a: waveform(p, packet.phy, length, coding=getattr(packet, "coding", 0)) for a in range(8)
             if (p := cluster.get_packet(b, a)) is not None and p.rx_gain is not None}
    reference = waves[anchor]
    if reference.end <= reference.begin:
        return BLEPacketIQ(np.full((*cluster.shape, 0), np.nan+1j*np.nan, np.complex64),
                           reference.sps*1e6, channel_frequency(packet.channel), reference.if_hz,
                           np.full(cluster.shape, np.nan), np.full(cluster.shape, np.nan),
                           b, cluster.get_sensor_position(b, anchor), 2e6 if packet.phy == 1 else 1e6)
    delays = np.full(cluster.shape, np.nan)
    coherence = np.full(cluster.shape, np.nan)
    fits = {}
    for a, wave in waves.items():
        p = cluster.get_packet(b, a)
        fit = align_waveforms(reference, wave, recovery=not p.crc_valid) if a != anchor else None
        if a != anchor and fit is None:
            continue
        delay, quality = (0., 1.) if a == anchor else (fit.delay, fit.coherence)
        position = cluster.get_sensor_position(b, a)
        delays[position], coherence[position] = delay, quality
        fits[a] = delay
    # Union of usable intervals. Pairwise covariance uses each pair's actual
    # overlap, so a late trigger on one sensor does not trim every other sensor.
    start = min(int(np.ceil(max(waves[a].begin, 8)-d)) for a, d in fits.items())
    end = max(int(np.floor(min(waves[a].end, len(waves[a].samples)-9)-d)) for a, d in fits.items())
    grid = np.arange(start, end)
    samples = np.full((*cluster.shape, len(grid)), np.nan+1j*np.nan, np.complex64)
    rate = reference.sps * 1e6
    for a, delay in fits.items():
        wave = waves[a]
        p = cluster.get_packet(b, a)
        t = grid + delay
        mask = (t >= max(wave.begin, 8)) & (t < min(wave.end, len(wave.samples)-9))
        if not mask.any():
            continue
        aligned = _interpolate(wave.samples, t[mask])
        aligned *= np.exp(2j*np.pi*wave.if_hz/rate*delay)
        aligned = gain_phase_calibration.apply(aligned.conj(), p.rx_gain) * 10**(-p.rx_gain/20)
        samples[cluster.get_sensor_position(b, a)][mask] = aligned
    return BLEPacketIQ(samples, rate, channel_frequency(packet.channel), reference.if_hz,
                       delays, coherence, b, cluster.get_sensor_position(b, anchor), 2e6 if packet.phy == 1 else 1e6)


def channel_response(iq, nfft=128, *, correct_timing=True):
    """Welch cross-spectral channel ratio over the occupied BLE band.

    Every usable sample belongs to a segment, including the final short segment.
    Empty spectrum bins stay NaN. Turning timing correction off restores capture
    delay slopes and the associated IF phase for diagnosis, not beamforming.
    """
    if type(nfft) is not int or nfft < 32 or nfft & (nfft-1):
        raise ValueError('nfft must be a power of two of at least 32')
    x = iq.samples
    reference = x[iq.reference_position]
    shape = x.shape[:-1]
    cross = np.zeros((*shape, nfft), complex)
    auto = np.zeros((*shape, nfft))
    refpower = np.zeros((*shape, nfft))
    counts = np.zeros(shape, int)
    for position in np.ndindex(shape):
        valid = np.isfinite(x[position]) & np.isfinite(reference)
        indices = np.flatnonzero(valid)
        if len(indices) < 32:
            continue
        # Both valid intervals are contiguous by construction.
        for begin in range(indices[0], indices[-1]+1, nfft):
            end = min(begin+nfft, indices[-1]+1)
            if end-begin < 2:
                continue
            window = np.hamming(end-begin)
            a = np.fft.fft(x[position][begin:end]*window, nfft)
            r = np.fft.fft(reference[begin:end]*window, nfft)
            cross[position] += a*r.conj()
            auto[position] += abs(a)**2
            refpower[position] += abs(r)**2
            counts[position] += end-begin
    offsets = -np.fft.fftshift(np.fft.fftfreq(nfft, 1/iq.sample_rate))[::-1]
    keep = abs(offsets) <= iq.bandwidth / 2
    cross, auto, refpower = (np.fft.fftshift(a, axes=-1)[..., ::-1][..., keep] for a in (cross, auto, refpower))
    supported = refpower > np.max(refpower, axis=-1, keepdims=True)*.01
    response = np.divide(cross, refpower, out=np.full(cross.shape, np.nan+1j*np.nan), where=supported & (refpower>0))
    quality = np.divide(abs(cross)**2, auto*refpower, out=np.zeros(auto.shape), where=auto*refpower>0)
    if not correct_timing:
        response *= np.exp(2j*np.pi*(iq.if_hz+offsets[keep])*iq.delays[..., None]/iq.sample_rate)
    return BLEChannelResponse(offsets[keep], response, np.clip(quality,0,1), counts)
