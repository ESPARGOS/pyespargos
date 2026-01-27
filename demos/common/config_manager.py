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

    # Emitted when a forceful config application has completed
    forceConfigApplied = PyQt6.QtCore.pyqtSignal()

    def __init__(self, default_ui_config: dict = None, parent=None):
        """
        Initialize ConfigManager with optional default configuration.

        :param default_ui_config: Default configuration for UI initialization. If app state is authoritative, this is just for initial UI state, the app should call set() to provide true state later on.
        """
        super().__init__(parent=parent)

        self.logger = logging.getLogger("demo.ConfigManager")
        # Keep separate copies of the app and UI state
        self.app_config: dict = dict()
        self.ui_config: dict = dict()

        # Initialize asynchronous apply machinery
        self._pending_to_app: dict = dict()
        self._pending_to_ui: dict = dict()
        self._apply_lock = threading.Lock()
        self._apply_in_flight = False
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
        self._handled_wait_timeout = 5.0

        # Initialize config with defaults
        # Note: UI must fetch initial config itself once ready
        if default_ui_config:
            deep_update(self.ui_config, default_ui_config)

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

    def _flatten_incoming(self, incoming: dict, prefix=None):
        if prefix is None:
            prefix = []
        items = []
        if not isinstance(incoming, dict):
            return items

        for k, v in incoming.items():
            key_parts = self._split_path(k)
            path_parts = prefix + key_parts
            if isinstance(v, dict):
                items.extend(self._flatten_incoming(v, path_parts))
            else:
                items.append((path_parts, v))
        return items        

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
        # Ensure only one background applier runs at a time; coalesce pending updates.
        with self._apply_lock:
            # Nothing to do if an apply is already in flight (will re-trigger on finish)
            if self._apply_in_flight:
                return
            
            # Nothing to do if there are no pending changes
            if not self._pending_to_app and not self._pending_to_ui:
                return

            # Pending config changes from app always take precedence over UI changes
            pending = self._pending_to_ui
            self._pending_to_ui = {}
            pending_source = "app"

            if not pending:
                pending = dict(self._pending_to_app)
                self._pending_to_app = {}
                pending_source = "ui"

            self._apply_in_flight = True

        def worker(pending: dict, pending_source: str):
            try:
                # Determine actual delta between current config and desired state
                target_cfg = self.app_config if pending_source == "ui" else self.ui_config
                delta = {}
                for k, v in pending.items():
                    if k not in target_cfg or target_cfg[k] != v:
                        delta[k] = v

                # Changes are applied to the target config (app/ui) *before* handlers are called,
                # so that handlers always see the latest state (the values that
                # changed are in delta).
                deep_update(target_cfg, delta)

                if pending_source == "ui":
                    self._update_app_handled_event.clear()
                    self.updateAppState.emit(delta)
                    self._wait_for_handled(self._update_app_handled_event, "ui")
                    if self.is_force_apply:
                        self.is_force_apply = False
                        self.forceConfigApplied.emit()
                else:
                    self._update_ui_handled_event.clear()
                    self.updateUIState.emit(json.dumps(delta))
                    self._wait_for_handled(self._update_ui_handled_event, "app")

            finally:
                with self._apply_lock:
                    self._apply_in_flight = False
                    has_more = bool(self._pending_to_app) or bool(self._pending_to_ui)

                # Trigger next run if more deltas arrived meanwhile (must be on QObject thread)
                if has_more:
                    self._scheduleApplyTimer.emit()

        threading.Thread(target=worker, args=(pending, pending_source), daemon=True).start()

    def get(self, *path_parts):
        # path_parts can be either
        # * none (returns whole config)
        # * multiple string arguments
        # * a single dot-separated string
        if len(path_parts) == 1 and isinstance(path_parts[0], str):
            path_parts = self._split_path(path_parts[0])

        found, value = self._get_path(self.app_config, list(path_parts))
        if not found:
            return None
        return copy.deepcopy(value)

    @PyQt6.QtCore.pyqtSlot(result=str)
    def getConfigFromUI(self):
        return json.dumps(self.ui_config)

    @PyQt6.QtCore.pyqtSlot(str)
    def setConfigFromUI(self, config_json):
        incoming = json.loads(config_json)
        if not isinstance(incoming, dict):
            raise ValueError("config_json must decode to an object")

        # Build a minimal delta from incoming changes (supports nested keys with dot notation)
        delta = dict()

        for path_parts, v in self._flatten_incoming(incoming):
            if not path_parts:
                continue
            found_ui, current_ui = self._get_path(self.ui_config, path_parts)
            found_app, current_app = self._get_path(self.app_config, path_parts)
            if not found_ui and not found_app:
                self.logger.warning(f"Ignoring unknown config key: {'.'.join(path_parts)}")
                continue

            current = current_ui if found_ui else current_app

            type_ = type(current)
            if type_ is bool:
                try:
                    value = bool(v)
                except Exception:
                    raise ValueError(f"Invalid bool value for {'.'.join(path_parts)}: {v}")
            elif type_ is int:
                try:
                    value = int(v)
                except Exception:
                    raise ValueError(f"Invalid int value for {'.'.join(path_parts)}: {v}")
            elif type_ is str:
                value = (str(v) or "").strip()
            else:
                value = v


            if current != value:
                self._set_path(delta, path_parts, value)

        # Queue pending changes (newest wins per key)
        if delta:
            # Update UI state immediately, queue changes to app
            deep_update(self.ui_config, delta)
            deep_update(self._pending_to_app, delta)

        self._scheduleApplyTimer.emit()