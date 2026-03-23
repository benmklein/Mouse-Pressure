"""Canonical runtime and launch configuration models for the bridge."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChannelConfig:
    raw_min: int = 80
    raw_max: int = 185
    deadzone_low: int = 0
    deadzone_high: int = 0
    curve: str = "linear"
    curve_strength: float = 1.0
    contact_preset: str = "medium"


@dataclass
class RuntimeConfig:
    schema_version: int = 1
    linked: bool = True
    left: ChannelConfig = field(default_factory=ChannelConfig)
    right: ChannelConfig = field(default_factory=ChannelConfig)
    app_profiles: dict[str, str] = field(default_factory=dict)


@dataclass
class LaunchConfig:
    mode: int = 3
    mode_arg: int = 0
    backend: str = "synthetic"
    hz: float = 60.0
    log_file: str | None = None
    config_dir: str | None = None


CONTACT_PRESETS = {
    "light": {"contact_threshold": 6, "release_threshold": 4},
    "medium": {"contact_threshold": 10, "release_threshold": 6},
    "firm": {"contact_threshold": 18, "release_threshold": 12},
}

