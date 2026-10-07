IQ sampling
===========

IQ sampling captures raw complex samples from the ESPARGOS receivers.
``board.iq`` configures the receivers, controls reference tones, and provides
subscriptions to raw and accumulated samples. ``IQPool`` synchronizes capture
across boards and assembles samples into clusters for processing.

Capture lifecycle
-----------------

.. code-block:: python

   import espargos

   board = espargos.Board("172.16.0.212")
   pool = espargos.IQPool([board])
   pool.add_iq_callback(lambda cluster: print(cluster.iq.shape))
   try:
       pool.start()
       pool.start_processing()
       pool.apply_config({"trigger_mode": 0, "trigger_config": [16384, 0, 4]})
       pool.sync()
       pool.enter_iq_mode()
       input("Capturing; press Enter to stop... ")
   finally:
       pool.stop_processing()
       pool.restore_wifi()
       pool.close()
       board.close()

``sync()`` acquires a common WiFi reference packet, posts per-sensor timestamp
anchors, and restores boards that were already in IQ mode. ``enter_iq_mode()``
waits for every sensor to report a fresh synchronized grid start. Fine timing
and phase calibration is a separate ``pool.calibrate()`` operation; it sweeps
the reference tone and stores a generic ``SensorCalibration``. The calibration
holds for one capture epoch: a resynchronization, a retune or a sample-rate
change restarts the sensors' sample engines and invalidates it. ``pool.calibration_applies_to(cluster)`` tells
whether a cluster or capture was recorded in the calibrated epoch.

Capture types
-------------

* Interval (trigger mode 0): ``IQCluster.iq`` contains complex64 samples shaped
  ``(boards, rows, columns, 256)``. ``add_iq_callback`` normally delivers
  complete clusters; ``include_partial=True`` also delivers settled partials.
* Accumulate (mode 3): ``trigger_config=[4, 32768, 0, 0]`` folds samples into
  four-chunk vectors. ``add_accumulation_callback`` delivers complete,
  coverage-consistent ``IQAccumCluster`` objects. Their IQ values are normalized
  by accumulation count; packets also preserve exact integer sums.
* Signal (mode 4): for example ``trigger_config=[64, 255, 300, 56, 0]`` selects
  ADC threshold, requesting-sensor mask, holdoff milliseconds, capture length
  in chunks (56 to 112), and quiet threshold. ``add_signal_capture_callback``
  delivers only complete, validated ``IQSignalCapture`` events. The pool handles
  acknowledgements and barrier recovery even without an event consumer.
  Signal consensus requires one IQPool per board; a wider coordinator can
  disable automatic acknowledgement and acknowledge accepted events explicitly.

``IQBacklog`` stores clusters for subsequent processing. The
``iq-signal-analyzer`` demo displays power and phase waterfalls, I/Q traces,
constellations, and spectra. It supports all three capture types.
