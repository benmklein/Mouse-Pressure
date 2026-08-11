"""Summarize the existing HID captures and recent structured stroke traces."""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=Path, default=Path("docs"))
    parser.add_argument(
        "--traces", type=Path, default=Path("work/stroke_traces")
    )
    parser.add_argument("--trace-limit", type=int, default=500)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/investigation/results/input_evidence_summary.json"),
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


def mode_log_summary(path: Path) -> dict[str, Any]:
    pattern = re.compile(
        r"\[\s*([0-9.]+)s\].*addr=0x([0-9A-Fa-f]{2}) raw=(.*)$"
    )
    rows: list[tuple[float, int, bytes]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.search(line)
        if match:
            rows.append(
                (
                    float(match.group(1)),
                    int(match.group(2), 16),
                    bytes.fromhex(match.group(3)),
                )
            )
    result: dict[str, Any] = {"file": str(path), "frame_count": len(rows), "addresses": {}}
    for address in sorted({address for _, address, _ in rows}):
        timestamps = [timestamp for timestamp, current, _ in rows if current == address]
        intervals = [
            (right - left) * 1000.0
            for left, right in zip(timestamps, timestamps[1:])
            if 0.0 < right - left < 0.1
        ]
        result["addresses"][f"0x{address:02X}"] = {
            "count": len(timestamps),
            "interval_ms": describe(intervals),
            "mean_hz": 1000.0 / statistics.mean(intervals) if intervals else None,
        }
    primary = [payload for _, address, payload in rows if address == 0x10]
    if primary:
        left = [(payload[4] << 2) | (payload[5] >> 6) for payload in primary]
        right = [(payload[6] << 2) | (payload[7] >> 6) for payload in primary]
        result["decoded_10bit"] = {
            "left_min": min(left),
            "left_max": max(left),
            "left_unique": len(set(left)),
            "right_min": min(right),
            "right_max": max(right),
            "right_unique": len(set(right)),
            "left_low_two_bit_codes": dict(
                Counter(payload[5] >> 6 for payload in primary)
            ),
            "right_low_two_bit_codes": dict(
                Counter(payload[7] >> 6 for payload in primary)
            ),
        }
    return result


def _edges(rows: list[tuple[float, int]]) -> list[tuple[float, int]]:
    if not rows:
        return []
    previous = rows[0][1]
    output: list[tuple[float, int]] = []
    for timestamp, state in rows:
        if state != previous:
            output.append((timestamp, state))
        previous = state
    return output


def ghub_summary(path: Path) -> dict[str, Any]:
    mouse: list[tuple[float, int]] = []
    spy: list[tuple[float, int]] = []
    pressure: list[tuple[float, int]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.reader(handle):
            if len(row) < 12 or not row[11]:
                continue
            try:
                timestamp = float(row[1])
                payload = bytes.fromhex(row[11])
            except (ValueError, IndexError):
                continue
            if row[4] == "1.2.1" and len(payload) == 13:
                mouse.append((timestamp, payload[0] & 1))
            elif row[4] == "1.2.3" and payload[:4] == bytes.fromhex("11010f00"):
                spy.append((timestamp, payload[5] & 1))
            elif row[4] == "1.2.3" and payload[:4] == bytes.fromhex("11010c00"):
                pressure.append((timestamp, payload[4]))
    mouse_downs = [timestamp for timestamp, state in _edges(mouse) if state]
    spy_downs = [timestamp for timestamp, state in _edges(spy) if state]
    metrics: dict[str, list[float]] = defaultdict(list)
    per_press: list[dict[str, Any]] = []
    for down in mouse_downs:
        nearby_spy = [timestamp for timestamp in spy_downs if abs(timestamp - down) <= 0.05]
        before = [row for row in pressure if row[0] <= down]
        after = [row for row in pressure if row[0] >= down]
        item: dict[str, Any] = {"mouse_down_s": down}
        if nearby_spy:
            closest = min(nearby_spy, key=lambda timestamp: abs(timestamp - down))
            item["spy_delta_ms"] = (closest - down) * 1000.0
            metrics["spy_minus_mouse_ms"].append(item["spy_delta_ms"])
        if before:
            item["preceding_pressure"] = before[-1][1]
            item["preceding_pressure_age_ms"] = (down - before[-1][0]) * 1000.0
            metrics["preceding_pressure_age_ms"].append(item["preceding_pressure_age_ms"])
        if after:
            item["next_pressure_delta_ms"] = (after[0][0] - down) * 1000.0
            item["next_pressure"] = after[0][1]
            metrics["next_pressure_delta_ms"].append(item["next_pressure_delta_ms"])
        nonzero = next((row for row in after if row[1] > 0), None)
        if nonzero:
            item["first_nonzero_delta_ms"] = (nonzero[0] - down) * 1000.0
            metrics["first_nonzero_delta_ms"].append(item["first_nonzero_delta_ms"])
        per_press.append(item)
    return {
        "file": str(path),
        "mouse_down_count": len(mouse_downs),
        "mouse_button_spy_down_count": len(spy_downs),
        "legacy_pressure_frame_count": len(pressure),
        "metrics": {name: describe(values) for name, values in metrics.items()},
        "presses": per_press,
    }


def trace_summary(path: Path, limit: int) -> dict[str, Any]:
    files = sorted(path.glob("stroke-*.json"), key=lambda item: item.stat().st_mtime)
    selected = files[-max(0, int(limit)) :] if limit else files
    configurations: Counter[str] = Counter()
    metrics: dict[str, list[float]] = defaultdict(list)
    usable = 0
    for trace in selected:
        try:
            payload = json.loads(trace.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        metadata = payload.get("metadata", {})
        key = json.dumps(
            {
                "onset_buffer": metadata.get("onset_buffer"),
                "true_low_latency": metadata.get("true_low_latency"),
                "min_contact_pressure": metadata.get("min_contact_pressure"),
                "button": metadata.get("button"),
            },
            sort_keys=True,
        )
        configurations[key] += 1
        events = payload.get("events", [])
        updates = [event for event in events if event.get("kind") == "update" and event.get("lmb")]
        contact = [
            event
            for event in events
            if event.get("kind") == "inject"
            and event.get("ok")
            and event.get("tag") == "contact"
        ]
        if not updates or not contact:
            continue
        usable += 1
        origin = float(updates[0]["t_ms"])
        metrics["button_proxy_to_virtual_down_ms"].append(
            float(contact[0]["t_ms"]) - origin
        )
        fresh = [
            event
            for event in updates
            if event.get("pressure_fresh") and float(event["t_ms"]) > origin + 0.05
        ]
        if fresh:
            metrics["button_proxy_to_next_fresh_ms"].append(
                float(fresh[0]["t_ms"]) - origin
            )
        initial = int(updates[0].get("mapped", 0))
        meaningful = next(
            (event for event in fresh if int(event.get("mapped", 0)) >= initial + 32),
            None,
        )
        if meaningful:
            metrics["button_proxy_to_pressure_rise_32_ms"].append(
                float(meaningful["t_ms"]) - origin
            )
    return {
        "directory": str(path),
        "total_files": len(files),
        "files_examined": len(selected),
        "usable_strokes": usable,
        "configuration_counts": dict(configurations),
        "metrics": {name: describe(values) for name, values in metrics.items()},
        "warning": "Stroke begin is an emitter-observed button proxy, not the original hook edge.",
    }


def main() -> int:
    args = parse_args()
    payload = {
        "schema_version": 1,
        "mode_logs": [
            mode_log_summary(args.docs / "pressure_mode2_log.txt"),
            mode_log_summary(args.docs / "pressure_mode3_log.txt"),
        ],
        "ghub_capture": ghub_summary(args.docs / "ghub_payloads_ext.csv"),
        "stroke_traces": trace_summary(args.traces, args.trace_limit),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
