from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from superstrike_pressure.bridge.config import ChannelConfig  # noqa: E402
from superstrike_pressure.pyside_ui import ChannelEditor  # noqa: E402
from superstrike_pressure.ui.qt_widgets import MappingGraph  # noqa: E402


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

    editor.curve.setCurrentIndex(editor.curve.findData("linear"))
    assert editor.curve_strength.isHidden()
    assert editor.curve_strength_value() == 1.0

    editor.curve.setCurrentIndex(editor.curve.findData("soft"))
    assert not editor.curve_strength.isHidden()


def test_mapping_graph_uses_observed_raw_pressure_domain() -> None:
    assert MappingGraph.RAW_MIN == 300
    assert MappingGraph.RAW_MAX == 700
    assert MappingGraph._raw_fraction(300) == 0.0
    assert MappingGraph._raw_fraction(500) == 0.5
    assert MappingGraph._raw_fraction(700) == 1.0
    assert MappingGraph._raw_fraction(200) == 0.0
    assert MappingGraph._raw_fraction(800) == 1.0
