"""Typed mouse hardware settings with one read/apply/restore implementation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self

from mouse_pressure.runtime.models import ValidationError
from mouse_pressure.sniff.hidpp_pressure import PressureHidppSession


@dataclass(frozen=True, slots=True)
class SessionDeviceSettings:
    """DPI and haptics requested while pressure output is active."""

    dpi: int
    haptic_left: int
    haptic_right: int

    @classmethod
    def from_mapping(cls, settings: Mapping[str, int] | Self) -> Self:
        if isinstance(settings, cls):
            return settings
        try:
            return cls(
                dpi=int(settings["dpi"]),
                haptic_left=int(settings["haptic_left"]),
                haptic_right=int(settings["haptic_right"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(
                "Mouse settings require DPI and left/right haptic levels"
            ) from exc

    def to_dict(self) -> dict[str, int]:
        return {
            "dpi": int(self.dpi),
            "haptic_left": int(self.haptic_left),
            "haptic_right": int(self.haptic_right),
        }


@dataclass(frozen=True, slots=True)
class DeviceSettingsSnapshot:
    """Complete hardware state captured for restoration after Stop or a crash."""

    session: SessionDeviceSettings
    onboard_profiles_enabled: bool | None = None
    onboard_profile_sector: int | None = None

    @classmethod
    def from_mapping(cls, settings: Mapping[str, int] | Self) -> Self:
        if isinstance(settings, cls):
            return settings
        session = SessionDeviceSettings.from_mapping(settings)
        profiles_enabled = settings.get("onboard_profiles_enabled")
        profile_sector = settings.get("onboard_profile_sector")
        return cls(
            session=session,
            onboard_profiles_enabled=(
                bool(int(profiles_enabled)) if profiles_enabled is not None else None
            ),
            onboard_profile_sector=(
                int(profile_sector) if profile_sector is not None else None
            ),
        )

    def to_dict(self) -> dict[str, int]:
        result = self.session.to_dict()
        if self.onboard_profiles_enabled is not None:
            result["onboard_profiles_enabled"] = int(self.onboard_profiles_enabled)
        if self.onboard_profile_sector is not None:
            result["onboard_profile_sector"] = int(self.onboard_profile_sector)
        return result


def validate_device_settings(
    settings: Mapping[str, int] | SessionDeviceSettings,
) -> SessionDeviceSettings:
    validated = SessionDeviceSettings.from_mapping(settings)
    if not 100 <= validated.dpi <= 32000 or validated.dpi % 50 != 0:
        raise ValidationError("DPI must be 100..32000 in 50-DPI increments")
    if not 0 <= validated.haptic_left <= 5 or not 0 <= validated.haptic_right <= 5:
        raise ValidationError("Haptic levels must be in 0..5")
    return validated


def read_device_settings(
    session: PressureHidppSession | Any,
    *,
    discover_feature: bool = True,
) -> DeviceSettingsSnapshot:
    discover = getattr(session, "discover_pressure_feature_index", None)
    if discover_feature and callable(discover):
        discover()
    dpi = session.get_dpi()
    left, right = session.get_haptic_levels()
    profiles_enabled: bool | None = None
    profile_sector: int | None = None
    profile_reader = getattr(session, "get_onboard_profile_state", None)
    if callable(profile_reader):
        enabled, sector = profile_reader()
        profiles_enabled = bool(enabled)
        profile_sector = int(sector or 0)
    return DeviceSettingsSnapshot(
        session=SessionDeviceSettings(
            dpi=int(dpi),
            haptic_left=int(left),
            haptic_right=int(right),
        ),
        onboard_profiles_enabled=profiles_enabled,
        onboard_profile_sector=profile_sector,
    )


def _session_settings(
    settings: SessionDeviceSettings | DeviceSettingsSnapshot,
) -> SessionDeviceSettings:
    return settings.session if isinstance(settings, DeviceSettingsSnapshot) else settings


def device_settings_differ(
    current: SessionDeviceSettings | DeviceSettingsSnapshot,
    requested: SessionDeviceSettings,
) -> bool:
    return _session_settings(current) != requested


def apply_device_settings(
    session: PressureHidppSession | Any,
    settings: SessionDeviceSettings,
    *,
    current_settings: DeviceSettingsSnapshot | None = None,
) -> SessionDeviceSettings:
    """Apply only changed settings, temporarily leaving onboard-profile mode."""
    current = current_settings or read_device_settings(
        session,
        discover_feature=False,
    )
    dpi = current.session.dpi
    left = current.session.haptic_left
    right = current.session.haptic_right
    if settings.dpi != dpi:
        if current.onboard_profiles_enabled:
            profile_writer = getattr(session, "set_onboard_profile_state", None)
            if not callable(profile_writer):
                raise RuntimeError(
                    "DPI is controlled by an onboard profile that cannot be "
                    "temporarily disabled"
                )
            profile_writer(enabled=False)
        dpi = session.set_dpi(settings.dpi)
    if settings.haptic_left != left or settings.haptic_right != right:
        left, right = session.set_haptic_levels(
            left=settings.haptic_left,
            right=settings.haptic_right,
        )
    return SessionDeviceSettings(
        dpi=int(dpi),
        haptic_left=int(left),
        haptic_right=int(right),
    )


def restore_device_settings(
    session: PressureHidppSession | Any,
    settings: DeviceSettingsSnapshot,
    *,
    discover_feature: bool = True,
) -> SessionDeviceSettings:
    """Restore DPI, haptics, and the original onboard-profile state."""
    current = read_device_settings(session, discover_feature=discover_feature)
    restored = apply_device_settings(
        session,
        settings.session,
        current_settings=current,
    )
    original_profiles_enabled = settings.onboard_profiles_enabled
    original_profile_sector = settings.onboard_profile_sector
    current_profiles_enabled = current.onboard_profiles_enabled
    current_profile_sector = current.onboard_profile_sector
    profile_reader = getattr(session, "get_onboard_profile_state", None)
    if original_profiles_enabled is not None and callable(profile_reader):
        current_profiles_enabled, current_profile_sector = profile_reader()
        current_profiles_enabled = bool(current_profiles_enabled)
        current_profile_sector = int(current_profile_sector or 0)
    if original_profiles_enabled is not None and (
        original_profiles_enabled != current_profiles_enabled
        or (
            original_profiles_enabled
            and original_profile_sector != current_profile_sector
        )
    ):
        profile_writer = getattr(session, "set_onboard_profile_state", None)
        if not callable(profile_writer):
            raise RuntimeError("Could not restore the original onboard profile")
        profile_writer(
            enabled=original_profiles_enabled,
            active_sector=(
                original_profile_sector if original_profiles_enabled else None
            ),
        )
    return restored
