from __future__ import annotations

import math
from pathlib import Path

from mouse_pressure.ink.raster_ink import (
    InkPoint,
    LowLatencyInkFilter,
    StartupPressurePreviewFilter,
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


def test_startup_preview_eases_pressure_without_filtering_position() -> None:
    pressure_filter = StartupPressurePreviewFilter()
    points = [
        InkPoint(0.0, 0.0, pressure=0.15, time_ms=0.0),
        InkPoint(20.0, 5.0, pressure=0.15, time_ms=30.0),
        InkPoint(30.0, 7.0, pressure=0.45, time_ms=44.0),
        InkPoint(31.0, 8.0, pressure=0.45, time_ms=45.0),
        InkPoint(40.0, 9.0, pressure=0.5, time_ms=70.0),
    ]

    preview = [pressure_filter.update(point) for point in points]

    assert [(point.x, point.y) for point in preview] == [
        (point.x, point.y) for point in points
    ]
    assert preview[1].pressure == 0.15
    assert 0.15 < preview[2].pressure < points[2].pressure
    assert preview[2].pressure < preview[3].pressure < points[3].pressure
    assert preview[-1].pressure == points[-1].pressure


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
    assert replay[0].pressure >= (580 / 1024) * 0.45
    assert replay[0].pressure < replay[2].pressure
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


def test_startup_correction_ignores_slow_intentional_pressure_ramp() -> None:
    points = [
        InkPoint(0.0, 0.0, pressure=0.12, time_ms=0.0),
        InkPoint(20.0, 0.0, pressure=0.25, time_ms=90.0),
        InkPoint(45.0, 0.0, pressure=0.8, time_ms=180.0),
        InkPoint(120.0, 0.0, pressure=0.8, time_ms=240.0),
    ]

    replay = prepare_replay_stroke(points)

    assert math.isclose(replay[0].pressure, points[0].pressure)
    assert replay[10].pressure < 0.25


def test_startup_correction_backfills_fast_sensor_ramp_without_hairline() -> None:
    points = [
        InkPoint(0.0, 0.0, pressure=0.15, time_ms=0.0),
        InkPoint(12.0, 0.0, pressure=0.18, time_ms=20.0),
        InkPoint(36.0, 0.0, pressure=0.72, time_ms=38.0),
        InkPoint(120.0, 0.0, pressure=0.74, time_ms=90.0),
    ]

    replay = prepare_replay_stroke(points)

    assert replay[0].pressure >= 0.72 * 0.45
    assert replay[0].pressure < replay[8].pressure < replay[35].pressure


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
        / "mouse_pressure_brush"
        / "kis_tool_mouse_pressure.h"
    ).read_text(encoding="utf-8")

    assert 'setToolTip(i18n("Mouse Pressure Brush Tool (Shift+B)"))' in header
    assert 'setIconName(koIconNameCStr("mouse_pressure_mouse"))' in header
    assert "setShortcut(QKeySequence(Qt::SHIFT | Qt::Key_B))" in header
    action = (
        REPO_ROOT
        / "integrations"
        / "krita"
        / "mouse_pressure_brush"
        / "mouse_pressure_brush.action"
    ).read_text(encoding="utf-8")
    assert '<Action name="KritaShape/KisToolMousePressure">' in action
    assert "<toolTip>Mouse Pressure Brush Tool (Shift+B)</toolTip>" in action
    assert "<shortcut>Shift+B</shortcut>" in action


def test_krita_mouse_icons_are_packaged_for_all_themes() -> None:
    plugin_dir = (
        REPO_ROOT / "integrations" / "krita" / "mouse_pressure_brush"
    )
    cmake = (plugin_dir / "CMakeLists.txt").read_text(encoding="utf-8")
    resources = (plugin_dir / "mouse_pressure_icons.qrc").read_text(encoding="utf-8")
    for name in (
        "mouse_pressure_mouse.png",
        "dark_mouse_pressure_mouse.png",
        "light_mouse_pressure_mouse.png",
    ):
        assert (plugin_dir / name).is_file()
        assert name in cmake
        assert name in resources


