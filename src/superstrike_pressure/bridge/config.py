"""Canonical runtime and launch configuration models for the bridge."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChannelConfig:
    raw_min: int = 320
    raw_max: int = 740
    deadzone_low: int = 0
    deadzone_high: int = 0
    curve: str = "linear"
    curve_strength: float = 1.0
    contact_preset: str = "medium"
    # Prevent very low pressure samples from stretching into a long hairline.
    # Zero still means pen-up; this floor applies only while contact is active.
    pressure_floor: int = 12
    # Causal, bounded-lag center-path cleanup. Zero is the direct/raw path.
    path_stabilization: int = 0
    # How strongly real pressure changes affect width. 100 preserves the
    # sensor exactly; lower values compress variation toward mid-pressure.
    pressure_influence: int = 85
    # Optional one-hardware-sample delay for extra-smooth stroke starts.
    # Disabled by default when Krita owns geometry stabilization.
    onset_buffer: bool = False
    # Favor the newest physical position and pressure over interpolation.
    # Intended for applications such as Krita that already smooth geometry.
    true_low_latency: bool = False


@dataclass
class RuntimeConfig:
    schema_version: int = 1
    linked: bool = True
    suppress_lmb: bool = False
    suppress_rmb: bool = False
    release_teardown: bool = False
    session_dpi: int = 800
    session_haptic_left: int = 5
    session_haptic_right: int = 5
    left: ChannelConfig = field(default_factory=ChannelConfig)
    right: ChannelConfig = field(default_factory=ChannelConfig)
    app_profiles: dict[str, str] = field(default_factory=dict)


@dataclass
class LaunchConfig:
    mode: int = 3
    mode_arg: int = 0
    backend: str = "synthetic"
    # Pressure arrives at roughly 60 Hz, but cursor position can be sampled and
    # injected more frequently while holding the latest pressure value.
    hz: float = 240.0
    log_file: str | None = None
    config_dir: str | None = None
    trace_dir: str | None = None


CONTACT_PRESETS = {
    "light": {"contact_threshold": 6, "release_threshold": 4},
    "medium": {"contact_threshold": 10, "release_threshold": 6},
    "firm": {"contact_threshold": 18, "release_threshold": 12},
}

