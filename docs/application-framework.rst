The Common Demo Application Framework
=======================================

The Qt demos share application setup, settings drawers and plotting components
from ``demos/common``. We can reuse these components when building a new demo,
while keeping signal processing and hardware control in the ``espargos``
library. Applications without a Qt interface can use :class:`.Pool` directly.

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
     - WiFi CSI reception through ``pool.wifi``, calibration and the WiFi settings drawer.
   * - ``ESPARGOSIQApplication``
     - IQ reception through ``pool.iq`` and a controller for the IQ settings drawer.
   * - ``ESPARGOSTxApplication``
     - IQ transmission controls, startup calibration and asynchronous hardware updates.

The CSI classes are available from ``demos.common``. Import the IQ classes
from ``demos.common.iq_application`` and ``demos.common.iq_tx_application``.
These modules belong to the repository's demo code, so run your application
from a checkout or include the common directory with it.

Receive demos call ``initialize_pool()`` and then ``initialize_qml()``. CSI
initialization can calibrate automatically. IQ reception starts processing,
but its calibration is initiated through the IQ controller. The TX application
starts connecting after QML loads and reports progress through ``txcontrol``.

The existing ``instantaneous-csi``, ``iq-signal-analyzer`` and
``iq-transmit-beamforming`` demos show complete examples of these three paths.

Optional mixins
-----------------

Mixins add features to a receive application. Put them before the application
class in the inheritance list::

   from demos.common import (
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
   import "../common" as Common

   Common.ESPARGOSApplication {
       receiverDrawerComponent: Component {
           Common.WiFiTransceiverDrawer {}
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
``Pool.calibrate()`` on the controller's worker thread. Changing a selector
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

   ./demos/instantaneous-csi/instantaneous-csi.py 192.168.1.2 \
       -o pool.channel=6 -o backlog.size=64

Values are parsed as YAML: ``true`` is a boolean, ``64`` an integer, and
``[a, b]`` a list. Repeat ``-o`` to set several keys. For combined arrays, use
one of the supplied layouts and override individual settings as needed::

   ./demos/combined-array/combined-array.py \
       -c config/aperture-kit-8x4.yml -o backlog.size=100

Host arguments differ between demos. Receive demos may accept positional
comma-separated hosts. The TX application requires either ``-s HOST`` or
``-c FILE``::

   ./demos/iq-transmit-beamforming/iq-transmit-beamforming.py \
       -s 192.168.1.2 --tx-calibration monitor

   ./demos/iq-transmit-beamforming/iq-transmit-beamforming.py \
       -c config/aperture-kit-8x4.yml \
       --tx-calibration reciprocity --calibration-path reference

Without ``--tx-calibration``, the TX application uses the controller monitor
for one board and reciprocity for multiple boards. See :doc:`calibration`
for the RF connections each method requires.
