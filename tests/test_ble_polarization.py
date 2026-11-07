import time
import unittest
from unittest.mock import patch
import numpy as np
from espargos.ble_phase_history import BLEPhaseHistory
from espargos.array_processing import steering_vectors_2d, root_music_2d, spatial_smoothing_2d
from espargos.polarization import antenna_response
from espargos.ble_beamforming import BLEBeamformer, BLEBeamformingConfig


class PolarizationTests(unittest.TestCase):
    def test_mixed_feeds_recover_amplitude_and_relative_phase(self):
        rng = np.random.default_rng(17)
        field = (rng.normal(size=(8,2)) + 1j*rng.normal(size=(8,2)))
        backlog = BLEPhaseHistory(size=32, max_age=1)
        for _ in range(32):
            inputs = rng.integers(2,4,8)
            values = field[np.arange(8),inputs-2] * np.exp(1j*rng.uniform(-np.pi,np.pi)) * rng.uniform(.5,2)
            backlog.append(values,inputs)
        estimate = backlog.coherent_feeds()
        self.assertIsNotNone(estimate)
        scale = np.vdot(estimate,field) / np.vdot(estimate,estimate)
        np.testing.assert_allclose(estimate*scale,field,rtol=1e-3,atol=1e-3)

    def test_whole_array_feed_switching_cannot_establish_relative_phase(self):
        backlog = BLEPhaseHistory(size=16,max_age=1)
        for i in range(16):
            backlog.append(np.ones(8,complex),np.full(8,2+i%2))
        self.assertIsNone(backlog.coherent_feeds())

    def test_two_advertisers_keep_separate_directions_and_polarizations(self):
        class Orientation:
            def rotation_matrix(self): return np.eye(2)
        orientations = np.full((2,4),Orientation(),object)
        steering = steering_vectors_2d(2,4,64,48,frequency_hz=2402e6,pixel_centers=True)
        response = antenna_response(np.array([2402e6]))[0]
        processor = BLEBeamformer()
        config = BLEBeamformingConfig(history_size=32, history_age=1, polarization=True)
        rng = np.random.default_rng(81)
        sources = [('a',(19,18),np.array([1,.3j])),('b',(42,29),np.array([.2,1]))]
        with patch('espargos.ble_phase_history.time.monotonic',return_value=10):
            for i in range(32):
                for name, target, jones in sources:
                    feeds = steering[:,:,target[0],target[1],None]*(response @ jones)
                    routes = rng.integers(2,4,(2,4))
                    field = np.take_along_axis(feeds,(routes-2)[...,None],axis=-1)[...,0]*np.exp(1j*rng.uniform(-np.pi,np.pi))
                    result = processor.process(name,field/abs(field),routes,10,steering,2402e6,config,
                        amplitude=abs(field),orientations=orientations)
                    power, estimate = result.spectrum, result.jones
                    if i == 31:
                        self.assertIsNotNone(estimate)
                        self.assertEqual(np.unravel_index(power.argmax(),power.shape),target)
                        actual = estimate[target]
                        np.testing.assert_allclose(actual/actual[np.argmax(abs(jones))],jones/jones[np.argmax(abs(jones))],atol=.01)
        self.assertEqual(len(processor._histories),2)

    def test_music_methods_allow_different_arbitrary_path_polarizations(self):
        class Orientation:
            def rotation_matrix(self): return np.eye(2)
        orientations = np.full((2,4),Orientation(),object)
        grid = steering_vectors_2d(2,4,64,48,frequency_hz=2402e6,pixel_centers=True)
        targets = [(17,19),(45,29)]
        expected = np.array([[(2*a+1)/64-1,(2*e+1)/48-1] for a,e in targets])
        response = antenna_response(np.array([2402e6]))[0]
        cases = ([np.array([1,0]),np.array([0,1])],
                 [np.array([1,1j])/np.sqrt(2),np.array([.3+.4j,1])])
        for polarizations in cases:
            feeds = sum(grid[:,:,a,e,None]*(response@p)*amplitude
                        for (a,e),p,amplitude in zip(targets,polarizations,[1,.8*np.exp(.7j)]))
            for method in ("MUSIC","Root-MUSIC"):
                processor = BLEBeamformer(clock=lambda:10.)
                rng = np.random.default_rng(20)
                for _ in range(32):
                    routes = rng.integers(2,4,(2,4))
                    field = np.take_along_axis(feeds,(routes-2)[...,None],axis=-1)[...,0]*np.exp(1j*rng.uniform(-np.pi,np.pi))
                    result = processor.process("a",field/abs(field),routes,10,grid,2402e6,
                        BLEBeamformingConfig(method=method,history_size=32,history_age=1,polarization=True),
                        amplitude=abs(field),orientations=orientations,
                        covariance=np.outer(field.ravel(),field.ravel().conj()),sample_counts=np.full((8,8),800))
                with self.subTest(method=method,polarizations=polarizations):
                    self.assertIsNotNone(result.jones)
                    self.assertIsNotNone(result.path_jones)
                    for direction, polarization in zip(expected,polarizations):
                        nearest = np.argmin(np.linalg.norm(result.directions-direction,axis=1))
                        estimate = result.path_jones[nearest]
                        polarization = polarization/np.linalg.norm(polarization)
                        np.testing.assert_allclose(np.outer(estimate,estimate.conj()),
                            np.outer(polarization,polarization.conj()),atol=.005)
                    if method == "MUSIC":
                        self.assertTrue(all(result.spectrum[t] > .9 for t in targets))
                    else:
                        for direction in expected:
                            self.assertLess(np.min(np.linalg.norm(result.directions-direction,axis=1)),.005)

    def test_display_phase_is_continuous_when_dominant_component_changes(self):
        from espargos.polarization import polarization_ellipse
        previous = None
        displayed = []
        for delta in np.linspace(-.02,.02,21):
            vector = np.array([1+delta,1j])
            coherency = np.outer(vector,vector.conj())[None,None]
            result = polarization_ellipse(coherency,previous)
            displayed.append(result[0,0])
            previous = result
            np.testing.assert_allclose(result[0,0,1]/result[0,0,0],1j/(1+delta),atol=1e-12)
        overlap = np.sum(np.array(displayed[:-1]).conj()*np.array(displayed[1:]),axis=-1)
        np.testing.assert_allclose(np.angle(overlap),0,atol=1e-12)

    def test_display_arbitrary_polarization_and_missing_estimates(self):
        from espargos.polarization import polarization_ellipse
        for vector in ([1,0],[0,1],[1,1],[1,-1],[1,1j],[1,-1j],[.3+.4j,1]):
            vector = np.asarray(vector,complex)
            result = polarization_ellipse(np.outer(vector,vector.conj())[None,None])[0,0]
            expected = vector/np.linalg.norm(vector)
            np.testing.assert_allclose(np.outer(result,result.conj()),np.outer(expected,expected.conj()),atol=1e-12)
        np.testing.assert_array_equal(polarization_ellipse(np.zeros((4,3,2,2))),0)
        np.testing.assert_array_equal(polarization_ellipse(np.broadcast_to(np.eye(2),(4,3,2,2))),0)

    def test_coverage_gap_does_not_switch_back_to_uncorrected_bartlett(self):
        class Orientation:
            def rotation_matrix(self): return np.eye(2)
        orientations = np.full((2,4),Orientation(),object)
        grid = steering_vectors_2d(2,4,32,24,frequency_hz=2402e6,pixel_centers=True)
        response = antenna_response(np.array([2402e6]))[0]
        feeds = grid[:,:,12,10,None]*(response@np.array([1,.4j]))
        processor = BLEBeamformer(clock=lambda:10.)
        config = BLEBeamformingConfig(history_size=16,history_age=1,polarization=True)
        rng = np.random.default_rng(81)
        def process(routes):
            field = np.take_along_axis(feeds,(routes-2)[...,None],axis=-1)[...,0]
            return processor.process("a",field/abs(field),routes,10,grid,2402e6,config,
                                     amplitude=abs(field),orientations=orientations)
        for _ in range(32):
            result = process(rng.integers(2,4,(2,4)))
        self.assertIsNotNone(result.jones)
        for _ in range(16):
            result = process(np.full((2,4),2))
        self.assertIsNone(result.spectrum)
        for _ in range(32):
            result = process(rng.integers(2,4,(2,4)))
        self.assertIsNotNone(result.spectrum)
        self.assertIsNotNone(result.jones)
        processor.clear()
        self.assertIsNotNone(process(np.full((2,4),2)).spectrum)

    def test_missing_polarization_coverage_keeps_bartlett_direction_visible(self):
        class Orientation:
            def rotation_matrix(self): return np.eye(2)
        steering = steering_vectors_2d(2, 4, 64, 48, frequency_hz=2402e6, pixel_centers=True)
        target = (25, 20)
        vector = steering[:, :, target[0], target[1]]
        config = BLEBeamformingConfig(polarization=True)
        result = BLEBeamformer().process("slow", vector, np.full((2, 4), 2),
            time.monotonic(), steering, 2402e6, config, amplitude=np.ones((2, 4)),
            orientations=np.full((2, 4), Orientation(), object))
        power, jones = result.spectrum, result.jones
        self.assertIsNone(jones)
        self.assertEqual(np.unravel_index(power.argmax(), power.shape), target)
        self.assertGreater(power.max(), .9)

    def test_single_packet_multipath_keeps_measured_amplitudes(self):
        r, c = np.indices((2, 4))
        directions = np.array([[-.45, -.15], [.4, .25]])
        scale = 2*np.pi*.06*2402e6/299792458
        manifold = np.exp(1j*scale*(c.ravel()[:, None]*directions[:, 0] + r.ravel()[:, None]*directions[:, 1]))
        field = (manifold @ [1, .7*np.exp(.8j)]).reshape(2, 4)
        steering = steering_vectors_2d(2, 4, 64, 48, frequency_hz=2402e6, pixel_centers=True)
        config = BLEBeamformingConfig(method="Root-MUSIC")
        result = BLEBeamformer().process("slow", field/abs(field), np.full((2, 4), 2),
            time.monotonic(), steering, 2402e6, config, amplitude=abs(field))
        found = result.directions
        self.assertIsNone(result.spectrum)
        self.assertEqual(result.strengths.shape, (2,))
        self.assertEqual(len(found), 2)
        for target in directions:
            self.assertLess(np.min(np.linalg.norm(np.asarray(found)-target, axis=1)), .03)

    def test_music_ignores_weak_covariance_model_error(self):
        # A weak coherent residual should not become a second bright target.
        # Full packet covariance retains that energy, rather than discarding it.
        grid = steering_vectors_2d(2, 4, 64, 48, frequency_hz=2402e6, pixel_centers=True)
        target, residue = (44, 20), (16, 30)
        field = grid[:, :, *target]
        error = grid[:, :, *residue].ravel()
        covariance = np.outer(field.ravel(), field.ravel().conj())
        covariance += .015 * np.outer(error, error.conj()) + .0001 * np.eye(8)
        for method in ("MUSIC", "Root-MUSIC"):
            with self.subTest(method=method):
                result = BLEBeamformer(clock=lambda: 10.).process(
                    "source", field, np.full((2, 4), 2), 10., grid, 2402e6,
                    BLEBeamformingConfig(method=method), amplitude=np.ones((2, 4)),
                    covariance=covariance, sample_counts=np.full((8, 8), 800))
                self.assertEqual(len(result.directions), 1)
                expected = [(2*target[0]+1)/64-1, (2*target[1]+1)/48-1]
                np.testing.assert_allclose(result.directions[0], expected, atol=.025)

    def test_spatial_smoothing_resolves_coherent_paths(self):
        scale = 2*np.pi*.06*2402e6/299792458
        r,c=np.indices((2,4))
        directions=np.array([[-.45,-.15],[.4,.25]])
        a=np.exp(1j*scale*(c.ravel()[:,None]*directions[:,0]+r.ravel()[:,None]*directions[:,1]))
        field=a @ np.array([1,.7*np.exp(.8j)])
        covariance=np.outer(field,field.conj())+.001*np.eye(8)
        smoothed,rows,columns=spatial_smoothing_2d(covariance,2,4)
        found,powers,_=root_music_2d(smoothed,rows,columns,frequency_hz=2402e6,snapshots=16,dynamic_range_db=10)
        self.assertGreaterEqual(len(found),2)
        for target in directions:
            self.assertLess(np.min(np.linalg.norm(found-target,axis=1)),.03)

    def test_replay_clock_controls_history_age(self):
        clock = [10.]
        history = BLEPhaseHistory(max_age=.5, clock=lambda: clock[0])
        history.append(np.ones((2, 4), complex), np.full((2, 4), 2), timestamp=10)
        self.assertEqual(history.snapshot_count, 1)
        clock[0] += .6
        self.assertEqual(history.snapshot_count, 0)

    def test_bartlett_ignores_missing_sensor_without_nan_spectrum(self):
        steering = steering_vectors_2d(2, 4, 32, 24, frequency_hz=2402e6, pixel_centers=True)
        vector = steering[:, :, 15, 10].copy()
        vector[0, 0] = np.nan
        result = BLEBeamformer().process('partial', vector, np.full((2, 4), 2),
            time.monotonic(), steering, 2402e6, BLEBeamformingConfig())
        self.assertTrue(np.isfinite(result.spectrum).all())
        self.assertEqual(np.unravel_index(result.spectrum.argmax(), result.spectrum.shape), (15, 10))
