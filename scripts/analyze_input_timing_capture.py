"""Analyze a click/HID timing capture and compare causal onset estimates."""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--raw-min", type=int, required=True)
    parser.add_argument("--raw-max", type=int, required=True)
    parser.add_argument("--floor-percent", type=float, default=15.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("work/input_timing/analysis.json"),
    )
    return parser.parse_args()


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def describe(values: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(values),
        "min": min(values) if values else None,
        "median": statistics.median(values) if values else None,
        "p90": percentile(values, 0.90),
        "p99": percentile(values, 0.99),
        "max": max(values) if values else None,
    }


def mapped_percent(raw: float, raw_min: int, raw_max: int) -> float:
    span = max(1, raw_max - raw_min)
    return max(0.0, min(100.0, (raw - raw_min) * 100.0 / span))


def _median_without(values: list[float], omitted: int) -> float:
    return statistics.median(value for index, value in enumerate(values) if index != omitted)


def _interpolate(left: dict[str, Any], right: dict[str, Any], at: float) -> float:
    duration = float(right["at"]) - float(left["at"])
    if duration <= 0.0:
        return float(left["left_raw"])
    fraction = max(0.0, min(1.0, (at - float(left["at"])) / duration))
    return float(left["left_raw"]) + fraction * (
        float(right["left_raw"]) - float(left["left_raw"])
    )


