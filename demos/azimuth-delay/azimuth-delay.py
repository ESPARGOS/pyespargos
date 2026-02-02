#!/usr/bin/env python3

import numpy as np
import espargos
import argparse
from PyQt6 import QtCore
from PyQt6 import QtQml
from PyQt6 import QtWidgets
from matplotlib import colormaps
import pathlib
import sys


BEAMSPACE_OVERSAMPLING = 16
DELAY_OVERSAMPLING = 10
DELAY_MIN = -3
DELAY_MAX = 5
ANTENNAS_ROWS = 2
ANTENNAS_COLS = 4
SUBCARRIERS_LLTF = 53
SUBCARRIERS_HT20 = 56
SUBCARRIERS_HT40 = 117


class q_image_app(QtWidgets.QApplication):
    dataChanged = QtCore.pyqtSignal(list)
    def __init__(self, argv):
        super().__init__(argv) 

        parser = argparse.ArgumentParser(description="Demo: Azimuth-Range (single board)")
        parser.add_argument("host", type=str, help="Host address (IP or hostname) of ESPARGOS controller")
        parser.add_argument("-b", "--backlog", type = int, default = 1, help = "Number of CSI datapoints to average over in backlog")
        format_group = parser.add_mutually_exclusive_group()
        format_group.add_argument("-l", "--lltf", default = False, help = "Use only CSI from L-LTF", action = "store_true")
        format_group.add_argument("-ht40", "--ht40", default = False, help = "Use only CSI from HT40", action = "store_true")
        format_group.add_argument("-ht20", "--ht20", default = False, help = "Use only CSI from HT20", action = "store_true")
        self.args = parser.parse_args()

        # Check if at least one format is selected
        if not (self.args.lltf or self.args.ht40 or self.args.ht20):
            print("Error: At least one of --lltf, --ht40 or --ht20 must be selected.")
            sys.exit(1)

        if self.args.lltf:
            self.subcarriers = SUBCARRIERS_LLTF
        elif self.args.ht20:
            self.subcarriers = SUBCARRIERS_HT20
        else:
            self.subcarriers = SUBCARRIERS_HT40

        self.delay_size = (DELAY_MAX-DELAY_MIN)*DELAY_OVERSAMPLING+1
        self.angle_size = ANTENNAS_COLS*BEAMSPACE_OVERSAMPLING+1
        self.pool = espargos.Pool([espargos.Board(self.args.host)])
        self.pool.start()
        self.pool.calibrate(duration = 2, per_board=False)
        enable = ["rssi", "timestamp", "host_timestamp", "mac"]
        if self.args.lltf:
            enable.append("lltf")
        if self.args.ht40:
            enable.append("ht40")
        if self.args.ht20:
            enable.append("ht20")
        self.backlog = espargos.CSIBacklog(self.pool, size = self.args.backlog, enable = enable)
        self.backlog.start()
        self.aboutToQuit.connect(self.onAboutToQuit)
        self.engine = QtQml.QQmlApplicationEngine()
        self.data = []
        self.engine.rootContext().setContextProperty("backend", self)
        self.engine.rootContext().setContextProperty("delay_size", self.delay_size)
        self.engine.rootContext().setContextProperty("angle_size", self.angle_size)
        self.engine.rootContext().setContextProperty("Delay_max", DELAY_MAX) 
        self.engine.rootContext().setContextProperty("Delay_min", DELAY_MIN) 



    def exec(self):
        qmlFile = pathlib.Path(__file__).resolve().parent /"azimuth-delay.qml"
        self.engine.load(qmlFile.as_uri())
        if not self.engine.rootObjects():
            return -1
        return super().exec()
    

    def onAboutToQuit(self):
        self.pool.stop()
        self.backlog.stop()
        self.engine.deleteLater()
          
    def colormap(self, data):
        colormap = (colormaps.get_cmap('viridis'))
        image_data_list = []
        norm_data = data/np.max(data) if np.max(data)!=0 else data
        np_data = np.zeros((data.shape[1],data.shape[0],4), dtype=np.uint8)
        color_data=colormap(np.transpose(norm_data))
        np_data = color_data*255
        self.angle_size = np_data.shape[1]
        self.delay_size = np_data.shape[0]
        self.engine.rootContext().setContextProperty("delay_size", self.delay_size)
        self.engine.rootContext().setContextProperty("angle_size", self.angle_size)
        image_data_list = np_data.flatten().tolist()
        return image_data_list
    
    def get_csi(self):
        if self.backlog.nonempty():
            self.backlog.read_start()
            if self.args.lltf:
                csi = self.backlog.get("lltf")
                espargos.util.interpolate_lltf_gap(csi)
            elif self.args.ht20:
                csi = self.backlog.get("ht20")
                espargos.util.interpolate_ht20ltf_gap(csi)
            else:
                csi = self.backlog.get("ht40")
                espargos.util.interpolate_ht40ltf_gap(csi)
            self.backlog.read_finish()
            return csi
        else:
            return np.zeros((1,1,2,ANTENNAS_COLS,self.subcarriers), dtype=complex)
        
    
    @QtCore.pyqtSlot()
    def update_data(self):
            csi=self.get_csi()
            csi = csi[:,0,...] #only one board used
            csi = np.sum(csi, axis = 1)  #beamform over both rows of array
            csi_padded = np.zeros((csi.shape[0], ANTENNAS_COLS*BEAMSPACE_OVERSAMPLING, self.subcarriers*DELAY_OVERSAMPLING), dtype=csi.dtype)
            csi_padded[:,:csi.shape[1], :csi.shape[2]] = csi
            csi_padded = np.roll(np.fft.fft(csi_padded, axis=1), (ANTENNAS_COLS*BEAMSPACE_OVERSAMPLING)/2, axis=1) #beamspace
            csi_padded = np.roll(np.fft.ifft(csi_padded, axis=2), -DELAY_MIN*DELAY_OVERSAMPLING, axis=2) #from frequency to delay domain
            csi_padded = np.abs(csi_padded)
            csi_padded = np.sum(csi_padded, axis=0) #sum of all backlog samples
            self.data = csi_padded[:,:(DELAY_MAX-DELAY_MIN)*DELAY_OVERSAMPLING+1]# only relevant delays
            self.data = np.append(self.data, self.data[0,:].reshape(1,(DELAY_MAX-DELAY_MIN)*DELAY_OVERSAMPLING+1), axis=0) #beamspace -pi identical to pi
            self.image_data = self.colormap((self.data))
            self.dataChanged.emit(self.image_data)

app = q_image_app(sys.argv)
sys.exit(app.exec())
