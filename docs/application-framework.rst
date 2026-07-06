The Common Demo Application Framework
=====================================

All demo applications shipped with *pyespargos* are built on top of a shared *common* application framework, located in the :code:`demos/common` directory of the repository.
Instead of every demo re-implementing the same boilerplate (connecting to ESPARGOS devices, calibrating, collecting a CSI backlog, drawing a Qt/QML user interface), the framework provides these building blocks once, so that each demo only has to implement its own signal-processing and visualization logic.

If you write your own ESPARGOS application on top of the demo framework (the fastest way to get started is to copy and modify an existing demo), you get its features for free.
This page describes the framework in general and, in particular, how it is configured through YAML configuration files and command-line options.

Structure
---------
The framework is centered on the :code:`ESPARGOSApplication` base class, which every demo subclasses.
Optional functionality is added through *mixins* that a demo combines as needed:

* :code:`ESPARGOSApplication`: the base class. Handles command-line argument parsing, YAML configuration loading, QML engine setup, and creation and calibration of the ESPARGOS :class:`.Pool`.
* :code:`BacklogMixin`: adds a :class:`.CSIBacklog` and a settings panel for it (backlog size, stored CSI fields, packet filters).
* :code:`CombinedArrayMixin`: adds support for phase-coherent multi-board *combined arrays* (see :doc:`combined-arrays`), including parsing of the array geometry and per-cable length compensation.
* :code:`SingleCSIFormatMixin`: adds selection of a single preamble format (L-LTF, HT20, HT40 or HE20) to work with.

A typical demo therefore starts like this:

.. code-block:: python

  from demos.common import ESPARGOSApplication, BacklogMixin, SingleCSIFormatMixin

  class EspargosDemoPhasesOverSpace(BacklogMixin, SingleCSIFormatMixin, ESPARGOSApplication):
      ...

Beyond configuration, the framework also provides a graphical *pool drawer* for connecting to and calibrating ESPARGOS devices at runtime, so many settings can additionally be changed interactively from the user interface while a demo is running.
The remainder of this page focuses on how a demo is configured *before* it starts, from configuration files and the command line.

Configuration Overview
----------------------
Every demo is configured from a single nested configuration dictionary that is assembled, in order of increasing precedence, from three sources:

#. **Built-in defaults**: sensible defaults defined by the framework and by the individual demo.
#. **A YAML configuration file**, loaded with :code:`-c`/:code:`--config`.
#. **Command-line option overrides**, passed with :code:`-o`/:code:`--option`.

Later sources are *deep-merged* on top of earlier ones: a source only needs to specify the keys it wants to change, and all other keys keep their previous value.
The configuration is grouped into top-level sections:

* :code:`pool`: which ESPARGOS devices to connect to (:code:`hosts`) and how to operate them: WiFi channel, calibration behavior, RF switch, receiver/FFT gain, MAC filtering, and so on.
* :code:`backlog`: CSI backlog settings, i.e., :code:`size`, which CSI :code:`fields` to store (:code:`lltf`, :code:`ht20`, :code:`ht40`, :code:`he20`), and packet :code:`filters` (e.g. :code:`exclude_11b`). Only relevant for demos using :code:`BacklogMixin`.
* :code:`combined-array`: the geometry of a phase-coherent multi-board array. Only relevant for demos using :code:`CombinedArrayMixin` (see :doc:`combined-arrays`).
* :code:`generic`: framework-wide application settings, such as :code:`preamble_format` and :code:`kiosk_mode`.
* :code:`app`: reserved for settings specific to the individual demo.

To see the complete list of configuration keys and their default values for any demo, run it with :code:`--help`.
The bottom of the help output lists every available option, for example:

.. code-block:: text

  Configuration options for -o/--option (as CLI arguments) or -c/--config (as YAML file):
    backlog:
      backlog.size: default: 20
      fields:
        backlog.fields.lltf: default: true
        backlog.fields.ht20: default: false
        ...
    pool:
      pool.hosts: default: []
      pool.channel: default: 13
      ...
    generic:
      generic.preamble_format: default: 'auto'
      generic.kiosk_mode: default: false
    app: default: {}

YAML Configuration Files (``-c``/``--config``)
----------------------------------------------
A configuration file is a YAML document whose top level is an object (dictionary) with the same section structure as above.
It is loaded with the :code:`-c` (or :code:`--config`) argument and merged on top of the defaults, so you only need to include the sections and keys you actually want to override.

The most common reason to use a configuration file is to describe a *combined array*, because its geometry is too complex to express on the command line.
Ready-made example configuration files ship in the :code:`config` directory of the repository:

