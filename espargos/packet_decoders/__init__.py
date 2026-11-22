"""WiFi and BLE waveform decoders bundled with pyespargos."""

try:
    from _pyespargos_packet_decoders import STAGES, ble, dsss_symbols, wifi
except ModuleNotFoundError as exc:
    if exc.name != "_pyespargos_packet_decoders":
        raise
    # A source checkout with an in-place extension has no installed package mapping.
    from pathlib import Path

    __path__.append(str(Path(__file__).resolve().parents[2] / "external/espargos-packet-decoders/python"))
    from .espargos_packet_decoders import STAGES, ble, dsss_symbols, wifi

__all__ = ["STAGES", "ble", "dsss_symbols", "wifi"]
