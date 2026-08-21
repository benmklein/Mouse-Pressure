from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from mouse_pressure.bridge.config import ChannelConfig, RuntimeConfig  # noqa: E402
from mouse_pressure.pyside_ui import (  # noqa: E402
    ChannelEditor,
    ConfirmationDialog,
    MainWindow,
)
from mouse_pressure.ui.qt_widgets import MappingGraph, SliderField  # noqa: E402
from mouse_pressure.ui.settings_model import SettingsDraft  # noqa: E402


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_channel_editor_uses_outcome_labels_and_exposes_raw_values() -> None:
    _app()
    editor = ChannelEditor("right", ChannelConfig(), enabled=False)

    labels = {label.text() for label in editor.findChildren(QLabel)}
    curve_options = [editor.curve.itemText(index) for index in range(editor.curve.count())]
    contact_options = [
        editor.contact.itemText(index) for index in range(editor.contact.count())
    ]

    assert editor.enabled.isChecked() is False
    assert [
        editor.output_target.itemText(index)
        for index in range(editor.output_target.count())
    ] == ["Pressure", "X-tilt", "Y-tilt", "Rotation"]
    assert editor.output_target.currentData() == "pressure"
    assert editor.calibrate_button.text() == "Calibrate pressure range…"
    assert curve_options == [
        "Linear",
        "Logarithmic",
        "Exponential",
    ]
    assert contact_options == [
        "Activates early",
        "Balanced",
        "Requires a firmer press",
    ]
    assert "Raw activation value" in labels
    assert "Raw full-pressure value" in labels
    assert editor.advanced.isHidden()
    assert editor.suppress.title.text() == "Block the normal mouse click"
    assert editor.suppress.description.isHidden()
    assert editor.curve_strength.slider.minimum() == 11
    assert editor.curve_strength.slider.maximum() == 40
    assert editor.reset_button.text() == "Reset right-click settings"
    assert "Immediate stroke start (experimental)" not in labels
    assert "Clean stroke endings" not in labels

    editor.curve.setCurrentIndex(editor.curve.findData("linear"))
    assert editor.curve_strength.isHidden()
    assert editor.curve_strength_value() == 1.0

    editor.curve.setCurrentIndex(editor.curve.findData("soft"))
    assert not editor.curve_strength.isHidden()


def test_debug_mode_controls_diagnostic_navigation() -> None:
    class _Button:
        def __init__(self) -> None:
            self.visible = True
            self.checked = False

        def setVisible(self, visible: bool) -> None:  # noqa: N802
            self.visible = visible

        def setChecked(self, checked: bool) -> None:  # noqa: N802
            self.checked = checked

    buttons = [_Button() for _ in range(4)]
    selected: list[int] = []
    window = SimpleNamespace(
        nav_buttons=buttons,
        pages=SimpleNamespace(currentIndex=lambda: 2),
        _select_page=selected.append,
    )

    MainWindow._set_debug_navigation(window, False)

    assert buttons[2].visible is False
    assert buttons[3].visible is False
    assert buttons[1].checked is True
    assert selected == [1]

    MainWindow._set_debug_navigation(window, True)
    assert buttons[2].visible is True
    assert buttons[3].visible is True


