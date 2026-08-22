"""Measure raw analog values at each hardware actuation level.

Stop Mouse Pressure before running this tool. It temporarily changes the
selected button's hardware actuation level, captures deliberately slow clicks,
and restores both original levels on every normal/error/interrupt exit.
"""

from __future__ import annotations

import argparse
import json
import queue
import statistics
import time
from pathlib import Path
from typing import Any

from mouse_pressure.bridge.synthetic_pen import (
    RI_MOUSE_LEFT_BUTTON_DOWN,
    RI_MOUSE_LEFT_BUTTON_UP,
    RI_MOUSE_RIGHT_BUTTON_DOWN,
    RI_MOUSE_RIGHT_BUTTON_UP,
    _MouseLmbSuppressor,
)
from mouse_pressure.sniff.hidpp_pressure import (
    PressureHidppSession,
    extract_mode3_lr_pressure_raw,
    parse_feature_0c_frame,
)


def _parse_levels(value: str) -> list[int]:
    levels = [int(part.strip()) for part in value.split(",") if part.strip()]
    if not levels or any(level < 1 or level > 10 for level in levels):
        raise argparse.ArgumentTypeError(
            "levels must be comma-separated values in 1..10"
        )
    return levels


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure Superstrike raw pressure at hardware actuation levels."
    )
    parser.add_argument("--button", choices=("left", "right", "both"), default="both")
    parser.add_argument("--presses", type=int, default=5)
    parser.add_argument("--levels", type=_parse_levels, default=list(range(1, 11)))
    parser.add_argument(
        "--output",
        default="docs/investigation/actuation_raw_mapping_capture.json",
    )
    return parser.parse_args()


def _countdown(button: str, level: int) -> None:
    print("\n" + "=" * 68)
    print(f"{button.upper()} BUTTON — ACTUATION LEVEL {level}")
    print("Press slowly until the haptic fires, pause briefly, then release fully.")
    for remaining in (3, 2, 1):
        print(f"Starting in {remaining}…", flush=True)
        time.sleep(1.0)
    print("PRESS NOW", flush=True)


def _capture_presses(
    session: PressureHidppSession,
    native_events: queue.SimpleQueue[tuple[str, float]],
    *,
    button: str,
    count: int,
    timeout_s: float = 90.0,
) -> list[dict[str, float | int]]:
    samples: list[dict[str, float | int]] = []
    pressure_samples: list[tuple[float, int]] = []
    pending_down: float | None = None
    down_kind = f"{button}_down"
    up_kind = f"{button}_up"
    button_is_up = True
    deadline = time.perf_counter() + timeout_s

    while not native_events.empty():
        native_events.get()

    while len(samples) < count:
        if time.perf_counter() >= deadline:
            raise TimeoutError(f"Timed out waiting for {button} presses")

        def drain_native_events() -> None:
            nonlocal button_is_up, pending_down
            while not native_events.empty():
                kind, observed_at = native_events.get()
                if kind == up_kind:
                    button_is_up = True
                elif kind == down_kind and button_is_up and pending_down is None:
                    pending_down = observed_at
                    button_is_up = False

        drain_native_events()

        item = session.read_next(timeout_s=0.01)
        if item is None:
            continue
        timestamp, data = item
        frame = parse_feature_0c_frame(
            data,
            timestamp,
            feature_index=session.pressure_feature_index,
            device_index=session.device_index,
        )
        if frame is None:
            continue
        left_raw, right_raw = extract_mode3_lr_pressure_raw(frame)
        raw = left_raw if button == "left" else right_raw
        if raw is None:
            continue

        pressure_samples.append((float(timestamp), int(raw)))
        pressure_samples[:] = pressure_samples[-16:]
        drain_native_events()
        if pending_down is not None:
            before = [
                sample for sample in pressure_samples if sample[0] <= pending_down
            ]
            after = [sample for sample in pressure_samples if sample[0] >= pending_down]
            if not before or not after:
                continue
            pressure_before = before[-1]
            pressure_after = after[0]
            span_s = pressure_after[0] - pressure_before[0]
            fraction = (
                (pending_down - pressure_before[0]) / span_s if span_s > 0.0 else 1.0
            )
            interpolated = pressure_before[1] + fraction * (
                pressure_after[1] - pressure_before[1]
            )
            sample = {
                "raw_before": pressure_before[1],
                "raw_after": pressure_after[1],
                "raw_interpolated": round(interpolated, 3),
                "before_age_ms": round((pending_down - pressure_before[0]) * 1000.0, 3),
                "after_age_ms": round((pressure_after[0] - pending_down) * 1000.0, 3),
                "sample_gap_ms": round(span_s * 1000.0, 3),
            }
            samples.append(sample)
            print(
                f"  captured {len(samples)}/{count}: "
                f"raw@edge={sample['raw_interpolated']} "
                f"({sample['raw_before']}→{sample['raw_after']})",
                flush=True,
            )
            pending_down = None

    return samples


