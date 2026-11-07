Shared packet decoders
======================

``espargos.packet_decoders`` wraps the portable LGPL-3.0-or-later C decoder
library in ``external/espargos-packet-decoders``. Build/install pyespargos to
compile its Python extension. A C compiler and Python development headers are
required. The source repository is
https://github.com/ESPARGOS/espargos-packet-decoders. Before building from a
Git checkout, initialize the pinned source with::

    git submodule sync -- external/espargos-packet-decoders
    git submodule update --init --recursive -- external/espargos-packet-decoders

For packed ESPARGOS uint32 IQ samples::

    from espargos.packet_decoders import wifi, ble

    packet = wifi(samples)
    training = wifi(samples, stage="native_ofdm", training_hint=80)
    advertisement = ble(samples, sample_rate=4_000_000, phy=0, channel=37)

Use native metadata when the radio already decoded the packet. The
``native_ofdm`` and ``native_dsss`` stages validate IQ training without decoding
MAC/header/CRC again. ``training`` locates OFDM training; ``extended_header``
validates an extended PHY header. Full decoding is bounded by the available
capture; it does not claim payload FCS validation when the capture is partial.
All stages return dictionaries, or None on failed qualification.

WiFi samples are 20 MS/s and use the raw tap's Q polarity. BLE samples use
IF +2 MHz at 4/8/16 MS/s. I and Q are signed 10-bit values in bits 0..9 and
10..19; upper debug bits are ignored. Python accepts packed words or
native-endian bytes. The C decoder releases the GIL while processing.

BLE unknown connection CRC seeds remain unverified. Coded-PHY hints can lack
an identity; they are not complete decoded packets. Check BLE
``identity_valid`` (or WiFi ``mac_valid``), ``crc_checked`` and ``crc_valid``
independently. BLE access-address identity is not a decoded WiFi MAC address.
