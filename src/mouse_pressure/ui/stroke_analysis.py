"""Transform captured stroke traces into UI-ready metrics and series."""

from __future__ import annotations

import math
import statistics
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
    all_injected = [
        event
        for event in events
        if event.get("kind") == "inject" and event.get("ok")
    ]
    deliveries = [
        event for event in events if event.get("kind") == "native_delivery"
    ]
    delivery_by_token = {
        int(event["token"]): event
        for event in deliveries
        if event.get("token") is not None
    }

    def completed_time_ms(event: dict[str, Any]) -> float:
        submitted = float(event.get("t_ms", 0.0))
        token = event.get("submission_token")
        delivery = (
            delivery_by_token.get(int(token)) if token is not None else None
        )
        if delivery is not None:
            return submitted + (
                float(delivery.get("queue_delay_us", 0.0))
                + float(delivery.get("inject_call_us", 0.0))
            ) / 1000.0
        return submitted + float(event.get("call_duration_us", 0.0)) / 1000.0

    def paired_hook_latency(
        *,
        hook_suffix: str,
        report: dict[str, Any] | None,
    ) -> float | None:
        if report is None:
            return None
        report_time = float(report.get("t_ms", 0.0))
        candidates = [
            float(event.get("t_ms", 0.0))
            for event in events
            if str(event.get("kind", "")).endswith(hook_suffix)
            and float(event.get("t_ms", 0.0)) <= report_time
        ]
        if not candidates:
            return None
        return max(0.0, completed_time_ms(report) - max(candidates))

    backend = str(metadata.get("output_backend", "unknown")).strip().lower()
    backend_label = {
        "vmulti": "VMulti",
        "native_synthetic": "Native synthetic",
        "synthetic": "Synthetic",
    }.get(backend, "Unknown backend")
    first_contact = injected[0] if injected else None
    release_reports = [
        event for event in all_injected if int(event.get("flags", 0)) & 0x00040000
    ]
    release_report = release_reports[-1] if release_reports else None
    button = str(metadata.get("button", "left"))
    onset_ms = paired_hook_latency(
        hook_suffix=f"hook_{button}_down",
        report=first_contact,
    )
    release_ms = paired_hook_latency(
        hook_suffix=f"hook_{button}_up",
        report=release_report,
    )

    native_delivery_ms = [
        (
            float(event.get("queue_delay_us", 0.0))
            + float(event.get("inject_call_us", 0.0))
        )
        / 1000.0
        for event in deliveries
        if event.get("success")
    ]
    delivery_latency_median_ms = (
        statistics.median(native_delivery_ms) if native_delivery_ms else None
    )
    delivery_latency_p95_ms = (
        _percentile(native_delivery_ms, 0.95) if native_delivery_ms else None
    )

    completion_times: list[float] = []
    if deliveries:
        contact_tokens = {
            int(event["submission_token"])
            for event in injected
            if event.get("submission_token") is not None
        }
        completed_qpc = [
            (
                int(event.get("completed_qpc", 0)),
                int(event.get("qpc_frequency", 0)),
            )
            for event in deliveries
            if int(event.get("token", -1)) in contact_tokens
            and event.get("success")
            and int(event.get("qpc_frequency", 0)) > 0
        ]
        if completed_qpc:
            origin, frequency = completed_qpc[0]
            completion_times = [
                (qpc - origin) * 1000.0 / freq for qpc, freq in completed_qpc
            ]
    if not completion_times:
        completion_times = [completed_time_ms(event) for event in injected]
    delivery_intervals = [
        current - previous
        for previous, current in zip(completion_times, completion_times[1:])
        if current >= previous
    ]
    interval_median = (
        statistics.median(delivery_intervals) if delivery_intervals else 0.0
    )
    delivery_jitter_ms = _percentile(
        [abs(value - interval_median) for value in delivery_intervals],
        0.95,
    )

    motion_to_output: list[float] = []
    motion_to_output_series: list[tuple[float, float]] = []
    injections_by_position: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for event in injected:
        injections_by_position.setdefault(
            (int(event.get("x", 0)), int(event.get("y", 0))), []
        ).append(event)
    for motion in motions:
        motion_time = float(motion.get("t_ms", 0.0))
        candidates = injections_by_position.get(
            (int(motion.get("x", 0)), int(motion.get("y", 0))), []
        )
        completed = [
            completed_time_ms(event)
            for event in candidates
            if completed_time_ms(event) >= motion_time
        ]
        if completed:
            latency = min(completed) - motion_time
            motion_to_output.append(latency)
            motion_to_output_series.append((motion_time, latency))

    motion_to_output_median_ms = (
        statistics.median(motion_to_output) if motion_to_output else None
    )
    motion_to_output_p95_ms = (
        _percentile(motion_to_output, 0.95) if motion_to_output else None
    )
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
        "backend": backend,
        "backend_label": backend_label,
        "diagnosis": diagnosis,
        "motion_hz": motion_hz,
        "p95_motion_segment": p95_motion_segment,
        "p95_pressure_step": p95_pressure_step,
        "max_pressure_step": max_pressure_step,
        "max_mapped_step": max_mapped_step,
        "path_px": distance,
        "stationary_dab_points": stationary_points,
        "onset_ms": onset_ms,
        "release_ms": release_ms,
        "delivery_latency_median_ms": delivery_latency_median_ms,
        "delivery_latency_p95_ms": delivery_latency_p95_ms,
        "delivery_jitter_ms": delivery_jitter_ms,
        "motion_to_output_median_ms": motion_to_output_median_ms,
        "motion_to_output_p95_ms": motion_to_output_p95_ms,
        "motion_to_output_series": motion_to_output_series,
        "delivery_interval_series": [
            (completion_times[index], interval)
            for index, interval in enumerate(delivery_intervals, start=1)
        ],
        "raw": raw_series,
        "mapped": mapped_series,
        "interpolated": interpolated_series,
        "injected_time": injected_time,
        "injected_distance": injected_distance,
    }
