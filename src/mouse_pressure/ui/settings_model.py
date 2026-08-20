"""Toolkit-independent editable settings and pressure presentation rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from mouse_pressure.bridge.config import ChannelConfig, RuntimeConfig
from mouse_pressure.bridge.curves import (
    map_pressure,
    pressure_config_for_channel,
)
from mouse_pressure.runtime.config_store import runtime_config_to_dict
from mouse_pressure.runtime.device_settings import SessionDeviceSettings
from mouse_pressure.runtime.models import validate_channel_config


def curve_pressure_for_raw(channel: ChannelConfig, raw: int) -> int:
    pressure_config = pressure_config_for_channel(channel)
    return max(0, min(1024, int(map_pressure(raw, pressure_config))))


def effective_pressure_for_raw(channel: ChannelConfig, raw: int) -> int:
    floor = round(channel.pressure_floor * 1024 / 100)
    mapped = curve_pressure_for_raw(channel, raw)
    if mapped > 0 and channel.pressure_influence < 100:
        mapped = round(512 + (mapped - 512) * channel.pressure_influence / 100.0)
    if mapped > 0 and floor > 0:
        mapped = max(mapped, floor)
    return max(0, min(1024, int(mapped)))


@dataclass(frozen=True)
class SettingsDraft:
    """One validated snapshot of every editable driver setting."""

    config: RuntimeConfig
    injection_hz: float
    normal_device: SessionDeviceSettings | None

    def validate(self) -> None:
        errors: list[str] = []
        for name, channel in (("Left", self.config.left), ("Right", self.config.right)):
            errors.extend(
                f"{name}: {error}" for error in validate_channel_config(asdict(channel))
            )
        if not 30.0 <= float(self.injection_hz) <= 500.0:
            errors.append("Pen injection rate must be between 30 and 500 Hz")
        targets = [
            target
            for enabled, target in (
                (self.config.left_enabled, self.config.left.output_target),
                (
                    self.config.right_enabled,
                    self.effective_channel("right").output_target,
                ),
            )
            if enabled
        ]
        if targets and "pressure" not in targets:
            errors.append(
                "At least one enabled button must map to Pressure; "
                "X-tilt modifies an active pressure stroke"
            )
        if errors:
            raise ValueError(". ".join(errors) + ".")

    def effective_channel(self, channel: str) -> ChannelConfig:
        if channel not in {"left", "right"}:
            raise ValueError(f"Unknown pressure channel {channel!r}")
        if channel == "right" and self.config.linked:
            return self.config.left
        return self.config.left if channel == "left" else self.config.right

    def effective_pressure(self, channel: str, raw: int) -> int:
        return effective_pressure_for_raw(self.effective_channel(channel), raw)

    def mapping_points(
        self,
        channel: str,
        *,
        raw_start: int,
        raw_end: int,
        step: int = 4,
    ) -> list[tuple[int, int]]:
        increment = max(1, int(step))
        return [
            (raw, self.effective_pressure(channel, raw))
            for raw in range(int(raw_start), int(raw_end) + 1, increment)
        ]

    def runtime_patch(self) -> dict:
        self.validate()
        patch = runtime_config_to_dict(self.config)
        normal = self.normal_device
        patch["session_device_settings_follow_normal"] = bool(
            normal is not None
            and self.config.session_dpi == normal.dpi
            and self.config.session_haptic_left == normal.haptic_left
            and self.config.session_haptic_right == normal.haptic_right
        )
        return patch

    def reset_channel(self, channel: str) -> SettingsDraft:
        if channel not in {"left", "right"}:
            raise ValueError(f"Unknown pressure channel {channel!r}")
        defaults = RuntimeConfig()
        if channel == "left":
            config = replace(
                self.config,
                left=defaults.left,
                suppress_lmb=defaults.suppress_lmb,
            )
        else:
            config = replace(
                self.config,
                right=defaults.right,
                suppress_rmb=defaults.suppress_rmb,
            )
        return replace(self, config=config)