def main() -> int:
    args = parse_args()
    if args.raw_min >= args.raw_max:
        raise SystemExit("--raw-min must be less than --raw-max")
    payload = json.loads(args.capture.read_text(encoding="utf-8"))
    events = payload.get("events", [])
    pressure = [
        event
        for event in events
        if event.get("kind") == "hidpp_0c"
        and event.get("address") == 0x10
        and event.get("left_raw") is not None
    ]
    pressure_times = [float(event["at"]) for event in pressure]
    downs = [event for event in events if event.get("kind") == "raw_left_down"]
    ups = [event for event in events if event.get("kind") == "raw_left_up"]
    movement = [event for event in events if event.get("kind") == "raw_mouse"]
    intervals_ms = [
        (right - left) * 1000.0
        for left, right in zip(pressure_times, pressure_times[1:])
        if 0.0 < right - left < 0.1
    ]
    cadence_s = statistics.median(intervals_ms) / 1000.0
    rows: list[dict[str, Any]] = []
    for down in downs:
        down_at = float(down["at"])
        up = next((item for item in ups if float(item["at"]) >= down_at), None)
        if up is None:
            continue
        up_at = float(up["at"])
        index = bisect.bisect_left(pressure_times, down_at)
        if index < 2 or index >= len(pressure):
            continue
        previous2 = pressure[index - 2]
        previous = pressure[index - 1]
        following = pressure[index]
        during = pressure[index : bisect.bisect_left(pressure_times, up_at)]
        maximum = max(
            (int(item["left_raw"]) for item in during),
            default=int(following["left_raw"]),
        )
        first_floor = next(
            (
                item
                for item in during
                if mapped_percent(
                    float(item["left_raw"]), args.raw_min, args.raw_max
                )
                >= args.floor_percent
            ),
            None,
        )
        next_at = float(following["at"])
        moves_to_next = [
            item for item in movement if down_at <= float(item["at"]) < next_at
        ]
        path_to_next = sum(
            math.hypot(float(item.get("dx", 0)), float(item.get("dy", 0)))
            for item in moves_to_next
        )
        net_to_next = math.hypot(
            sum(float(item.get("dx", 0)) for item in moves_to_next),
            sum(float(item.get("dy", 0)) for item in moves_to_next),
        )
        previous_gap = float(previous["at"]) - float(previous2["at"])
        previous_slope = (
            (float(previous["left_raw"]) - float(previous2["left_raw"]))
            / previous_gap
            if previous_gap > 0.0
            else 0.0
        )
        bounded_slope_next = max(
            float(args.raw_min),
            min(
                float(args.raw_max),
                float(previous["left_raw"]) + previous_slope * cadence_s,
            ),
        )
        click_reference = _interpolate(previous, following, down_at)
        row: dict[str, Any] = {
            "down_at": down_at,
            "press_duration_ms": (up_at - down_at) * 1000.0,
            "preceding_age_ms": (down_at - float(previous["at"])) * 1000.0,
            "next_sample_ms": (next_at - down_at) * 1000.0,
            "previous_raw": int(previous["left_raw"]),
            "next_raw": int(following["left_raw"]),
            "offline_interpolated_click_raw": click_reference,
            "max_raw_during_press": maximum,
            "path_to_next_sample_counts": path_to_next,
            "net_to_next_sample_counts": net_to_next,
            "bounded_slope_next_raw": bounded_slope_next,
        }
        if first_floor is not None:
            row["first_floor_ms"] = (
                float(first_floor["at"]) - down_at
            ) * 1000.0
        rows.append(row)

    if len(rows) < 2:
        raise SystemExit("Capture does not contain enough aligned Raw Input presses")

    next_values = [float(row["next_raw"]) for row in rows]
    learned_click_values = [
        float(row["offline_interpolated_click_raw"]) for row in rows
    ]
    floor_raw = args.raw_min + (
        args.raw_max - args.raw_min
    ) * args.floor_percent / 100.0
    strategies: dict[str, dict[str, list[float]]] = {
        name: {"raw_error": [], "mapped_error_points": [], "wait_ms": []}
        for name in (
            "last_sample",
            "fixed_floor",
            "learned_click",
            "learned_next_sample",
            "bounded_slope_next_sample",
            "adaptive_wait_4ms",
            "adaptive_wait_8ms",
            "wait_for_next_sample",
        )
    }
    for index, row in enumerate(rows):
        click_reference = float(row["offline_interpolated_click_raw"])
        next_reference = float(row["next_raw"])
        estimates = {
            "last_sample": (float(row["previous_raw"]), click_reference, 0.0),
            "fixed_floor": (floor_raw, click_reference, 0.0),
            "learned_click": (
                _median_without(learned_click_values, index),
                click_reference,
                0.0,
            ),
            "learned_next_sample": (
                _median_without(next_values, index),
                next_reference,
                0.0,
            ),
            "bounded_slope_next_sample": (
                float(row["bounded_slope_next_raw"]),
                next_reference,
                0.0,
            ),
            "wait_for_next_sample": (
                next_reference,
                next_reference,
                float(row["next_sample_ms"]),
            ),
        }
        for bound in (4.0, 8.0):
            name = f"adaptive_wait_{int(bound)}ms"
            next_delay = float(row["next_sample_ms"])
            if next_delay <= bound:
                estimates[name] = (next_reference, next_reference, next_delay)
            else:
                target_at_bound = _interpolate(
                    {
                        "at": row["down_at"],
                        "left_raw": click_reference,
                    },
                    {
                        "at": row["down_at"] + next_delay / 1000.0,
                        "left_raw": next_reference,
                    },
                    row["down_at"] + bound / 1000.0,
                )
                estimates[name] = (
                    _median_without(next_values, index),
                    target_at_bound,
                    bound,
                )
        for name, (estimate, reference, wait_ms) in estimates.items():
            raw_error = abs(estimate - reference)
            strategies[name]["raw_error"].append(raw_error)
            strategies[name]["mapped_error_points"].append(
                abs(
                    mapped_percent(estimate, args.raw_min, args.raw_max)
                    - mapped_percent(reference, args.raw_min, args.raw_max)
                )
            )
            strategies[name]["wait_ms"].append(wait_ms)

    summary = {
        "schema_version": 1,
        "capture": str(args.capture),
        "presses": len(rows),
        "configuration": {
            "raw_min": args.raw_min,
            "raw_max": args.raw_max,
            "floor_percent": args.floor_percent,
            "floor_equivalent_raw": floor_raw,
        },
        "pressure_cadence_ms": describe(intervals_ms),
        "observed": {
            "preceding_sample_age_ms": describe(
                [float(row["preceding_age_ms"]) for row in rows]
            ),
            "next_sample_delay_ms": describe(
                [float(row["next_sample_ms"]) for row in rows]
            ),
            "previous_raw": describe(
                [float(row["previous_raw"]) for row in rows]
            ),
            "next_raw": describe([float(row["next_raw"]) for row in rows]),
            "offline_interpolated_click_raw": describe(
                learned_click_values
            ),
            "first_floor_delay_ms": describe(
                [float(row["first_floor_ms"]) for row in rows if "first_floor_ms" in row]
            ),
            "path_to_next_sample_counts": describe(
                [float(row["path_to_next_sample_counts"]) for row in rows]
            ),
            "net_to_next_sample_counts": describe(
                [float(row["net_to_next_sample_counts"]) for row in rows]
            ),
            "press_duration_ms": describe(
                [float(row["press_duration_ms"]) for row in rows]
            ),
            "max_raw_during_press": describe(
                [float(row["max_raw_during_press"]) for row in rows]
            ),
        },
        "strategy_comparison": {
            name: {
                "reference": (
                    "offline interpolated pressure at click"
                    if name in {"last_sample", "fixed_floor", "learned_click"}
                    else "pressure at strategy emission time"
                ),
                "absolute_raw_error": describe(values["raw_error"]),
                "absolute_mapped_error_percentage_points": describe(
                    values["mapped_error_points"]
                ),
                "intentional_wait_ms": describe(values["wait_ms"]),
            }
            for name, values in strategies.items()
        },
        "limitations": [
            "The exact analog value at the digital edge is not directly sampled; the offline click reference linearly interpolates the bracketing 0x10 frames.",
            "The varied press categories were not labeled during capture.",
            "Raw Input counts are device motion counts, not display pixels.",
            "Strategy errors use a linear raw-to-output mapping and do not include a nonlinear pressure curve.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    csv_path = args.output.with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(summary, indent=2))
    print(f"Saved {args.output.resolve()}")
    print(f"Saved {csv_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
