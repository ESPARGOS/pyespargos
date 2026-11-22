#!/usr/bin/env python

import PyQt6.QtCore

import espargos.csi_processing

import numpy as np
import copy

from demos.common.config_manager import ConfigManager, deep_update
from demos.wifi.common.csi_backlog_settings import CSIBacklogSettings
from demos.wifi.common.wifi_transceiver_drawer import WiFiTransceiverDrawer
from demos.common.application import ESPARGOSApplication


class CSIApplication(ESPARGOSApplication):
    """Shared configuration and format selection for applications that consume CSI."""

    CSI_DEFAULT_CONFIG = {
        # CSI-specific application settings, handled by CSIAppSettings
        "preamble_format": "auto",
    }

    preambleFormatChanged = PyQt6.QtCore.pyqtSignal()

    def _extend_default_config(self, config):
        super()._extend_default_config(config)
        deep_update(config, copy.deepcopy({"csi": self.CSI_DEFAULT_CONFIG}))

    def _read_channel_fields(self, additional_keys, allow_incomplete):
        key = self._resolve_backlog_preamble_format(allow_incomplete=allow_incomplete)
        if key == "ht40" and self.pooldrawer.configManager().get("secondary_channel") == 0:
            self.backlog.clear()
            return None
        return key, list(self.backlog.get_multiple([key, *additional_keys]))

    def _uses_backlog(self):
        """Check if this class uses the CSIBacklogMixin."""
        return isinstance(self, CSIBacklogMixin)

    def _uses_single_csi_format(self):
        """Check if this class uses the SingleCSIFormatMixin."""
        return isinstance(self, SingleCSIFormatMixin)

    def _init_config_managers(self):
        super()._init_config_managers()
        self.csiconfig = ConfigManager(self.get_initial_config("csi"), parent=self)
        self.csiconfig.updateAppState.connect(self._on_csi_config_updated)

    def _on_csi_config_updated(self, newcfg):
        """
        Handler for CSI configuration changes.
        Override in subclasses to handle specific config changes, then call super()._on_csi_config_updated(newcfg).
        """
        if "preamble_format" in newcfg:
            if self._uses_single_csi_format():
                self._ensure_backlog_fields_for_preamble_format()
            self.preambleFormatChanged.emit()
        self.csiconfig.updateAppStateHandled.emit()

    def _setup_qml_context(self, context):
        super()._setup_qml_context(context)
        context.setContextProperty("csiconfig", self.csiconfig)


class ESPARGOSCSIApplication(CSIApplication):
    """Native WiFi CSI acquisition, receiver controls and calibration."""

    # Formats are ordered by priority: the first format wins if sample counts tie.
    CSI_FORMATS = ("ht40", "ht20", "he20", "lltf")

    def _extend_default_config(self, config):
        super()._extend_default_config(config)
        deep_update(config, copy.deepcopy({"pool": WiFiTransceiverDrawer.DEFAULT_CONFIG}))

    def _create_receiver(self, owner: espargos.Pool) -> espargos.WiFiTransceiver:
        # A previous IQ demo may have left sensors outside WiFi mode.
        for board in owner.boards:
            if board.api_version >= (4, 4):
                board.control.post_json("set_controller_mode", {"mode": "wifi"})
            if board.api_version[0] >= 4:
                owner.select(boards=board.host).configure(mode="wifi")
                # These demos use one channel per board. Clear overrides left
                # by an application using independently tuned antennas.
                board.wifi_rx.set_channel_overrides({"override_active": False})
        pool = owner.wifi
        pool.gain_phase_compensation = bool(self.get_initial_config("pool", "gain_phase_compensation", default=True))
        return pool

    def _create_pool_drawer(self):
        explicit = self.get_explicit_initial_config("pool", default={})
        pool_cfg = {key: value for key, value in explicit.items() if key in WiFiTransceiverDrawer.DEFAULT_CONFIG}
        pooldrawer = WiFiTransceiverDrawer(self.pool, pool_cfg or None, parent=self)
        pooldrawer.calibrationStarted.connect(self._on_pool_calibration_started)
        pooldrawer.calibrationFinished.connect(self._on_pool_calibration_finished)
        return pooldrawer

    def _calibrate_pool(self, calibrate: bool, additional_calibrate_args: dict) -> bool:
        complete = super()._calibrate_pool(calibrate=calibrate, additional_calibrate_args=additional_calibrate_args)
        if not calibrate:
            return complete

        try:
            per_board = bool(
                self.pooldrawer.configManager().get(
                    "calibration",
                    "per_board",
                )
            )
            self.pool.calibrate(duration=2, per_board=per_board)
        except espargos.CalibrationError as exc:
            error_message = str(exc)
            self.logger.error(error_message)
            self.pooldrawer.configManager().emitShowError("Initialization failed", error_message)
            return False

        return complete

    def _on_pool_calibration_started(self):
        if hasattr(self, "backlog"):
            self.backlog.apply_calibration = False
            self.backlog.clear()

    def _on_pool_calibration_finished(self, success: bool, _error_message: str):
        if hasattr(self, "backlog"):
            self.backlog.apply_calibration = bool(success and self.receiver.calibration is not None)
            self.backlog.clear()


