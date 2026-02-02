#!/usr/bin/env python3

import PyQt6.QtWidgets
import PyQt6.QtCore

import threading
import argparse
import yaml
import espargos.util
import PyQt6.QtQml

from .pool_drawer import PoolDrawer


class DemoApplication(PyQt6.QtWidgets.QApplication):
    initComplete = PyQt6.QtCore.pyqtSignal()

    def __init__(self, argv : list[str], argparse_parent : argparse.ArgumentParser = None):
        super().__init__(argv)

        # Basic app initialization
        self.ready = False
        self.engine = PyQt6.QtQml.QQmlApplicationEngine()
        self.aboutToQuit.connect(self.onAboutToQuit)
        self._qml_ok = True

        # Parse command-line arguments
        parser = argparse.ArgumentParser(parents = [argparse_parent] if argparse_parent else [])
        parser.add_argument("-c", "--config", type = str, default = None, help = "Path to YAML configuration file to load")
        self.args = parser.parse_args()

        # Load initial configuration if provided
        self.config_path = None
        self.initial_config = dict()
        if self.args.config is not None:
            with open(self.args.config, "r") as config_file:
                self.initial_config = yaml.safe_load(config_file) or {}

            if not isinstance(self.initial_config, dict):
                raise ValueError("Config file must contain a YAML object at the root")

    def get_initial_config(self, *keys, default=None):
        for key in keys:
            if key in self.initial_config and isinstance(self.initial_config[key], dict):
                return self.initial_config[key]
        return default

    def init_qml(self, qml_file, context_props: dict | None = None):
        context = self.engine.rootContext()

        # Provide backend and optional additional context properties
        context.setContextProperty("backend", self)
        if hasattr(self, "pooldrawer"):
            context.setContextProperty("poolconfig", self.pooldrawer.configManager())

        for key, value in (context_props or {}).items():
            if key != "backend":
                context.setContextProperty(key, value)

        qml_url = qml_file.as_uri() if hasattr(qml_file, "as_uri") else qml_file
        self.engine.load(qml_url)

    def initialize_pool(
            self,
            hosts : list[str],
            enable_backlog : bool = False,
            backlog_cb_predicate = None,
            additional_calibrate_args : dict = {},
            calibrate : bool = True):
        """
        Initialize ESPARGOS Pool for combined-array setups.
        Also triggers creation of pool drawer.

        enable_backlog: If True, also set up CSI backlog after pool initialization.
        """
        # Setup ESPARGOS pool for combined array setup and calibrate
        self.pool = espargos.Pool([espargos.Board(host) for host in hosts])

        # Pool configuration UI
        pool_cfg = self.get_initial_config("pool", default = {})
        self.pooldrawer = PoolDrawer(self.pool, pool_cfg, parent = self)

        # Wait for config to be applied before starting pool and calibration
        def config_applied():
            def _init_worker():
                self.pool.start()
                if calibrate:
                    self.pool.calibrate(duration = 2, per_board = False, **additional_calibrate_args)

                if enable_backlog:
                    # TODO: do not rely on self.args and self._cb_predicate here
                    enable = ["rssi", "timestamp", "host_timestamp", "mac"]
                    if self.args.lltf:
                        enable.append("lltf")
                    if self.args.ht40:
                        enable.append("ht40")
                    if self.args.ht20:
                        enable.append("ht20")

                    self.backlog = espargos.CSIBacklog(self.pool, size = self.args.backlog, enable = enable, cb_predicate = backlog_cb_predicate, calibrate = calibrate)
                    self.backlog.start()

                self.ready = True
                self.initComplete.emit()

            threading.Thread(target=_init_worker, daemon=True).start()

        self.pooldrawer.initComplete.connect(config_applied)

    # Helper functions for common initialization tasks
    def initialize_combined_array(self, **kwargs):
        """
        Initialize ESPARGOS Pool for combined-array setups.
        Also triggers creation of pool drawer.

        backlog: If True, also set up CSI backlog after pool initialization.
        """
        # Ensure that initial config is not empty
        if not self.initial_config:
            raise ValueError("Configuration for combined-array setups must be provided for combined-array setups!")

        # Load config file
        self.indexing_matrix, self.board_names_hosts, self.cable_lengths, self.cable_velocity_factors, self.n_rows, self.n_cols = espargos.util.parse_combined_array_config(self.get_initial_config("combined-array"))

        # Set up ESPARGOS pool and backlog
        self.initialize_pool(list(self.board_names_hosts.values()), **kwargs, additional_calibrate_args = {
            "cable_lengths": self.cable_lengths,
            "cable_velocity_factors": self.cable_velocity_factors
        })

    def onAboutToQuit(self):
        self.pool.stop()
        if hasattr(self, "backlog"):
            self.backlog.stop()
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