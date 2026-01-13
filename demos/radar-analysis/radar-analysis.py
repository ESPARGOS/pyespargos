#!/usr/bin/env python3

#!/usr/bin/env python3

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).absolute().parents[2]))

import numpy as np
import espargos
import argparse

import PyQt6.QtWidgets
import PyQt6.QtCore

class EspargosDemoRadarAnalysis(PyQt6.QtWidgets.QApplication):
    def __init__(self, argv):
        super().__init__(argv)

        # Parse command line arguments
        parser = argparse.ArgumentParser(description = "ESPARGOS Demo: Show instantaneous CSI over subcarrier index (single board)")
        parser.add_argument("hosts", type = str, help = "Comma-separated list of host addresses (IP or hostname) of ESPARGOS controllers")
        format_group = parser.add_mutually_exclusive_group()
        format_group.add_argument("-l", "--lltf", default = False, help = "Use only CSI from L-LTF", action = "store_true")
        format_group.add_argument("-ht40", "--ht40", default = False, help = "Use only CSI from HT40", action = "store_true")
        format_group.add_argument("-ht20", "--ht20", default = False, help = "Use only CSI from HT20", action = "store_true")
        self.args = parser.parse_args()

        # Check if at least one format is selected
        if not (self.args.lltf or self.args.ht40 or self.args.ht20):
            print("Error: At least one of --lltf, --ht40 or --ht20 must be selected.")
            sys.exit(1)

        # Set up ESPARGOS pool and backlog
        hosts = self.args.hosts.split(",")
        self.pool = espargos.Pool([espargos.Board(host) for host in hosts])
        self.pool.start()
        def is_complete(csi_completion_state, csi_age):
            # Seven complete sensors is sufficient
            return np.sum(csi_completion_state) >= 6
        self.pool.add_csi_callback(self.onCSI, cb_predicate=is_complete)

        # Thread to continuously poll the pool
        self.poll_timer = PyQt6.QtCore.QTimer()
        self.poll_timer.timeout.connect(self.poll_csi)
        self.poll_timer.start(10)

    def poll_csi(self):
        self.pool.run()

    def exec(self):
        #context = self.engine.rootContext()
        #context.setContextProperty("backend", self)
        #qmlFile = pathlib.Path(__file__).resolve().parent / "instantaneous-csi-ui.qml"
        #self.engine.load(qmlFile.as_uri())
        #if not self.engine.rootObjects():
        #    return -1

        return super().exec()
    
    def onCSI(self, csi_cluster):
        timestamps = csi_cluster.get_sensor_timestamps()
        if hasattr(self, 'previous_timestamps'):
            dt = timestamps - self.previous_timestamps
            print("dt = ", dt * 1e3, " ms")
        self.previous_timestamps = timestamps



app = EspargosDemoRadarAnalysis(sys.argv)
sys.exit(app.exec())
