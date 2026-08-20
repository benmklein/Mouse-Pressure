"""Pressure calibration and curve mapping."""

from dataclasses import dataclass

from mouse_pressure.bridge.config import ChannelConfig

_CURVE_ALIASES = {
    "linear": "linear",
    "soft": "ease_in",
    "hard": "ease_out",
    "scurve": "s_curve",
    # Legacy aliases (deprecated at CLI boundary, still accepted)
    "ease_in": "ease_in",
    "ease_out": "ease_out",
    "s_curve": "s_curve",
}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


@dataclass
class PressureConfig:
    """Configuration for pressure mapping."""
    raw_min: int = 320        # 10-bit ADC code at rest
    raw_max: int = 680        # 10-bit ADC code at full press
    out_min: int = 0          # Minimum output pressure
    out_max: int = 1023       # Maximum output pressure (Wintab standard)
    deadzone_low: float = 0.05   # Ignore bottom 5% of range (noise floor)
    deadzone_high: float = 0.95  # Cap top 5% (avoid needing max force)
    curve: str = "s_curve"       # linear, ease_in, ease_out, s_curve
    curve_strength: float = 2.0  # How aggressive the curve is (1.0 = mild)


def pressure_config_for_channel(channel: ChannelConfig) -> PressureConfig:
    """Compile one channel's persisted settings into mapping semantics."""
    return PressureConfig(
        raw_min=channel.raw_min,
        raw_max=channel.raw_max,
        out_min=0,
        out_max=1023,
        deadzone_low=channel.deadzone_low / 100.0,
        deadzone_high=1.0 - channel.deadzone_high / 100.0,
        curve=normalize_curve_name(channel.curve),
        curve_strength=channel.curve_strength,
    )


def normalize_curve_name(name: str) -> str:
    normalized = _CURVE_ALIASES.get(str(name).lower())
    if normalized is None:
        raise ValueError(f"Unknown curve name: {name!r}")
    return normalized


def apply_curve(normalized: float, config: PressureConfig) -> float:
    """Apply pressure curve to a 0-1 normalized value.
    
    Args:
        normalized: Input pressure, 0.0 to 1.0
        config: Pressure curve configuration
        
    Returns:
        Curved pressure value, 0.0 to 1.0
    """
    t = _clamp01(normalized)
    gamma = config.curve_strength
    curve = normalize_curve_name(config.curve)

    if curve == "linear":
        return float(t)

    elif curve == "ease_in":
        # Power curve — slow start, fast finish
        return float(t ** gamma)

    elif curve == "ease_out":
        # Inverse power — fast start, slow finish
        return float(1.0 - (1.0 - t) ** gamma)

    elif curve == "s_curve":
        # Hermite-style S-curve — soft start and end
        if t < 0.5:
            return float(0.5 * (2.0 * t) ** gamma)
        else:
            return float(1.0 - 0.5 * (2.0 * (1.0 - t)) ** gamma)
    
    return float(t)


def map_pressure(raw_value: int, config: PressureConfig) -> int:
    """Map a raw analog value to output pressure.
    
    Args:
        raw_value: Raw value from the compatible HID report
        config: Pressure mapping configuration
        
    Returns:
        Mapped pressure value in the output range
    """
    # Normalize to 0-1 within the raw range
    raw_range = config.raw_max - config.raw_min
    if raw_range <= 0:
        return config.out_min
    
    normalized = (raw_value - config.raw_min) / raw_range
    normalized = _clamp01(normalized)
    
    # Apply deadzones
    if normalized < config.deadzone_low:
        return config.out_min
    if normalized > config.deadzone_high:
        return config.out_max
    
    # Rescale within deadzones
    active_range = config.deadzone_high - config.deadzone_low
    normalized = (normalized - config.deadzone_low) / active_range
    normalized = _clamp01(normalized)
    
    # Apply curve
    curved = apply_curve(normalized, config)
    
    # Map to output range
    out_range = config.out_max - config.out_min
    return int(config.out_min + curved * out_range)


def map_normalized_pressure(normalized: float, config: PressureConfig) -> int:
    """Map normalized pressure (0..1) to output range using configured curve."""
    t = _clamp01(normalized)

    if t < config.deadzone_low:
        return config.out_min
    if t > config.deadzone_high:
        return config.out_max

    active_range = config.deadzone_high - config.deadzone_low
    if active_range <= 0.0:
        return config.out_min

    t = (t - config.deadzone_low) / active_range
    t = _clamp01(t)
    curved = apply_curve(t, config)
    out_range = config.out_max - config.out_min
    return int(config.out_min + curved * out_range)
