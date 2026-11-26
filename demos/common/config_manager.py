#!/usr/bin/env python

import PyQt6.QtCore

import threading
import logging
import json
import copy

# The ConfigManager is an interface between the UI (QML) and the underlying
# configuration of a demo application. It handles updating the program state
# when the user changes settings in the UI, and notifying the UI when the
# program state changes (e.g., due to external events).

# A class deriving from ConfigManager must
# - hook up to updateAppState signal to receive config changes from UI
#   * delta contains only keys to change
#   * if the desired state cannot be achieved, the difference must be
#     provided back via set(), an error (emitShowError) may optionally be emitted
# - optionally implement _action_{action_name}() methods for actions
#
# It may then
# - use self.logger for logging
# - use set to update config from the application
# - use get(*path_parts) to read current config values
# - use emitShowError(title: str, message: str) to notify QML of errors


def deep_update(original: dict, updates: dict) -> dict:
    """
    Recursively update a dictionary with another dictionary.
    Similar to dict.update(), but merges nested dictionaries instead of replacing them.
    """
    for k, v in updates.items():
        if isinstance(v, dict) and k in original and isinstance(original[k], dict):
            deep_update(original[k], v)
        else:
            original[k] = v

    return original


class ConfigManager(PyQt6.QtCore.QObject):
    # Internal: schedule starting the QTimer on the QObject's thread
    _scheduleApplyTimer = PyQt6.QtCore.pyqtSignal()

    # QML hook (ConfigManager.qml listens via Connections.onUpdateUIState)
    # Emitted when the application triggered a config update
    updateUIState = PyQt6.QtCore.pyqtSignal(str)
    updateUIStateHandled = PyQt6.QtCore.pyqtSignal()

    # Emitted when the UI triggered a config update
    updateAppState = PyQt6.QtCore.pyqtSignal(dict)
    updateAppStateHandled = PyQt6.QtCore.pyqtSignal()

    # Emitted when UI requests an action
    action = PyQt6.QtCore.pyqtSignal(str)

    # QML hook (ConfigManager.qml listens via Connections.onShowError)
    showError = PyQt6.QtCore.pyqtSignal(str, str)

    # Emitted when a forceful config change to app has completed
    forceConfigAppApplied = PyQt6.QtCore.pyqtSignal()

    def __init__(self, default_config: dict = None, parent=None):
        """
        Initialize ConfigManager with optional default configuration.
        Configuration keys that are not present in the default will remain None, i.e., uninitialized.

        :param default_config: Default configuration for app and UI initialization. If app state is authoritative, this is just for initial UI state, the app should call set() to provide true state later on.
        """
        super().__init__(parent=parent)

        self.logger = logging.getLogger("demo.ConfigManager")

        # Keep separate copies of the app and UI state, which may diverge temporarily during updates
        # Both UI and app are responsible for fetching initial state, they will not receive signals initially.
        self.app_config: dict = copy.deepcopy(default_config) if default_config is not None else dict()
        self.ui_config: dict = copy.deepcopy(default_config) if default_config is not None else dict()

        # Initialize asynchronous apply machinery
        self._pending_to_app: dict = dict()
        self._pending_to_ui: dict = dict()
        self._apply_lock = threading.Lock()
        self._force_app_pending = False
        self._force_ui_pending = False
        self._force_app_in_flight = False
        self._apply_timer = PyQt6.QtCore.QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.timeout.connect(self._async_apply)
        self._scheduleApplyTimer.connect(self._schedule_apply)

        # Each timer is active until its handler acknowledges the update.
        # Parenting the timers keeps delivery and teardown on the QObject thread.
        self._app_ack = self._ack_timer(self._app_handled, "app")
        self._ui_ack = self._ack_timer(self._ui_handled, "ui")
        self.updateAppStateHandled.connect(self._app_handled)
        self.updateUIStateHandled.connect(self._ui_handled)

    def _ack_timer(self, handled, name):
        timer = PyQt6.QtCore.QTimer(self)
        timer.setInterval(5000)

        def expired():
            self.logger.warning("Timed out waiting for %s update to be handled", name)
            handled()

        timer.timeout.connect(expired)
        return timer

    @PyQt6.QtCore.pyqtSlot()
    def _schedule_apply(self):
        self._apply_timer.start(0)

    @PyQt6.QtCore.pyqtSlot()
    def _app_handled(self):
        if self._app_ack.isActive():
            self._app_ack.stop()
            if self._force_app_in_flight:
                self._force_app_in_flight = False
                self.forceConfigAppApplied.emit()
            self._schedule_apply()

    @PyQt6.QtCore.pyqtSlot()
    def _ui_handled(self):
        if self._ui_ack.isActive():
            self._ui_ack.stop()
            self._schedule_apply()

    def _split_path(self, key: str):
        if key is None:
            return []
        return [p for p in str(key).split(".") if p]

    def _get_path(self, cfg: dict, path_parts: list):
        cur = cfg
        # If path_parts is empty, return the whole cfg
        if not path_parts:
            return True, cur
        for part in path_parts:
            if not isinstance(cur, dict) or part not in cur:
                return False, None
            cur = cur[part]
        return True, cur

    def _set_path(self, cfg: dict, path_parts: list, value):
        if not path_parts:
            return
        cur = cfg
        for part in path_parts[:-1]:
            if part not in cur or not isinstance(cur[part], dict):
                cur[part] = {}
            cur = cur[part]
        cur[path_parts[-1]] = value

    def emitShowError(self, title: str, message: str):
        self.showError.emit(title, message)

    def set(self, new_config: dict):
        """
        Update configuration from application.
        """
        # Update app-side state immediately, queue changes to UI
        with self._apply_lock:
            deep_update(self.app_config, new_config)
            deep_update(self._pending_to_ui, new_config)
        self._scheduleApplyTimer.emit()

    def force(self, new_config: dict):
        """
        Forcefully set the configuration to new_config, updating both UI and app state.
        """
        if new_config is None:
            return

        # Queue pending changes for both sides (applied asynchronously)
        with self._apply_lock:
            deep_update(self._pending_to_ui, new_config)
            deep_update(self._pending_to_app, new_config)
            self._force_app_pending = self._force_ui_pending = True
        self._scheduleApplyTimer.emit()

    @PyQt6.QtCore.pyqtSlot()
    def _async_apply(self):
        def apply_delta(current, pending, force):
            delta = pending if force else {k: v for k, v in pending.items() if k not in current or current[k] != v}
            deep_update(current, delta)
            return delta

        ui = app = None
        with self._apply_lock:
            if self._pending_to_ui and not self._ui_ack.isActive():
                ui = apply_delta(self.ui_config, self._pending_to_ui, self._force_ui_pending)
                self._pending_to_ui = {}
                self._force_ui_pending = False
                self._ui_ack.start()

            if self._pending_to_app and not self._app_ack.isActive():
                app = apply_delta(self.app_config, self._pending_to_app, self._force_app_pending)
                self._pending_to_app = {}
                self._force_app_in_flight = self._force_app_pending
                self._force_app_pending = False
                self._app_ack.start()

        # A handler may acknowledge synchronously or enqueue another edit.
        if ui is not None:
            self.updateUIState.emit(json.dumps(ui))
        if app is not None:
            self.updateAppState.emit(app)

    def get(self, *path_parts):
        # path_parts are multiple arguments that form the path in the config dict
        with self._apply_lock:
            found, value = self._get_path(self.app_config, list(path_parts))
            return copy.deepcopy(value) if found else None

    @PyQt6.QtCore.pyqtSlot(result=str)
    def getConfigFromUI(self):
        return json.dumps(self.ui_config)

    @PyQt6.QtCore.pyqtSlot(str)
    def setConfigFromUI(self, config_json):
        delta = json.loads(config_json)
        if not isinstance(delta, dict):
            raise ValueError("config_json must decode to an object")

        # Queue pending changes (newest wins per key)
        # Update UI state immediately, queue changes to app
        with self._apply_lock:
            deep_update(self.ui_config, delta)
            deep_update(self._pending_to_app, delta)

        self._scheduleApplyTimer.emit()