class CSIBacklogMixin:
    """Create the CSI backlog and expose its settings to the demo."""

    def _extend_default_config(self, config):
        super()._extend_default_config(config)
        config["backlog"] = {
            "size": 20,
            "fields": {name: name in ("lltf", "dsss") for name in self.CSI_FORMATS},
            "filters": {"exclude_11b": "dsss" not in self.CSI_FORMATS},
        }

    def _init_config_managers(self):
        super()._init_config_managers()
        self.backlog_settings = CSIBacklogSettings(self.get_initial_config("backlog"), parent=self)

    def _setup_qml_context(self, context):
        super()._setup_qml_context(context)
        context.setContextProperty("backlogconfig", self.backlog_settings.configManager())

    def _finalize_pool_init(self, backlog_cb_predicate, calibrate):
        super()._finalize_pool_init(backlog_cb_predicate, calibrate)
        config = self.get_initial_config("backlog")
        available = espargos.CSIBacklog.fields_for(self.receiver)
        defaults = espargos.CSIBacklog.default_fields_for(self.receiver)
        self.backlog = espargos.CSIBacklog(
            self.receiver,
            fields={field for field in available if config["fields"].get(field, field in defaults)},
            size=config["size"],
            callback_predicate=backlog_cb_predicate,
            apply_calibration=calibrate,
        )
        self.backlog_settings.set_backlog(self.backlog)
        self.pool.start_processing()


