from __future__ import annotations

import json
import sys
from pathlib import Path

import mouse_pressure.runtime.device_restore_watchdog as watchdog_module
from mouse_pressure.runtime.device_restore_watchdog import (
    arm_restore_watchdog,
    run_watchdog,
)
from mouse_pressure.runtime.device_settings import (
    DeviceSettingsSnapshot,
    SessionDeviceSettings,
    restore_device_settings,
)


class _FakeSession:
    def __init__(self, _log) -> None:
        self.dpi = 400
        self.haptics = (0, 0)
        self.actuation = (1, 2)
        self.profile = (True, 1)
        self.profile_writes: list[tuple[bool, int | None]] = []
        self.closed = False

    def open(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def discover_pressure_feature_index(self) -> None:
        pass

    def get_dpi(self) -> int:
        return self.dpi

    def set_dpi(self, dpi: int) -> int:
        self.dpi = dpi
        return dpi

    def get_haptic_levels(self) -> tuple[int, int]:
        return self.haptics

    def set_haptic_levels(self, *, left: int, right: int) -> tuple[int, int]:
        self.haptics = (left, right)
        return self.haptics

    def get_actuation_levels(self) -> tuple[int, int]:
        return self.actuation

    def set_actuation_levels(self, *, left: int, right: int) -> tuple[int, int]:
        self.actuation = (left, right)
        return self.actuation

    def get_onboard_profile_state(self) -> tuple[bool, int]:
        return self.profile

    def set_onboard_profile_state(
        self,
        *,
        enabled: bool,
        active_sector: int | None = None,
    ) -> None:
        self.profile_writes.append((enabled, active_sector))
        self.profile = (enabled, active_sector or 0)


def _original_settings() -> DeviceSettingsSnapshot:
    return DeviceSettingsSnapshot(
        session=SessionDeviceSettings(dpi=800, haptic_left=3, haptic_right=4),
        onboard_profiles_enabled=True,
        onboard_profile_sector=2,
    )


def test_restore_device_settings_restores_dpi_haptics_and_profile() -> None:
    session = _FakeSession(lambda _line: None)

    restored = restore_device_settings(session, _original_settings())

    assert restored == SessionDeviceSettings(dpi=800, haptic_left=3, haptic_right=4)
    assert session.dpi == 800
    assert session.haptics == (3, 4)
    assert session.actuation == (5, 5)
    assert session.profile_writes == [(False, None), (True, 2)]


def test_watchdog_restores_after_parent_is_already_gone(tmp_path: Path) -> None:
    state_path = tmp_path / "device_restore.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "parent_pid": 0x7FFFFFFF,
                "settings": _original_settings().to_dict(),
            }
        ),
        encoding="utf-8",
    )
    sessions: list[_FakeSession] = []

    def factory(log):
        session = _FakeSession(log)
        sessions.append(session)
        return session

    restored = run_watchdog(
        parent_pid=0x7FFFFFFF,
        state_path=state_path,
        session_factory=factory,
    )

    assert restored is True
    assert not state_path.exists()
    assert sessions[0].closed is True
    assert sessions[0].dpi == 800


def test_frozen_watchdog_relaunches_installed_executable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[list[str]] = []

    class _Process:
        pass

    def fake_popen(command, **_kwargs):
        calls.append(list(command))
        return _Process()

    monkeypatch.setattr(watchdog_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\Mouse Pressure\MousePressure.exe")

    process, state_path = arm_restore_watchdog(
        config_dir=tmp_path,
        parent_pid=123,
        settings=_original_settings(),
    )

    assert isinstance(process, _Process)
    assert calls == [
        [
            r"C:\Program Files\Mouse Pressure\MousePressure.exe",
            "--device-restore-watchdog",
            "--parent-pid",
            "123",
            "--state-file",
            str(state_path),
        ]
    ]
