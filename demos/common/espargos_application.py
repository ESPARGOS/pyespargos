#!/usr/bin/env python

import PyQt6.QtWidgets
import PyQt6.QtCore
import PyQt6.QtQml

import espargos.util

import subprocess
import threading
import argparse
import copy
import yaml
import logging

from .config_manager import ConfigManager, deep_update


class ESPARGOSApplication(PyQt6.QtWidgets.QApplication):
    """
    Generic base class for ESPARGOS demo applications.

    This class provides the functionality shared by all ESPARGOS demos:
    - Command-line argument parsing (config file support)
    - YAML configuration loading
    - QML engine initialization
    - Pool lifecycle management (creation, start-up, calibration, teardown)

    The concrete sensing modality is supplied by a subclass such as
    :class:`.ESPARGOSCSIApplication`, which implements the pool factory
    (:meth:`_create_pool`) and may provide a receiver-drawer backend
    (:meth:`_create_pool_drawer`) and a calibration procedure
    (:meth:`_calibrate_pool`).

    Optional features are added through cooperative mixins that a demo
    combines as needed, for example :class:`CombinedArrayMixin` or the
    CSI-specific mixins from :mod:`.csi_application`.
    """

    BASE_DEFAULT_CONFIG = {
        "pool": {
            # Pool configuration settings; "hosts" is generic, additional keys
            # are contributed by the modality subclass and its drawer backend
            "hosts": []
        },
        "combined-array": {
            # Combined array configuration settings
        },
        "generic": {
            # Generic application settings
            "kiosk_mode": False,
        },
    }

    initComplete = PyQt6.QtCore.pyqtSignal()
    appConfigChanged = PyQt6.QtCore.pyqtSignal(dict)  # Emitted when app config changes, with the changed keys

    DEFAULT_CONFIG = {}  # Override in subclasses to provide app-specific defaults

    def _default_config_template(self) -> dict:
        config = copy.deepcopy(self.BASE_DEFAULT_CONFIG)
        self._extend_default_config(config)
        # The 'app' section is reserved for application-specific settings
        deep_update(config, copy.deepcopy({"app": getattr(self, "DEFAULT_CONFIG", {})}))
        return config

    def _extend_default_config(self, config: dict):
        """
        Hook for modality subclasses and mixins to contribute their default configuration.

        Override cooperatively: call super()._extend_default_config(config) first, then
        deep_update(config, ...) with a deep copy of the contributed defaults.
        """
        pass

    @staticmethod
    def _format_config_value(value):
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return repr(value)
        return repr(value)

    def _format_config_options(self, config: dict, prefix: str = "", indent: int = 2):
        lines = []
        for key, value in config.items():
            path = f"{prefix}.{key}" if prefix else key
            spacing = " " * indent
            if isinstance(value, dict) and value:
                lines.append(f"{spacing}{key}:")
                lines.extend(self._format_config_options(value, path, indent + 2))
            else:
                default_text = self._format_config_value(value)
                lines.append(f"{spacing}{path}: default: {default_text}")
        return lines

    def _argparse_epilog(self) -> str:
        return "Configuration options for -o/--option (as CLI arguments) or -c/--config (as YAML file):\n" + "\n".join(self._format_config_options(self._default_config_template()))

    def __init__(
        self,
        argv: list[str],
        argparse_parent: argparse.ArgumentParser = None,
    ):
        super().__init__(argv)

        # Basic app initialization
        self.ready = False
        self.engine = PyQt6.QtQml.QQmlApplicationEngine()
        self.logger = logging.getLogger("demo.Application")
        self.aboutToQuit.connect(self.onAboutToQuit)
        self._qml_ok = True

        # Parse command-line arguments
        parser = argparse.ArgumentParser(
            parents=[argparse_parent] if argparse_parent else [],
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=self._argparse_epilog(),
        )
        parser.add_argument(
            "-c",
            "--config",
            type=str,
            default=None,
            help="Path to YAML configuration file to load",
        )
        parser.add_argument(
            "-o",
            "--option",
            action="append",
            default=[],
            metavar="KEY=VALUE",
            help="Override a configuration option, e.g. -o generic.kiosk_mode=True, multiple -o options are allowed (overrides config file and defaults)",
        )

        # Let mixins add their arguments
        self._add_argparse_arguments(parser)

        self.args = parser.parse_args()

        # Load initial configuration if provided
        self.config_path = None
        self.initial_config = self._default_config_template()
        self.explicit_initial_config = {}
        if self.args.config is not None:
            with open(self.args.config, "r") as config_file:
                cfg_from_file = yaml.safe_load(config_file)

                if not isinstance(cfg_from_file, dict):
                    raise ValueError("Config file must contain a YAML object at the root")

                deep_update(self.initial_config, cfg_from_file)
                deep_update(self.explicit_initial_config, cfg_from_file)

        # Apply command-line option overrides (-o key=value)
        for opt in self.args.option:
            if "=" not in opt:
                raise ValueError(f"Invalid option format '{opt}', expected KEY=VALUE")
            key, value = opt.split("=", 1)
            value = yaml.safe_load(value)  # Parse value as YAML to support int, float, bool, etc.
            parts = key.split(".")
            nested = {}
            current = nested
            for part in parts[:-1]:
                current[part] = {}
                current = current[part]
            current[parts[-1]] = value
            print(f"Overriding config option '{key}' with value: {value}")
            deep_update(self.initial_config, nested)
            deep_update(self.explicit_initial_config, nested)

        # Let mixins process their arguments and update config
        self._process_args()

        # Configuration managers
        self._init_config_managers()

        self.genericconfig = ConfigManager(self.get_initial_config("generic"), parent=self)
        self.genericconfig.updateAppState.connect(self._on_generic_config_updated)

        # App configuration manager (uses DEFAULT_CONFIG from subclass)
        self.appconfig = ConfigManager(self.get_initial_config("app"), parent=self)
        self.appconfig.updateAppState.connect(self._on_update_app_state)

    def _on_update_app_state(self, newcfg):
        """
        Handler for app configuration changes.
        Override in subclasses to handle specific config changes, then call super()._on_update_app_state(newcfg).
        """
        self.appConfigChanged.emit(newcfg)
        self.appconfig.updateAppStateHandled.emit()

    def _on_generic_config_updated(self, newcfg):
        """
        Handler for generic configuration changes.
        Override in subclasses to handle specific config changes, then call super()._on_generic_config_updated(newcfg).
        """
        self.genericconfig.updateAppStateHandled.emit()

    def _add_argparse_arguments(self, parser):
        """
        Hook for mixins to add command-line arguments.
        Override in mixins and call super()._add_argparse_arguments(parser).
        """
        # Base class adds hosts argument by default (non-combined-array mode)
        if not self._uses_combined_array():
            parser.add_argument(
                "hosts",
                type=str,
                default="",
                help="Comma-separated list of host addresses (IP or hostname) of ESPARGOS devices",
            )

    def _uses_combined_array(self):
        """Check if this class uses the CombinedArrayMixin."""
        return isinstance(self, CombinedArrayMixin)

    def _process_args(self):
        """
        Hook for mixins to process command-line arguments.
        Override in mixins and call super()._process_args().
        """
        # If hosts are provided on command line, override pool config
        if hasattr(self.args, "hosts") and len(self.args.hosts) > 0:
            hosts = self.args.hosts.split(",")
            self.initial_config["pool"]["hosts"] = hosts

    def _init_config_managers(self):
        """
        Hook for mixins to initialize their config managers.
        Override in mixins and call super()._init_config_managers().
        """
        pass

    def get_initial_config(self, *path, default=None):
        """
        Retrieve initial configuration value for given path.

        Examples:
            * self.get_initial_config("pool", "hosts", default=[])
            * self.get_initial_config("combined-array")

        Path can be either a sequence of keys, or a single key.
        """
        cfg = self.initial_config
        for key in path if isinstance(path, tuple) else (path,):
            if key in cfg:
                cfg = cfg[key]
            else:
                return default

        return cfg

    def get_explicit_initial_config(self, *path, default=None):
        cfg = self.explicit_initial_config
        for key in path if isinstance(path, tuple) else (path,):
            if key in cfg:
                cfg = cfg[key]
            else:
                return default

        return cfg

    def initialize_qml(self, qml_file, context_props: dict | None = None):
        context = self.engine.rootContext()

        # Let mixins set up their context properties
        self._setup_qml_context(context)

        # Generic app config manager
        context.setContextProperty("genericconfig", self.genericconfig)

        # Provide backend and optional additional context properties
        context.setContextProperty("backend", self)
        context.setContextProperty("appconfig", self.appconfig)
        if getattr(self, "pooldrawer", None) is not None:
            context.setContextProperty("poolconfig", self.pooldrawer.configManager())

        for key, value in (context_props or {}).items():
            if key not in ("backend", "appconfig"):
                context.setContextProperty(key, value)

        qml_url = qml_file.as_uri() if hasattr(qml_file, "as_uri") else qml_file
        self.engine.load(qml_url)

    def _setup_qml_context(self, context):
        """
        Hook for mixins to set up QML context properties.
        Override in mixins and call super()._setup_qml_context(context).
        """
        pass

    def initialize_pool(
        self,
        backlog_cb_predicate=None,
        additional_calibrate_args: dict = {},
        calibrate: bool = True,
    ):
        """
        Initialize the ESPARGOS pool for this application's sensing modality.

        Creates the boards and the modality's pool (:meth:`_create_pool`) and,
        if the modality provides one, its receiver-drawer backend
        (:meth:`_create_pool_drawer`). On a background thread, the pool is
        then started and calibrated (:meth:`_calibrate_pool`); once the
        mixins' :meth:`_finalize_pool_init` hooks have completed as well, the
        application becomes ready and emits :code:`initComplete`.
        """
        # Let mixins prepare pool initialization
        additional_calibrate_args = self._prepare_pool_init(additional_calibrate_args)

        self.pool = self._create_pool([espargos.Board(host) for host in self.get_initial_config("pool", "hosts")])
        self.pooldrawer = self._create_pool_drawer()

        def config_applied():
            def _init_worker():
                self.pool.start()
                complete = self._calibrate_pool(calibrate=calibrate, additional_calibrate_args=additional_calibrate_args)

                # Let mixins finalize pool initialization
                self._finalize_pool_init(backlog_cb_predicate, calibrate and complete)

                self.ready = True
                self.initComplete.emit()

            threading.Thread(target=_init_worker, daemon=True).start()

        # If the modality provides a drawer backend, wait for its initial
        # configuration to be applied before starting the pool.
        if self.pooldrawer is not None:
            self.pooldrawer.initComplete.connect(config_applied)
        else:
            config_applied()

    def _create_pool(self, boards: list) -> espargos.Pool:
        """
        Create this application's pool over the given boards.

        Must be implemented by a modality subclass such as
        :class:`.ESPARGOSCSIApplication`.
        """
        raise NotImplementedError("ESPARGOSApplication does not implement a sensing modality; subclass e.g. ESPARGOSCSIApplication instead")

    def _create_pool_drawer(self):
        """
        Create the receiver-drawer backend for this application's modality, or return None.

        A returned backend must provide a :code:`configManager()` accessor and
        an :code:`initComplete` signal that fires once its initial
        configuration has been applied; pool start-up and calibration are
        deferred until then.
        """
        return None

    def _calibrate_pool(self, calibrate: bool, additional_calibrate_args: dict) -> bool:
        """
        Perform the modality's calibration procedure on a background thread.

        Called after sensor-message reception has been started, before the
        application becomes ready. The default implementation does nothing.
        Modality subclasses override this with their calibration procedure and
        return False if the pool came up in a degraded state (calibration
        failed); feature mixins then finalize without calibration.
        """
        return True

    def _prepare_pool_init(self, additional_calibrate_args):
        """
        Hook for mixins to prepare pool initialization.
        Override in mixins and call super()._prepare_pool_init(additional_calibrate_args).
        Returns the (possibly modified) additional_calibrate_args.
        """
        return additional_calibrate_args

    def _finalize_pool_init(self, backlog_cb_predicate, calibrate):
        """
        Hook for mixins to finalize pool initialization.
        Override in mixins and call super()._finalize_pool_init(backlog_cb_predicate, calibrate).
        """
        pass

    def onAboutToQuit(self):
        if hasattr(self, "backlog"):
            self.backlog.close()
        if hasattr(self, "pool"):
            self.pool.stop()
        if hasattr(self, "engine"):
            self.engine.deleteLater()

    def exec(self):
        if not self._qml_ok:
            return -1
        return super().exec()

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=initComplete)
    def initializing(self):
        return not self.ready

    @PyQt6.QtCore.pyqtProperty(object, constant=False, notify=initComplete)
    def hasBacklog(self):
        return hasattr(self, "backlog")

    @PyQt6.QtCore.pyqtProperty(bool, constant=True)
    def kioskMode(self):
        return bool(self.genericconfig.get("kiosk_mode"))

    @PyQt6.QtCore.pyqtSlot()
    def shutdownComputer(self):
        self.onAboutToQuit()
        subprocess.Popen(["systemctl", "poweroff"])
        self.quit()


