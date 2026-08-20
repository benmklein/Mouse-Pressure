"""Validation errors and helpers shared by runtime configuration callers."""
from __future__ import annotations


class ValidationError(ValueError):
    pass


class SchemaMismatchError(ValueError):
    pass


class StreamAlreadyActiveError(RuntimeError):
    pass


class StreamNotActiveError(RuntimeError):
    pass


_PROTOCOL_CURVES = {"linear", "soft", "hard", "scurve"}
_CONTACT_PRESETS = {"light", "medium", "firm"}
_OUTPUT_TARGETS = {"pressure", "x_tilt"}
CURVE_STRENGTH_MIN = 0.5
CURVE_STRENGTH_MAX = 4.0


def deadzone_pct_to_float(pct: int) -> float:
    return pct / 100.0


def validate_channel_config(ch: dict) -> list[str]:
    """Return validation errors for a channel config. Empty list means valid."""
    errors: list[str] = []

    output_target = ch.get("output_target", "pressure")
    raw_min = ch.get("raw_min")
    raw_max = ch.get("raw_max")
    deadzone_low = ch.get("deadzone_low")
    deadzone_high = ch.get("deadzone_high")
    curve = ch.get("curve")
    curve_strength = ch.get("curve_strength")
    contact_preset = ch.get("contact_preset")
    pressure_floor = ch.get("pressure_floor")
    path_stabilization = ch.get("path_stabilization")
    pressure_influence = ch.get("pressure_influence")
    onset_buffer = ch.get("onset_buffer")
    # Optional for backward-compatible validation of version-1 channel payloads.
    true_low_latency = ch.get("true_low_latency", False)
    stationary_pressure_updates = ch.get("stationary_pressure_updates", False)
    immediate_button_wake = ch.get("immediate_button_wake", False)
    clean_stroke_endings = ch.get("clean_stroke_endings", False)

    if not isinstance(output_target, str):
        errors.append("output_target must be a string")
    elif output_target not in _OUTPUT_TARGETS:
        errors.append("output_target must be one of: pressure, x_tilt")

    if not isinstance(raw_min, int):
        errors.append("raw_min must be an integer")
    if not isinstance(raw_max, int):
        errors.append("raw_max must be an integer")
    if isinstance(raw_min, int) and isinstance(raw_max, int):
        if not (0 <= raw_min <= 1023):
            errors.append("raw_min must be in 0..1023")
        if not (0 <= raw_max <= 1023):
            errors.append("raw_max must be in 0..1023")
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
        if not (CURVE_STRENGTH_MIN <= strength <= CURVE_STRENGTH_MAX):
            errors.append(
                f"curve_strength must be in "
                f"{CURVE_STRENGTH_MIN:g}..{CURVE_STRENGTH_MAX:g}"
            )

    if not isinstance(contact_preset, str):
        errors.append("contact_preset must be a string")
    elif contact_preset not in _CONTACT_PRESETS:
        errors.append("contact_preset must be one of: light, medium, firm")

    if not isinstance(pressure_floor, int):
        errors.append("pressure_floor must be an integer")
    elif not (0 <= pressure_floor <= 100):
        errors.append("pressure_floor must be in 0..100")

    if not isinstance(path_stabilization, int):
        errors.append("path_stabilization must be an integer")
    elif not (0 <= path_stabilization <= 100):
        errors.append("path_stabilization must be in 0..100")

    if not isinstance(pressure_influence, int):
        errors.append("pressure_influence must be an integer")
    elif not (0 <= pressure_influence <= 100):
        errors.append("pressure_influence must be in 0..100")

    if not isinstance(onset_buffer, bool):
        errors.append("onset_buffer must be a boolean")

    if not isinstance(true_low_latency, bool):
        errors.append("true_low_latency must be a boolean")

    if not isinstance(stationary_pressure_updates, bool):
        errors.append("stationary_pressure_updates must be a boolean")

    if not isinstance(immediate_button_wake, bool):
        errors.append("immediate_button_wake must be a boolean")

    if not isinstance(clean_stroke_endings, bool):
        errors.append("clean_stroke_endings must be a boolean")

    return errors
