"""Command-line interface for the Windows synthetic-pen bridge."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from mouse_pressure.bridge.curves import PressureConfig, normalize_curve_name
from mouse_pressure.bridge.synthetic_pen import (
    SyntheticPenConfig,
    run_synthetic_pen_bridge,
)

CURVE_CHOICES = [
    "linear",
    "soft",
    "hard",
    "scurve",
    # Deprecated legacy aliases (kept for compatibility).
    "ease_in",
    "ease_out",
    "s_curve",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read compatible analog mouse pressure and inject Windows "
            "synthetic pen input with real pressure."
        )
    )
    parser.add_argument("--hz", type=float, default=240.0, help="Loop/inject rate (default: 240).")
    parser.add_argument("--mode", type=int, default=3, help="Pressure mode byte (default: 3).")
    parser.add_argument("--mode-arg", type=int, default=0, help="Pressure mode arg (default: 0).")
    parser.add_argument("--raw-min", type=int, default=320, help="10-bit ADC min calibration (default: 320).")
    parser.add_argument("--raw-max", type=int, default=680, help="10-bit ADC max calibration (default: 680).")
    parser.add_argument(
        "--curve",
        choices=CURVE_CHOICES,
        default="scurve",
        help="Pressure curve (default: scurve).",
    )
    parser.add_argument(
        "--curve-strength",
        type=float,
        default=2.0,
        help="Curve strength (default: 2.0).",
    )
    parser.add_argument(
        "--deadzone-low",
        type=float,
        default=0.05,
        help="Low deadzone (default: 0.05).",
    )
    parser.add_argument(
        "--deadzone-high",
        type=float,
        default=0.95,
        help="High deadzone (default: 0.95).",
    )
    parser.add_argument(
        "--contact-threshold",
        type=int,
        default=10,
        help="Mapped pressure threshold (0..1023) for contact (default: 10).",
    )
    parser.add_argument(
        "--release-threshold",
        type=int,
        default=6,
        help="Mapped pressure release threshold (0..1023, default: 6).",
    )
    parser.add_argument(
        "--contact-source",
        choices=["lmb_and_pressure", "pressure_only"],
        default="lmb_and_pressure",
        help="Contact gating source (default: lmb_and_pressure).",
    )
    parser.add_argument(
        "--pressure-mode",
        choices=["absolute", "stroke_relative"],
        default="absolute",
        help="Pressure mapping mode (default: absolute).",
    )
    parser.add_argument("--duration", type=float, default=None, help="Optional runtime duration (seconds).")
    parser.add_argument(
        "--suppress-lmb",
        action="store_true",
        help="Suppress native left mouse click events while bridge is running.",
    )
    parser.add_argument(
        "--no-click-through",
        action="store_true",
        help="Disable synthetic passthrough clicks when --suppress-lmb is active.",
    )
    parser.add_argument(
        "--click-max-ms",
        type=int,
        default=220,
        help="Max hold duration for passthrough click (default: 220ms).",
    )
    parser.add_argument(
        "--click-move-px",
        type=int,
        default=6,
        help="Max cursor movement for passthrough click (default: 6px Manhattan).",
    )
    parser.add_argument(
        "--click-pressure-max",
        type=int,
        default=12,
        help="Max mapped pressure allowed for passthrough click (default: 12).",
    )
    parser.add_argument(
        "--rise-per-frame",
        type=int,
        default=256,
        help="Max pressure rise per decoded frame (0..1024, default: 256).",
    )
    parser.add_argument(
        "--fall-per-frame",
        type=int,
        default=512,
        help="Max pressure fall per decoded frame (0..1024, default: 512).",
    )
    parser.add_argument(
        "--min-contact-pressure",
        type=int,
        default=0,
        help="Minimum pen pressure while in contact (0..1024, default: 0).",
    )
    parser.add_argument(
        "--release-teardown",
        action="store_true",
        help="After pen UP, also emit hover+end-hover frames to end the in-range session.",
    )
    parser.add_argument(
        "--log-file",
        default="mouse-pressure.log",
        help="Log file path (default: mouse-pressure.log).",
    )
    parser.add_argument(
        "--trace-dir",
        default=None,
        help="Optional directory for structured per-stroke diagnostic traces.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if sys.platform != "win32":
        print("ERROR: Mouse Pressure Driver is Windows-only.")
        return 1

    args = build_parser().parse_args(argv)
    curve_name = normalize_curve_name(args.curve)
    pressure_config = PressureConfig(
        raw_min=args.raw_min,
        raw_max=args.raw_max,
        out_min=0,
        out_max=1023,
        deadzone_low=args.deadzone_low,
        deadzone_high=args.deadzone_high,
        curve=curve_name,
        curve_strength=args.curve_strength,
    )
    emitter_config = SyntheticPenConfig(
        contact_threshold=args.contact_threshold,
        release_threshold=args.release_threshold,
        contact_source=args.contact_source,
        pressure_mode=args.pressure_mode,
        rise_per_frame=args.rise_per_frame,
        fall_per_frame=args.fall_per_frame,
        min_contact_pressure=args.min_contact_pressure,
        suppress_lmb=args.suppress_lmb,
        no_click_through=args.no_click_through,
        click_max_ms=args.click_max_ms,
        click_move_px=args.click_move_px,
        click_pressure_max=args.click_pressure_max,
        release_teardown=args.release_teardown,
        trace_dir=args.trace_dir,
    )
    return run_synthetic_pen_bridge(
        emitter_config=emitter_config,
        pressure_config=pressure_config,
        mode=args.mode,
        mode_arg=args.mode_arg,
        hz=args.hz,
        duration=args.duration,
        log_file=args.log_file,
    )


if __name__ == "__main__":
    raise SystemExit(main())
