from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from mouse_pressure.bridge.config import ChannelConfig, RuntimeConfig  # noqa: E402
from mouse_pressure.dev_ui import DevSettings  # noqa: E402
from mouse_pressure.pyside_ui import (  # noqa: E402
    ChannelEditor,
    ConfirmationDialog,
    MainWindow,
)
from mouse_pressure.ui.qt_widgets import MappingGraph  # noqa: E402


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
    assert editor.immediate_button_wake.isChecked() is True
    assert "Immediate stroke start (experimental)" in labels
    assert editor.clean_stroke_endings.isChecked() is False
    assert "Clean stroke endings" in labels

    editor.curve.setCurrentIndex(editor.curve.findData("linear"))
    assert editor.curve_strength.isHidden()
    assert editor.curve_strength_value() == 1.0

    editor.curve.setCurrentIndex(editor.curve.findData("soft"))
    assert not editor.curve_strength.isHidden()


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
    settings = DevSettings(
        raw_min=320,
        raw_max=680,
        deadzone=0,
        curve="linear",
        curve_strength=1.0,
        contact_preset="medium",
        suppress_lmb=True,
        release_teardown=False,
        pressure_floor=20,
        pressure_influence=100,
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
        linked=SimpleNamespace(isChecked=lambda: False),
        channel_tabs=SimpleNamespace(currentIndex=lambda: 0),
        mapping_graph=graph,
        input_metric=_Metric(),
        output_metric=_Metric(),
        raw_metric=_Metric(),
        running=False,
        _channel_settings=lambda _channel: settings,
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

    expected_floor = round(settings.pressure_floor * 1024 / 100)
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
