#!/usr/bin/env python3

import PyQt6.QtWidgets
import PyQt6.QtCore

import threading

import argparse
import yaml

import espargos.util

from .pool_drawer import PoolDrawer

class DemoApplication(PyQt6.QtWidgets.QApplication):
    initComplete = PyQt6.QtCore.pyqtSignal()

    def __init__(self, argv):
        super().__init__(argv)

        # Not ready yet
        self.ready = False

        # Add parent argument parser
        self.common_args = argparse.ArgumentParser(add_help = False)
        self.common_args.add_argument("-c", "--config", type = str, default = None, help = "Path to YAML configuration file to load")

        self.config_path = None
        self.initial_config = {}

    def parse_args(self, parser: argparse.ArgumentParser):
        args = parser.parse_args()
        self.config_path = getattr(args, "config", None)
        self.initial_config = self._load_config(self.config_path)
        return args

    def _load_config(self, config_path: str):
        if not config_path:
            return {}

        with open(config_path, "r") as config_file:
            data = yaml.safe_load(config_file) or {}

        if not isinstance(data, dict):
            raise ValueError("Config file must contain a YAML object at the root")

        return data

    def get_initial_config(self, *keys, default=None):
        for key in keys:
            if key in self.initial_config and isinstance(self.initial_config[key], dict):
                return self.initial_config[key]
        return default
    
    def initialize_pool(self, hosts, enable_backlog = False, backlog_cb_predicate = None, additional_calibrate_args = {}, calibrate = True):
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
                    self.backlog = espargos.CSIBacklog(self.pool, size = self.args.backlog, enable_lltf = self.args.lltf,  enable_ht40 = self.args.ht40, enable_ht20 = self.args.ht20, cb_predicate = backlog_cb_predicate, calibrate = calibrate)
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

    @PyQt6.QtCore.pyqtProperty(bool, constant=False, notify=initComplete)
    def initializing(self):
        return not self.ready