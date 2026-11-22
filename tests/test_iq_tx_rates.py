"""Physical frequency, delay and calibration units at different replay rates."""
from dataclasses import replace
import numpy as np
import pytest

from espargos.calibration import Antenna, AntennaCorrection, Calibration, CalibrationGroup, CalibrationState
from espargos.waveforms import CW, LFM
from espargos.steering import Beamspace, Weights
from espargos._iq_calibration.pilots import pilots
from espargos._iq_calibration.monitor_fit import estimate
from test_tx_signals import transmitter


@pytest.mark.parametrize('rate', [80_000_000, 40_000_000, 32_000_000, 16_000_000])
def test_waveform_frequency_and_time_units(rate):
    z = CW(1.25e6).synthesize(1024, sample_rate_hz=rate)
    assert np.argmax(abs(np.fft.fft(z))) == round(1.25e6 * 1024 / rate)
    chirp = LFM(-5e6, 5e6, duration_s=1e-6, delay_s=.5e-6)
    recipe = chirp.recipe(1024, sample_rate_hz=rate)
    assert recipe['chirp_samples'] == rate // 1_000_000
    assert recipe['offset_samples'] == rate // 2_000_000
    with pytest.raises(ValueError):
        CW(rate / 2 + 1).synthesize(sample_rate_hz=rate)
    phase = Beamspace(30).coefficients(np.array([.06, 0]), 2.437e9, CW(0))
    assert abs(phase - np.exp(-2j*np.pi*2.437e9*.03/299792458)) < 1e-12


def test_beam_compilation_uses_board_rate():
    tx = transmitter()
    tx.states[0]['sample_rate_hz'] = 40_000_000
    a = Antenna('board0', 2)
    tx.signals.add(waveform=LFM(-5e6, 5e6, duration_s=1e-6), antennas=[a], steering=Weights({a:1}))
    plans = tx._apply_signals.call_args.args[0]
    recipe = plans[0][0][1][0][1]
    assert recipe['waveform']['chirp_samples'] == 40
    assert set(recipe) == {'waveform', 'real', 'imag'}


@pytest.mark.parametrize('ratio', [1, 2, 2.5, 4, 5])
def test_oversampled_monitor_uses_main_band_without_aliasing(ratio):
    n = 256
    model = pilots(n)
    size = int(n * ratio)
    bins = np.arange(-n//2, n//2)
    spectrum = np.zeros(size, complex)
    for a, (_, reference, _) in enumerate(model):
        phase = a * .27 - 2*np.pi*bins*(31.7+a*.21)/n
        spectrum[bins % size] += .12 * reference[bins % n] * np.exp(1j*phase) * ratio
    # Deliberate replay images: naive decimation would fold these into pilots.
    if ratio > 1:
        spectrum[(bins + n) % size] += .15 * model[0][1][bins % n] * np.exp(1j*.7)
    samples = np.fft.ifft(spectrum)
    phase, delay = estimate(np.tile(samples, 3), model, ratio)
    np.testing.assert_allclose(phase, np.arange(8)*.27, atol=.02)
    np.testing.assert_allclose(delay, np.arange(8)*.21, atol=.01)


def test_half_rate_calibration_requires_measurement_after_restart():
    state = CalibrationState(Antenna('board',0), 'tx', 2.437e9, 40e6, 1, 1000, (('samples',1024),))
    cal = Calibration((CalibrationGroup((AntennaCorrection(state, .4, 0),)),))
    # Even a whole-period restart cannot resolve the half-rate start ambiguity.
    moved = replace(state, epoch=26600)
    assert not cal.retime([moved]).groups
    assert cal.retime([state]).correction(state).phase_rad == .4


def test_tx_state_rejects_a_rate_change_in_progress():
    sensor = dict(antid=0, applied_mode='iq_tx', enabled=True, origin_ns=1000,
                  frequency_hz=2.437e9, samples=1024, boot_id=1,
                  sample_rate_hz=40e6, applied_sample_rate_hz=80e6)
    with pytest.raises(ValueError, match='still changing'):
        CalibrationState.from_tx_status('board', sensor)
    sensor['applied_sample_rate_hz'] = 40e6
    assert CalibrationState.from_tx_status('board', sensor).sample_rate_hz == 40e6


def test_rate_only_configuration_has_no_empty_transmitter_list(monkeypatch):
    from types import SimpleNamespace
    from espargos import configuration
    writes = []
    monkeypatch.setattr(configuration, '_rpc', lambda *args: (writes.append(args), {'sample_rate_hz':80_000_000})[1])
    ctx = SimpleNamespace(board=SimpleNamespace(host='board'), antennas=list(range(8)),
                          before=['iq_tx']*8, modes=['iq_tx']*8, snapshot={})
    update = configuration._iq_tx(ctx, {'sample_rate_hz':40_000_000})
    update.before_modes()
    assert writes[-1][2] == {'sample_rate_hz':40_000_000, 'muted':True}
    assert len(update.invalidated) == 8
    same = configuration._iq_tx(ctx, {'sample_rate_hz':80_000_000})
    assert not same.invalidated
    ctx.antennas = [0,1]
    with pytest.raises(ValueError, match='every IQ TX'):
        configuration._iq_tx(ctx, {'sample_rate_hz':40_000_000})
    ctx.modes = ['iq_tx']*2 + ['iq_rx']*6
    assert len(configuration._iq_tx(ctx, {'sample_rate_hz':40_000_000}).invalidated) == 2
    with pytest.raises(ValueError, match='Unsupported'):
        configuration._iq_tx(ctx, {'sample_rate_hz':20_000_000})


@pytest.mark.parametrize('size', [100, 108, 112])
def test_streamed_tx_status_rate_and_backward_compatibility(size):
    import struct
    import zlib
    from types import SimpleNamespace
    from espargos.iq_tx_control import TXStatus

    class Board:
        def subscribe_sensor_messages(self, *args):
            return 1
        def unsubscribe_sensor_messages(self, subscription):
            pass

    board = Board()
    status = TXStatus([board])
    values = [0] * (size // 4)
    values[13] = 2
    if size >= 108:
        values[25:27] = [123, 1]
    if size == 112:
        values[27] = 40_000_000
    payload = struct.pack('<9I3f' + str(size//4-12) + 'I', *values)
    frame = bytes(4) + struct.pack('<BH', 6, size) + payload
    frame += struct.pack('<BHI', 255, 4, zlib.crc32(frame))
    status.receive_status(board, SimpleNamespace(payload=frame, antenna_id=0))
    parsed = status.status[board][0]
    assert parsed['sample_rate_hz'] == (40_000_000 if size == 112 else 80_000_000)
    assert parsed['origin_ns'] == (123 + (1 << 32) if size >= 108 else 0)
    status.close()


def test_half_rate_reciprocity_is_rejected_before_hardware_changes(monkeypatch):
    from types import SimpleNamespace
    from espargos._iq_calibration import reciprocity
    calls = []
    board = SimpleNamespace(iq=SimpleNamespace(get_config=lambda: {'modes':['iq_tx']*8}))
    pool = SimpleNamespace(_calibrating=False, boards=[board])
    scope = SimpleNamespace(pairs=[(0,0), (0,1)])
    def rpc(board, path):
        calls.append(path)
        return {'sensors':[{'sample_rate_hz':40_000_000}]*8}
    monkeypatch.setattr(reciprocity, '_rpc', rpc)
    with pytest.raises(ValueError, match="tx_method='monitor'"):
        reciprocity.calibrate(pool, scope=scope)
    assert calls == ['get_iq_tx']
