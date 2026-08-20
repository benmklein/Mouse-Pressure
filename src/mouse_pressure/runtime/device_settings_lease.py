"""Temporary mouse-settings transaction with crash-safe restoration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from mouse_pressure.runtime.device_restore_watchdog import (
    arm_restore_watchdog,
    disarm_restore_watchdog,
)
from mouse_pressure.runtime.device_settings import (
    DeviceSettingsSnapshot,
    SessionDeviceSettings,
    apply_device_settings,
    device_settings_differ,
    read_device_settings,
    restore_device_settings,
)


class TemporaryDeviceSettingsLease:
    """Own temporary settings from capture through successful restoration."""

    def __init__(
        self,
        *,
        config_dir: Path | None,
        recovery_enabled: bool,
        pressure_mode: int,
        pressure_mode_arg: int,
        parent_pid: int | None = None,
    ) -> None:
        self._config_dir = config_dir
        self._recovery_enabled = bool(recovery_enabled)
        self._pressure_mode = int(pressure_mode)
        self._pressure_mode_arg = int(pressure_mode_arg)
        self._parent_pid = os.getpid() if parent_pid is None else int(parent_pid)
        self._original: DeviceSettingsSnapshot | None = None
        self._watchdog_process: Any | None = None
        self._watchdog_state_path: Path | None = None

    @property
    def active(self) -> bool:
        return self._original is not None

    @property
    def original(self) -> DeviceSettingsSnapshot | None:
        return self._original

    def activate(
        self,
        session: Any,
        requested: SessionDeviceSettings,
    ) -> SessionDeviceSettings:
        """Capture current settings and apply temporary session settings."""
        if self._original is not None:
            raise RuntimeError("Mouse-settings lease is already active")
        original = read_device_settings(session, discover_feature=False)
        self._original = original
        changed = device_settings_differ(original, requested)
        try:
            if changed:
                self._arm_watchdog(original)
                session.disable_pressure_stream()
            applied = apply_device_settings(
                session,
                requested,
                current_settings=original,
            )
            if changed:
                self._resume_pressure_stream(session)
            return applied
        except Exception:
            self._restore_after_failed_activation(session)
            raise

    def apply_live(
        self,
        session: Any,
        requested: SessionDeviceSettings,
        *,
        current_settings: DeviceSettingsSnapshot | None = None,
    ) -> SessionDeviceSettings:
        """Apply a live change without replacing the original restore snapshot."""
        original = self._original
        if original is None:
            raise RuntimeError("Mouse-settings lease is not active")
        current = current_settings or read_device_settings(
            session,
            discover_feature=False,
        )
        changed = device_settings_differ(current, requested)
        paused = False
        try:
            if changed:
                if self._recovery_enabled and self._watchdog_state_path is None:
                    self._arm_watchdog(original)
                session.disable_pressure_stream()
                paused = True
            applied = apply_device_settings(
                session,
                requested,
                current_settings=current,
            )
            if paused:
                self._resume_pressure_stream(session)
                paused = False
            return applied
        finally:
            if paused:
                self._resume_pressure_stream(session)

    def restore(
        self,
        session: Any,
        *,
        on_pause_error: Callable[[Exception], None] | None = None,
    ) -> SessionDeviceSettings | None:
        """Restore the captured snapshot and disarm recovery after success."""
        original = self._original
        if original is None:
            return None
        try:
            session.disable_pressure_stream()
        except Exception as exc:
            if on_pause_error is not None:
                on_pause_error(exc)
        restored = restore_device_settings(
            session,
            original,
            discover_feature=False,
        )
        self._disarm_watchdog()
        self._original = None
        return restored

    def _restore_after_failed_activation(self, session: Any) -> None:
        original = self._original
        if original is None:
            return
        try:
            session.disable_pressure_stream()
        except Exception:
            pass
        try:
            restore_device_settings(
                session,
                original,
                discover_feature=False,
            )
        except Exception:
            return
        self._disarm_watchdog()
        self._original = None

    def _resume_pressure_stream(self, session: Any) -> None:
        session.enable_pressure_stream(
            mode=self._pressure_mode,
            mode_arg=self._pressure_mode_arg,
        )

    def _arm_watchdog(self, settings: DeviceSettingsSnapshot) -> None:
        if not self._recovery_enabled:
            return
        if self._config_dir is None:
            raise RuntimeError("Mouse-settings crash recovery needs a config directory")
        try:
            process, state_path = arm_restore_watchdog(
                config_dir=self._config_dir,
                parent_pid=self._parent_pid,
                settings=settings,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not arm mouse-settings crash recovery: {exc}"
            ) from exc
        self._watchdog_process = process
        self._watchdog_state_path = state_path

    def _disarm_watchdog(self) -> None:
        disarm_restore_watchdog(self._watchdog_state_path)
        self._watchdog_state_path = None
        self._watchdog_process = None
