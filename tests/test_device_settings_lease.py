from __future__ import annotations

from pathlib import Path

import pytest

import mouse_pressure.runtime.device_settings_lease as lease_module
from mouse_pressure.runtime.device_settings import (
    DeviceSettingsSnapshot,
    SessionDeviceSettings,
    restore_device_settings,
)
from mouse_pressure.runtime.device_settings_lease import TemporaryDeviceSettingsLease


class _Session:
    def __init__(self) -> None:
        self.dpi = 1200
        self.haptics = (4, 2)
        self.actuation = (7, 4)
        self.profile = (True, 2)
        self.events: list[str] = []
        self.fail_haptics = False

    def get_dpi(self) -> int:
        return self.dpi

    def set_dpi(self, dpi: int) -> int:
        self.dpi = dpi
        self.events.append(f"dpi={dpi}")
        return dpi

    def get_haptic_levels(self) -> tuple[int, int]:
        return self.haptics

    def set_haptic_levels(self, *, left: int, right: int) -> tuple[int, int]:
        if self.fail_haptics:
            self.fail_haptics = False
            raise RuntimeError("haptics failed")
        self.haptics = (left, right)
        self.events.append(f"haptics={left}/{right}")
        return self.haptics

    def get_actuation_levels(self) -> tuple[int, int]:
        return self.actuation

    def set_actuation_levels(self, *, left: int, right: int) -> tuple[int, int]:
        self.actuation = (left, right)
        self.events.append(f"actuation={left}/{right}")
        return self.actuation

    def get_onboard_profile_state(self) -> tuple[bool, int]:
        return self.profile

    def set_onboard_profile_state(
        self,
        *,
        enabled: bool,
        active_sector: int | None = None,
    ) -> None:
        self.profile = (enabled, active_sector or 0)
        self.events.append(
            f"profile={active_sector}" if enabled else "profile=host"
        )

    def disable_pressure_stream(self) -> None:
        self.events.append("pressure_disable")

    def enable_pressure_stream(self, *, mode: int, mode_arg: int) -> None:
        self.events.append(f"pressure_enable={mode}/{mode_arg}")


def _lease() -> TemporaryDeviceSettingsLease:
    return TemporaryDeviceSettingsLease(
        config_dir=None,
        recovery_enabled=False,
        pressure_mode=3,
        pressure_mode_arg=0,
    )


def _requested() -> SessionDeviceSettings:
    return SessionDeviceSettings(dpi=1600, haptic_left=0, haptic_right=3)


def test_lease_applies_and_restores_the_complete_snapshot() -> None:
    session = _Session()
    lease = _lease()

    applied = lease.activate(session, _requested())

    assert applied == _requested()
    assert lease.active is True
    assert session.events == [
        "pressure_disable",
        "profile=host",
        "dpi=1600",
        "haptics=0/3",
        "actuation=5/5",
        "pressure_enable=3/0",
    ]

    restored = lease.restore(session)

    assert restored == SessionDeviceSettings(
        dpi=1200,
        haptic_left=4,
        haptic_right=2,
        actuation_left=7,
        actuation_right=4,
    )
    assert lease.active is False
    assert session.events[-5:] == [
        "pressure_disable",
        "dpi=1200",
        "haptics=4/2",
        "actuation=7/4",
        "profile=2",
    ]


def test_failed_activation_rolls_back_partial_hardware_changes() -> None:
    session = _Session()
    session.fail_haptics = True
    lease = _lease()

    try:
        lease.activate(session, _requested())
    except RuntimeError as exc:
        assert str(exc) == "haptics failed"
    else:
        raise AssertionError("activation should fail")

    assert lease.active is False
    assert session.dpi == 1200
    assert session.haptics == (4, 2)
    assert session.actuation == (7, 4)
    assert session.profile == (True, 2)


def test_live_changes_do_not_replace_the_restore_snapshot() -> None:
    session = _Session()
    lease = _lease()
    lease.activate(session, _requested())

    lease.apply_live(
        session,
        SessionDeviceSettings(dpi=2000, haptic_left=1, haptic_right=1),
    )
    restored = lease.restore(session)

    assert restored == SessionDeviceSettings(
        dpi=1200,
        haptic_left=4,
        haptic_right=2,
        actuation_left=7,
        actuation_right=4,
    )
    assert session.dpi == 1200
    assert session.haptics == (4, 2)
    assert session.actuation == (7, 4)
    assert session.profile == (True, 2)


def test_restore_reenables_profile_mode_after_dpi_change() -> None:
    session = _Session()
    session.dpi = 400
    original = DeviceSettingsSnapshot(
        session=SessionDeviceSettings(
            dpi=800,
            haptic_left=session.haptics[0],
            haptic_right=session.haptics[1],
            actuation_left=session.actuation[0],
            actuation_right=session.actuation[1],
        ),
        onboard_profiles_enabled=True,
        onboard_profile_sector=2,
    )

    restored = restore_device_settings(session, original)

    assert restored.dpi == 800
    assert session.profile == (True, 2)
    assert session.events == ["profile=host", "dpi=800", "profile=2"]


def test_first_live_change_arms_recovery_after_noop_activation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _Session()
    armed: list[dict[str, int]] = []

    def fake_arm_restore_watchdog(**fields):
        armed.append(fields["settings"].to_dict())
        return object(), tmp_path / "restore.json"

    monkeypatch.setattr(
        lease_module,
        "arm_restore_watchdog",
        fake_arm_restore_watchdog,
    )
    lease = TemporaryDeviceSettingsLease(
        config_dir=tmp_path,
        recovery_enabled=True,
        pressure_mode=3,
        pressure_mode_arg=0,
    )
    current = DeviceSettingsSnapshot(
        session=SessionDeviceSettings(
            dpi=1200,
            haptic_left=4,
            haptic_right=2,
            actuation_left=7,
            actuation_right=4,
        ),
        onboard_profiles_enabled=True,
        onboard_profile_sector=2,
    )

    lease.activate(session, current.session)
    assert armed == []

    lease.apply_live(
        session,
        SessionDeviceSettings(
            dpi=1600,
            haptic_left=4,
            haptic_right=2,
            actuation_left=7,
            actuation_right=4,
        ),
        current_settings=current,
    )

    assert armed == [
        {
            "dpi": 1200,
            "haptic_left": 4,
            "haptic_right": 2,
            "actuation_left": 7,
            "actuation_right": 4,
            "onboard_profiles_enabled": 1,
            "onboard_profile_sector": 2,
        }
    ]
