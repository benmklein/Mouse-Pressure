"""Transform captured stroke traces into UI-ready metrics and series."""

from __future__ import annotations

import math
from typing import Any


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[round((len(ordered) - 1) * fraction)])


def _geometry_injections(
    injected: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Remove intentional stationary-dab loops from stroke geometry."""
    cleaned: list[dict[str, Any]] = []
    removed = 0
    for event in injected:
        if event.get("tag") == "stationary_contact":
            removed += 1
            continue
        cleaned.append(event)
        while len(cleaned) >= 3:
            first, middle, last = cleaned[-3:]
            excursion = math.hypot(
                int(middle["x"]) - int(first["x"]),
                int(middle["y"]) - int(first["y"]),
            )
            if (
                (first["x"], first["y"]) == (last["x"], last["y"])
                and 0.0 < excursion <= 1.01
            ):
                cleaned.pop()
                cleaned.pop()
                removed += 2
            else:
                break
    return cleaned, removed


def stroke_analysis_data(payload: dict[str, Any]) -> dict[str, Any]:
    """Build compact metrics and graph series for the desktop analyzer."""
    events = list(payload.get("events", []))
    metadata = dict(payload.get("metadata", {}))
    updates = [event for event in events if event.get("kind") == "update"]
    fresh = [event for event in updates if event.get("pressure_fresh")]
    motions = [event for event in events if event.get("kind") == "motion"]
    injected = [
        event
        for event in events
        if event.get("kind") == "inject"
        and event.get("ok")
        and int(event.get("flags", 0)) & 0x00000004
    ]
    geometry, stationary_points = _geometry_injections(injected)
    raw_key = "right_raw" if metadata.get("button") == "right" else "left_raw"
    raw_series = [
        (float(event.get("t_ms", 0.0)), float(event[raw_key]))
        for event in fresh
        if event.get(raw_key) is not None
    ]
    mapped_series = [
        (float(event.get("t_ms", 0.0)), float(event.get("mapped", 0)))
        for event in fresh
    ]
    interpolated_series = [
        (
            float(event.get("t_ms", 0.0)),
            float(event.get("actual_pressure", event.get("interpolated_mapped", 0))),
        )
        for event in updates
    ]
    injected_time = [
        (float(event.get("t_ms", 0.0)), float(event.get("pressure", 0)))
        for event in injected
    ]
    injected_distance: list[tuple[float, float]] = []
    distance = 0.0
    previous: dict[str, Any] | None = None
    for event in geometry:
        if previous is not None:
            distance += math.hypot(
                int(event["x"]) - int(previous["x"]),
                int(event["y"]) - int(previous["y"]),
            )
        injected_distance.append((distance, float(event.get("pressure", 0))))
        previous = event

    motion_distances = [
        math.hypot(
            int(current["x"]) - int(previous_event["x"]),
            int(current["y"]) - int(previous_event["y"]),
        )
        for previous_event, current in zip(motions, motions[1:])
    ]
    motion_duration = (
        float(motions[-1].get("t_ms", 0.0)) - float(motions[0].get("t_ms", 0.0))
        if len(motions) >= 2
        else 0.0
    )
    motion_hz = (
        (len(motions) - 1) * 1000.0 / motion_duration
        if motion_duration > 0.0
        else 0.0
    )
    pressure_steps = [
        abs(float(current.get("pressure", 0)) - float(previous_event.get("pressure", 0)))
        for previous_event, current in zip(geometry, geometry[1:])
    ]
    mapped_steps = [
        abs(float(current.get("mapped", 0)) - float(previous_event.get("mapped", 0)))
        for previous_event, current in zip(fresh, fresh[1:])
    ]
    max_pressure_step = max(pressure_steps, default=0.0)
    p95_pressure_step = _percentile(pressure_steps, 0.95)
    max_mapped_step = max(mapped_steps, default=0.0)
    p95_motion_segment = _percentile(motion_distances, 0.95)
    true_low_latency = bool(metadata.get("true_low_latency", False))
    if max_pressure_step > 128 and true_low_latency:
        diagnosis = (
            "Pressure steps reach the pen unchanged because True low latency "
            "disables pressure interpolation."
        )
    elif max_pressure_step > 128:
        diagnosis = "Large pressure steps exist before Krita receives the stroke."
    elif motion_hz and motion_hz < 90 and p95_motion_segment > 12:
        diagnosis = "Position anchors are sparse enough to make fast curves angular."
    else:
        diagnosis = "The injected path and pressure are comparatively smooth."

    return {
        "metadata": metadata,
        "diagnosis": diagnosis,
        "motion_hz": motion_hz,
        "p95_motion_segment": p95_motion_segment,
        "p95_pressure_step": p95_pressure_step,
        "max_pressure_step": max_pressure_step,
        "max_mapped_step": max_mapped_step,
        "path_px": distance,
        "stationary_dab_points": stationary_points,
        "raw": raw_series,
        "mapped": mapped_series,
        "interpolated": interpolated_series,
        "injected_time": injected_time,
        "injected_distance": injected_distance,
    }
