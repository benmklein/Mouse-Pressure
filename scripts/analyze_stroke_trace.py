"""Analyze and plot the newest structured mouse-pressure stroke trace."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def _latest_trace(path: Path) -> Path:
    if path.is_file():
        return path
    traces = sorted(
        path.glob("stroke-*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not traces:
        raise FileNotFoundError(f"No stroke traces found in {path}")
    # Clicking the bridge's Stop button can create a final zero-distance pen
    # contact. Prefer the newest trace that looks like an intentional drawn
    # stroke, while retaining newest-file fallback for dot diagnostics.
    for trace in traces[:25]:
        try:
            result = analyze(json.loads(trace.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            continue
        if (
            float(result["path_px"]) >= 25.0
            and int(result["motion_events"]) >= 10
            and int(result["injected_contact_points"]) >= 10
        ):
            return trace
    return traces[0]


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * fraction)
    return float(ordered[index])


def _without_stationary_dabs(
    injected: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Remove tagged and legacy one-pixel closed stationary dab paths."""
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


def analyze(payload: dict[str, Any]) -> dict[str, float | int | str]:
    events = list(payload.get("events", []))
    metadata = dict(payload.get("metadata", {}))
    motions = [event for event in events if event.get("kind") == "motion"]
    updates = [event for event in events if event.get("kind") == "update"]
    fresh = [event for event in updates if event.get("pressure_fresh")]
    fresh_raw = [int(event["left_raw"]) for event in fresh if event.get("left_raw") is not None]
    budgets = [event for event in events if event.get("kind") == "path_budget"]
    injected = [
        event
        for event in events
        if event.get("kind") == "inject"
        and event.get("ok")
        and int(event.get("flags", 0)) & 0x00000004
    ]
    geometry_injected, stationary_dab_points = _without_stationary_dabs(injected)

    contact_start_ms = float(injected[0]["t_ms"]) if injected else 0.0
    contact_end_ms = float(injected[-1]["t_ms"]) if injected else contact_start_ms
    contact_motions = [
        event
        for event in motions
        if contact_start_ms <= float(event.get("t_ms", 0.0)) <= contact_end_ms
    ]
    motion_distances = [
        math.hypot(
            int(current["x"]) - int(previous["x"]),
            int(current["y"]) - int(previous["y"]),
        )
        for previous, current in zip(contact_motions, contact_motions[1:])
    ]
    motion_duration_ms = (
        float(contact_motions[-1]["t_ms"]) - float(contact_motions[0]["t_ms"])
        if len(contact_motions) >= 2
        else 0.0
    )
    motion_anchor_hz = (
        (len(contact_motions) - 1) * 1000.0 / motion_duration_ms
        if motion_duration_ms > 0.0
        else 0.0
    )
    contact_duration_ms = contact_end_ms - contact_start_ms
    injected_contact_hz = (
        (len(injected) - 1) * 1000.0 / contact_duration_ms
        if len(injected) >= 2 and contact_duration_ms > 0.0
        else 0.0
    )

    distances: list[float] = []
    pressure_steps: list[float] = []
    cumulative_distance = 0.0
    plateau_distance = 0.0
    stationary_pressure_updates = stationary_dab_points // 2
    for previous, current in zip(geometry_injected, geometry_injected[1:]):
        distance = math.hypot(
            int(current["x"]) - int(previous["x"]),
            int(current["y"]) - int(previous["y"]),
        )
        pressure_step = abs(int(current["pressure"]) - int(previous["pressure"]))
        distances.append(distance)
        cumulative_distance += distance
        if distance > 0.0:
            pressure_steps.append(float(pressure_step))
            if pressure_step == 0:
                plateau_distance += distance
        elif pressure_step > 0:
            stationary_pressure_updates += 1

    mapped_steps = [
        abs(int(current["mapped"]) - int(previous["mapped"]))
        for previous, current in zip(fresh, fresh[1:])
    ]
    unique_path: list[dict[str, Any]] = []
    for event in geometry_injected:
        if not unique_path or (event["x"], event["y"]) != (
            unique_path[-1]["x"],
            unique_path[-1]["y"],
        ):
            unique_path.append(event)
    turn_angles: list[float] = []
    for first, middle, last in zip(unique_path, unique_path[1:], unique_path[2:]):
        incoming = (
            int(middle["x"]) - int(first["x"]),
            int(middle["y"]) - int(first["y"]),
        )
        outgoing = (
            int(last["x"]) - int(middle["x"]),
            int(last["y"]) - int(middle["y"]),
        )
        incoming_length = math.hypot(*incoming)
        outgoing_length = math.hypot(*outgoing)
        if incoming_length and outgoing_length:
            cosine = max(
                -1.0,
                min(
                    1.0,
                    (incoming[0] * outgoing[0] + incoming[1] * outgoing[1])
                    / (incoming_length * outgoing_length),
                ),
            )
            turn_angles.append(math.degrees(math.acos(cosine)))
    reversals = sum(angle > 90.0 for angle in turn_angles)
    motion_to_update_ms: list[float] = []
    pending_motion_times: list[float] = []
    for event in events:
        if event.get("kind") == "motion":
            pending_motion_times.append(float(event.get("t_ms", 0.0)))
        elif event.get("kind") == "update" and pending_motion_times:
            motion_to_update_ms.append(
                max(0.0, float(event.get("t_ms", 0.0)) - max(pending_motion_times))
            )
            pending_motion_times.clear()
    budget_values = [float(event.get("budget", 0)) for event in budgets]
    emitted_values = [float(event.get("emitted_points", 0)) for event in budgets]
    configured_raw_min = metadata.get("configured_raw_min")
    configured_raw_max = metadata.get("configured_raw_max")
    at_or_above_max = (
        sum(value >= int(configured_raw_max) for value in fresh_raw)
        if fresh_raw and configured_raw_max is not None
        else 0
    )
    raw_saturation_pct = 100.0 * at_or_above_max / len(fresh_raw) if fresh_raw else 0.0
    result: dict[str, float | int | str] = {
        "motion_events": len(motions),
        "update_events": len(updates),
        "fresh_pressure_samples": len(fresh),
        "injected_contact_points": len(injected),
        "stationary_dab_points": stationary_dab_points,
        "motion_anchor_hz": round(motion_anchor_hz, 2),
        "p95_motion_segment_px": round(_percentile(motion_distances, 0.95), 2),
        "max_motion_segment_px": round(max(motion_distances, default=0.0), 2),
        "injected_contact_hz": round(injected_contact_hz, 2),
        "path_px": round(cumulative_distance, 2),
        "p95_segment_px": round(_percentile(distances, 0.95), 2),
        "max_segment_px": round(max(distances, default=0.0), 2),
        "p95_pressure_step": round(_percentile(pressure_steps, 0.95), 2),
        "max_pressure_step": round(max(pressure_steps, default=0.0), 2),
        "max_fresh_mapped_step": max(mapped_steps, default=0),
        "pressure_plateau_px": round(plateau_distance, 2),
        "stationary_pressure_updates": stationary_pressure_updates,
        "direction_reversals": reversals,
        "p95_turn_degrees": round(_percentile(turn_angles, 0.95), 2),
        "motion_to_update_median_ms": round(_percentile(motion_to_update_ms, 0.5), 3),
        "motion_to_update_p95_ms": round(_percentile(motion_to_update_ms, 0.95), 3),
        "motion_to_update_max_ms": round(max(motion_to_update_ms, default=0.0), 3),
    }
    if budgets:
        result["path_budget_median"] = round(_percentile(budget_values, 0.5), 2)
        result["path_budget_p95"] = round(_percentile(budget_values, 0.95), 2)
        result["emitted_batch_p95"] = round(_percentile(emitted_values, 0.95), 2)
    if fresh_raw:
        result["raw_min_seen"] = min(fresh_raw)
        result["raw_max_seen"] = max(fresh_raw)
        result["raw_span_seen"] = max(fresh_raw) - min(fresh_raw)
        result["raw_at_or_above_configured_max_pct"] = round(raw_saturation_pct, 2)
    if configured_raw_min is not None:
        result["configured_raw_min"] = int(configured_raw_min)
    if configured_raw_max is not None:
        result["configured_raw_max"] = int(configured_raw_max)
    if metadata.get("configured_curve") is not None:
        result["configured_curve"] = str(metadata["configured_curve"])
    if metadata.get("configured_curve_strength") is not None:
        result["configured_curve_strength"] = float(metadata["configured_curve_strength"])
    if metadata.get("path_stabilization") is not None:
        result["path_stabilization"] = int(metadata["path_stabilization"])
    if metadata.get("pressure_influence") is not None:
        result["pressure_influence"] = int(metadata["pressure_influence"])
    if metadata.get("onset_buffer") is not None:
        result["onset_buffer"] = bool(metadata["onset_buffer"])
    if metadata.get("stationary_pressure_updates") is not None:
        result["stationary_pressure_enabled"] = bool(
            metadata["stationary_pressure_updates"]
        )

    if len(geometry_injected) < 10 or (
        cumulative_distance > 50 and len(motions) < 10
    ):
        diagnosis = "SPATIAL_INPUT_SPARSE: too few movement/injection points for interpolation."
    elif reversals >= 3:
        diagnosis = "PATH_BACKTRACKING: injected coordinates repeatedly reverse direction."
    elif (
        motion_anchor_hz > 0.0
        and motion_anchor_hz < 90.0
        and _percentile(motion_distances, 0.95) > 12.0
    ):
        diagnosis = (
            "POSITION_ANCHOR_LIMIT: fast motion is arriving in sparse physical anchors; "
            "synthetic points can smooth each known segment but cannot recover missing curvature."
        )
    elif len(fresh_raw) >= 10 and raw_saturation_pct > 25.0:
        diagnosis = (
            "RAW_RANGE_SATURATION: much of the stroke is pinned at configured raw maximum; "
            "run raw-range calibration with a firm comfortable press."
        )
    elif max(pressure_steps, default=0.0) > 128:
        diagnosis = "INJECTION_PRESSURE_STEP: the discontinuity exists before Krita."
    elif cumulative_distance and plateau_distance / cumulative_distance > 0.45:
        diagnosis = "PRESSURE_HELD: injected pressure remains constant over much of the path."
    else:
        diagnosis = (
            "INJECTION_SMOOTH: injected pressure/path are dense; compare Krita Tablet Tester "
            "or brush spacing/pressure configuration."
        )
    result["diagnosis"] = diagnosis
    return result


