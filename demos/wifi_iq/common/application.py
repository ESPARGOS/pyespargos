"""Qt application support for CSI estimated from WiFi packet IQ."""

import sys

import numpy as np

from espargos import BufferPolicy
from espargos.csi_backlog import CSIBacklogFilter
from demos.wifi.common.application import CSIApplication
from .receiver import WiFiIQControls


class _StreamFilter(CSIBacklogFilter):
    def __init__(self, number):
        self.number = number

    def matches(self, cluster):
        measurement = cluster.spatial_csi
        return measurement is not None and measurement.coefficients.shape[-2] >= self.number


class WiFiIQApplication(CSIApplication):
    """Configure a WiFi IQ receiver and expose its controls to the RX drawer."""

    CSI_FORMATS = ("lltf", "dsss")
    CSI_DEFAULT_CONFIG = {**CSIApplication.CSI_DEFAULT_CONFIG, "space_time_stream": 0}

    @staticmethod
    def adc_normalized_channel(csi):
        # DSSS amplitudes are fitted in ADC counts. OFDM coefficients use
        # the unnormalized transform for the displayed subcarrier grid.
        bins = csi.shape[-1]
        fft_size = 1 if bins == 35 else 256 if bins == 245 else 64
        return csi / (512 * fft_size)

    def _select_channel(self, stream):
        fields = self.backlog.fields
        previous = getattr(self, "_stream_filter", None)
        if previous is not None:
            self.backlog.remove_filter(previous)
        self._stream_filter = _StreamFilter(stream) if stream else None
        if stream:
            fields.add("spatial_csi")
            self.backlog.add_filter(self._stream_filter)
        else:
            fields.discard("spatial_csi")
        if fields != self.backlog.fields:
            self.backlog.fields = fields
        self.backlog.clear()

    def _on_csi_config_updated(self, settings):
        if "space_time_stream" in settings and hasattr(self, "backlog"):
            self._select_channel(settings["space_time_stream"])
            if hasattr(self, "_clear_measurements"):
                self._clear_measurements()
        super()._on_csi_config_updated(settings)

    def _read_channel_fields(self, additional_keys, allow_incomplete):
        stream = self.csiconfig.get("space_time_stream")
        if not stream:
            return super()._read_channel_fields(additional_keys, allow_incomplete)
        measurements, *extra = self.backlog.get_multiple(["spatial_csi", *additional_keys])
        selected = [(i, m) for i, m in enumerate(measurements) if m is not None and m.coefficients.shape[-2] >= stream]
        if not selected:
            return None
        phy = selected[-1][1].phy
        selected = [(i, m) for i, m in selected if m.phy == phy]
        # Both demos expect a regularly spaced frequency axis. Interpolate
        # between measured tones for display while leaving the SDK data intact.
        radius, spacing = (28, 312500) if phy == "HT20" else (122, 78125)
        frequencies = np.arange(-radius, radius + 1) * spacing
        channels = np.full((len(selected), *self.receiver.shape, frequencies.size), np.nan, np.complex64)
        for j, (_, measurement) in enumerate(selected):
            for pos in np.ndindex(self.receiver.shape):
                values = measurement.coefficients[pos][stream - 1]
                valid = np.isfinite(values)
                if valid.sum() >= 2:
                    channels[(j, *pos)] = np.interp(frequencies, measurement.frequencies_hz[valid], values[valid], left=np.nan, right=np.nan)
        indices = [i for i, _ in selected]
        return phy.lower(), [channels, *(field[indices] for field in extra)]

    def _extend_default_config(self, config):
        super()._extend_default_config(config)
        config["wifi_iq"] = {"channel": 6, "rx_gain": 30, "gain_mode": "manual", "boot_sync": True, "estimator": "headers"}

    def _process_args(self):
        super()._process_args()
        stream = self.initial_config["csi"]["space_time_stream"]
        if type(stream) is not int or not 0 <= stream <= 8:
            raise ValueError("csi.space_time_stream must be 0 (common preamble) or 1 through 8")
        self.initial_config["backlog"]["fields"]["spatial_csi"] = bool(stream)
        settings = self.initial_config["wifi_iq"]
        from espargos.wifi_iq.estimator import MODES

        if settings["estimator"] not in MODES:
            raise ValueError(f"wifi_iq.estimator must be one of {MODES}")
        if type(settings["boot_sync"]) is not bool:
            raise ValueError("wifi_iq.boot_sync must be a boolean")
        if settings["gain_mode"] not in ("auto", "manual"):
            raise ValueError("wifi_iq.gain_mode must be auto or manual")
        for key, choices in (("channel", range(1, 15)), ("rx_gain", range(77))):
            if type(settings[key]) is not int or settings[key] not in choices:
                raise ValueError(f"wifi_iq.{key} must be an integer from {choices.start} to {choices.stop - 1}")

    def _create_receiver(self, owner):
        # Check before reconfiguring hardware or starting the packet processing thread.
        try:
            from espargos import packet_decoders  # Loads the native extension.
        except ImportError as exc:
            raise RuntimeError("Build the packet decoders with this Python interpreter: " f"{sys.executable} -m pip install -e . (from the pyespargos checkout)") from exc
        if len(owner.boards) != 1:
            raise ValueError("The WiFi IQ drawer controls one receiving board")
        owner.configure(mode="wifi_iq")
        receiver = owner.wifi_iq
        receiver.configure_buffering(BufferPolicy(capacity=256, max_age=0.4, batch_size=32))
        self.controls = WiFiIQControls(owner, self)
        if self._uses_backlog():

            def attach():
                self.controls.attach_backlog(self.backlog)
                self._select_channel(self.csiconfig.get("space_time_stream"))

            self.initComplete.connect(attach)
        return receiver

    def _setup_qml_context(self, context):
        super()._setup_qml_context(context)
        context.setContextProperty("wifiIQControls", self.controls)

    def _calibrate_pool(self, calibrate, additional_calibrate_args):
        settings = self.get_initial_config("wifi_iq")
        self.controls.start(**settings, calibrate=calibrate).result()
        # A failed measurement leaves the calibrated backlog empty until a retry succeeds.
        return True

    def onAboutToQuit(self):
        if hasattr(self, "controls"):
            self.controls.close()
        super().onAboutToQuit()
