#!/usr/bin/env python

import PyQt6.QtCore

import espargos.util

import numpy as np
import copy

from .config_manager import ConfigManager, deep_update
from .csi_backlog_settings import CSIBacklogSettings
from .csi_pool_drawer import CSIPoolDrawer
from .espargos_application import ESPARGOSApplication


class ESPARGOSCSIApplication(ESPARGOSApplication):
    """
    ESPARGOS application whose sensing modality is WiFi channel state information (CSI).

    On top of the generic :class:`.ESPARGOSApplication`, this class provides
    the :class:`.CSIPool`, the CSI receiver-drawer backend
    (:class:`.CSIPoolDrawer`), pool calibration during initialization, and the
    :code:`csi` configuration section (preamble format selection, exposed to
    QML as the :code:`csiconfig` context property).

    Use mixins to extend functionality:
    - CSIBacklogMixin: Adds CSI backlog support
    - CombinedArrayMixin: Adds combined array configuration support
    - SingleCSIFormatMixin: Adds single preamble format selection
    """

    CSI_DEFAULT_CONFIG = {
        # CSI-specific application settings, handled by CSIAppSettings
        "preamble_format": "auto",
    }

    preambleFormatChanged = PyQt6.QtCore.pyqtSignal()

    def _extend_default_config(self, config):
        super()._extend_default_config(config)
        deep_update(config, copy.deepcopy({"pool": CSIPoolDrawer.DEFAULT_CONFIG}))
        deep_update(config, copy.deepcopy({"csi": self.CSI_DEFAULT_CONFIG}))

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

    def _create_pool(self, boards: list) -> espargos.CSIPool:
        return espargos.CSIPool(boards)

    def _create_pool_drawer(self):
        pool_cfg = self.get_explicit_initial_config("pool", default={})
        pooldrawer = CSIPoolDrawer(self.pool, pool_cfg or None, parent=self)
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
            self.pool.calibrate(
                duration=2,
                per_board=per_board,
                **additional_calibrate_args,
            )
        except espargos.CalibrationError as exc:
            error_message = str(exc)
            self.logger.error(error_message)
            self.pooldrawer.configManager().emitShowError("Initialization failed", error_message)
            return False

        return complete

    def _on_pool_calibration_started(self):
        if hasattr(self, "backlog"):
            self.backlog.calibrate = False
            self.backlog.clear()

    def _on_pool_calibration_finished(self, success: bool, _error_message: str):
        if hasattr(self, "backlog"):
            self.backlog.calibrate = bool(success and self.pool.get_calibration() is not None)
            self.backlog.clear()


class CSIBacklogMixin:
    """
    Mixin that adds CSI backlog support to an ESPARGOSCSIApplication.

    Provides:
    - Backlog size command-line argument (-b/--backlog-size)
    - CSIBacklogSettings configuration manager
    - Automatic backlog creation during pool initialization
    """

    def _extend_default_config(self, config):
        super()._extend_default_config(config)
        deep_update(config, copy.deepcopy({"backlog": CSIBacklogSettings.DEFAULT_CONFIG}))

    def _add_argparse_arguments(self, parser):
        super()._add_argparse_arguments(parser)
        parser.add_argument(
            "-b",
            "--backlog-size",
            type=int,
            default=20,
            help="Size of CSI datapoint backlog",
        )

    def _process_args(self):
        super()._process_args()
        self.initial_config["backlog"]["size"] = self.args.backlog_size

    def _init_config_managers(self):
        super()._init_config_managers()
        self.backlog_settings = CSIBacklogSettings(force_config=self.get_initial_config("backlog"), parent=self)

    def _setup_qml_context(self, context):
        super()._setup_qml_context(context)
        context.setContextProperty("backlogconfig", self.backlog_settings.configManager())

    def _finalize_pool_init(self, backlog_cb_predicate, calibrate):
        super()._finalize_pool_init(backlog_cb_predicate, calibrate)
        fields_cfg = self.get_initial_config("backlog", "fields", default=None)
        initial_fields = None
        if isinstance(fields_cfg, dict):
            initial_fields = set(espargos.CSIBacklog.FIELD_SPECS)
            for field, enabled in fields_cfg.items():
                if enabled:
                    initial_fields.add(field)
                else:
                    initial_fields.discard(field)

        self.backlog = espargos.CSIBacklog(
            self.pool,
            fields=initial_fields,
            cb_predicate=backlog_cb_predicate,
            calibrate=calibrate,
        )
        self.backlog.start()

        self.backlog_settings.set_backlog(self.backlog)