* :code:`config/single-espargos-one.yml`: a single ESPARGOS One board in its default orientation.
* :code:`config/aperture-kit-6x4.yml`: a phase-coherent large-aperture 6 × 4 array built from three boards.

For example, :code:`config/single-espargos-one.yml` describes a single board:

.. code-block:: yaml

  combined-array:
    boards:
      espargosone:
        host: 192.168.1.2
        cable:
          length: 0.0
          velocity_factor: 0.76

    array:
      - - "espargosone.0.0"
        - "espargosone.0.1"
        - "espargosone.0.2"
        - "espargosone.0.3"
      - - "espargosone.1.0"
        - "espargosone.1.1"
        - "espargosone.1.2"
        - "espargosone.1.3"

Here, the :code:`boards` section maps a freely chosen board name to its network :code:`host` and the properties of its reference-signal :code:`cable` (used for phase compensation, see :doc:`combined-arrays`).
The :code:`array` section is a matrix that lays out the antennas physically: each entry :code:`"<board>.<row>.<column>"` refers to one antenna of one board and its position in the matrix corresponds to its physical position in the combined array.

To run a demo with a configuration file:

.. code-block:: bash

  ./demos/combined-array/combined-array.py -c config/single-espargos-one.yml

A configuration file is of course not limited to the :code:`combined-array` section; any configuration key may be set in it, for example:

.. code-block:: yaml

  pool:
    channel: 6
    secondary_channel: 2
    gain:
      automatic: false
      rx_gain_value: 40
  backlog:
    size: 64
  generic:
    kiosk_mode: true

Overriding Options on the Command Line (``-o``/``--option``)
------------------------------------------------------------
Individual configuration options can be set directly on the command line with :code:`-o`/:code:`--option`, without editing or creating a configuration file.
Each override has the form :code:`-o KEY=VALUE`, where :code:`KEY` is the dotted path to a configuration key and :code:`VALUE` is the new value.
Overrides are applied on top of both the built-in defaults and any configuration file loaded with :code:`-c`, so they always take precedence.

* **The key is a dotted path** into the nested configuration. For example, :code:`generic.kiosk_mode` refers to the :code:`kiosk_mode` key inside the :code:`generic` section, and :code:`pool.gain.rx_gain_value` refers to :code:`rx_gain_value` inside :code:`gain` inside :code:`pool`.
* **The value is parsed as YAML**, so its type is inferred automatically: :code:`true`/:code:`false` become booleans, :code:`32` an integer, :code:`3.5` a float, :code:`null` becomes :code:`None`, and anything else a string. Lists work too, e.g. :code:`pool.hosts=[192.168.1.2,192.168.1.3]`.
* **Multiple** :code:`-o` **options are allowed**; each sets one key, and they are applied left to right.

For example, to run a demo in kiosk mode, on WiFi channel 6, with a fixed receiver gain:

.. code-block:: bash

  ./demos/instantaneous-csi/instantaneous-csi.py 192.168.1.2 \
      -o generic.kiosk_mode=true \
      -o pool.channel=6 \
      -o pool.gain.automatic=false \
      -o pool.gain.rx_gain_value=40

:code:`-o` and :code:`-c` combine naturally: load a base configuration from a file, then tweak individual keys for a single run without modifying the file:

.. code-block:: bash

  ./demos/combined-array/combined-array.py -c config/aperture-kit-6x4.yml -o backlog.size=100

Convenience Command-Line Arguments
----------------------------------
For the most frequently used settings, the framework also provides dedicated command-line arguments as shortcuts, so you do not have to spell out the full configuration key.
Which of these are available depends on the mixins a demo uses; run the demo with :code:`--help` to see its exact set of arguments.

* **Device hosts** (positional argument): for demos that operate on plain, independent arrays, a comma-separated list of host addresses sets :code:`pool.hosts`, e.g. :code:`instantaneous-csi.py 192.168.1.2,192.168.1.3`.
* :code:`-s`/:code:`--single-array HOST` (with :code:`CombinedArrayMixin`): a shortcut that auto-generates a single-board :code:`combined-array` configuration for the given host, so you do not need a configuration file just to run a combined-array demo on one board.
* :code:`-b`/:code:`--backlog-size N` (with :code:`BacklogMixin`): sets :code:`backlog.size`.
* :code:`--lltf` / :code:`--ht20` / :code:`--ht40` / :code:`--he20` (with :code:`SingleCSIFormatMixin`): restrict the demo to a single preamble format (:code:`generic.preamble_format`). Without any of these flags, the format is chosen automatically.

.. note::
   These shortcuts and the corresponding configuration keys control the same underlying settings.
   For the handful of settings that have a dedicated argument (device hosts, single-array layout, backlog size and preamble format), prefer the dedicated argument; use :code:`-c` and :code:`-o` for everything else.
