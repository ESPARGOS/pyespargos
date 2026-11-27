Controller API
==============

Most applications use a :class:`~espargos.Pool` or a :class:`~espargos.Board`.
The SDK selects the control protocol when it connects. The resource names
below are for programs that talk to the controller directly, for example
when implementing a client in another language.

Finding the interface
---------------------

Read ``/api_info`` once when opening a connection. Controllers with the
versioned interface advertise ``"control_api": "/api/v6"``. The device
resource under that root reports the native API version. The version in
``/api_info`` describes the older command interface so existing clients
can continue to use it.

Use GET to read a resource. To change radio settings, POST a JSON object
containing only the fields you want to change. The same resource paths
and request bodies work over the controller's serial RPC transport.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Resource under ``/api/v6/``
     - Settings
   * - ``wifi/rx``
     - CSI acquisition, gain, RF input, channels and receive filters.
   * - ``wifi/tx``
     - WiFi packet format, transmit power, per-sensor schedule and RF output.
   * - ``wifi/iq``, ``ble/rx``
     - Packet capture settings and RF inputs for each receiver profile.
   * - ``iq/rx``
     - Raw IQ capture path, sample rate, trigger, receiver gains, frequencies and inputs.
   * - ``iq/tx``
     - Playback settings, per-sensor RF outputs and transmitter status.
   * - ``roles/sensors``, ``roles/controller``
     - Sensor roles and the controller's independent radio mode.
   * - ``reference/wifi``, ``reference/ble``, ``reference/iq``
     - The controller's reference generator. POST to ``wifi/tune`` to retune the WiFi reference and all CSI receivers together.

The mode settings are independent. For example, changing ``wifi/tx`` selects
the feed used for a WiFi transmission. Between transmissions the sensor
returns to the input selected in ``wifi/rx``. To check a role change, read
``roles/sensors``. It reports both the requested modes and the mode each
sensor has acknowledged.

Selecting antenna feeds
-----------------------

For example, POST the following to ``/api/v6/wifi/rx`` to select the right
feed on every sensor except antenna 3. The other receive settings keep
their current values.

.. code-block:: json

   {
     "input": "right",
     "sensors": [{"antid": 3, "input": "left"}]
   }

To change antenna 3 again, send only the ``sensors`` entry. Antenna IDs run
from 0 to 7 on each board. A top-level value applies to all eight first.
Each sensor entry then overrides that value for its antenna. Duplicate
antenna IDs are rejected before the settings are applied.

A GET returns a top-level ``input`` or ``output`` when every sensor uses
the same path. With mixed paths, read the field from the ``sensors`` entries.
The path names are ``left``, ``right``, ``reference`` and ``isolation``.
WiFi RX, WiFi TX, WiFi IQ and BLE also support ``random``. Raw IQ capture
and playback use a fixed path on each sensor.

With an older controller, the SDK uses its original commands. A request
for a per-sensor feature that the old firmware cannot express raises an
error. An older client can still broadcast its usual RF-switch value to a
new controller. A legacy scalar read returns HTTP 409 when the sensors
have different paths. IQ TX already reported per-sensor outputs in the old
interface and continues to do so.

Live and saved settings
-----------------------

Mode resources control the running radios. Resources under ``config/``
store settings in nonvolatile memory. For example, ``config/wifi`` saves
defaults for the next boot, while ``reference/wifi`` changes the live
generator. Saving ``config/network`` and rebooting the controller are
separate operations. HTTP maintenance endpoints under ``/api/v6/updates/`` handle firmware
transfers and streamed progress. GET ``check`` to check for a release
and ``status`` to read progress. POST to ``start`` to begin an update.
The upload targets are ``controller``, ``sensors/application`` and
``partition?partition=<label>``. POST to ``sensors/flash`` to flash sensors
from the controller's bundled image.

For looping IQ playback, upload a buffer through ``iq/tx/upload`` and
prepare the waveform through ``iq/tx/synthesize``. Live sample data and
scheduled steering updates use the bidirectional sensor stream. On API 6.1, connect to
``/api/v6/stream`` for WebSocket or POST to ``/api/v6/stream/udp``
to set up UDP. The live modulation settings at ``/api/v6/iq/tx/stream``
reset to CS8 at 16 kSa/s when the controller restarts. See :doc:`iq-transmit-streaming` and
:doc:`iq-beam-steering`.
