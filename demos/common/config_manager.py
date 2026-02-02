#!/usr/bin/env python3

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
        self._apply_in_flight_app = False
        self._apply_in_flight_ui = False
        self._apply_timer = PyQt6.QtCore.QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.timeout.connect(self._async_apply)
        self._scheduleApplyTimer.connect(lambda: self._apply_timer.start(0))
        self.is_force_apply = False

        # Signal handled synchronization
        self._update_app_handled_event = threading.Event()
        self._update_ui_handled_event = threading.Event()
        self.updateAppStateHandled.connect(lambda: self._update_app_handled_event.set())
        self.updateUIStateHandled.connect(lambda: self._update_ui_handled_event.set())
        self._handled_wait_timeout = 20.0

    def _wait_for_handled(self, event: threading.Event, name: str):
        if not event.wait(timeout=self._handled_wait_timeout):
            self.logger.warning(f"Timed out waiting for {name} update to be handled")

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
        deep_update(self._pending_to_ui, new_config)
        deep_update(self._pending_to_app, new_config)
        self.is_force_apply = True
        self._scheduleApplyTimer.emit()

    def _async_apply(self):
        # Run app/UI appliers independently; coalesce pending updates per target.
        with self._apply_lock:
            if not self._pending_to_app and not self._pending_to_ui:
                return

            force_apply = self.is_force_apply
            pending_ui = pending_app = None

            if self._pending_to_ui and not self._apply_in_flight_ui:
                pending_ui = self._pending_to_ui
                self._pending_to_ui = {}
                self._apply_in_flight_ui = True

            if self._pending_to_app and not self._apply_in_flight_app:
                pending_app = self._pending_to_app
                self._pending_to_app = {}
                self._apply_in_flight_app = True
                if force_apply:
                    self.is_force_apply = False

        def get_delta(current_cfg: dict, target_cfg: dict, force_apply: bool) -> dict:
            delta = dict()
            if force_apply:
                # Apply all pending changes, regardless of current state
                delta = target_cfg
            else:
                # Determine actual delta between current config and desired state
                for k, v in target_cfg.items():
                    if k not in current_cfg or current_cfg[k] != v:
                        delta[k] = v
            return delta

        def app_worker(delta: dict, force_apply: bool):
            try:
                delta = get_delta(self.app_config, delta, force_apply)

                # Changes are applied to the app *before* handlers are called,
                # so that handlers always see the latest state (the values that
                # changed are in delta, unless force-applying, in which case
                # values are all applied even though they may be unchanged).
                deep_update(self.app_config, delta)

                self._update_app_handled_event.clear()
                self.updateAppState.emit(delta)
                self._wait_for_handled(self._update_app_handled_event, "app")
                if force_apply:
                    self.forceConfigAppApplied.emit()

            finally:
                with self._apply_lock:
                    self._apply_in_flight_app = False
                    has_more = bool(self._pending_to_app)

                # Trigger next run if more deltas arrived meanwhile (must be on QObject thread)
                if has_more:
                    self._scheduleApplyTimer.emit()

        def ui_worker(delta: dict, force_apply: bool):
            try:
                delta = get_delta(self.ui_config, delta, force_apply)

                # Changes are applied to the UI *before* handlers are called,
                # so that handlers always see the latest state (the values that
                # changed are in delta, unless force-applying, in which case
                # values are all applied even though they may be unchanged).
                deep_update(self.ui_config, delta)

                self._update_ui_handled_event.clear()
                self.updateUIState.emit(json.dumps(delta))
                self._wait_for_handled(self._update_ui_handled_event, "ui")

            finally:
                with self._apply_lock:
                    self._apply_in_flight_ui = False
                    has_more = bool(self._pending_to_ui)

                # Trigger next run if more deltas arrived meanwhile (must be on QObject thread)
                if has_more:
                    self._scheduleApplyTimer.emit()

        if pending_ui:
            threading.Thread(
                target=ui_worker,
                args=(pending_ui, force_apply),
                daemon=True,
            ).start()

        if pending_app:
            threading.Thread(
                target=app_worker,
                args=(pending_app, force_apply),
                daemon=True,
            ).start()

    def get(self, *path_parts):
        # path_parts are multiple arguments that form the path in the config dict
        found, value = self._get_path(self.app_config, list(path_parts))
        if not found:
            return None
        return copy.deepcopy(value)

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
        deep_update(self.ui_config, delta)
        deep_update(self._pending_to_app, delta)

        self._scheduleApplyTimer.emit()