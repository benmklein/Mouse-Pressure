"""Runtime configuration persistence."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from superstrike_pressure.bridge.config import ChannelConfig, RuntimeConfig
from superstrike_pressure.web.models import (
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
    env_dir = os.environ.get("SUPERSTRIKE_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".superstrike"


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


def _channel_from_dict(raw: Any) -> ChannelConfig:
    if not isinstance(raw, dict):
        raise ValidationError("channel config must be an object")

    raw_min = int(raw.get("raw_min", ChannelConfig.raw_min))
    raw_max = int(raw.get("raw_max", ChannelConfig.raw_max))
    # Version-1 configs originally stored only the high byte of each ADC word.
    # A real Superstrike rest value is above 255 in the decoded 10-bit space,
    # so a pair wholly in the byte range can be upgraded without ambiguity.
    if raw_min <= 0xFF and raw_max <= 0xFF:
        raw_min *= 4
        raw_max *= 4

    channel = ChannelConfig(
        raw_min=raw_min,
        raw_max=raw_max,
        deadzone_low=int(raw.get("deadzone_low", ChannelConfig.deadzone_low)),
        deadzone_high=int(raw.get("deadzone_high", ChannelConfig.deadzone_high)),
        curve=str(raw.get("curve", ChannelConfig.curve)),
        curve_strength=float(raw.get("curve_strength", ChannelConfig.curve_strength)),
        contact_preset=str(raw.get("contact_preset", ChannelConfig.contact_preset)),
        pressure_floor=int(raw.get("pressure_floor", ChannelConfig.pressure_floor)),
        path_stabilization=int(
            raw.get("path_stabilization", ChannelConfig.path_stabilization)
        ),
        pressure_influence=int(
            raw.get("pressure_influence", ChannelConfig.pressure_influence)
        ),
        onset_buffer=raw.get("onset_buffer", ChannelConfig.onset_buffer),
        true_low_latency=raw.get(
            "true_low_latency", ChannelConfig.true_low_latency
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

    linked = raw.get("linked", True)
    if not isinstance(linked, bool):
        raise ValidationError("linked must be a boolean")

    suppress_lmb = raw.get("suppress_lmb", False)
    if not isinstance(suppress_lmb, bool):
        raise ValidationError("suppress_lmb must be a boolean")

    suppress_rmb = raw.get("suppress_rmb", False)
    if not isinstance(suppress_rmb, bool):
        raise ValidationError("suppress_rmb must be a boolean")

    release_teardown = raw.get("release_teardown", False)
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

    return RuntimeConfig(
        schema_version=SCHEMA_VERSION,
        linked=linked,
        suppress_lmb=suppress_lmb,
        suppress_rmb=suppress_rmb,
        release_teardown=release_teardown,
        session_dpi=session_dpi,
        session_haptic_left=session_haptic_left,
        session_haptic_right=session_haptic_right,
        left=_channel_from_dict(raw.get("left", {})),
        right=_channel_from_dict(raw.get("right", {})),
        app_profiles=_normalize_app_profiles(raw.get("app_profiles")),
    )


def runtime_config_to_dict(config: RuntimeConfig) -> dict[str, Any]:
    """Serialize RuntimeConfig into protocol/storage JSON shape."""
    return {
        "schema_version": config.schema_version,
        "linked": config.linked,
        "suppress_lmb": config.suppress_lmb,
        "suppress_rmb": config.suppress_rmb,
        "release_teardown": config.release_teardown,
        "session_dpi": config.session_dpi,
        "session_haptic_left": config.session_haptic_left,
        "session_haptic_right": config.session_haptic_right,
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
