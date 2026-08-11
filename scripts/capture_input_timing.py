"""Capture native click, Raw Input, HID++ pressure, and MouseButtonSpy timing.

This is an isolated diagnostic.  It does not suppress mouse clicks and does
not inject a virtual pen.  The normal driver remains the source for contact
and injection timing; this tool fills the earlier native-button/HID gap using
one ``time.perf_counter`` clock.
"""

from __future__ import annotations

import argparse
import json
import queue
import statistics
import threading
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
    MOUSE_BUTTON_SPY_INDEX,
    PressureHidppSession,
    extract_mode3_lr_pressure_raw,
    hex_bytes,
    parse_feature_0c_frame,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument(
        "--output",
        default="work/input_timing/input-timing.json",
        help="JSON result path",
    )
    parser.add_argument(
        "--meaningful-raw",
        type=int,
        default=420,
        help="Raw threshold used only for summary statistics",
    )
    return parser.parse_args()


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def _describe(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "min_ms": min(values) if values else None,
        "median_ms": statistics.median(values) if values else None,
        "p90_ms": _percentile(values, 0.90),
        "p99_ms": _percentile(values, 0.99),
        "max_ms": max(values) if values else None,
    }


def _summarize(
    events: list[dict[str, Any]], meaningful_raw: int
) -> dict[str, Any]:
    hook_downs = [event for event in events if event["kind"] == "hook_left_down"]
    raw_downs = [event for event in events if event["kind"] == "raw_left_down"]
    downs = hook_downs or raw_downs
    down_source = "hook_left_down" if hook_downs else "raw_left_down"
    spy_downs = [
        event
        for event in events
        if event["kind"] == "mouse_button_spy" and event.get("state") == 1
    ]
    pressure = [
        event
        for event in events
        if event["kind"] == "hidpp_0c" and event.get("address") == 0x10
    ]
    metrics: dict[str, list[float]] = {
        "button_to_raw_down": [],
        "button_to_mouse_button_spy": [],
        "button_to_next_pressure": [],
        "button_to_meaningful_pressure": [],
        "preceding_pressure_age": [],
    }
    for down in downs:
        at = float(down["at"])
        for key, candidates in (
            ("button_to_raw_down", raw_downs),
            ("button_to_mouse_button_spy", spy_downs),
        ):
            nearby = [event for event in candidates if abs(float(event["at"]) - at) <= 0.05]
            if nearby:
                closest = min(nearby, key=lambda event: abs(float(event["at"]) - at))
                metrics[key].append((float(closest["at"]) - at) * 1000.0)
        before = [event for event in pressure if float(event["at"]) <= at]
        after = [event for event in pressure if float(event["at"]) >= at]
        if before:
            metrics["preceding_pressure_age"].append(
                (at - float(before[-1]["at"])) * 1000.0
            )
        if after:
            metrics["button_to_next_pressure"].append(
                (float(after[0]["at"]) - at) * 1000.0
            )
        meaningful = next(
            (
                event
                for event in after
                if int(event.get("left_raw") or 0) >= int(meaningful_raw)
            ),
            None,
        )
        if meaningful is not None:
            metrics["button_to_meaningful_pressure"].append(
                (float(meaningful["at"]) - at) * 1000.0
            )
    return {
        "button_down_source": down_source,
        "metrics": {
            name: _describe(values) for name, values in metrics.items()
        },
    }


def main() -> int:
    args = parse_args()
    if args.duration <= 0.0 or args.duration > 900.0:
        raise SystemExit("--duration must be between 0 and 900 seconds")

    event_queue: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
    stop_requested = threading.Event()
    events: list[dict[str, Any]] = []
    session_log: list[str] = []

    def on_native(
        kind: str,
        observed_at: float,
        fields: dict[str, int | float | str],
    ) -> None:
        event_queue.put({"kind": kind, "at": observed_at, **fields})
        if kind != "raw_mouse":
            return
        flags = int(fields.get("button_flags", 0))
        for mask, derived in (
            (RI_MOUSE_LEFT_BUTTON_DOWN, "raw_left_down"),
            (RI_MOUSE_LEFT_BUTTON_UP, "raw_left_up"),
            (RI_MOUSE_RIGHT_BUTTON_DOWN, "raw_right_down"),
            (RI_MOUSE_RIGHT_BUTTON_UP, "raw_right_up"),
        ):
            if flags & mask:
                event_queue.put({"kind": derived, "at": observed_at, **fields})

    suppressor = _MouseLmbSuppressor(
        log=lambda line: session_log.append(line),
        suppress_left=False,
        suppress_right=False,
        debug_mode=False,
    )
    suppressor.set_timing_callback(on_native)
    suppressor.set_force_stop_callback(lambda _reason: stop_requested.set())
    session = PressureHidppSession(log=lambda line: session_log.append(line))
    started_at = time.perf_counter()
    try:
        session.open()
        session.enable_pressure_stream(mode=0x03, mode_arg=0x00)
        suppressor.start()
        print("Input timing capture is ready. Native clicks remain enabled.")
        print("Perform varied presses; Ctrl+Shift+F12 stops early.")
        deadline = started_at + float(args.duration)
        while time.perf_counter() < deadline and not stop_requested.is_set():
            while not event_queue.empty():
                events.append(event_queue.get())
            item = session.read_next(timeout_s=0.05)
            if item is None:
                continue
            observed_at, data = item
            frame = parse_feature_0c_frame(
                data,
                observed_at,
                feature_index=session.pressure_feature_index,
                device_index=session.device_index,
            )
            if frame is not None:
                left_raw, right_raw = extract_mode3_lr_pressure_raw(frame)
                events.append(
                    {
                        "kind": "hidpp_0c",
                        "at": observed_at,
                        "address": frame.addr,
                        "payload": hex_bytes(frame.raw),
                        "left_raw": left_raw,
                        "right_raw": right_raw,
                    }
                )
            elif (
                len(data) >= 6
                and data[0] == 0x11
                and data[1] == session.device_index
                and data[2] == MOUSE_BUTTON_SPY_INDEX
                and data[3] == 0x00
            ):
                events.append(
                    {
                        "kind": "mouse_button_spy",
                        "at": observed_at,
                        "state": int(data[5]),
                        "payload": hex_bytes(data),
                    }
                )
    except KeyboardInterrupt:
        pass
    finally:
        # Stop the observer before device cleanup; clicks were never blocked.
        suppressor.stop()
        session.close()
        while not event_queue.empty():
            events.append(event_queue.get())

    events.sort(key=lambda event: float(event["at"]))
    for index, event in enumerate(events, start=1):
        event["seq"] = index
        event["t_ms"] = (float(event["at"]) - started_at) * 1000.0
    payload = {
        "schema_version": 1,
        "clock": "time.perf_counter",
        "duration_s": time.perf_counter() - started_at,
        "meaningful_raw": int(args.meaningful_raw),
        "safety": {
            "native_click_suppression": False,
            "virtual_pen_injection": False,
            "automatic_timeout": True,
            "known_pressure_stream_writes_only": True,
        },
        "summary": _summarize(events, int(args.meaningful_raw)),
        "events": events,
        "session_log": session_log,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Saved {len(events)} events to {output.resolve()}")
    print(json.dumps(payload["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