class SingleCSIFormatMixin:
    """
    Mixin that adds single preamble format selection to an ESPARGOSCSIApplication.

    Provides:
    - Mutually exclusive command-line arguments (--lltf, --ht20, --ht40, --he20)
    - Automatic backlog field configuration when combined with CSIBacklogMixin.
      Without a format command-line argument, all CSI formats are stored and
      the backlog reader resolves the concrete format automatically. Passing a
      format argument stores and reads only that CSI format.
    """

    # Formats are ordered by priority: the first format wins if sample counts tie.
    CSI_FORMATS = ("ht40", "ht20", "he20", "lltf")

    def _ensure_backlog_fields_for_preamble_format(self):
        if not self._uses_backlog() or not hasattr(self, "backlog"):
            return

        preamble_format = self.csiconfig.get("preamble_format")
        fields = set(self.backlog.get_fields())
        if preamble_format == "auto":
            fields.update(self.CSI_FORMATS)
        elif preamble_format in self.CSI_FORMATS:
            fields.add(preamble_format)
        self.backlog.set_fields(fields)

    def _configured_preamble_format(self, default: str = "lltf") -> str:
        preamble_format = self.csiconfig.get("preamble_format")
        return default if preamble_format == "auto" else preamble_format

    def _resolve_backlog_preamble_format(self, allow_incomplete: bool = False, default: str = "lltf") -> str:
        preamble_format = self.csiconfig.get("preamble_format")
        if preamble_format != "auto":
            return preamble_format
        if not hasattr(self, "backlog"):
            return default

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
        if not hasattr(self, "backlog") or not self.backlog.nonempty():
            return None

        csi_key = self._resolve_backlog_preamble_format(allow_incomplete=allow_incomplete)

        try:
            results = list(self.backlog.get_multiple([csi_key, *additional_keys]))
        except ValueError:
            print(f"Requested CSI key {csi_key} not in backlog")
            return None

        csi_backlog = results[0]

        # Apply STO removal if requested
        if remove_global_sto:
            espargos.util.remove_mean_sto(csi_backlog)

        # Interpolate DC subcarrier gap for HT formats
        if csi_key == "ht40":
            espargos.util.interpolate_ht40ltf_gap(csi_backlog)
        elif csi_key == "ht20":
            espargos.util.interpolate_ht20ltf_gap(csi_backlog)
        elif csi_key == "he20":
            espargos.util.interpolate_he20ltf_gaps(csi_backlog)

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

    def _add_argparse_arguments(self, parser):
        super()._add_argparse_arguments(parser)
        format_group = parser.add_mutually_exclusive_group()
        format_group.add_argument(
            "--lltf",
            default=False,
            help="Store and read only CSI from L-LTF",
            action="store_true",
        )
        format_group.add_argument(
            "--ht40",
            default=False,
            help="Store and read only CSI from HT40",
            action="store_true",
        )
        format_group.add_argument(
            "--ht20",
            default=False,
            help="Store and read only CSI from HT20",
            action="store_true",
        )
        format_group.add_argument(
            "--he20",
            default=False,
            help="Store and read only CSI from HE20",
            action="store_true",
        )

    def _process_args(self):
        super()._process_args()

        # Make sure that only one format is selected
        selected_formats = []
        if self.args.lltf:
            selected_formats.append("lltf")
        if self.args.ht40:
            selected_formats.append("ht40")
        if self.args.ht20:
            selected_formats.append("ht20")
        if self.args.he20:
            selected_formats.append("he20")
        if len(selected_formats) > 1:
            raise ValueError("At most one of --lltf, --ht40, --ht20 or --he20 can be selected!")

        if len(selected_formats) == 1:
            # Format flags explicitly narrow both backlog storage and readback.
            self.initial_config["csi"]["preamble_format"] = selected_formats[0]
            if self._uses_backlog():
                self.initial_config["backlog"]["fields"] = {
                    "lltf": self.args.lltf,
                    "ht20": self.args.ht20,
                    "ht40": self.args.ht40,
                    "he20": self.args.he20,
                }
        elif self._uses_backlog():
            # No format flag means Auto readback with all CSI formats available.
            self.initial_config["csi"]["preamble_format"] = "auto"
            for fmt in self.CSI_FORMATS:
                self.initial_config["backlog"]["fields"][fmt] = True