def _summary(samples: list[dict[str, float | int]]) -> dict[str, float | int]:
    interpolated = [float(sample["raw_interpolated"]) for sample in samples]
    return {
        "count": len(samples),
        "raw_interpolated_median": statistics.median(interpolated),
        "raw_interpolated_min": min(interpolated),
        "raw_interpolated_max": max(interpolated),
    }


def main() -> int:
    args = _args()
    if args.presses < 1:
        raise SystemExit("--presses must be at least 1")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    print("Stop Mouse Pressure before continuing.")
    input("Press Enter when the driver is stopped and both buttons are released…")

    session = PressureHidppSession(log=lambda line: print(f"[device] {line}"))
    native_events: queue.SimpleQueue[tuple[str, float]] = queue.SimpleQueue()

    def on_native(
        kind: str,
        observed_at: float,
        fields: dict[str, int | float | str],
    ) -> None:
        if kind != "raw_mouse":
            return
        flags = int(fields.get("button_flags", 0))
        for mask, derived in (
            (RI_MOUSE_LEFT_BUTTON_DOWN, "left_down"),
            (RI_MOUSE_LEFT_BUTTON_UP, "left_up"),
            (RI_MOUSE_RIGHT_BUTTON_DOWN, "right_down"),
            (RI_MOUSE_RIGHT_BUTTON_UP, "right_up"),
        ):
            if flags & mask:
                native_events.put((derived, observed_at))

    observer = _MouseLmbSuppressor(
        log=lambda _line: None,
        suppress_left=False,
        suppress_right=False,
        debug_mode=False,
    )
    observer.set_timing_callback(on_native)
    original: tuple[int, int] | None = None
    results: dict[str, Any] = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "presses_per_level": args.presses,
        "levels": args.levels,
        "buttons": {},
    }
    try:
        session.open()
        session.enable_pressure_stream()
        observer.start()
        original = session.get_actuation_levels()
        results["original_actuation"] = {"left": original[0], "right": original[1]}
        buttons = ("left", "right") if args.button == "both" else (args.button,)
        for button in buttons:
            button_results: dict[str, Any] = {}
            for level in args.levels:
                left = level if button == "left" else original[0]
                right = level if button == "right" else original[1]
                session.set_actuation_levels(left=left, right=right)
                time.sleep(0.2)
                _countdown(button, level)
                samples = _capture_presses(
                    session,
                    native_events,
                    button=button,
                    count=args.presses,
                )
                button_results[str(level)] = {
                    "samples": samples,
                    "summary": _summary(samples),
                }
            results["buttons"][button] = button_results
    except KeyboardInterrupt:
        print("\nInterrupted; restoring original actuation settings.")
        return 130
    finally:
        observer.stop()
        if original is not None:
            try:
                session.set_actuation_levels(left=original[0], right=original[1])
                print(f"Restored actuation: left={original[0]}, right={original[1]}")
            except Exception as exc:
                print(f"WARNING: could not restore original actuation: {exc}")
        session.close()
        if results["buttons"]:
            output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
            print(f"Saved capture: {output.resolve()}")

    print("\nMEDIAN INTERPOLATED RAW VALUE AT ACTUATION")
    for button, levels in results["buttons"].items():
        print(button.upper())
        for level, result in levels.items():
            summary = result["summary"]
            print(
                f"  L{level}: {summary['raw_interpolated_median']} "
                f"(range {summary['raw_interpolated_min']}–"
                f"{summary['raw_interpolated_max']})"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