def render_plot(payload: dict[str, Any], output: Path) -> None:
    import matplotlib.pyplot as plt

    events = list(payload.get("events", []))
    updates = [event for event in events if event.get("kind") == "update"]
    fresh = [event for event in updates if event.get("pressure_fresh")]
    injected = [
        event
        for event in events
        if event.get("kind") == "inject"
        and event.get("ok")
        and int(event.get("flags", 0)) & 0x00000004
    ]
    geometry_injected, _stationary_dab_points = _without_stationary_dabs(injected)

    distance = [0.0]
    for previous, current in zip(geometry_injected, geometry_injected[1:]):
        distance.append(
            distance[-1]
            + math.hypot(
                int(current["x"]) - int(previous["x"]),
                int(current["y"]) - int(previous["y"]),
            )
        )

    figure, axes = plt.subplots(2, 1, figsize=(11, 7), constrained_layout=True)
    axes[0].plot(
        [event["t_ms"] for event in updates],
        [event["interpolated_mapped"] for event in updates],
        label="interpolated update",
        linewidth=1.4,
    )
    axes[0].scatter(
        [event["t_ms"] for event in fresh],
        [event["mapped"] for event in fresh],
        label="fresh mapped sample",
        s=18,
        zorder=3,
    )
    raw_updates = [event for event in fresh if event.get("left_raw") is not None]
    if raw_updates:
        axes[0].scatter(
            [event["t_ms"] for event in raw_updates],
            [event["left_raw"] for event in raw_updates],
            label="raw ADC",
            s=12,
            alpha=0.6,
            zorder=2,
        )
    axes[0].plot(
        [event["t_ms"] for event in injected],
        [event["pressure"] for event in injected],
        label="Windows pen injection",
        linewidth=1.1,
    )
    axes[0].set(xlabel="time (ms)", ylabel="pressure (0–1024)", title="Pressure pipeline")
    axes[0].legend()
    axes[0].grid(alpha=0.25)

    axes[1].plot(
        distance,
        [event["pressure"] for event in geometry_injected],
        marker=".",
        markersize=3,
        linewidth=1.2,
    )
    axes[1].set(
        xlabel="stroke path distance (px)",
        ylabel="injected pressure (0–1024)",
        title="Pressure actually attached to spatial pen points",
    )
    axes[1].grid(alpha=0.25)
    figure.savefig(output, dpi=150)
    plt.close(figure)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        default=Path.home() / ".mouse-pressure" / "stroke_traces",
    )
    args = parser.parse_args()

    trace_path = _latest_trace(Path(args.path))
    payload = json.loads(trace_path.read_text(encoding="utf-8"))
    result = analyze(payload)
    report_path = trace_path.with_suffix(".png")
    render_plot(payload, report_path)

    print(f"Trace: {trace_path.resolve()}")
    for key, value in result.items():
        print(f"{key}: {value}")
    print(f"Plot: {report_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
