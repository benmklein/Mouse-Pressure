"""
Pressure curve mapping.

VIBE CODE INSTRUCTIONS:
-----------------------
This maps the raw analog value from the Superstrike to a pressure value
that drawing apps expect. Different curves feel different:

  - Linear: direct 1:1 mapping. Simple but may feel stiff.
  - Ease-in (soft start): light pressure = very little output, heavy = ramps up.
    Good for detail work where you want light default.
  - Ease-out (soft end): light pressure = immediate response, heavy = plateaus.
    Good for broad strokes where you want quick coverage.  
  - S-curve: soft start AND soft end, most pressure change in the middle.
    Closest to how most Wacom tablets feel by default.

The raw range and output range need to be configured once we know
what the Superstrike actually reports (Phase 1 discovery).
"""

import numpy as np
from dataclasses import dataclass

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
    t = np.clip(normalized, 0.0, 1.0)
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
        raw_value: Raw value from the Superstrike HID report
        config: Pressure mapping configuration
        
    Returns:
        Mapped pressure value in the output range
    """
    # Normalize to 0-1 within the raw range
    raw_range = config.raw_max - config.raw_min
    if raw_range <= 0:
        return config.out_min
    
    normalized = (raw_value - config.raw_min) / raw_range
    normalized = np.clip(normalized, 0.0, 1.0)
    
    # Apply deadzones
    if normalized < config.deadzone_low:
        return config.out_min
    if normalized > config.deadzone_high:
        return config.out_max
    
    # Rescale within deadzones
    active_range = config.deadzone_high - config.deadzone_low
    normalized = (normalized - config.deadzone_low) / active_range
    normalized = np.clip(normalized, 0.0, 1.0)
    
    # Apply curve
    curved = apply_curve(normalized, config)
    
    # Map to output range
    out_range = config.out_max - config.out_min
    return int(config.out_min + curved * out_range)


def map_normalized_pressure(normalized: float, config: PressureConfig) -> int:
    """Map normalized pressure (0..1) to output range using configured curve."""
    t = float(np.clip(normalized, 0.0, 1.0))

    if t < config.deadzone_low:
        return config.out_min
    if t > config.deadzone_high:
        return config.out_max

    active_range = config.deadzone_high - config.deadzone_low
    if active_range <= 0.0:
        return config.out_min

    t = (t - config.deadzone_low) / active_range
    t = float(np.clip(t, 0.0, 1.0))
    curved = apply_curve(t, config)
    out_range = config.out_max - config.out_min
    return int(config.out_min + curved * out_range)
