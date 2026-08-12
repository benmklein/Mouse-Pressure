from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest


def _load_analyzer():
    script = Path(__file__).parents[1] / "scripts" / "analyze_stroke_trace.py"
    spec = importlib.util.spec_from_file_location("analyze_stroke_trace", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_analyzer_reports_sparse_input_and_renders_plot(tmp_path: Path) -> None:
    analyzer = _load_analyzer()
    payload = {
        "events": [
            {
                "kind": "update",
                "t_ms": 0.0,
                "pressure_fresh": True,
                "mapped": 100,
                "interpolated_mapped": 100,
            },
            {
                "kind": "inject",
                "t_ms": 0.0,
                "x": 10,
                "y": 10,
                "pressure": 100,
                "flags": 0x00000004,
                "ok": True,
            },
            {
                "kind": "inject",
                "t_ms": 10.0,
                "x": 110,
                "y": 10,
                "pressure": 800,
                "flags": 0x00000004,
                "ok": True,
            },
        ]
    }

    result = analyzer.analyze(payload)
    output = tmp_path / "trace.png"
    analyzer.render_plot(payload, output)

    assert str(result["diagnosis"]).startswith("SPATIAL_INPUT_SPARSE")
    assert output.exists()
    assert output.stat().st_size > 0


def test_latest_trace_skips_newer_zero_distance_stop_click(tmp_path: Path) -> None:
    analyzer = _load_analyzer()
    stroke = tmp_path / "stroke-1.json"
    stop_click = tmp_path / "stroke-2.json"
    stroke_events = [
        {"kind": "motion", "t_ms": float(index), "x": index * 5, "y": 0}
        for index in range(12)
    ] + [
        {
            "kind": "inject",
            "t_ms": float(index),
            "x": index * 5,
            "y": 0,
            "pressure": 400 + index,
            "flags": 0x00000004,
            "ok": True,
        }
        for index in range(12)
    ]
    stroke.write_text(json.dumps({"events": stroke_events}), encoding="utf-8")
    stop_click.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "kind": "inject",
                        "t_ms": 0.0,
                        "x": 10,
                        "y": 10,
                        "pressure": 100,
                        "flags": 0x00000004,
                        "ok": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    os.utime(stroke, (1.0, 1.0))
    os.utime(stop_click, (2.0, 2.0))

    assert analyzer._latest_trace(tmp_path) == stroke


def test_analyzer_identifies_fast_sparse_position_anchors() -> None:
    analyzer = _load_analyzer()
    motions = [
        {"kind": "motion", "t_ms": float(index * 20), "x": index * 40, "y": index}
        for index in range(12)
    ]
    injected = [
        {
            "kind": "inject",
            "t_ms": float(index),
            "x": index * 2,
            "y": 0,
            "pressure": 300 + index,
            "flags": 0x00000004,
            "ok": True,
        }
        for index in range(221)
    ]

    result = analyzer.analyze({"events": motions + injected})

    assert result["motion_anchor_hz"] == 50.0
    assert float(result["p95_motion_segment_px"]) > 39.0
    assert str(result["diagnosis"]).startswith("POSITION_ANCHOR_LIMIT")


def test_analyzer_reports_raw_saturation_latency_and_adaptive_budget() -> None:
    analyzer = _load_analyzer()
    events = []
    for index in range(12):
        events.extend(
            [
                {"kind": "motion", "t_ms": index * 10.0, "x": index * 3, "y": 0},
                {
                    "kind": "update",
                    "t_ms": index * 10.0 + 0.4,
                    "pressure_fresh": True,
                    "mapped": 900,
                    "interpolated_mapped": 900,
                    "left_raw": 640 if index >= 2 else 600,
                },
                {
                    "kind": "path_budget",
                    "t_ms": index * 10.0 + 0.5,
                    "budget": 8,
                    "emitted_points": 7,
                },
                {
                    "kind": "inject",
                    "t_ms": index * 10.0 + 0.6,
                    "x": index * 3,
                    "y": 0,
                    "pressure": 900,
                    "flags": 0x00000004,
                    "ok": True,
                },
            ]
        )

    result = analyzer.analyze(
        {
            "metadata": {"configured_raw_min": 380, "configured_raw_max": 640},
            "events": events,
        }
    )

    assert result["motion_to_update_p95_ms"] == 0.4
    assert result["path_budget_median"] == 8.0
    assert float(result["raw_at_or_above_configured_max_pct"]) > 80.0
    assert str(result["diagnosis"]).startswith("RAW_RANGE_SATURATION")


def test_analyzer_excludes_closed_stationary_dabs_from_path_geometry() -> None:
    analyzer = _load_analyzer()
    events = [
        {
            "kind": "inject",
            "t_ms": float(index),
            "x": x,
            "y": 0,
            "pressure": 400,
            "flags": 0x00000004,
            "ok": True,
        }
        for index, x in enumerate((0, 1, 0, 10))
    ]

    result = analyzer.analyze({"events": events})

    assert result["stationary_dab_points"] == 2
    assert result["stationary_pressure_updates"] == 1
    assert result["path_px"] == 10.0
    assert result["direction_reversals"] == 0


def test_ui_analysis_reports_native_backend_delivery_and_onset() -> None:
    from mouse_pressure.ui.stroke_analysis import stroke_analysis_data

    payload = {
        "metadata": {"button": "left", "output_backend": "native_synthetic"},
        "events": [
            {"kind": "hook_left_down", "t_ms": -0.5},
            {"kind": "motion", "t_ms": 0.0, "x": 10, "y": 20},
            {
                "kind": "inject",
                "t_ms": 0.2,
                "x": 10,
                "y": 20,
                "pressure": 300,
                "flags": 0x00000004,
                "ok": True,
                "submission_token": 4,
            },
            {
                "kind": "native_delivery",
                "t_ms": 1.0,
                "token": 4,
                "queue_delay_us": 200,
                "inject_call_us": 100,
                "completed_qpc": 1000,
                "qpc_frequency": 10_000_000,
                "success": True,
            },
        ],
    }

    result = stroke_analysis_data(payload)

    assert result["backend_label"] == "Native synthetic"
    assert result["onset_ms"] == pytest.approx(1.0)
    assert result["delivery_latency_median_ms"] == pytest.approx(0.3)
    assert result["motion_to_output_median_ms"] == pytest.approx(0.5)
