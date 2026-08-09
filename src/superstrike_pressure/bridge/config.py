"""Canonical runtime and launch configuration models for the bridge."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChannelConfig:
    raw_min: int = 350
    raw_max: int = 680
    deadzone_low: int = 0
    deadzone_high: int = 0
    curve: str = "soft"
    curve_strength: float = 3.0
    contact_preset: str = "firm"
    # Prevent very low pressure samples from stretching into a long hairline.
    # Zero still means pen-up; this floor applies only while contact is active.
    pressure_floor: int = 25
    # Causal, bounded-lag center-path cleanup. Zero is the direct/raw path.
    path_stabilization: int = 0
    # How strongly real pressure changes affect width. 100 preserves the
    # sensor exactly; lower values compress variation toward mid-pressure.
    pressure_influence: int = 100
    # Optional one-hardware-sample delay for extra-smooth stroke starts.
    # Disabled by default when Krita owns geometry stabilization.
    onset_buffer: bool = False
    # Favor the newest physical position and pressure over interpolation.
    # Intended for applications such as Krita that already smooth geometry.
    true_low_latency: bool = False
    # Re-emit a held contact point when a fresh hardware pressure sample
    # changes meaningfully, allowing pressure-sensitive stationary dabs.
    stationary_pressure_updates: bool = False
    # End synthetic contact immediately on the first fresh pressure sample at
    # or below this percentage, then wait for physical button-up before a new
    # stroke. Zero disables the drawing-specific rapid-release behavior.
    rapid_release_threshold: int = 2


@dataclass
class RuntimeConfig:
    schema_version: int = 1
    linked: bool = False
    left_enabled: bool = True
    right_enabled: bool = False
    suppress_lmb: bool = True
    suppress_rmb: bool = True
    # Experimental: keep LMB as pen pressure and publish RMB pressure as X-Tilt.
    rmb_aux_xtilt: bool = False
    # Capture detailed per-stroke traces and verbose input diagnostics.
    debug_mode: bool = False
    # Hide the desktop control panel in the notification area when minimized.
    minimize_to_tray: bool = True
    release_teardown: bool = False
    session_dpi: int = 800
    session_haptic_left: int = 3
    session_haptic_right: int = 3
    # Until a Mapping-on hardware value is intentionally changed, mirror the
    # DPI and haptics detected in the Mapping-off state.
    session_device_settings_follow_normal: bool = True
    left: ChannelConfig = field(default_factory=ChannelConfig)
    right: ChannelConfig = field(
        default_factory=lambda: ChannelConfig(curve_strength=3.1)
    )
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

