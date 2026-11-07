"""Advertiser list with stable rows and independent selection state."""

import time
from PyQt6 import QtCore


class Advertisers(QtCore.QAbstractListModel):
    selectionChanged = QtCore.pyqtSignal()
    ROLES = ("identity", "address", "addressType", "name", "packets", "age", "selected")

    def __init__(self, parent=None, selected=()):
        super().__init__(parent)
        self.rows = []
        self.initial_selection = set(selected)

    def roleNames(self):
        return {QtCore.Qt.ItemDataRole.UserRole + i: name.encode() for i, name in enumerate(self.ROLES)}

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        offset = role - QtCore.Qt.ItemDataRole.UserRole
        return self.rows[index.row()].get(self.ROLES[offset]) if 0 <= offset < len(self.ROLES) else None

    def observe(self, record):
        key = record["identity"]
        for row, item in enumerate(self.rows):
            if item["identity"] == key:
                break
        else:
            if len(self.rows) >= 128:
                removable = [i for i, item in enumerate(self.rows) if not item["selected"]]
                if not removable:
                    return
                oldest = min(removable, key=lambda i: self.rows[i]["seen"])
                self.beginRemoveRows(QtCore.QModelIndex(), oldest, oldest)
                self.rows.pop(oldest)
                self.endRemoveRows()
            row = len(self.rows)
            self.beginInsertRows(QtCore.QModelIndex(), row, row)
            item = dict(record, packets=0, age=0.0, selected=record["address"] in self.initial_selection, event=None)
            self.rows.append(item)
            self.endInsertRows()
        # Every sensor reports the same event. Count the event once.
        event = (record["clock"], record["time"])
        if item["event"] is None or event[0] != item["event"][0] or abs(event[1] - item["event"][1]) > 2000:
            item["packets"] += 1
            item["event"] = event
        item.update(seen=time.monotonic(), age=0.0)
        if record["name"]:
            item["name"] = record["name"]
        self.dataChanged.emit(self.index(row), self.index(row))

    @property
    def selected(self):
        return {item["identity"] for item in self.rows if item["selected"]}

    @QtCore.pyqtSlot(str, bool)
    def select(self, identity, selected):
        for row, item in enumerate(self.rows):
            if item["identity"] == identity and item["selected"] != selected:
                item["selected"] = selected
                self.dataChanged.emit(self.index(row), self.index(row))
                self.selectionChanged.emit()
                return

    @QtCore.pyqtSlot(str)
    def selectOnly(self, identity):
        """Select one known advertiser, notifying consumers after the whole change."""
        if not any(item["identity"] == identity for item in self.rows):
            return
        changed = False
        for row, item in enumerate(self.rows):
            selected = item["identity"] == identity
            if item["selected"] != selected:
                item["selected"] = selected
                self.dataChanged.emit(self.index(row), self.index(row))
                changed = True
        if changed:
            self.selectionChanged.emit()

    @QtCore.pyqtSlot(bool)
    def selectAll(self, selected):
        for item in self.rows:
            item["selected"] = selected
        if self.rows:
            self.dataChanged.emit(self.index(0), self.index(len(self.rows) - 1))
        self.selectionChanged.emit()

    def refresh(self):
        now = time.monotonic()
        for row, item in enumerate(self.rows):
            age = now - item["seen"]
            label = round(max(0.0, age), 1)
            if item["age"] != label:
                item["age"] = label
                self.dataChanged.emit(self.index(row), self.index(row))


def advertiser_record(packet):
    """Qt-independent discovery record from a CRC-validated advertisement."""
    return dict(identity=packet.address_type+":"+packet.advertiser,
                address=packet.advertiser, addressType=packet.address_type,
                name=packet.name, clock=packet.clock_epoch,
                time=packet.time_us)
