"""Small custom widgets used by the modern Qt control panel."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Property, QEasingCurve, QPointF, QPropertyAnimation, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from superstrike_pressure.ui.qt_theme import Theme


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None, *, padding: int = 18) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(padding, padding, padding, padding)
        self.content.setSpacing(12)


class Switch(QWidget):
    toggled = Signal(bool)

    def __init__(self, checked: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._checked = bool(checked)
        self._offset = 1.0 if checked else 0.0
        self._theme: Theme | None = None
        self.setFixedSize(38, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Toggle")

    def isChecked(self) -> bool:  # noqa: N802 - Qt naming
        return self._checked

    def setChecked(self, value: bool, *, emit: bool = False) -> None:  # noqa: N802
        value = bool(value)
        changed = value != self._checked
        self._checked = value
        self._offset = 1.0 if value else 0.0
        self.update()
        if changed and emit:
            self.toggled.emit(value)

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            start = self._offset
            self._checked = not self._checked
            animation = QPropertyAnimation(self, b"offset", self)
            animation.setDuration(120)
            animation.setStartValue(start)
            animation.setEndValue(1.0 if self._checked else 0.0)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
            self.toggled.emit(self._checked)
        super().mouseReleaseEvent(event)

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = float(value)
        self.update()

    offset = Property(float, _get_offset, _set_offset)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802
        theme = self._theme
        accent = QColor(theme.accent if theme else "#635BFF")
        off = QColor(theme.border if theme else "#CBD0D8")
        knob = QColor(theme.surface if theme else "#FFFFFF")
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(1, 2, 36, 18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent if self._checked else off)
        painter.drawRoundedRect(track, 9, 9)
        center_x = 11 + self._offset * 16
        painter.setBrush(knob)
        painter.drawEllipse(QPointF(center_x, 11), 7, 7)


class LabeledSwitch(QWidget):
    toggled = Signal(bool)

    def __init__(
        self,
        title: str,
        description: str = "",
        *,
        checked: bool = False,
        compact: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Maximum,
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        labels = QVBoxLayout()
        labels.setSpacing(2)
        self.title = QLabel(title)
        labels.addWidget(self.title)
        self.description = QLabel(description)
        self.description.setObjectName("muted")
        self.description.setWordWrap(True)
        self.description.setVisible(bool(description))
        labels.addWidget(self.description)
        layout.addLayout(labels, 0 if compact else 1)
        self.switch = Switch(checked)
        self.switch.toggled.connect(self.toggled)
        layout.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignVCenter)
        if compact:
            layout.addStretch(1)

    def isChecked(self) -> bool:  # noqa: N802
        return self.switch.isChecked()

    def setChecked(self, value: bool) -> None:  # noqa: N802
        self.switch.setChecked(value)

    def set_theme(self, theme: Theme) -> None:
        self.switch.set_theme(theme)


class SliderField(QWidget):
    valueChanged = Signal(int)

    def __init__(
        self,
        title: str,
        minimum: int,
        maximum: int,
        value: int,
        *,
        suffix: str = "",
        description: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.suffix = suffix
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(7)
        header = QHBoxLayout()
        self.title = QLabel(title)
        header.addWidget(self.title)
        header.addStretch(1)
        self.value_label = QLabel()
        self.value_label.setObjectName("muted")
        header.addWidget(self.value_label)
        root.addLayout(header)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(minimum, maximum)
        self.slider.setValue(value)
        self.spin = QSpinBox()
        self.spin.setRange(minimum, maximum)
        self.spin.setValue(value)
        self.spin.setFixedWidth(82)
        self.spin.setSuffix(suffix)
        row.addWidget(self.slider, 1)
        row.addWidget(self.spin)
        root.addLayout(row)
        self.description = QLabel(description)
        self.description.setObjectName("muted")
        self.description.setWordWrap(True)
        self.description.setVisible(bool(description))
        root.addWidget(self.description)
        self.slider.valueChanged.connect(self._slider_changed)
        self.spin.valueChanged.connect(self._spin_changed)
        self._set_label(value)

    def _set_label(self, value: int) -> None:
        self.value_label.setText(f"{value}{self.suffix}")

    def _slider_changed(self, value: int) -> None:
        self.spin.blockSignals(True)
        self.spin.setValue(value)
        self.spin.blockSignals(False)
        self._set_label(value)
        self.valueChanged.emit(value)

    def _spin_changed(self, value: int) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(False)
        self._set_label(value)
        self.valueChanged.emit(value)

    def value(self) -> int:
        return self.spin.value()

    def setValue(self, value: int) -> None:  # noqa: N802
        self.spin.setValue(value)


class MappingGraph(QWidget):
    """Antialiased pressure mapping plot with live channel markers."""

    RAW_MIN = 300
    RAW_MAX = 700

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(270)
        self._theme: Theme | None = None
        self._series: dict[str, list[tuple[int, int]]] = {}
        self._current: dict[str, tuple[int, int] | None] = {"left": None, "right": None}
        self._visible_channels = ("left",)
        self._live_preview = False

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def set_data(
        self,
        series: dict[str, list[tuple[int, int]]],
        _raw_range: dict[str, tuple[int, int]],
        *,
        channels: tuple[str, ...],
    ) -> None:
        self._series = series
        self._visible_channels = channels
        self.update()

    @classmethod
    def _raw_fraction(cls, raw: int) -> float:
        return max(0.0, min(1.0, (raw - cls.RAW_MIN) / (cls.RAW_MAX - cls.RAW_MIN)))

    def set_current(self, channel: str, raw: int, pressure: int) -> None:
        self._current[channel] = (int(raw), int(pressure))
        self.update()

    def set_live_preview(self, live: bool) -> None:
        self._live_preview = bool(live)
        self.update()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802
        theme = self._theme
        if theme is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(theme.surface))
        plot = QRectF(48, 8, max(1, self.width() - 70), max(1, self.height() - 58))
        grid_pen = QPen(QColor(theme.grid), 1)
        painter.setPen(grid_pen)
        for i in range(5):
            y = plot.top() + plot.height() * i / 4
            x = plot.left() + plot.width() * i / 4
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        painter.setPen(QColor(theme.muted))
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        painter.drawText(QRectF(0, plot.top() - 7, 42, 18), Qt.AlignmentFlag.AlignRight, "100%")
        painter.drawText(QRectF(0, plot.bottom() - 9, 42, 18), Qt.AlignmentFlag.AlignRight, "0%")
        colors = {"left": "#378ADD", "right": "#EF9F27"}
        for channel in self._visible_channels:
            points = self._series.get(channel, [])
            if len(points) < 2:
                continue
            path = QPainterPath()
            for index, (raw, pressure) in enumerate(points):
                px = plot.left() + self._raw_fraction(raw) * plot.width()
                py = plot.bottom() - pressure / 1024 * plot.height()
                if index == 0:
                    path.moveTo(px, py)
                else:
                    path.lineTo(px, py)
            painter.setPen(QPen(QColor(colors[channel]), 2.3))
            painter.drawPath(path)
            current = self._current.get(channel)
            if self._live_preview and current is not None:
                raw, pressure = current
                px = plot.left() + self._raw_fraction(raw) * plot.width()
                py = plot.bottom() - pressure / 1024 * plot.height()
                px = min(plot.right(), max(plot.left(), px))
                py = min(plot.bottom(), max(plot.top(), py))
                painter.setBrush(QColor(colors[channel]))
                painter.setPen(QPen(QColor(theme.surface), 3))
                painter.drawEllipse(QPointF(px, py), 6, 6)
        painter.setPen(QColor(theme.muted))
        painter.drawText(
            QRectF(plot.left(), plot.bottom() + 8, 48, 18),
            Qt.AlignmentFlag.AlignLeft,
            str(self.RAW_MIN),
        )
        painter.drawText(
            QRectF(plot.right() - 48, plot.bottom() + 8, 48, 18),
            Qt.AlignmentFlag.AlignRight,
            str(self.RAW_MAX),
        )
        painter.drawText(
            QRectF(plot.left(), plot.bottom() + 27, plot.width(), 20),
            Qt.AlignmentFlag.AlignCenter,
            "Physical pressure",
        )
        if not self._live_preview:
            message_box = QRectF(
                plot.center().x() - 145,
                plot.center().y() - 37,
                290,
                74,
            )
            overlay = QColor(theme.surface_alt)
            overlay.setAlpha(238)
            painter.setPen(QPen(QColor(theme.border), 1))
            painter.setBrush(overlay)
            painter.drawRoundedRect(message_box, 9, 9)
            painter.setPen(QColor(theme.text))
            painter.drawText(
                message_box.adjusted(12, 10, -12, -34),
                Qt.AlignmentFlag.AlignCenter,
                "Live preview is off",
            )
            painter.setPen(QColor(theme.muted))
            painter.drawText(
                message_box.adjusted(12, 34, -12, -9),
                Qt.AlignmentFlag.AlignCenter,
                "Start to test your pressure settings.",
            )


class StrokeGraph(QWidget):
    """Compact multi-series plot for saved stroke traces."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(300)
        self._theme: Theme | None = None
        self._analysis: dict[str, Any] | None = None

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def set_analysis(self, analysis: dict[str, Any] | None) -> None:
        self._analysis = analysis
        self.update()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802
        theme = self._theme
        if theme is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(theme.surface))
        plot = QRectF(48, 20, max(1, self.width() - 72), max(1, self.height() - 56))
        painter.setPen(QPen(QColor(theme.grid), 1))
        for i in range(5):
            y = plot.top() + plot.height() * i / 4
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        if not self._analysis:
            painter.setPen(QColor(theme.muted))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "Select a recorded stroke")
            return
        series = [
            ("mapped", "#378ADD", "Mapped"),
            ("interpolated", "#8B83FF", "Smoothed"),
            ("injected_time", "#EF9F27", "Injected"),
        ]
        all_points = [point for key, _, _ in series for point in self._analysis.get(key, [])]
        if not all_points:
            return
        max_x = max(float(x) for x, _ in all_points) or 1.0
        for key, color, _label in series:
            points = self._analysis.get(key, [])
            if len(points) < 2:
                continue
            path = QPainterPath()
            for index, (x, y) in enumerate(points):
                px = plot.left() + float(x) / max_x * plot.width()
                py = plot.bottom() - min(1024.0, max(0.0, float(y))) / 1024 * plot.height()
                if index == 0:
                    path.moveTo(px, py)
                else:
                    path.lineTo(px, py)
            painter.setPen(QPen(QColor(color), 2))
            painter.drawPath(path)
        painter.setPen(QColor(theme.muted))
        painter.drawText(
            QRectF(plot.left(), plot.bottom() + 9, plot.width(), 20),
            Qt.AlignmentFlag.AlignCenter,
            f"{max_x:.0f} ms",
        )
        x = plot.left()
        for _key, color, label in series:
            painter.setBrush(QColor(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(x + 4, 10), 4, 4)
            painter.setPen(QColor(theme.muted))
            painter.drawText(QRectF(x + 12, 1, 72, 18), label)
            x += 82


def metric_card(label: str, value: str = "—") -> tuple[QFrame, QLabel]:
    frame = QFrame()
    frame.setObjectName("statCard")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(2)
    caption = QLabel(label)
    caption.setObjectName("muted")
    metric = QLabel(value)
    font = QFont(metric.font())
    font.setPointSize(13)
    font.setWeight(QFont.Weight.DemiBold)
    metric.setFont(font)
    layout.addWidget(caption)
    layout.addWidget(metric)
    return frame, metric
