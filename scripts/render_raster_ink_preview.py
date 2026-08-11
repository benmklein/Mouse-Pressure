from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib.pyplot as plt

from mouse_pressure.ink.raster_ink import (
    InkPoint,
    LowLatencyInkFilter,
    refine_stroke,
)


def _load_motion(trace_path: Path) -> list[InkPoint]:
    payload = json.loads(trace_path.read_text(encoding="utf-8"))
    points: list[InkPoint] = []
    previous: tuple[float, float] | None = None
    for event in payload.get("events", []):
        if event.get("kind") != "motion":
            continue
        position = (float(event["x"]), float(event["y"]))
        if position == previous:
            continue
        points.append(
            InkPoint(
                x=position[0],
                y=position[1],
                time_ms=float(event.get("t_ms", len(points) * 4.0)),
            )
        )
        previous = position
    return points


def _path_length(points: list[InkPoint]) -> float:
    return sum(
        math.hypot(right.x - left.x, right.y - left.y)
        for left, right in zip(points, points[1:])
    )


def _mean_preview_lag(raw: list[InkPoint], preview: list[InkPoint]) -> float:
    if not raw:
        return 0.0
    return sum(
        math.hypot(source.x - filtered.x, source.y - filtered.y)
        for source, filtered in zip(raw, preview)
    ) / len(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    raw = _load_motion(args.trace)
    if len(raw) < 2:
        raise SystemExit("Trace does not contain a usable motion path")
    preview = LowLatencyInkFilter().process(raw)
    final = refine_stroke(raw)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    figure, axes = plt.subplots(1, 2, figsize=(12, 5), constrained_layout=True)
    for axis in axes:
        axis.set_aspect("equal", adjustable="datalim")
        axis.invert_yaxis()
        axis.grid(alpha=0.15)
    axes[0].plot([p.x for p in raw], [p.y for p in raw], color="#999", label="raw")
    axes[0].plot(
        [p.x for p in preview],
        [p.y for p in preview],
        color="#d33",
        label="live preview",
    )
    axes[0].set_title("Causal live preview")
    axes[0].legend()
    axes[1].plot([p.x for p in raw], [p.y for p in raw], color="#bbb", label="raw")
    axes[1].plot(
        [p.x for p in final],
        [p.y for p in final],
        color="#1769aa",
        label="release-time final",
    )
    axes[1].set_title("Corner-aware final refinement")
    axes[1].legend()
    figure.suptitle(args.trace.name)
    figure.savefig(args.output, dpi=160)
    plt.close(figure)

    metrics = {
        "samples": len(raw),
        "raw_path_px": round(_path_length(raw), 3),
        "preview_path_px": round(_path_length(preview), 3),
        "final_path_px": round(_path_length(final), 3),
        "mean_preview_lag_px": round(_mean_preview_lag(raw, preview), 3),
    }
    metrics_path = args.output.with_suffix(".json")
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(args.output)
    print(metrics_path)
    print(json.dumps(metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
