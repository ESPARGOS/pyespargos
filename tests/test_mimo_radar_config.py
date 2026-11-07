"""Exercise the supported radar's configuration and QML without hardware."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'demos/iq/mimo-radar'


def test_cli_exposes_config_keys_instead_of_dedicated_flags():
    result = subprocess.run(
        [sys.executable, str(DEMO / 'main.py'), '--help'],
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'},
        capture_output=True, text=True, check=True,
    )
    for key in ('tx_calibration', 'frequency', 'amplitude', 'integration_time', 'sample_rate', 'rx_gain', 'range_maximum'):
        assert f'app.{key}:' in result.stdout
        assert '--' + key.replace('_', '-') not in result.stdout


def test_yaml_overrides_gui_updates_and_validation(tmp_path):
    config = tmp_path / 'radar.yml'
    config.write_text('app:\n  frequency: 2400\n  integration_time: 0.2\n  linear_scale: false\n')
    script = r'''
import json, sys, time
from PyQt6 import QtCore
from main import MIMORadar

# Keep the worker running without connecting to hardware.
MIMORadar._work = lambda self, config=None: self.stopping.wait()
app = MIMORadar(sys.argv)
root = app.engine.rootObjects()[0]
def drain():
    for _ in range(30):
        app.processEvents()
        time.sleep(.01)
try:
    assert app._settings['frequency'] == 2412
    assert app._settings['integration_time'] == .2
    assert root.property('linearScale') is False
    assert root.findChild(QtCore.QObject, 'frequencyInput').property('text') == '2412'
    assert root.findChild(QtCore.QObject, 'integration_timeInput').property('text') == '0.2'
    app._busy = False
    field = root.findChild(QtCore.QObject, 'frequencyInput')
    field.setProperty('text', '2420')
    QtCore.QMetaObject.invokeMethod(field, 'editingFinished')
    drain()
    name, value = app.commands.get_nowait()
    assert name == 'config' and value['frequency'] == 2420
    # GUI edits stay queued until the worker applies them.
    assert app._settings['frequency'] == 2412
    app._busy = False
    for key, value in [('amplitude', 2), ('integration_time', .01), ('sample_rate', 13),
                       ('rx_gain', 1.5), ('range_maximum', -1), ('range_maximum', float('inf')), ('frequency', float('nan')),
                       ('tx_feed', 2), ('tx_calibration', 'invalid')]:
        previous = app.appconfig.get()
        app.appconfig.setConfigFromUI(json.dumps({key: value}))
        drain()
        assert app.appconfig.get() == previous
        assert app.commands.empty()
    field = root.findChild(QtCore.QObject, 'rangeMaximumInput')
    assert field is not None
    field.setProperty('text', '900')
    QtCore.QMetaObject.invokeMethod(field, 'editingFinished')
    drain()
    name, value = app.commands.get_nowait()
    assert name == 'config' and value['range_maximum'] == 900
    app._apply_radar_settings(value)
    assert app.rangeMaximum == 239  # Limited at 80 MSa/s.
    app._apply_radar_settings(dict(value, sample_rate=20))
    assert app.rangeMaximum == 900
    app._busy = False
    root.setProperty('fullVelocityRange', True)
    drain()
    assert app.appconfig.get('full_velocity_range') is True
    assert app.commands.empty()  # Display changes leave calibration alone.
finally:
    app.onAboutToQuit()
    root.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
'''
    result = subprocess.run(
        [sys.executable, '-c', script, '-c', str(config), '-o', 'app.frequency=2412'],
        cwd=DEMO,
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'QT_QUICK_BACKEND': ''},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ReferenceError' not in result.stderr
    assert 'TypeError' not in result.stderr


def test_worker_applies_config_and_preserves_paused_state():
    script = r'''
import queue, threading
from types import SimpleNamespace
from unittest.mock import Mock
import main

main.ProcessPoolExecutor = Mock()
radar = SimpleNamespace(
    _settings=dict(main.MIMORadar.DEFAULT_CONFIG),
    RECALIBRATION_SETTINGS=main.MIMORadar.RECALIBRATION_SETTINGS,
    provider=Mock(), workerState=Mock(), workerError=Mock(), logger=Mock(),
    stopping=threading.Event(), commands=queue.Queue(),
)
radar._apply_radar_settings = lambda cfg: main.MIMORadar._apply_radar_settings(radar, cfg)
radar._apply_radar_settings(radar._settings)
opened = []
def open_radar():
    opened.append(dict(radar._settings))
    radar._pool = object()
    radar._processor = SimpleNamespace(beamspace_period=2)
    radar._capturing = False if len(opened) == 1 else True
radar._open_radar = open_radar
radar._close_radar = Mock()
def set_running(value):
    assert value is False
    radar._capturing = value
    radar.stopping.set()
radar._set_running = set_running
changed = dict(radar._settings, frequency=2412, sample_rate=40, integration_time=.2,
               amplitude=.3, rx_gain=30, tx_calibration='reciprocity', tx_feed=0)
radar.commands.put(('config', changed))
main.MIMORadar._work(radar)
assert opened == [main.MIMORadar.DEFAULT_CONFIG, changed]
assert radar._sampling.rate == 40e6 and radar._tx_feed == 0
assert radar.stopping.is_set() and radar._capturing is False
radar.provider.clear.assert_called_once()
radar.workerError.emit.assert_not_called()
assert radar._close_radar.call_count == 2  # Close once to reconfigure and once at shutdown.
'''
    result = subprocess.run(
        [sys.executable, '-c', script], cwd=DEMO,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_settings_retain_calibration_and_paused_state():
    script = r'''
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
import main

for running in (False, True):
    for key, value in [('integration_time', .2), ('range_maximum', 200), ('amplitude', .3), ('rx_gain', 30)]:
        config = dict(main.MIMORadar.DEFAULT_CONFIG)
        pool = Mock()
        signals = [SimpleNamespace(amplitude=.4), SimpleNamespace(amplitude=.4)]
        pool.tx.signals = signals
        pool.tx.update.return_value = nullcontext()
        pool.tx.positions = np.zeros((8, 2))
        previous = SimpleNamespace(range_weights=np.arange(256))
        processor = SimpleNamespace()
        main.MIMOProcessor = Mock(return_value=processor)
        radar = SimpleNamespace(
            RADAR_SETTINGS=main.MIMORadar.RADAR_SETTINGS,
            _settings=config, _pool=pool, _capturing=running,
            _processor=previous, _frequency=2385e6, _history=20,
            _rx_antennas=['rx'], _transmitters=Mock(),
            _capture_boundary=Mock(), _clear_history=Mock(), _check_transmitters=Mock(),
        )
        radar._apply_radar_settings = lambda cfg: main.MIMORadar._apply_radar_settings(radar, cfg)
        main.MIMORadar._update_live_settings(radar, dict(config, **{key:value}))
        assert radar._capturing == running
        pool.calibrate.assert_not_called()
        pool.calibrate_tx_rx_delay.assert_not_called()
        pool.close.assert_not_called()
        if key in ('integration_time', 'range_maximum'):
            np.testing.assert_array_equal(processor.range_weights, previous.range_weights)
            pool.select.assert_not_called()
            pool.tx.update.assert_not_called()
            radar._capture_boundary.assert_not_called()
        elif key == 'amplitude':
            assert all(s.amplitude == .3 for s in signals)
            assert radar._transmitters.stop_transmission.call_count == (0 if running else 1)
        else:
            pool.select.assert_called_once_with(antennas=['rx'])
            pool.select.return_value.configure.assert_called_once_with(rx_gain=30)
        radar._clear_history.assert_called_once()

radar._peak_dbfs = -6.0206
text = main.MIMORadar._headroom_text(radar)
assert '6.0 dB' in text and '-6.0 dBFS' in text
radar._peak_dbfs = None
assert '—' in main.MIMORadar._headroom_text(radar)
'''
    result = subprocess.run(
        [sys.executable, '-c', script], cwd=DEMO,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
