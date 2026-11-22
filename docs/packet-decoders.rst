Shared packet decoders
======================

A sensor can record the waveform of a Wi-Fi or Bluetooth LE packet. The
decoder reads that waveform and looks for the packet's timing, header and
identity. Use it when you have a saved IQ capture, for example to find the
start of a Wi-Fi training sequence and compare the same part of the packet
across antennas.

If a live receiver in your pool already gives you a packet's address or
channel estimate, use those fields directly. The decoder operates on one
antenna's capture at a time. The pool handles collecting reports from
several antennas for you.

``espargos.packet_decoders`` is a Python wrapper around the
`espargos-packet-decoders <https://github.com/ESPARGOS/espargos-packet-decoders>`_
C library. See the library repository for the C API and LGPL license terms.

Decode captured IQ
------------------

Pass one capture at a time. For example, use the Wi-Fi decoder to examine a
packet or locate its training, and the BLE decoder to read an advertisement
on channel 37:

.. code-block:: python

   from espargos.packet_decoders import ble, wifi

   packet = wifi(wifi_samples)
   training = wifi(wifi_samples, stage="training")
   advertisement = ble(
       ble_samples, sample_rate=4_000_000, phy=0, channel=37
   )

Give the decoder ESPARGOS packed IQ words as a NumPy ``uint32`` array or
native-endian bytes. Each word holds signed 10-bit I in bits 0–9 and Q in
bits 10–19. For Wi-Fi, use the native 20 MSa/s capture and its original Q
polarity. BLE expects the raw samples at the ESPARGOS +2 MHz intermediate
frequency. Pass the capture rate, channel and radio setting: ``phy=0`` for
LE 1M at 4, 8 or 16 MSa/s, or ``phy=1`` for LE 2M at 8 or 16 MSa/s. Use
:doc:`ble-reception` for full LE Coded decoding in the Python receiver.

Choose a Wi-Fi stage
~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 32 68

   * - Stage
     - When to use it
   * - Default
     - Decode as much of the packet as the capture supports.
   * - ``training``
     - Locate OFDM training in a raw capture.
   * - ``native_ofdm`` or ``native_dsss``
     - Check training near a sample offset you got from the modem. Give
       that offset as ``training_hint``.
   * - ``extended_header``
     - Validate the extended PHY header without decoding the payload.

Check the result
~~~~~~~~~~~~~~~~

The result is a dictionary of measurements and decoded fields, or ``None``
if qualification failed. Even a CRC-verified result contains only a
prefix of the decoded bytes. A short Wi-Fi capture can contain usable
training without the MAC header. A BLE connection packet can have a
decoded access address with an unverified CRC.

Check ``mac_valid`` for a recovered Wi-Fi transmitter address or
``identity_valid`` for a decoded BLE access address and header.
``crc_checked`` tells you whether the decoder could test the checksum,
and ``crc_valid`` tells you whether it passed.

Building from a checkout
------------------------

Initialize the decoder submodule before installing pyespargos from source.
The native extension needs a C compiler and Python development headers::

   git submodule sync -- external/espargos-packet-decoders
   git submodule update --init --recursive -- external/espargos-packet-decoders
   python -m pip install -e .

Run the installation with the same Python interpreter you use for the camera.
You can check the extension before connecting to an array::

   python -c "from espargos.packet_decoders import wifi"

See :doc:`wifi-iq-reception` and :doc:`ble-reception` for the pool receiver
APIs.