def test_slider_press_cannot_retain_a_pointer_drag() -> None:
    _app()
    field = SliderField("Haptics", 0, 100, 50)
    field.resize(400, 80)
    field.show()
    _app().processEvents()

    slider = field.slider
    QTest.mousePress(
        slider,
        Qt.MouseButton.LeftButton,
        pos=QPoint(slider.width() // 2, slider.height() // 2),
    )
    _app().processEvents()
    pressed_value = slider.value()
    QTest.mouseMove(slider, QPoint(slider.width() + 300, slider.height() // 2))
    _app().processEvents()

    assert slider.isSliderDown() is False
    assert slider.value() == pressed_value


def test_mapping_graph_uses_observed_raw_pressure_domain() -> None:
    assert MappingGraph.RAW_MIN == 300
    assert MappingGraph.RAW_MAX == 750
    assert MappingGraph._raw_fraction(300) == 0.0
    assert MappingGraph._raw_fraction(525) == 0.5
    assert MappingGraph._raw_fraction(750) == 1.0
    assert MappingGraph._raw_fraction(200) == 0.0
    assert MappingGraph._raw_fraction(800) == 1.0


def test_reset_confirmation_uses_compact_themed_dialog() -> None:
    _app()
    dialog = ConfirmationDialog(
        None,
        title="Reset right-click settings?",
        message="Restore the recommended pressure settings for right-click only.",
        confirm_text="Reset settings",
    )

    assert dialog.isModal()
    assert dialog.width() == 420
    assert dialog.title_label.text() == "Reset right-click settings?"
    assert dialog.message_label.text().startswith("Restore the recommended")
    assert dialog.cancel_button.text() == "Cancel"
    assert dialog.confirm_button.text() == "Reset settings"
    assert dialog.confirm_button.objectName() == "primary"


def test_live_mapping_marker_uses_effective_pressure_floor() -> None:
    channel = ChannelConfig(
        raw_min=320,
        raw_max=680,
        pressure_floor=20,
        pressure_influence=100,
    )
    draft = SettingsDraft(
        config=RuntimeConfig(left=channel),
        injection_hz=240.0,
        normal_device={},
    )

    class _Graph:
        def __init__(self) -> None:
            self.current: dict[str, tuple[int, int]] = {}

        def set_current(self, channel: str, raw: int, pressure: int) -> None:
            self.current[channel] = (raw, pressure)

    class _Metric:
        def __init__(self) -> None:
            self.text = ""

        def setText(self, text: str) -> None:  # noqa: N802
            self.text = text

    graph = _Graph()
    window = SimpleNamespace(
        _latest_raw={"left": 0, "right": 0},
        _latest_mapped={"left": 0, "right": 0},
        channel_tabs=SimpleNamespace(currentIndex=lambda: 0),
        mapping_graph=graph,
        input_metric=_Metric(),
        output_metric=_Metric(),
        raw_metric=_Metric(),
        running=False,
        _settings_draft=lambda: draft,
    )

    MainWindow._handle_telemetry(
        window,
        {
            "left_raw": 321,
            "right_raw": 321,
            "left_mapped": 3,
            "right_mapped": 3,
            "hz": 60.0,
        },
    )

    expected_floor = round(channel.pressure_floor * 1024 / 100)
    assert graph.current["left"] == (321, expected_floor)
    assert window.input_metric.text == "0%"
    assert window.output_metric.text == "20%"


def test_detected_mapping_off_values_seed_mapping_on_defaults() -> None:
    class _Value:
        def __init__(self) -> None:
            self.value = None

        def setValue(self, value: int) -> None:  # noqa: N802
            self.value = value

    class _Text:
        def __init__(self) -> None:
            self.text = ""

        def setText(self, text: str) -> None:  # noqa: N802
            self.text = text

        def setEnabled(self, _enabled: bool) -> None:  # noqa: N802
            return

    dpi = _Value()
    haptic_left = _Value()
    haptic_right = _Value()
    window = SimpleNamespace(
        detecting=True,
        _normal_device={},
        normal_dpi=_Text(),
        normal_haptics={"left": _Text(), "right": _Text()},
        service=SimpleNamespace(
            get_config=lambda: RuntimeConfig(
                session_device_settings_follow_normal=True
            )
        ),
        dpi=dpi,
        haptics={"left": haptic_left, "right": haptic_right},
        start_button=_Text(),
        _set_status=lambda *_args: None,
        write_system=lambda *_args, **_kwargs: None,
    )

    MainWindow._handle_device_detected(
        window,
        {"dpi": 1600, "haptic_left": 2, "haptic_right": 4},
    )

    assert dpi.value == 1600
    assert haptic_left.value == 2
    assert haptic_right.value == 4
