from __future__ import annotations

import math
from pathlib import Path

from superstrike_pressure.ink.raster_ink import (
    InkPoint,
    LowLatencyInkFilter,
    prepare_replay_stroke,
    refine_stroke,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _mean_abs_y(points: list[InkPoint]) -> float:
    return sum(abs(point.y) for point in points) / max(1, len(points))


def test_live_filter_preserves_first_sample_and_pressure() -> None:
    points = [
        InkPoint(10.0, 20.0, pressure=0.2, time_ms=0.0),
        InkPoint(12.0, 21.0, pressure=0.8, time_ms=4.0),
    ]

    filtered = LowLatencyInkFilter().process(points)

    assert filtered[0] == points[0]
    assert filtered[1].pressure == points[1].pressure
    assert filtered[1].time_ms == points[1].time_ms


def test_live_filter_tracks_fast_motion_more_closely_than_slow_motion() -> None:
    slow_filter = LowLatencyInkFilter()
    fast_filter = LowLatencyInkFilter()
    slow_filter.update(InkPoint(0.0, 0.0, time_ms=0.0))
    fast_filter.update(InkPoint(0.0, 0.0, time_ms=0.0))

    slow = slow_filter.update(InkPoint(2.0, 0.0, time_ms=10.0))
    fast = fast_filter.update(InkPoint(20.0, 0.0, time_ms=10.0))

    slow_fraction = slow.x / 2.0
    fast_fraction = fast.x / 20.0
    assert fast_fraction > slow_fraction


def test_final_refinement_reduces_straight_line_wobble() -> None:
    points = [
        InkPoint(float(index), 0.8 if index % 2 else -0.8, time_ms=index * 4.0)
        for index in range(20)
    ]

    refined = refine_stroke(points, amount=0.7, passes=3)

    assert refined[0] == points[0]
    assert refined[-1] == points[-1]
    assert _mean_abs_y(refined[2:-2]) < _mean_abs_y(points[2:-2]) * 0.55


def test_final_refinement_preserves_deliberate_corner() -> None:
    points = [
        InkPoint(0.0, 10.0),
        InkPoint(5.0, 10.0),
        InkPoint(10.0, 10.0),
        InkPoint(10.0, 5.0),
        InkPoint(10.0, 0.0),
    ]

    refined = refine_stroke(points, amount=1.0, passes=4)

    assert math.isclose(refined[2].x, 10.0)
    assert math.isclose(refined[2].y, 10.0)


def test_final_refinement_does_not_change_pressure_samples() -> None:
    points = [
        InkPoint(float(index), math.sin(index), pressure=index / 9.0)
        for index in range(10)
    ]

    refined = refine_stroke(points)

    assert [point.pressure for point in refined] == [point.pressure for point in points]


def test_final_refinement_is_stable_with_uneven_input_spacing() -> None:
    x_positions = [0.0, 0.2, 0.4, 1.0, 2.5, 2.7, 4.5, 7.0, 7.2, 10.0]
    points = [
        InkPoint(x, 0.7 if index % 2 else -0.7)
        for index, x in enumerate(x_positions)
    ]

    refined = refine_stroke(points, amount=0.65, passes=2)

    assert refined[0] == points[0]
    assert refined[-1] == points[-1]
    assert _mean_abs_y(refined[1:-1]) < _mean_abs_y(points[1:-1]) * 0.7


def test_replay_densifies_and_detects_gradual_tails() -> None:
    points = [
        InkPoint(0.0, 0.0, pressure=25 / 1024, time_ms=0.0),
        InkPoint(30.0, 0.0, pressure=580 / 1024, time_ms=60.0),
        InkPoint(100.0, 0.0, pressure=615 / 1024, time_ms=110.0),
        InkPoint(124.0, 0.0, pressure=25 / 1024, time_ms=133.0),
    ]

    replay = prepare_replay_stroke(points)

    assert len(replay) >= 125
    assert 0.0 < replay[0].pressure < replay[2].pressure
    assert replay[2].pressure < replay[8].pressure < replay[30].pressure
    assert replay[-1].pressure < replay[-3].pressure < replay[-9].pressure


def test_long_replay_can_detect_tail_independent_of_total_length() -> None:
    points = [
        InkPoint(0.0, 0.0, pressure=0.02, time_ms=0.0),
        InkPoint(40.0, 0.0, pressure=0.8, time_ms=40.0),
        InkPoint(400.0, 0.0, pressure=0.8, time_ms=160.0),
    ]

    replay = prepare_replay_stroke(points)

    assert replay[0].pressure < replay[5].pressure < replay[30].pressure
    assert replay[100].pressure == points[1].pressure


def test_adaptive_tail_shaping_can_be_disabled() -> None:
    points = [
        InkPoint(0.0, 0.0, pressure=0.2, time_ms=0.0),
        InkPoint(50.0, 0.0, pressure=0.8, time_ms=80.0),
    ]

    replay = prepare_replay_stroke(points, adaptive_tails=False)

    assert replay[0].pressure == points[0].pressure
    assert replay[-1].pressure == points[-1].pressure


def test_replay_collapses_stationary_pressure_updates() -> None:
    points = [
        InkPoint(0.0, 0.0, pressure=0.05, time_ms=0.0),
        InkPoint(0.0, 0.0, pressure=0.4, time_ms=10.0),
        InkPoint(0.0, 0.0, pressure=0.7, time_ms=20.0),
        InkPoint(20.0, 0.0, pressure=0.8, time_ms=40.0),
    ]

    replay = prepare_replay_stroke(points, adaptive_tails=False)

    assert len(replay) == 21
    assert [(point.x, point.y) for point in replay].count((0.0, 0.0)) == 1


def test_krita_tool_registers_mouse_icon_and_editable_shortcut() -> None:
    header = (
        REPO_ROOT
        / "integrations"
        / "krita"
        / "superstrike_raster_ink"
        / "kis_tool_superstrike_ink.h"
    ).read_text(encoding="utf-8")

    assert 'setToolTip(i18n("Superstrike Raster Ink Tool"))' in header
    assert 'setIconName(koIconNameCStr("superstrike_mouse"))' in header
    assert "setShortcut(QKeySequence(Qt::SHIFT | Qt::Key_B))" in header
    action = (
        REPO_ROOT
        / "integrations"
        / "krita"
        / "superstrike_raster_ink"
        / "superstrike_raster_ink.action"
    ).read_text(encoding="utf-8")
    assert '<Action name="KritaShape/KisToolSuperstrikeInk">' in action
    assert "<toolTip>Superstrike Raster Ink Tool</toolTip>" in action
    assert "<shortcut>Shift+B</shortcut>" in action


def test_krita_mouse_icons_are_packaged_for_all_themes() -> None:
    plugin_dir = (
        REPO_ROOT / "integrations" / "krita" / "superstrike_raster_ink"
    )
    cmake = (plugin_dir / "CMakeLists.txt").read_text(encoding="utf-8")
    resources = (plugin_dir / "superstrike_icons.qrc").read_text(encoding="utf-8")
    for name in (
        "superstrike_mouse.png",
        "dark_superstrike_mouse.png",
        "light_superstrike_mouse.png",
    ):
        assert (plugin_dir / name).is_file()
        assert name in cmake
        assert name in resources
