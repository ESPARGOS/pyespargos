The Common Demo Application Framework
=======================================

The Qt demos share application setup, settings drawers and plotting components
from ``demos/common``, ``demos/wifi/common`` and ``demos/iq/common``.
These components can also be used in new demos. Hardware control lives in the ``espargos`` library, while application-specific
processing stays with each demo. Applications without a Qt interface can use :class:`.Pool` directly.

Choose an application class
-----------------------------

Each application owns one ``self.pool``. Receive demos access its processing
interface through ``self.receiver``. Drawers use the same connections.

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Class
     - Provides
   * - ``ESPARGOSApplication``
     - Command-line parsing, configuration, QML setup and shutdown hooks.
   * - ``ESPARGOSCSIApplication``
     - WiFi CSI reception through :attr:`pool.wifi <espargos.pool.Pool.wifi>`, calibration and the WiFi settings drawer.
   * - ``ESPARGOSIQApplication``
     - IQ reception through :attr:`pool.iq <espargos.pool.Pool.iq>` and a controller for the IQ settings drawer.
   * - ``ESPARGOSTxApplication``
     - IQ transmission controls, startup calibration and asynchronous hardware updates.

The CSI classes are available from ``demos.wifi.common.application``. Import the IQ classes
from ``demos.iq.common.application`` and the transmit-beamforming demo’s local
``application.py``.
These modules belong to the repository's demo code, so run your application
from a checkout or include the common directory with it.

Receive demos call ``initialize_pool()`` and then ``initialize_qml()``. CSI
initialization can calibrate automatically. IQ reception starts processing,
but its calibration is initiated through the IQ controller. The TX application
starts connecting after QML loads and reports progress through ``txcontrol``.

The existing ``instantaneous-csi``, ``signal-analyzer`` and
``transmit-beamforming`` demos show complete examples of these three paths.

Optional mixins
-----------------

Mixins add features to a receive application. Put them before the application
class in the inheritance list::

   from demos.wifi.common.application import (
       ESPARGOSCSIApplication, CSIBacklogMixin, SingleCSIFormatMixin,
   )

   class MyCSIApplication(CSIBacklogMixin, SingleCSIFormatMixin,
                          ESPARGOSCSIApplication):
       DEFAULT_CONFIG = {"show_phase": True}

``CSIBacklogMixin`` collects recent CSI in ``self.backlog`` and provides
backlog-size, field and packet-filter settings. ``SingleCSIFormatMixin``
selects the preamble format used by a visualization.

``CombinedArrayMixin`` loads antenna geometry and reference-cable properties
into the pool. It adds ``-s HOST`` for a single board with the standard layout.
``ESPARGOSTxApplication`` already includes this mixin. See
:doc:`combined-arrays` for the layout format.

The two drawers
-----------------

The shared QML window, ``Common.ESPARGOSApplication``, has optional left and
right drawers. The left drawer normally contains radio and calibration
controls, and the right contains display or application settings. Assign
``receiverDrawerComponent`` and ``appDrawerComponent`` to provide them.
Omitting either component hides its button.

For example, a CSI window can use the shared WiFi drawer:

.. code-block:: qml

   import QtQuick
   import "../../common" as Common
   import "../common" as WiFi

   Common.ESPARGOSApplication {
       receiverDrawerComponent: Component {
           WiFi.WiFiTransceiverDrawer {}
       }
   }

``initialize_qml()`` exposes the Python application as ``backend``, its
application settings as ``appconfig``, and common settings as ``genericconfig``.
Additional context objects can be passed in its ``context_props`` argument.

TX controls
-------------

``ESPARGOSTxApplication`` exposes ``txcontrol`` to QML. Its reusable components
include ``Common.IQTxDrawer``, ``Common.IQTxWorkspace`` and
``Common.IQTxMonitorSettings``. To use just the calibration controls in a
custom drawer:

.. code-block:: qml

   Common.IQTxCalibrationSettings {
       controller: txcontrol
       Layout.fillWidth: true
   }

The controls select monitor-based or reciprocal calibration and, for the
reciprocal method, the signal path. Calibration runs through
:meth:`Pool.calibrate() <espargos.pool.Pool.calibrate>` on the controller's worker thread. Changing a selector
sets the next calibration method without starting it.

The TX application selects IQ TX on all sensors, calibrates, then starts
transmission. Closing its window must call ``txcontrol.close()`` so the worker
can mute the transmitters before quitting. The beamforming demo's QML shows
this shutdown handler and its calibration progress dialog.

Configuration
---------------

The framework builds its initial configuration from defaults, then merges the
YAML file passed with ``-c``, then applies ``-o KEY=VALUE`` overrides. Nested
keys are merged, so a file only needs to contain the settings we want to change.
Dedicated arguments such as ``-s``, ``-b`` and ``--lltf`` are processed afterward
and can override the corresponding settings.

Available sections depend on the application:

* ``pool``: board addresses and, for CSI demos, WiFi and calibration settings.
* ``combined-array``: board arrangement and reference-cable properties.
* ``backlog``: CSI history size, stored fields and packet filters.
* ``csi``: preamble-format selection.
* ``generic``: shared UI settings such as ``kiosk_mode``.
* ``app``: settings defined by the demo's ``DEFAULT_CONFIG``.

Run a demo with ``--help`` to see its arguments and configuration defaults.
TX waveform, steering and monitor controls are managed by ``txcontrol`` rather
than the CSI ``pool`` configuration section.

For example, this YAML file changes the CSI channel and backlog size:

.. code-block:: yaml

   pool:
     channel: 6
   backlog:
     size: 64

We can use dotted keys for a single run instead::

   ./demos/wifi/instantaneous-csi/main.py 192.168.1.2 \
       -o pool.channel=6 -o backlog.size=64

Values are parsed as YAML: ``true`` is a boolean, ``64`` an integer, and
``[a, b]`` a list. Repeat ``-o`` to set several keys. For combined arrays, use
one of the supplied layouts and override individual settings as needed::

   ./demos/wifi/phases-by-antenna/main.py \
       -c config/aperture-kit-8x4.yml -o backlog.size=100

Host arguments differ between demos. Receive demos may accept positional
comma-separated hosts. The TX application requires either ``-s HOST`` or
``-c FILE``::

   ./demos/iq/transmit-beamforming/main.py \
       -s 192.168.1.2 --tx-calibration monitor

   ./demos/iq/transmit-beamforming/main.py \
       -c config/aperture-kit-8x4.yml \
       --tx-calibration reciprocity --calibration-path reference

Without ``--tx-calibration``, the TX application uses the controller monitor
for one board and reciprocity for multiple boards. See :doc:`calibration`
for the RF connections each method requires.

Demo layout
-----------

Each published demo has a ``main.py`` entry point and a ``Main.qml`` window.
Keep its processing, shaders and application-specific controls beside them.
Only components used across demos belong in the shared folders:

* ``demos/common``: application shell, configuration and branding.
* ``demos/wifi/common``: CSI application support and WiFi controls.
* ``demos/iq/common``: IQ receive controls and waterfall rendering.

Import Python helpers from their modules explicitly. This keeps camera and
other optional dependencies out of demos that do not use them. Local
experiments belong in ``demos/custom`` and are ignored by Git.

Use ``Pool.configure()`` and ``Pool.calibrate()`` for radio configuration and
calibration. Receiver-specific capture options and data callbacks remain on
``pool.iq`` and ``pool.wifi``. Reference cable properties come from the pool’s
array configuration.
