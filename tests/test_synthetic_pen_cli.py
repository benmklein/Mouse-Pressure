from __future__ import annotations

from superstrike_pressure.bridge.synthetic_pen_cli import build_parser


def test_parser_uses_proven_bridge_defaults() -> None:
    args = build_parser().parse_args([])

    assert args.mode == 3
    assert args.hz == 240.0
    assert args.raw_min == 320
    assert args.raw_max == 680
    assert args.curve == "scurve"
    assert args.contact_source == "lmb_and_pressure"
    assert args.release_teardown is False
    assert args.log_file == "pressure-bridge.log"


def test_parser_accepts_release_and_calibration_options() -> None:
    args = build_parser().parse_args(
        [
            "--raw-min",
            "304",
            "--raw-max",
            "656",
            "--release-teardown",
            "--suppress-lmb",
        ]
    )

    assert args.raw_min == 304
    assert args.raw_max == 656
    assert args.release_teardown is True
    assert args.suppress_lmb is True
