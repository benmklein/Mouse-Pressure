"""Validation models/helpers shared by CLI and WS layers."""

from __future__ import annotations

import re


class ValidationError(ValueError):
    pass


class ProfileNotFoundError(FileNotFoundError):
    pass


class SchemaMismatchError(ValueError):
    pass


class StreamAlreadyActiveError(RuntimeError):
    pass


class StreamNotActiveError(RuntimeError):
    pass


_PROTOCOL_CURVES = {"linear", "soft", "hard", "scurve"}
_CONTACT_PRESETS = {"light", "medium", "firm"}
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9 _-]{1,64}$")


def deadzone_pct_to_float(pct: int) -> float:
    return pct / 100.0


def validate_channel_config(ch: dict) -> list[str]:
    """Return validation errors for a channel config. Empty list means valid."""
    errors: list[str] = []

    raw_min = ch.get("raw_min")
    raw_max = ch.get("raw_max")
    deadzone_low = ch.get("deadzone_low")
    deadzone_high = ch.get("deadzone_high")
    curve = ch.get("curve")
    curve_strength = ch.get("curve_strength")
    contact_preset = ch.get("contact_preset")

    if not isinstance(raw_min, int):
        errors.append("raw_min must be an integer")
    if not isinstance(raw_max, int):
        errors.append("raw_max must be an integer")
    if isinstance(raw_min, int) and isinstance(raw_max, int):
        if not (50 <= raw_min <= 150):
            errors.append("raw_min must be in 50..150")
        if not (120 <= raw_max <= 220):
            errors.append("raw_max must be in 120..220")
        if raw_min >= raw_max:
            errors.append("raw_min must be strictly less than raw_max")

    if not isinstance(deadzone_low, int):
        errors.append("deadzone_low must be an integer")
    if not isinstance(deadzone_high, int):
        errors.append("deadzone_high must be an integer")
    if isinstance(deadzone_low, int) and isinstance(deadzone_high, int):
        if not (0 <= deadzone_low <= 20):
            errors.append("deadzone_low must be in 0..20")
        if not (0 <= deadzone_high <= 20):
            errors.append("deadzone_high must be in 0..20")
        if deadzone_low > deadzone_high:
            errors.append("deadzone_low must be <= deadzone_high")

    if not isinstance(curve, str):
        errors.append("curve must be a string")
    elif curve not in _PROTOCOL_CURVES:
        errors.append("curve must be one of: linear, soft, hard, scurve")

    if not isinstance(curve_strength, (int, float)):
        errors.append("curve_strength must be numeric")
    else:
        strength = float(curve_strength)
        if not (0.5 <= strength <= 2.0):
            errors.append("curve_strength must be in 0.5..2.0")

    if not isinstance(contact_preset, str):
        errors.append("contact_preset must be a string")
    elif contact_preset not in _CONTACT_PRESETS:
        errors.append("contact_preset must be one of: light, medium, firm")

    return errors


def validate_profile_name(name: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(name, str):
        return ["profile name must be a string"]
    stripped = name.strip()
    if not stripped:
        errors.append("profile name cannot be empty")
    if len(stripped) > 64:
        errors.append("profile name must be 1..64 chars")
    if not _PROFILE_NAME_RE.fullmatch(stripped):
        errors.append("profile name may only contain alphanumeric, spaces, hyphen, underscore")
    return errors


def validate_process_name(proc: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(proc, str):
        return ["process name must be a string"]
    if len(proc) < 1 or len(proc) > 128:
        errors.append("process name must be 1..128 chars")
    if "/" in proc or "\\" in proc:
        errors.append("process name must not contain path separators")
    if not proc.lower().endswith(".exe"):
        errors.append("process name must end with .exe")
    return errors

