"""Runtime configuration persistence."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mouse_pressure.bridge.config import ChannelConfig, RuntimeConfig
from mouse_pressure.web.models import (
    SchemaMismatchError,
    ValidationError,
    validate_channel_config,
    validate_process_name,
)

SCHEMA_VERSION = 1


def resolve_config_dir(config_dir: str | Path | None = None) -> Path:
    """Resolve config directory from explicit arg, env var, then default."""
    if config_dir is not None:
        return Path(config_dir).expanduser()
    env_dir = os.environ.get("MOUSE_PRESSURE_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".mouse-pressure"


def _normalize_app_profiles(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError("app_profiles must be an object")

    out: dict[str, str] = {}
    for proc, profile_name in raw.items():
        if not isinstance(proc, str):
            raise ValidationError("app_profiles keys must be strings")
        proc_errors = validate_process_name(proc)
        if proc_errors:
            raise ValidationError(proc_errors[0])
        if not isinstance(profile_name, str):
            raise ValidationError("app_profiles values must be strings")
        out[proc] = profile_name
    return out


def _channel_from_dict(raw: Any, defaults: ChannelConfig) -> ChannelConfig:
    if not isinstance(raw, dict):
        raise ValidationError("channel config must be an object")

    raw_min = int(raw.get("raw_min", defaults.raw_min))
    raw_max = int(raw.get("raw_max", defaults.raw_max))
    # Version-1 configs originally stored only the high byte of each ADC word.
    # A real supported-device rest value is above 255 in the decoded 10-bit space,
    # so a pair wholly in the byte range can be upgraded without ambiguity.
    if raw_min <= 0xFF and raw_max <= 0xFF:
        raw_min *= 4
        raw_max *= 4

    channel = ChannelConfig(
        output_target=str(raw.get("output_target", defaults.output_target)),
        raw_min=raw_min,
        raw_max=raw_max,
        deadzone_low=int(raw.get("deadzone_low", defaults.deadzone_low)),
        deadzone_high=int(raw.get("deadzone_high", defaults.deadzone_high)),
        curve=str(raw.get("curve", defaults.curve)),
        curve_strength=float(raw.get("curve_strength", defaults.curve_strength)),
        contact_preset=str(raw.get("contact_preset", defaults.contact_preset)),
        pressure_floor=int(raw.get("pressure_floor", defaults.pressure_floor)),
        path_stabilization=int(
            raw.get("path_stabilization", defaults.path_stabilization)
        ),
        pressure_influence=int(
            raw.get("pressure_influence", defaults.pressure_influence)
        ),
        onset_buffer=raw.get("onset_buffer", defaults.onset_buffer),
        true_low_latency=raw.get(
            "true_low_latency", defaults.true_low_latency
        ),
        stationary_pressure_updates=raw.get(
            "stationary_pressure_updates",
            defaults.stationary_pressure_updates,
        ),
        immediate_button_wake=raw.get(
            "immediate_button_wake",
            defaults.immediate_button_wake,
        ),
        clean_stroke_endings=raw.get(
            "clean_stroke_endings",
            defaults.clean_stroke_endings,
        ),
    )
    errors = validate_channel_config(asdict(channel))
    if errors:
        raise ValidationError(errors[0])
    return channel


def runtime_config_from_dict(raw: Any) -> RuntimeConfig:
    if not isinstance(raw, dict):
        raise ValidationError("config must be an object")

    schema_version = raw.get("schema_version", SCHEMA_VERSION)
    if schema_version != SCHEMA_VERSION:
        raise SchemaMismatchError(
            f"Unsupported schema_version: {schema_version!r}; expected {SCHEMA_VERSION}"
        )

    linked = raw.get("linked", RuntimeConfig.linked)
    if not isinstance(linked, bool):
        raise ValidationError("linked must be a boolean")

    left_enabled = raw.get("left_enabled", RuntimeConfig.left_enabled)
    if not isinstance(left_enabled, bool):
        raise ValidationError("left_enabled must be a boolean")

    right_enabled = raw.get("right_enabled", RuntimeConfig.right_enabled)
    if not isinstance(right_enabled, bool):
        raise ValidationError("right_enabled must be a boolean")

    suppress_lmb = raw.get("suppress_lmb", RuntimeConfig.suppress_lmb)
    if not isinstance(suppress_lmb, bool):
        raise ValidationError("suppress_lmb must be a boolean")

    suppress_rmb = raw.get("suppress_rmb", RuntimeConfig.suppress_rmb)
    if not isinstance(suppress_rmb, bool):
        raise ValidationError("suppress_rmb must be a boolean")

    debug_mode = raw.get("debug_mode", RuntimeConfig.debug_mode)
    if not isinstance(debug_mode, bool):
        raise ValidationError("debug_mode must be a boolean")

    minimize_to_tray = raw.get(
        "minimize_to_tray", RuntimeConfig.minimize_to_tray
    )
    if not isinstance(minimize_to_tray, bool):
        raise ValidationError("minimize_to_tray must be a boolean")

    release_teardown = raw.get("release_teardown", RuntimeConfig.release_teardown)
    if not isinstance(release_teardown, bool):
        raise ValidationError("release_teardown must be a boolean")

    session_dpi = int(raw.get("session_dpi", RuntimeConfig.session_dpi))
    session_haptic_left = int(
        raw.get("session_haptic_left", RuntimeConfig.session_haptic_left)
    )
    session_haptic_right = int(
        raw.get("session_haptic_right", RuntimeConfig.session_haptic_right)
    )
    if not 100 <= session_dpi <= 32000 or session_dpi % 50 != 0:
        raise ValidationError("session_dpi must be 100..32000 in 50-DPI increments")
    if not 0 <= session_haptic_left <= 5 or not 0 <= session_haptic_right <= 5:
        raise ValidationError("session haptic levels must be in 0..5")

    follow_normal = raw.get(
        "session_device_settings_follow_normal",
        RuntimeConfig.session_device_settings_follow_normal,
    )
    if not isinstance(follow_normal, bool):
        raise ValidationError("session_device_settings_follow_normal must be a boolean")

    defaults = RuntimeConfig()

    return RuntimeConfig(
        schema_version=SCHEMA_VERSION,
        linked=linked,
        left_enabled=left_enabled,
        right_enabled=right_enabled,
        suppress_lmb=suppress_lmb,
        suppress_rmb=suppress_rmb,
        debug_mode=debug_mode,
        minimize_to_tray=minimize_to_tray,
        release_teardown=release_teardown,
        session_dpi=session_dpi,
        session_haptic_left=session_haptic_left,
        session_haptic_right=session_haptic_right,
        session_device_settings_follow_normal=follow_normal,
        left=_channel_from_dict(raw.get("left", {}), defaults.left),
        right=_channel_from_dict(raw.get("right", {}), defaults.right),
        app_profiles=_normalize_app_profiles(raw.get("app_profiles")),
    )


def runtime_config_to_dict(config: RuntimeConfig) -> dict[str, Any]:
    """Serialize RuntimeConfig into protocol/storage JSON shape."""
    return {
        "schema_version": config.schema_version,
        "linked": config.linked,
        "left_enabled": config.left_enabled,
        "right_enabled": config.right_enabled,
        "suppress_lmb": config.suppress_lmb,
        "suppress_rmb": config.suppress_rmb,
        "debug_mode": config.debug_mode,
        "minimize_to_tray": config.minimize_to_tray,
        "release_teardown": config.release_teardown,
        "session_dpi": config.session_dpi,
        "session_haptic_left": config.session_haptic_left,
        "session_haptic_right": config.session_haptic_right,
        "session_device_settings_follow_normal": (
            config.session_device_settings_follow_normal
        ),
        "left": asdict(config.left),
        "right": asdict(config.right),
        "app_profiles": dict(config.app_profiles),
    }


class ConfigStore:
    """Read/write bridge runtime config on disk."""

    def __init__(self, config_dir: str | Path | None = None) -> None:
        self.config_dir = resolve_config_dir(config_dir)
        self.path = self.config_dir / "config.json"

    def load(self) -> RuntimeConfig:
        if not self.path.exists():
            return RuntimeConfig()
        with self.path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return runtime_config_from_dict(raw)

    def save(self, config: RuntimeConfig) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        payload = runtime_config_to_dict(config)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, self.path)
