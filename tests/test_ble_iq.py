"""Timing, IF and reference calibration against independent signal models."""
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from espargos.ble_processing import Waveform, align_waveforms
from espargos.ble_iq import BLEPacketIQ, BLEChannelResponse, align_packet_iq, channel_response
from espargos.ble_calibration import fit_reference_response
from espargos.sensor_calibration import SensorCalibration, ClockReferenceScope
from espargos.ble_beamforming import BLEBeamformer, BLEBeamformingConfig
from espargos.array_processing import steering_vectors_2d


class TimingTests(unittest.TestCase):
    def test_fractional_capture_origins_and_if_sign(self):
        rng = np.random.default_rng(1)
        f = np.linspace(-.11,.11,71)
        coefficients = rng.normal(size=len(f)) + 1j*rng.normal(size=len(f))
        def signal(t):
            return np.exp(2j*np.pi*t[:,None]*f) @ coefficients
        n = np.arange(1400)
        for if_hz in (-2e6, 0, 2e6):
            reference = Waveform(signal(n),100,1200,4,if_hz)
            for delay in (-20.8,-2.2,-.49,.17,3.7,18.9):
                with self.subTest(if_hz=if_hz,delay=delay):
                    other = Waveform(signal(n-delay)*np.exp(1j*(.7-2*np.pi*if_hz/4e6*delay)),
                                     100+int(delay),1200+int(delay),4,if_hz)
                    fit = align_waveforms(reference,other)
                    self.assertIsNotNone(fit)
                    self.assertAlmostEqual(fit.delay,delay,delta=.001)
                    self.assertAlmostEqual(fit.phase,.7,delta=.002)

    def test_fixed_receiver_delay_cancels_with_reference_under_new_triggers(self):
        rng = np.random.default_rng(56)
        rate, carrier = 4e6, 2e6
        f = np.linspace(-.1,.1,63)
        coefficients = rng.normal(size=len(f))+1j*rng.normal(size=len(f))
        receiver_delay = np.array([0,.13,-.28,.41,-.17,.23,.09,-.33])
        hardware_phase = rng.uniform(-np.pi,np.pi,8)
        ref_delay = np.linspace(0,1.7e-9,8)
        center = 2402e6
        n = np.arange(1400)
        def measure(origins, incident_phase):
            waves = []
            for a in range(8):
                t = n+origins[a]-receiver_delay[a]
                z = (np.exp(2j*np.pi*t[:,None]*f)@coefficients)*np.exp(1j*(2*np.pi*carrier/rate*origins[a]-hardware_phase[a]-incident_phase[a]))
                waves.append(Waveform(z,100-int(origins[a]),1200-int(origins[a]),4,carrier))
            phases = np.array([0]+[-align_waveforms(waves[0],w).phase for w in waves[1:]])
            return phases.reshape(1,2,4)
        ref = measure(rng.uniform(-15,15,8),-2*np.pi*center*ref_delay)
        model = SensorCalibration(sensor_shape=(1,2,4), timing_offsets=np.zeros((1,2,4)), phase_offsets=ref,
            phase_reference_frequency=center, clock_scope=ClockReferenceScope.PER_BOARD, reference_path_delays=ref_delay.reshape(1,2,4))
        expected = np.linspace(-.8,.9,8).reshape(1,2,4)
        for _ in range(5):
            measured = measure(rng.uniform(-15,15,8),expected.ravel())
            corrected = np.exp(1j*measured)*model.phase_time_correction(np.array([center]))[...,0]
            corrected /= corrected.flat[0]
            np.testing.assert_allclose(corrected,np.exp(1j*(expected-expected.flat[0])),atol=.003)

    def test_native_iq_filter_decimation_and_fractional_capture_alignment(self):
        rng = np.random.default_rng(18)
        frequencies = np.linspace(-.045,.045,53)
        coefficients = rng.normal(size=53)+1j*rng.normal(size=53)
        phases = np.arange(8)*.23
        origins = np.array([0,23.27,-13.51,9.83,-40.18,12.31,-3.7,31.14])
        for rate in (4_000_000, 8_000_000, 16_000_000):
            for duration_us in (300, 400):
                with self.subTest(rate=rate, duration_us=duration_us):
                    packets = []
                    for origin, phase in zip(origins, phases):
                        # Express time on the original 8 MHz grid so every rate
                        # samples the same continuous waveform and trigger offset.
                        t = np.arange(int(duration_us*rate/1e6))*8e6/rate + origin
                        signal = np.exp(2j*np.pi*t[:,None]*frequencies)@coefficients
                        samples = signal*np.exp(1j*(2*np.pi*.25*t-phase))*(t < duration_us*8-300)
                        packets.append(SimpleNamespace(samples=samples,sample_rate=rate,if_hz=2_000_000,
                            crc_valid=True,qualified=True,rx_gain=0,phy=0,channel=37,prefix=bytes([0,37])))
                    cluster = SimpleNamespace(board_index=0,shape=(1,2,4),
                        get_packet=lambda b,a: packets[a], get_sensor_position=lambda b,a: (b,a//4,a%4))
                    iq = align_packet_iq(cluster)
                    self.assertTrue(iq.valid.all())
                    self.assertGreater(iq.samples.shape[-1],800)
                    vector = iq.phase().vector.ravel()
                    np.testing.assert_allclose(vector,np.exp(1j*phases),atol=.004)
                    np.testing.assert_allclose(iq.delays.ravel(),-origins/2,atol=.002)


def iq_samples(samples, rate=4e6):
    shape = samples.shape[:-1]
    return BLEPacketIQ(samples,rate,2402e6,2e6,np.zeros(shape),np.ones(shape),0,(0,0,0))


class FullIQTests(unittest.TestCase):
    def test_all_samples_and_packet_gauge_contribute_to_covariance(self):
        rng=np.random.default_rng(34)
        x=rng.normal(size=(1,2,4,1000))+1j*rng.normal(size=(1,2,4,1000))
        x[0,0,0,:25]=np.nan
        iq=iq_samples(x)
        covariance,counts=iq.covariance()
        expected=np.vdot(x[0,1,1,25:],x[0,0,0,25:])/975
        self.assertAlmostEqual(covariance[0,5],expected)
        self.assertEqual(counts[0,5],975)
        rotated=replace(iq,samples=x*np.exp(1.73j))
        np.testing.assert_allclose(rotated.covariance()[0],covariance,atol=1e-12)
        changed=x.copy()
        changed[...,:200]*=4
        self.assertGreater(np.linalg.norm(replace(iq,samples=changed).covariance()[0]-covariance),1)

    def test_bartlett_averages_selected_history(self):
        grid=steering_vectors_2d(2,4,32,24,frequency_hz=2402e6,pixel_centers=True)
        config=BLEBeamformingConfig(history_size=2,history_age=10)
        beamformer=BLEBeamformer(clock=lambda: 1.)
        vectors=[grid[:,:,8,10],grid[:,:,23,14]]
        expected=[]
        for v in vectors:
            result=beamformer.process('a',v,np.full((2,4),2),1,grid,2402e6,config)
            expected.append(abs(np.einsum('rcae,rc->ae',grid.conj(),v)/8)**2)
        np.testing.assert_allclose(result.spectrum,np.mean(expected,axis=0),atol=1e-12)

    def test_bartlett_uses_supplied_full_waveform_covariance(self):
        grid=steering_vectors_2d(2,4,32,24,frequency_hz=2402e6,pixel_centers=True)
        field=grid[:,:,23,14].ravel()
        covariance=np.outer(field,field.conj())+.1*np.eye(8)
        result=BLEBeamformer(clock=lambda:1).process('a',np.ones((2,4)),np.full((2,4),2),
            1,grid,2402e6,BLEBeamformingConfig(),covariance=covariance,sample_counts=np.full((8,8),700))
        self.assertEqual(np.unravel_index(result.spectrum.argmax(),result.spectrum.shape),(23,14))

    def test_channel_amplitude_phase_delay_and_frequency_convention(self):
        # A received packet is its own pilot. Independent phase between packets
        # cancels in the cross spectrum, without knowing any transmitted bits.
        rng=np.random.default_rng(7)
        size=8192
        rate=4e6
        frequencies=np.fft.fftfreq(size,1/rate)
        spectrum=rng.normal(size=size)+1j*rng.normal(size=size)
        spectrum[abs(frequencies)>.45e6]=0
        response=2*np.exp(1j*(.4+2*np.pi*frequencies*80e-9))
        reference=np.fft.ifft(spectrum)
        other=np.fft.ifft(spectrum*response)
        x=np.stack([reference,other]).reshape(1,1,2,-1)
        result=channel_response(iq_samples(x),nfft=512)
        valid=(abs(result.offsets_hz)<.35e6)&(result.coherence[0,0,1]>.98)
        h=result.response[0,0,1,valid]
        expected=2*np.exp(1j*(.4-2*np.pi*result.offsets_hz[valid]*80e-9))
        np.testing.assert_allclose(h,expected,atol=.015)
        self.assertEqual(result.sample_counts[0,0,1],size)
        np.testing.assert_allclose(result.group_delay()[0,0,1,valid],80e-9,atol=6e-9)
        model = SensorCalibration(sensor_shape=(1,1,2), timing_offsets=np.array([[[0,80e-9]]]),
            phase_offsets=np.array([[[0,.4]]]),phase_reference_frequency=2402e6,
            clock_scope=ClockReferenceScope.PER_BOARD)
        corrected = channel_response(iq_samples(x).corrected(model), nfft=512)
        np.testing.assert_allclose(corrected.response[0,0,1,valid],2,atol=.015)
        gauged=channel_response(iq_samples(x*np.exp(2.3j)),nfft=512)
        np.testing.assert_allclose(gauged.response,result.response,atol=1e-12)

    def test_reference_phase_slope_and_ref_network_removed(self):
        shape=(1,2,4)
        offsets=np.linspace(-.5e6,.5e6,33)
        center=2402e6
        phases=np.arange(8).reshape(shape)*.23
        delays=np.arange(8).reshape(shape)*7e-9
        reference_paths=np.arange(8).reshape(shape)*.3e-9
        h=np.exp(1j*(phases[...,None]-2*np.pi*offsets*delays[...,None]-2*np.pi*(center+offsets)*reference_paths[...,None]))
        responses=[BLEChannelResponse(offsets,h*np.exp(1j*g),np.ones(h.shape),np.full(shape,1000)) for g in (.7,-.3,1.9)]
        fitted_delay,fitted_phase=fit_reference_response(responses,0)
        model=SensorCalibration(sensor_shape=shape,timing_offsets=fitted_delay,phase_offsets=fitted_phase,
            phase_reference_frequency=center,clock_scope=ClockReferenceScope.PER_BOARD,reference_path_delays=reference_paths)
        received=np.exp(1j*(phases[...,None]-2*np.pi*offsets*delays[...,None]))
        np.testing.assert_allclose(received*model.phase_time_correction(center+offsets),1,atol=1e-6)


if __name__=='__main__':
    unittest.main()