class SingleCSIFormatMixin:
    """Read the configured CSI format or select one from the available measurements."""

    def _ensure_backlog_fields_for_preamble_format(self):
        if not self._uses_backlog() or not hasattr(self, "backlog"):
            return

        preamble_format = self.csiconfig.get("preamble_format")
        fields = set(self.backlog.fields)
        if preamble_format == "auto":
            fields.update(self.CSI_FORMATS)
        elif preamble_format in self.CSI_FORMATS:
            fields.add(preamble_format)
        fields.intersection_update(self.backlog.available_fields)
        if fields != self.backlog.fields:
            self.backlog.fields = fields

    def _configured_preamble_format(self, default: str = "lltf") -> str:
        preamble_format = self.csiconfig.get("preamble_format")
        return default if preamble_format == "auto" else preamble_format

    def _resolve_backlog_preamble_format(self, allow_incomplete: bool = False, default: str = "lltf") -> str:
        preamble_format = self.csiconfig.get("preamble_format")
        if preamble_format != "auto":
            return preamble_format
        if not hasattr(self, "backlog"):
            return default

        if "dsss" in self.CSI_FORMATS and "dsss" in self.backlog.fields:
            latest = self.backlog.get_latest("dsss")
            return "dsss" if latest is not None and np.isfinite(latest).any() else "lltf"

        counts = {}
        for fmt in self.CSI_FORMATS:
            try:
                count = self.backlog.count_valid_datapoints(fmt, allow_incomplete=allow_incomplete)
            except ValueError:
                count = 0
            counts[fmt] = count

        best_format = max(self.CSI_FORMATS, key=counts.get)
        return best_format if counts[best_format] > 0 else default

    def get_backlog_csi(self, *additional_keys: str, allow_incomplete: bool = False, remove_global_sto=True, return_format: bool = False) -> np.ndarray | tuple[np.ndarray, ...] | None:
        """
        Retrieve latest CSI datapoints from backlog for the selected preamble format.

        Returns None if backlog does not exist (yet), is empty, or data is otherwise unavailable.
        Automatically interpolates gaps for HT20/HT40 formats.

        :param additional_keys: Additional backlog keys to retrieve alongside the CSI data.
        :param allow_incomplete: If True, keep CSI datapoints with at least one antenna with valid CSI and
            leave missing antennas as NaN, instead of rejecting the whole backlog request.
            Additional returned backlog fields are filtered to the same datapoints. Useful when
            the CSI cluster predicate can create incomplete clusters (timeout or accepting non-full completion state).
        :param remove_global_sto: If True, remove global STO from CSI data by re-centering the cluster on the mean STO.
            This should be true unless you want to do processing across multiple subsequent CSI datapoints where the global STO would be relevant.
        :param return_format: If true, include the concrete preamble format used
            for this readback as the first returned tuple element.
        :return: CSI array if no additional keys, tuple of (csi, *additional) if keys specified,
                 tuple of (format, csi, *additional) if return_format is true, or None if unavailable.
        """
        if not hasattr(self, "backlog") or not self.backlog.has_data:
            return None

        try:
            snapshot = self._read_channel_fields(additional_keys, allow_incomplete)
        except ValueError as exc:
            self.logger.warning("Cannot read CSI backlog: %s", exc)
            return None
        if snapshot is None:
            return None
        csi_key, results = snapshot

        csi_backlog = results[0]

        # Apply STO removal if requested
        if remove_global_sto:
            espargos.csi_processing.remove_mean_sto(csi_backlog)

        # Interpolate DC subcarrier gap for HT formats
        if csi_key == "ht40":
            espargos.csi_processing.interpolate_ht40ltf_gap(csi_backlog)
        elif csi_key == "ht20":
            espargos.csi_processing.interpolate_ht20ltf_gap(csi_backlog)
        elif csi_key == "he20":
            espargos.csi_processing.interpolate_he20ltf_gaps(csi_backlog)

        # Handle data containing NaN values (from incomplete CSI clusters)
        if np.any(np.isnan(csi_backlog)):
            if allow_incomplete:
                valid_datapoints = np.any(np.isfinite(csi_backlog.reshape(csi_backlog.shape[0], -1)), axis=1)
                if not np.any(valid_datapoints):
                    return None
                results = [result[valid_datapoints] for result in results]
                csi_backlog = results[0]
            else:
                return None

        if return_format:
            return (csi_key, *results)
        if additional_keys:
            return tuple(results)
        return csi_backlog

    def _process_args(self):
        super()._process_args()
        selected = self.initial_config["csi"]["preamble_format"]
        if len(self.CSI_FORMATS) == 1:
            selected = self.CSI_FORMATS[0]
        if selected != "auto" and selected not in self.CSI_FORMATS:
            raise ValueError(f"Unsupported CSI format {selected!r}")
        self.initial_config["csi"]["preamble_format"] = selected
        if self._uses_backlog():
            self.initial_config["backlog"]["fields"].update(
                {name: selected == "auto" or name == selected for name in self.CSI_FORMATS}
            )