def test_krita_native_brush_is_default_and_preserves_pressure_events() -> None:
    plugin_dir = (
        REPO_ROOT / "integrations" / "krita" / "mouse_pressure_brush"
    )
    source = (plugin_dir / "kis_tool_mouse_pressure.cpp").read_text(
        encoding="utf-8"
    )
    header = (plugin_dir / "kis_tool_mouse_pressure.h").read_text(
        encoding="utf-8"
    )

    assert 'readEntry("inkMode", 0)' in source
    assert "InkMode m_inkMode {InkMode::NativeBrush};" in header
    assert 'modeCombo->addItem(i18n("Native brush"))' in source
    assert 'modeCombo->addItem(i18n("Native brush + Stroke smoothing"))' in source
    assert 'modeCombo->addItem(i18n("Experimental filled outline"))' in source
    assert "KisToolFreehand::beginPrimaryAction(event);" in source
    assert "KisToolFreehand::continuePrimaryAction(event);" in source
    assert "do not remap or smooth pressure" in source


def test_krita_perfect_freehand_outline_is_built_as_experimental_mode() -> None:
    plugin_dir = (
        REPO_ROOT / "integrations" / "krita" / "mouse_pressure_brush"
    )
    cmake = (plugin_dir / "CMakeLists.txt").read_text(encoding="utf-8")
    source = (plugin_dir / "kis_tool_mouse_pressure.cpp").read_text(
        encoding="utf-8"
    )
    header = (plugin_dir / "kis_tool_mouse_pressure.h").read_text(
        encoding="utf-8"
    )
    outline_source = (plugin_dir / "perfect_freehand_outline.cpp").read_text(
        encoding="utf-8"
    )
    notice = (plugin_dir / "THIRD_PARTY_NOTICES.md").read_text(
        encoding="utf-8"
    )

    assert "perfect_freehand_outline.cpp" in cmake
    assert 'readEntry(\n        "perfectFreehandThinning", 1.0)' in source
    assert "PerfectFreehandOutline::getStroke" in source
    assert "KisToolShapeUtils::FillStyleForegroundColor" in source
    assert "coordinatesConverter()->widgetToImage(point)" in source
    assert "outlinePath.setFillRule(Qt::WindingFill)" in source
    assert "outlinePath.quadTo(outline[1], firstMidpoint)" in source
    assert "outlinePath.lineTo(outline[index])" not in source
    assert "helper.paintPainterPath(outlinePath)" in source
    assert "const DetectedTail outlineStartTail" in source
    assert "const DetectedTail outlineEndTail" in source
    assert "replay = replay.mid(firstIndex, lastIndex - firstIndex + 1)" in source
    assert "helper.paintPolygon(outline)" not in source
    assert "m_inkMode == InkMode::PerfectInk" in source
    assert "qreal m_perfectFreehandThinning {1.0};" in header
    assert "MIN_STREAMLINE_T = 0.15" in outline_source
    assert "Stephen Ruiz Ltd" in notice


def test_krita_path_assist_only_changes_position() -> None:
    plugin_dir = (
        REPO_ROOT / "integrations" / "krita" / "mouse_pressure_brush"
    )
    source = (plugin_dir / "kis_tool_mouse_pressure.cpp").read_text(
        encoding="utf-8"
    )
    header = (plugin_dir / "kis_tool_mouse_pressure.h").read_text(
        encoding="utf-8"
    )

    assert 'readEntry("pathAssistStrength", 0.2)' in source
    assert 'readEntry("pressureSmoothing", 0.25)' in source
    assert "qreal m_pathAssistStrength {0.2};" in header
    assert "qreal m_pressureSmoothing {0.25};" in header
    assert "KoPointerEvent assistedEvent(event, pathAssistedPosition" in source
    assert "KisToolFreehand::endPrimaryAction(&assistedEvent);" in source
    assert "follow = 1.0 - 0.35 * strength" in source
    assert "m_pathAssistStrength * zoomAssistMultiplier()" in source
    assert "m_pressureSmoothing * zoomAssistMultiplier()" in source
    assert "coordinatesConverter()->effectiveZoom()" in source
    assert "cutoffHz = interpolate(40.0, 8.0, smoothing)" in source
    assert "copyTabletEventWithPressure" in source
    assert "Pressure and all" in source