class CombinedArrayMixin:
    """
    Mixin that adds combined array configuration support to an ESPARGOSApplication.

    Provides:
    - Single-array command-line argument (-s/--single-array)
    - Combined array configuration parsing
    - Automatic host extraction from combined array config
    - Cable length calibration support
    """

    def _add_argparse_arguments(self, parser):
        super()._add_argparse_arguments(parser)
        parser.add_argument(
            "-s",
            "--single-array",
            type=str,
            default="",
            help="Array consists of a single ESPARGOS device in horizontal orientation, specify host address (IP or hostname) of device",
        )

    def _process_args(self):
        super()._process_args()
        # In single-array mode, auto-generate combined array config
        if hasattr(self.args, "single_array") and len(self.args.single_array) > 0:
            host = self.args.single_array

            self.initial_config["combined-array"] = {
                "boards": {
                    "arr": {
                        "host": host,
                        "cable": {
                            "length": 0.0,
                            "velocity_factor": 1.0,
                        },
                    }
                },
                "array": [
                    ["arr.0.0", "arr.0.1", "arr.0.2", "arr.0.3"],
                    ["arr.1.0", "arr.1.1", "arr.1.2", "arr.1.3"],
                ],
            }

    def _prepare_pool_init(self, additional_calibrate_args):
        additional_calibrate_args = super()._prepare_pool_init(additional_calibrate_args)

        combined_array_cfg = self.get_initial_config("combined-array")

        # Check if combined_array_cfg was provided (not just empty dictionary)
        if combined_array_cfg is None or combined_array_cfg == {}:
            raise ValueError("Combined array configuration is required for applications using CombinedArrayMixin. You must either provide it via config file or use the --single-array option.")

        (
            self.indexing_matrix,
            hosts,
            cable_lengths,
            cable_velocity_factors,
            self.n_rows,
            self.n_cols,
            self.antenna_orientations,
        ) = espargos.util.parse_combined_array_config(combined_array_cfg)

        additional_calibrate_args["cable_lengths"] = cable_lengths
        additional_calibrate_args["cable_velocity_factors"] = cable_velocity_factors
        self.initial_config["pool"]["hosts"] = list(hosts.values())

        return additional_calibrate_args
