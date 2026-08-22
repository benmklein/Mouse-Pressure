"""Modern PySide6 control panel for Mouse Pressure."""

from __future__ import annotations

import datetime as dt
import json
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QEvent, QObject, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QFocusEvent, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from mouse_pressure import __version__
from mouse_pressure.bridge.config import ChannelConfig, LaunchConfig, RuntimeConfig
from mouse_pressure.dev_ui import (
    BridgeController,
    stroke_analysis_data,
)
from mouse_pressure.runtime.config_store import ConfigStore
from mouse_pressure.runtime.device_settings import SessionDeviceSettings
from mouse_pressure.runtime.log_bus import LogBus, LogEntry
from mouse_pressure.runtime.runtime_service import RuntimeService
from mouse_pressure.ui.hotkeys import parse_global_hotkey, parse_hold_hotkey
from mouse_pressure.ui.qt_theme import Theme, stylesheet, theme_for
from mouse_pressure.ui.qt_widgets import (
    Card,
    LabeledSwitch,
    MappingGraph,
    SliderField,
    StrokeGraph,
    metric_card,
)
from mouse_pressure.ui.settings_model import SettingsDraft, actuation_raw_estimate
from mouse_pressure.ui.windows_shell import (
    SingleInstanceGuard,
    StartHotkeyListener,
    asset_path,
    set_windows_app_identity,
)

CHANNEL_COLORS = {"left": "#378ADD", "right": "#EF9F27"}
MOUSE_PAGE_MAX_WIDTH = 720
FORM_LABEL_MIN_WIDTH = 112
FORM_HOTKEY_WIDTH = 240
FORM_COMPACT_WIDTH = 180


def _label(text: str, *, muted: bool = False, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if muted:
        widget.setObjectName("muted")
    widget.setWordWrap(wrap)
    return widget


def _page_container() -> tuple[QWidget, QVBoxLayout]:
    widget = QWidget()
    widget.setObjectName("page")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    return widget, layout


def _settings_form() -> QGridLayout:
    layout = QGridLayout()
    layout.setHorizontalSpacing(16)
    layout.setVerticalSpacing(10)
    layout.setColumnMinimumWidth(0, FORM_LABEL_MIN_WIDTH)
    layout.setColumnStretch(1, 1)
    return layout


class HotkeySequenceEdit(QKeySequenceEdit):
    """Release global shortcut registration while recording a replacement."""

    def __init__(
        self,
        binding: str,
        *,
        capture_started: Callable[[], None],
        capture_finished: Callable[[], None],
    ) -> None:
        super().__init__(QKeySequence(binding))
        self._capture_started = capture_started
        self._capture_finished = capture_finished
        self.setMaximumSequenceLength(1)
        self.setClearButtonEnabled(True)

    def focusInEvent(self, event: QFocusEvent) -> None:  # noqa: N802
        self._capture_started()
        super().focusInEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self._capture_finished()


class HoldShortcutEdit(QLineEdit):
    """Capture a keyboard chord or mouse side button for hold-to-remap."""

    bindingChanged = Signal(str)

    def __init__(
        self,
        binding: str,
        *,
        capture_started: Callable[[], None],
        capture_finished: Callable[[], None],
    ) -> None:
        super().__init__()
        self._capture_started = capture_started
        self._capture_finished = capture_finished
        self._binding = ""
        self.setReadOnly(True)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.set_binding(binding)

    def binding(self) -> str:
        return self._binding

    def set_binding(self, binding: str) -> None:
        normalized = parse_hold_hotkey(binding).label
        changed = normalized != self._binding
        self._binding = normalized
        self.setText(normalized)
        if changed:
            self.bindingChanged.emit(normalized)

    def focusInEvent(self, event: QFocusEvent) -> None:  # noqa: N802
        self._capture_started()
        super().focusInEvent(event)
        self.selectAll()

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self._capture_finished()

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802
        if event.key() in (
            Qt.Key.Key_Control,
            Qt.Key.Key_Alt,
            Qt.Key.Key_Shift,
            Qt.Key.Key_Meta,
        ):
            event.accept()
            return
        sequence = QKeySequence(event.keyCombination()).toString(
            QKeySequence.SequenceFormat.PortableText
        )
        try:
            self.set_binding(sequence)
        except ValueError:
            event.ignore()
            return
        event.accept()

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802
        binding = {
            Qt.MouseButton.MiddleButton: "Middle click",
            Qt.MouseButton.XButton1: "Mouse 4",
            Qt.MouseButton.XButton2: "Mouse 5",
        }.get(event.button())
        if binding is None:
            super().mousePressEvent(event)
            return
        self.setFocus()
        self.set_binding(binding)
        event.accept()


class ConfirmationDialog(QDialog):
    """Compact app-themed confirmation without the native message-box chrome."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        title: str,
        message: str,
        confirm_text: str,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("confirmationDialog")
        self.setModal(True)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(420)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        card = QFrame()
        card.setObjectName("dialogCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("dialogTitle")
        header.addWidget(self.title_label, 1)
        close_button = QToolButton()
        close_button.setObjectName("dialogClose")
        close_button.setText("×")
        close_button.setAccessibleName("Cancel")
        close_button.clicked.connect(self.reject)
        header.addWidget(close_button)
        layout.addLayout(header)

        self.message_label = _label(message, muted=True, wrap=True)
        layout.addWidget(self.message_label)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        self.cancel_button.setDefault(True)
        self.confirm_button = QPushButton(confirm_text)
        self.confirm_button.setObjectName("primary")
        self.confirm_button.setAutoDefault(False)
        self.confirm_button.clicked.connect(self.accept)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.confirm_button)
        layout.addLayout(actions)

        outer.addWidget(card)


class _WheelToScrollFilter(QObject):
    """Reserve the mouse wheel for scrolling, never value editing."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() != QEvent.Type.Wheel:
            return super().eventFilter(watched, event)

        parent = watched.parent()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                bar = parent.verticalScrollBar()
                if bar.maximum() > bar.minimum():
                    angle_delta = event.angleDelta().y()  # type: ignore[attr-defined]
                    pixel_delta = event.pixelDelta().y()  # type: ignore[attr-defined]
                    if pixel_delta:
                        distance = pixel_delta
                    elif angle_delta:
                        distance = round(
                            (angle_delta / 120.0) * max(48, bar.pageStep() // 8)
                        )
                    else:
                        distance = 0
                    bar.setValue(bar.value() - distance)
                    break
            parent = parent.parent()
        event.accept()
        return True


class _CurrentPageStack(QStackedWidget):
    """Size a stack from its visible page instead of its largest page."""

    def sizeHint(self) -> QSize:  # noqa: N802
        current = self.currentWidget()
        return current.sizeHint() if current is not None else super().sizeHint()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        current = self.currentWidget()
        return (
            current.minimumSizeHint()
            if current is not None
            else super().minimumSizeHint()
        )

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        super().setCurrentIndex(index)
        self.updateGeometry()


class ChannelEditor(QWidget):
    """One channel's primary and advanced pressure controls."""

    def __init__(
        self,
        channel: str,
        config: Any,
        *,
        enabled: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingsEditor")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.channel = channel
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        root.setSpacing(14)

        self.enabled = LabeledSwitch(
            f"Remap {channel.lower()} click",
            "Remap mouse button pressure to another signal",
            checked=enabled,
        )
        root.addWidget(self.enabled)

        self.linked_notice = _label(
            "Uses the left-click pressure settings while linking is on.",
            muted=True,
            wrap=True,
        )
        self.linked_notice.setVisible(False)
        root.addWidget(self.linked_notice)

        self.settings_content = QWidget()
        settings = QVBoxLayout(self.settings_content)
        settings.setContentsMargins(0, 0, 0, 0)
        settings.setSpacing(14)

        settings.addSpacing(8)
        self.mapping_top_rule = QFrame()
        self.mapping_top_rule.setFrameShape(QFrame.Shape.NoFrame)
        self.mapping_top_rule.setObjectName("mappingRule")
        self.mapping_top_rule.setFixedHeight(1)
        self.mapping_top_rule.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        settings.addWidget(self.mapping_top_rule)

        input_range = QGridLayout()
        input_range.setHorizontalSpacing(12)
        input_range.setVerticalSpacing(8)
        input_range.addWidget(_label("Min Mouse Pressure", muted=True), 0, 0)
        input_range.addWidget(_label("Max Mouse Pressure", muted=True), 0, 1)
        self.raw_min = QSpinBox()
        self.raw_min.setRange(0, 1022)
        self.raw_min.setValue(config.raw_min)
        self.raw_max = QSpinBox()
        self.raw_max.setRange(1, 1023)
        self.raw_max.setValue(config.raw_max)
        input_range.addWidget(self.raw_min, 1, 0)
        input_range.addWidget(self.raw_max, 1, 1)
        settings.addLayout(input_range)

        self.calibrate_button = QPushButton("Calibrate input pressure range…")
        settings.addWidget(self.calibrate_button)

        self.mapping_arrow = QLabel()
        self.mapping_arrow.setPixmap(
            QIcon(str(asset_path("arrow-down.svg"))).pixmap(QSize(20, 20))
        )
        self.mapping_arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        settings.addWidget(self.mapping_arrow)

        self.map_to_label = _label("Map to", muted=True)
        self.map_to_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        settings.addWidget(self.map_to_label)
        self.output_target = QComboBox()
        self.output_target.addItem("Pressure (Simulated Tablet)", "pressure")
        self.output_target.addItem("Mouse sensitivity", "mouse_sensitivity")
        self.output_target.addItem("X-tilt (Simulated Tablet)", "x_tilt")
        self.output_target.addItem("Y-tilt (Simulated Tablet)", "y_tilt")
        self.output_target.addItem("Rotation (Simulated Tablet)", "rotation")
        self.output_target.setCurrentIndex(
            max(0, self.output_target.findData(config.output_target))
        )
        settings.addWidget(self.output_target)

        self.output_range_widgets: dict[str, QWidget] = {}
        self.output_range_fields: dict[str, tuple[QSpinBox, QSpinBox]] = {}

        def add_output_range(
            target: str,
            minimum: int,
            maximum: int,
            light_value: int,
            firm_value: int,
            suffix: str,
            output_name: str,
        ) -> None:
            widget = QWidget()
            grid = QGridLayout(widget)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(8)
            grid.addWidget(_label(f"Min Output {output_name}", muted=True), 0, 0)
            grid.addWidget(_label(f"Max Output {output_name}", muted=True), 0, 1)
            light = QSpinBox()
            firm = QSpinBox()
            for field, value in ((light, light_value), (firm, firm_value)):
                field.setRange(minimum, maximum)
                field.setSuffix(suffix)
                field.setValue(value)
            grid.addWidget(light, 1, 0)
            grid.addWidget(firm, 1, 1)
            settings.addWidget(widget)
            self.output_range_widgets[target] = widget
            self.output_range_fields[target] = (light, firm)

        add_output_range(
            "mouse_sensitivity",
            0,
            200,
            config.sensitivity_light,
            config.sensitivity_firm,
            "%",
            "Sensitivity",
        )
        add_output_range(
            "x_tilt",
            -60,
            60,
            config.x_tilt_light,
            config.x_tilt_firm,
            "°",
            "X-Tilt",
        )
        add_output_range(
            "y_tilt",
            -60,
            60,
            config.y_tilt_light,
            config.y_tilt_firm,
            "°",
            "Y-Tilt",
        )
        add_output_range(
            "rotation",
            0,
            359,
            config.rotation_light,
            config.rotation_firm,
            "°",
            "Rotation",
        )
        self.sensitivity_light, self.sensitivity_firm = self.output_range_fields[
            "mouse_sensitivity"
        ]
        self.sensitivity_options = self.output_range_widgets["mouse_sensitivity"]
        self.output_target.currentIndexChanged.connect(
            self._update_output_target_controls
        )
        self._update_output_target_controls()

        self.mapping_bottom_rule = QFrame()
        self.mapping_bottom_rule.setFrameShape(QFrame.Shape.NoFrame)
        self.mapping_bottom_rule.setObjectName("mappingRule")
        self.mapping_bottom_rule.setFixedHeight(1)
        self.mapping_bottom_rule.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        settings.addWidget(self.mapping_bottom_rule)
        settings.addSpacing(8)

        settings.addWidget(_label("Pressure curve", muted=True))
        self.curve = QComboBox()
        self.curve.addItem("Linear", "linear")
        self.curve.addItem("Logarithmic", "hard")
        self.curve.addItem("Exponential", "soft")
        index = self.curve.findData(config.curve)
        self.curve.setCurrentIndex(max(0, index))
        settings.addWidget(self.curve)

        self.curve_strength = SliderField(
            "Curve strength",
            11,
            40,
            round(config.curve_strength * 10),
        )
        self.curve_strength.spin.setVisible(False)
        self.curve_strength.value_label.setText(
            f"{self.curve_strength.value() / 10:.1f}"
        )
        self.curve_strength.valueChanged.connect(
            lambda value: self.curve_strength.value_label.setText(f"{value / 10:.1f}")
        )
        settings.addWidget(self.curve_strength)
        self.curve.currentIndexChanged.connect(self._update_curve_controls)
        self._update_curve_controls()

        self.suppress = LabeledSwitch(
            "Block the normal mouse click",
        )
        settings.addWidget(self.suppress)

        self.advanced_button = QToolButton()
        self.advanced_button.setText("Advanced settings")
        self.advanced_button.setCheckable(True)
        self.advanced_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.advanced_button.setArrowType(Qt.ArrowType.NoArrow)
        self.advanced_button.setIcon(
            QIcon(str(asset_path("chevron-right.svg")))
        )
        self.advanced_button.setIconSize(QSize(10, 10))
        settings.addWidget(self.advanced_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.advanced = QWidget()
        advanced = QVBoxLayout(self.advanced)
        advanced.setContentsMargins(0, 2, 0, 0)
        advanced.setSpacing(14)
        self.deadzone = SliderField("Deadzone", 0, 20, config.deadzone_low, suffix="%")
        self.pressure_floor = SliderField(
            "Pressure floor", 0, 100, config.pressure_floor, suffix="%"
        )
        self.path_stabilization = SliderField(
            "Path stabilization",
            0,
            100,
            config.path_stabilization,
            suffix="%",
            description="Values above 0 add path smoothing and may increase latency.",
        )
        self.pressure_influence = SliderField(
            "Pressure influence",
            0,
            100,
            config.pressure_influence,
            suffix="%",
        )
        for widget in (
            self.deadzone,
            self.pressure_floor,
            self.path_stabilization,
            self.pressure_influence,
        ):
            advanced.addWidget(widget)
        self.advanced.setVisible(False)
        settings.addWidget(self.advanced)
        self.reset_button = QPushButton(f"Reset {channel.lower()}-click settings")
        settings.addWidget(self.reset_button)
        root.addWidget(self.settings_content)
        self.advanced_button.toggled.connect(self._show_advanced)

    def _update_curve_controls(self, *_args: Any) -> None:
        self.curve_strength.setVisible(self.curve.currentData() != "linear")
        self.curve_strength.updateGeometry()
        self.updateGeometry()

    def _show_advanced(self, visible: bool) -> None:
        self.advanced.setVisible(visible)
        self.advanced_button.setIcon(
            QIcon(
                str(
                    asset_path(
                        "chevron-down.svg" if visible else "chevron-right.svg"
                    )
                )
            )
        )
        self.advanced.updateGeometry()
        self.updateGeometry()

    def _update_output_target_controls(self, *_args: Any) -> None:
        selected = str(self.output_target.currentData())
        for target, widget in self.output_range_widgets.items():
            widget.setVisible(target == selected)
            widget.updateGeometry()
        self.updateGeometry()

    def curve_strength_value(self) -> float:
        if self.curve.currentData() == "linear":
            return 1.0
        return self.curve_strength.value() / 10.0

    def control_widgets(self) -> list[QWidget]:
        widgets: list[QWidget] = [
            self.enabled,
            self.output_target,
            self.raw_min,
            self.raw_max,
            self.curve,
            self.curve_strength,
            self.suppress,
            self.deadzone,
            self.pressure_floor,
            self.path_stabilization,
            self.pressure_influence,
        ]
        for fields in self.output_range_fields.values():
            widgets.extend(fields)
        return widgets


class MainWindow(QMainWindow):
    def __init__(
        self,
        service: RuntimeService,
        controller: BridgeController,
        log_bus: LogBus,
        config_store: ConfigStore,
    ) -> None:
        super().__init__()
        self.service = service
        self.controller = controller
        self.log_bus = log_bus
        self.config_store = config_store
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.running = False
        self.busy = False
        self.detecting = True
        self.closing = False
        self._allow_close = False
        self._close_complete = threading.Event()
        self._close_deadline = 0.0
        self._close_error: str | None = None
        self._loading = False
        self.settings_dirty = False
        self.calibrating = False
        self.calibration_dialog: QDialog | None = None
        self._latest_raw = {"left": 0, "right": 0}
        self._latest_mapped = {"left": 0, "right": 0}
        self._normal_device: SessionDeviceSettings | None = None
        self._trace_paths: dict[str, Path] = {}
        self._hotkey_capture_active = False
        self._qt_settings = QSettings("Mouse Pressure", "Mouse Pressure")
        self.theme_name = str(self._qt_settings.value("theme", "dark"))
        self.theme: Theme = theme_for(self.theme_name)

        self.setWindowTitle("Mouse Pressure")
        self.setWindowIcon(QIcon(str(asset_path("lucide_mouse.png"))))
        self.resize(1180, 780)
        self.setMinimumSize(QSize(980, 680))
        self._build_ui(service.get_config())
        self._wheel_to_scroll = _WheelToScrollFilter(self)
        for widget_type in (QSpinBox, QComboBox, QSlider):
            for widget in self.findChildren(widget_type):
                widget.installEventFilter(self._wheel_to_scroll)
        self._build_tray()
        self.apply_theme(self.theme_name)

        log_bus.subscribe(lambda entry: self.events.put(("log", entry)))
        service.set_telemetry_callback(
            lambda sample: self.events.put(("telemetry", sample))
        )
        service.set_failure_callback(
            lambda message: self.events.put(("runtime_error", message))
        )
        service.set_force_stop_callback(
            lambda message: self.events.put(("force_stopped", message))
        )

        self.start_hotkey = StartHotkeyListener(
            lambda: self.events.put(("start_hotkey", None)),
            service.get_config().activation_hotkey,
        )
        if not self.start_hotkey.start():
            self.write_system(
                f"{self.start_hotkey.hotkey.label} could not be registered.",
                level="WARN",
            )

        self.event_timer = QTimer(self)
        self.event_timer.timeout.connect(self._drain_events)
        self.event_timer.start(33)
        self.write_system("Ready. Settings are stored in ~/.mouse-pressure/config.json")
        self._redraw_mapping()
        self._begin_device_detection()

    # ---------- UI construction ----------
    def _build_ui(self, config: RuntimeConfig) -> None:
        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(204)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 18, 18, 16)
        side.setSpacing(8)
        brand_row = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(QIcon(str(asset_path("lucide_mouse.png"))).pixmap(26, 26))
        brand_row.addWidget(icon)
        brand = QLabel("Mouse Pressure")
        brand.setObjectName("brand")
        brand_row.addWidget(brand)
        brand_row.addStretch(1)
        side.addLayout(brand_row)
        side.addSpacing(18)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: list[QPushButton] = []
        for index, title in enumerate(("Pressure", "Mouse", "Stroke analysis", "Logs")):
            button = QPushButton(title)
            button.setObjectName("nav")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, i=index: self._select_page(i))
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            side.addWidget(button)
        self.nav_buttons[0].setChecked(True)
        side.addStretch(1)
        side.addWidget(_label("Output device", muted=True))
        backend_row = QHBoxLayout()
        backend_row.setContentsMargins(0, 0, 0, 0)
        backend_row.setSpacing(7)
        self.sidebar_backend_dot = QFrame()
        self.sidebar_backend_dot.setFixedSize(8, 8)
        self.sidebar_backend_dot.setObjectName("connectionDotDisconnected")
        backend_row.addWidget(self.sidebar_backend_dot)
        self.sidebar_backend = QLabel("Disconnected")
        self.sidebar_backend.setToolTip("Native Windows Ink output")
        backend_row.addWidget(self.sidebar_backend)
        backend_row.addStretch(1)
        side.addLayout(backend_row)
        footer_rule = QFrame()
        footer_rule.setFrameShape(QFrame.Shape.HLine)
        footer_rule.setObjectName("footerRule")
        side.addWidget(footer_rule)
        appearance_row = QHBoxLayout()
        appearance_row.addWidget(_label("Appearance", muted=True))
        self.theme_selector = QComboBox()
        self.theme_selector.addItem("Light", "light")
        self.theme_selector.addItem("Dark", "dark")
        self.theme_selector.setCurrentIndex(
            max(0, self.theme_selector.findData(self.theme_name))
        )
        self.theme_selector.currentIndexChanged.connect(
            lambda _index: self.apply_theme(str(self.theme_selector.currentData()))
        )
        appearance_row.addWidget(self.theme_selector, 1)
        side.addLayout(appearance_row)
        self.version_label = _label(f"Version {__version__}", muted=True)
        self.version_label.setObjectName("versionLabel")
        side.addWidget(self.version_label)
        layout.addWidget(sidebar)

        content = QVBoxLayout()
        content.setContentsMargins(24, 18, 24, 20)
        content.setSpacing(16)
        header = QHBoxLayout()
        self.page_title = QLabel("Pressure")
        self.page_title.setObjectName("pageTitle")
        header.addWidget(self.page_title)
        header.addStretch(1)
        self.status_label = QLabel("Detecting mouse…")
        self.status_label.setObjectName("statusBusy")
        header.addWidget(self.status_label)
        self.save_button = QPushButton("Apply changes")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save_or_apply)
        header.addWidget(self.save_button)
        self.start_button = QPushButton("Start")
        self.start_button.setObjectName("primary")
        self.start_button.setToolTip(
            "Apply the visible settings and start pressure output "
            f"({config.activation_hotkey})"
        )
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self._toggle_bridge)
        header.addWidget(self.start_button)
        content.addLayout(header)

        self.pages = QStackedWidget()
        self.pressure_page = self._build_pressure_page(config)
        self.mouse_page = self._build_mouse_page(config)
        self.analysis_page = self._build_analysis_page()
        self.logs_page = self._build_logs_page()
        for page in (
            self.pressure_page,
            self.mouse_page,
            self.analysis_page,
            self.logs_page,
        ):
            self.pages.addWidget(page)
        self._set_debug_navigation(config.debug_mode)
        content.addWidget(self.pages, 1)
        layout.addLayout(content, 1)
        self._connect_non_mapping_controls()
        self.settings_dirty = False
        self.save_button.setEnabled(False)

    def _scroll_page(self, content: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(content)
        return area

    def _build_pressure_page(self, config: RuntimeConfig) -> QWidget:
        content, layout = _page_container()
        layout.setSpacing(16)

        segment_row = QHBoxLayout()
        segment_row.setSpacing(0)
        self.channel_group = QButtonGroup(self)
        self.channel_group.setExclusive(True)
        self.channel_buttons: list[QPushButton] = []
        for index in range(2):
            button = QPushButton()
            button.setObjectName("channelSegment")
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, selected=index: (
                    self.channel_tabs.setCurrentIndex(selected)
                )
            )
            self.channel_group.addButton(button, index)
            self.channel_buttons.append(button)
            segment_row.addWidget(button, 1)
        self.channel_buttons[0].setChecked(True)
        segment_row.addStretch(2)
        layout.addLayout(segment_row)

        self.linked = QCheckBox("Use the same settings for both buttons")
        self.linked.setChecked(config.linked)
        layout.addWidget(self.linked, 0, Qt.AlignmentFlag.AlignLeft)

        body = QHBoxLayout()
        body.setSpacing(16)
        editor_card = Card()
        self.editor_card = editor_card
        editor_card.setMinimumWidth(390)
        editor_card.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Maximum,
        )
        self.channel_tabs = _CurrentPageStack()
        self.channel_tabs.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.editors = {
            "left": ChannelEditor("left", config.left, enabled=config.left_enabled),
            "right": ChannelEditor("right", config.right, enabled=config.right_enabled),
        }
        self.left_enabled = self.editors["left"].enabled
        self.right_enabled = self.editors["right"].enabled
        self.editors["left"].suppress.setChecked(config.suppress_lmb)
        self.editors["right"].suppress.setChecked(config.suppress_rmb)
        for channel in ("left", "right"):
            self.channel_tabs.addWidget(self.editors[channel])
            self.editors[channel].calibrate_button.clicked.connect(
                lambda _checked=False, selected=channel: self._begin_calibration(
                    selected
                )
            )
            self.editors[channel].reset_button.clicked.connect(
                lambda _checked=False, selected=channel: self._reset_channel_settings(
                    selected
                )
            )
            self.editors[channel].advanced_button.toggled.connect(
                lambda _checked=False: QTimer.singleShot(0, self._resize_editor_card)
            )
            self.editors[channel].curve.currentIndexChanged.connect(
                lambda _index: QTimer.singleShot(0, self._resize_editor_card)
            )
            self.editors[channel].output_target.currentIndexChanged.connect(
                lambda _index: QTimer.singleShot(0, self._resize_editor_card)
            )
        editor_card.content.addWidget(self.channel_tabs)
        body.addWidget(editor_card, 5, Qt.AlignmentFlag.AlignTop)

        graph_column = QVBoxLayout()
        graph_card = Card()
        self.graph_title = QLabel("Output Pressure")
        self.graph_title.setObjectName("sectionTitle")
        graph_card.content.addWidget(self.graph_title)
        self.mapping_graph = MappingGraph()
        self.mapping_graph.setFixedHeight(310)
        graph_card.content.addWidget(self.mapping_graph)
        stats = QHBoxLayout()
        raw_card, self.raw_metric = metric_card("Raw Pressure", "—")
        input_card, self.input_metric = metric_card("Input Pressure", "—")
        output_card, self.output_metric = metric_card("Output Pressure", "—")
        self.output_metric_caption = output_card.layout().itemAt(0).widget()
        for card in (raw_card, input_card, output_card):
            stats.addWidget(card, 1)
        graph_card.content.addLayout(stats)
        graph_card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        graph_column.addWidget(graph_card, 0, Qt.AlignmentFlag.AlignTop)
        body.addLayout(graph_column, 7)
        layout.addLayout(body)
        layout.addStretch(1)

        self._connect_mapping_controls()
        self.left_enabled.toggled.connect(self._pressure_options_changed)
        self.right_enabled.toggled.connect(self._pressure_options_changed)
        self.linked.toggled.connect(self._pressure_options_changed)
        self.channel_tabs.currentChanged.connect(self._channel_selected)
        self._pressure_options_changed(mark_dirty=False)
        return self._scroll_page(content)

    def _build_mouse_page(self, config: RuntimeConfig) -> QScrollArea:
        content, layout = _page_container()
        content.setMaximumWidth(MOUSE_PAGE_MAX_WIDTH)
        layout.setSpacing(16)
        hardware = Card()
        title = QLabel("Mouse Settings")
        title.setObjectName("sectionTitle")
        hardware.content.addWidget(title)
        grid = _settings_form()
        grid.setColumnStretch(1, 0)
        grid.setColumnStretch(2, 0)
        grid.setColumnStretch(3, 0)
        grid.setColumnStretch(4, 1)
        grid.setColumnStretch(5, 0)
        grid.setColumnMinimumWidth(2, 48)

        self.mouse_settings_rules: list[QFrame] = []
        for column in (1, 3):
            rule = QFrame()
            rule.setFrameShape(QFrame.Shape.NoFrame)
            rule.setObjectName("settingsColumnRule")
            rule.setFixedWidth(1)
            rule.setSizePolicy(
                QSizePolicy.Policy.Fixed,
                QSizePolicy.Policy.Expanding,
            )
            grid.addWidget(rule, 0, column, 6, 1)
            self.mouse_settings_rules.append(rule)
        grid.addWidget(
            _label("Off", muted=True),
            0,
            2,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        grid.addWidget(
            _label("On", muted=True),
            0,
            4,
            1,
            2,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        grid.addWidget(QLabel("DPI"), 1, 0)
        self.normal_dpi = _label("Detecting…", muted=True)
        grid.addWidget(self.normal_dpi, 1, 2, alignment=Qt.AlignmentFlag.AlignCenter)
        self.dpi = QSpinBox()
        self.dpi.setRange(100, 32000)
        self.dpi.setSingleStep(50)
        self.dpi.setValue(config.session_dpi)
        self.dpi.setFixedWidth(FORM_COMPACT_WIDTH)
        grid.addWidget(self.dpi, 1, 5, alignment=Qt.AlignmentFlag.AlignRight)
        self.haptics: dict[str, SliderField] = {}
        self.normal_haptics: dict[str, QLabel] = {}
        self.actuation: dict[str, SliderField] = {}
        self.normal_actuation: dict[str, QLabel] = {}
        for index, channel in enumerate(("left", "right")):
            row = 2 + index * 2
            grid.addWidget(QLabel(f"{channel.title()} haptics"), row, 0)
            normal = _label("—", muted=True)
            self.normal_haptics[channel] = normal
            grid.addWidget(normal, row, 2, alignment=Qt.AlignmentFlag.AlignCenter)
            value = (
                config.session_haptic_left
                if channel == "left"
                else config.session_haptic_right
            )
            slider = SliderField("", 0, 5, value)
            slider.title.setVisible(False)
            slider.value_label.setVisible(False)
            slider.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
            slider.slider.setTickInterval(1)
            slider.add_tick_marks(6)
            slider.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Maximum,
            )
            self.haptics[channel] = slider
            grid.addWidget(slider, row, 4, 1, 2)

            actuation_row = row + 1
            grid.addWidget(QLabel(f"{channel.title()} actuation point"), actuation_row, 0)
            normal_actuation = _label("—", muted=True)
            self.normal_actuation[channel] = normal_actuation
            grid.addWidget(
                normal_actuation,
                actuation_row,
                2,
                alignment=Qt.AlignmentFlag.AlignCenter,
            )
            channel_config = config.left if channel == "left" else config.right
            actuation = SliderField("", 1, 10, channel_config.actuation_level)
            actuation.title.setVisible(False)
            actuation.value_label.setVisible(False)
            actuation.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
            actuation.slider.setTickInterval(1)
            actuation.add_tick_marks(10)
            actuation.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Maximum,
            )
            self.actuation[channel] = actuation
            grid.addWidget(actuation, actuation_row, 4, 1, 2)
        hardware.content.addLayout(grid)
        hardware.content.addWidget(
            _label(
                "When deactivated, previous settings are restored. "
                "0 haptics (off) is recommended for digital painting.",
                muted=True,
                wrap=True,
            )
        )
        layout.addWidget(hardware)

        app = Card()
        app_title = QLabel("Application")
        app_title.setObjectName("sectionTitle")
        app.content.addWidget(app_title)

        shortcut_grid = _settings_form()
        shortcut_grid.addWidget(QLabel("Mode"), 0, 0)
        self.remap_mode = QComboBox()
        self.remap_mode.addItem("Always remap", "always")
        self.remap_mode.addItem("Hold to remap", "hold")
        self.remap_mode.setCurrentIndex(
            max(0, self.remap_mode.findData(config.remap_mode))
        )
        self.remap_mode.setFixedWidth(FORM_COMPACT_WIDTH)
        shortcut_grid.addWidget(
            self.remap_mode, 0, 2, alignment=Qt.AlignmentFlag.AlignRight
        )

        self.remap_hold_label = QLabel("Hold")
        self.remap_hold_hotkey = HoldShortcutEdit(
            config.remap_hold_hotkey,
            capture_started=self._suspend_start_hotkey,
            capture_finished=self._resume_start_hotkey,
        )
        self.remap_hold_hotkey.setFixedWidth(FORM_HOTKEY_WIDTH)
        shortcut_grid.addWidget(self.remap_hold_label, 1, 0)
        shortcut_grid.addWidget(
            self.remap_hold_hotkey, 1, 2, alignment=Qt.AlignmentFlag.AlignRight
        )
        shortcut_grid.addWidget(QLabel("Start"), 2, 0)
        self.activation_hotkey = HotkeySequenceEdit(
            config.activation_hotkey,
            capture_started=self._suspend_start_hotkey,
            capture_finished=self._resume_start_hotkey,
        )
        self.activation_hotkey.setFixedWidth(FORM_HOTKEY_WIDTH)
        shortcut_grid.addWidget(
            self.activation_hotkey, 2, 2, alignment=Qt.AlignmentFlag.AlignRight
        )
        shortcut_grid.addWidget(QLabel("Stop"), 3, 0)
        self.deactivation_hotkey = HotkeySequenceEdit(
            config.deactivation_hotkey,
            capture_started=self._suspend_start_hotkey,
            capture_finished=self._resume_start_hotkey,
        )
        self.deactivation_hotkey.setFixedWidth(FORM_HOTKEY_WIDTH)
        shortcut_grid.addWidget(
            self.deactivation_hotkey, 3, 2, alignment=Qt.AlignmentFlag.AlignRight
        )

        self.debug_mode = LabeledSwitch(
            "Debug mode",
            "Records stroke traces for analysis. Turn off for potentially reduced latency.",
            checked=config.debug_mode,
        )
        self.minimize_to_tray = LabeledSwitch(
            "Minimize to tray",
            "",
            checked=config.minimize_to_tray,
        )
        shortcut_grid.addWidget(self.debug_mode, 4, 0, 1, 3)
        shortcut_grid.addWidget(self.minimize_to_tray, 5, 0, 1, 3)
        sandbox_row = QHBoxLayout()
        sandbox_row.setContentsMargins(0, 0, 0, 0)
        sandbox_copy = QVBoxLayout()
        sandbox_copy.setSpacing(2)
        sandbox_copy.addWidget(QLabel("Pressure sandbox"))
        sandbox_copy.addWidget(
            _label(
                "Test left and right button pressure in a small game.",
                muted=True,
                wrap=True,
            )
        )
        sandbox_row.addLayout(sandbox_copy, 1)
        self.sandbox_button = QPushButton("Open sandbox")
        self.sandbox_button.setFixedWidth(FORM_COMPACT_WIDTH)
        self.sandbox_button.clicked.connect(self._launch_sandbox)
        sandbox_row.addWidget(self.sandbox_button)
        sandbox_widget = QWidget()
        sandbox_widget.setLayout(sandbox_row)
        shortcut_grid.addWidget(sandbox_widget, 6, 0, 1, 3)
        shortcut_grid.addWidget(QLabel("Injection rate"), 7, 0)
        self.injection_hz = QComboBox()
        for value in (60, 120, 240, 360):
            self.injection_hz.addItem(f"{value} Hz", value)
        desired = round(self.service.launch_config.hz)
        self.injection_hz.setCurrentIndex(max(0, self.injection_hz.findData(desired)))
        self.injection_hz.setFixedWidth(FORM_COMPACT_WIDTH)
        shortcut_grid.addWidget(
            self.injection_hz, 7, 2, alignment=Qt.AlignmentFlag.AlignRight
        )
        app.content.addLayout(shortcut_grid)
        self._update_remap_controls()
        layout.addWidget(app)
        layout.addStretch(1)
        return self._scroll_page(content)

    def _build_analysis_page(self) -> QWidget:
        content, layout = _page_container()
        layout.setSpacing(16)
        card = Card()
        toolbar = QHBoxLayout()
        toolbar.addWidget(_label("Stroke A", muted=True))
        self.stroke_selector = QComboBox()
        self.stroke_selector.setMinimumWidth(240)
        self.stroke_selector.currentIndexChanged.connect(self._load_selected_stroke)
        toolbar.addWidget(self.stroke_selector, 1)
        toolbar.addWidget(_label("Compare with", muted=True))
        self.compare_selector = QComboBox()
        self.compare_selector.setMinimumWidth(240)
        self.compare_selector.currentIndexChanged.connect(self._load_selected_stroke)
        toolbar.addWidget(self.compare_selector, 1)
        toolbar.addWidget(_label("Graph", muted=True))
        self.analysis_graph_mode = QComboBox()
        self.analysis_graph_mode.addItem("Pressure", "pressure")
        self.analysis_graph_mode.addItem("Motion latency", "latency")
        self.analysis_graph_mode.addItem("Output cadence", "cadence")
        toolbar.addWidget(self.analysis_graph_mode)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(lambda: self._refresh_strokes(select_latest=False))
        toolbar.addWidget(refresh)
        card.content.addLayout(toolbar)
        self.stroke_summary = _label(
            "Draw with Debug mode enabled, then select a stroke.", muted=True, wrap=True
        )
        card.content.addWidget(self.stroke_summary)
        self.stroke_graph = StrokeGraph()
        self.analysis_graph_mode.currentIndexChanged.connect(
            lambda _index: self.stroke_graph.set_mode(
                str(self.analysis_graph_mode.currentData())
            )
        )
        card.content.addWidget(self.stroke_graph, 1)
        metrics = QGridLayout()
        metric_specs = (
            ("Stroke onset", "onset_metric"),
            ("Motion → output", "motion_output_metric"),
            ("Relay delivery", "delivery_metric"),
            ("Output jitter", "jitter_metric"),
            ("Release", "release_metric"),
        )
        for index, (label, attribute) in enumerate(metric_specs):
            metric, value = metric_card(label, "—")
            setattr(self, attribute, value)
            metrics.addWidget(metric, index // 3, index % 3)
        card.content.addLayout(metrics)
        layout.addWidget(card, 1)
        return content

    def _build_logs_page(self) -> QWidget:
        content, layout = _page_container()
        layout.setSpacing(16)
        card = Card()
        toolbar = QHBoxLayout()
        toolbar.addWidget(_label("Session output", muted=True))
        toolbar.addStretch(1)
        clear = QPushButton("Clear")
        clear.clicked.connect(lambda: self.terminal.clear())
        toolbar.addWidget(clear)
        card.content.addLayout(toolbar)
        self.terminal = QPlainTextEdit()
        self.terminal.setReadOnly(True)
        self.terminal.document().setMaximumBlockCount(2500)
        card.content.addWidget(self.terminal, 1)
        layout.addWidget(card, 1)
        return content

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(QIcon(str(asset_path("lucide_mouse.png"))), self)
        self.tray.setToolTip("Mouse Pressure")
        menu = QMenu()
        show_action = QAction("Show", self)
        show_action.triggered.connect(self._restore_window)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit_application)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: (
                self._restore_window()
                if reason == QSystemTrayIcon.ActivationReason.DoubleClick
                else None
            )
        )

    # ---------- theme and navigation ----------
    def apply_theme(self, name: str) -> None:
        self.theme_name = name
        self.theme = theme_for(name)
        QApplication.instance().setStyleSheet(stylesheet(self.theme))
        self._qt_settings.setValue("theme", name)
        self.theme_selector.blockSignals(True)
        self.theme_selector.setCurrentIndex(max(0, self.theme_selector.findData(name)))
        self.theme_selector.blockSignals(False)
        switches = [
            self.debug_mode,
            self.minimize_to_tray,
        ]
        for editor in self.editors.values():
            switches.extend((editor.enabled, editor.suppress))
        for widget in switches:
            widget.set_theme(self.theme)
        self.mapping_graph.set_theme(self.theme)
        self.stroke_graph.set_theme(self.theme)

    def _select_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.page_title.setText(("Pressure", "Mouse", "Stroke analysis", "Logs")[index])
        if index == 2:
            self._refresh_strokes(select_latest=False)

    def _set_debug_navigation(self, enabled: bool) -> None:
        for index in (2, 3):
            self.nav_buttons[index].setVisible(enabled)
        if not enabled and self.pages.currentIndex() in {2, 3}:
            self.nav_buttons[1].setChecked(True)
            self._select_page(1)

    # ---------- settings ----------
    def _connect_mapping_controls(self) -> None:
        for name, editor in self.editors.items():
            for signal in (
                editor.raw_min.valueChanged,
                editor.raw_max.valueChanged,
                editor.output_target.currentIndexChanged,
                editor.curve.currentIndexChanged,
                editor.curve_strength.valueChanged,
                editor.deadzone.valueChanged,
                editor.pressure_floor.valueChanged,
                editor.path_stabilization.valueChanged,
                editor.pressure_influence.valueChanged,
            ):
                signal.connect(
                    lambda *_args, channel=name: self._mapping_control_changed(channel)
                )
            for fields in editor.output_range_fields.values():
                for field in fields:
                    field.valueChanged.connect(
                        lambda *_args, channel=name: self._mapping_control_changed(
                            channel
                        )
                    )
            editor.suppress.toggled.connect(
                lambda *_args, channel=name: self._mapping_control_changed(channel)
            )

    def _connect_non_mapping_controls(self) -> None:
        for signal in (
            self.dpi.valueChanged,
            self.haptics["left"].valueChanged,
            self.haptics["right"].valueChanged,
            self.debug_mode.toggled,
            self.minimize_to_tray.toggled,
            self.injection_hz.currentIndexChanged,
            self.remap_mode.currentIndexChanged,
        ):
            signal.connect(self._mark_dirty)
        self.remap_mode.currentIndexChanged.connect(self._update_remap_controls)
        for channel in ("left", "right"):
            self.actuation[channel].valueChanged.connect(
                lambda *_args, name=channel: self._mapping_control_changed(name)
            )
        self.remap_hold_hotkey.bindingChanged.connect(self._mark_dirty)
        self.activation_hotkey.keySequenceChanged.connect(self._mark_dirty)
        self.deactivation_hotkey.keySequenceChanged.connect(self._mark_dirty)
        self.debug_mode.toggled.connect(self._set_debug_navigation)

    def _mapping_control_changed(self, channel: str) -> None:
        if self._loading:
            return
        self._mark_dirty()
        self._redraw_mapping()

    def _update_remap_controls(self, *_args: Any) -> None:
        hold_mode = self.remap_mode.currentData() == "hold"
        self.remap_hold_label.setVisible(hold_mode)
        self.remap_hold_hotkey.setVisible(hold_mode)

    def _pressure_options_changed(self, *_args: Any, mark_dirty: bool = True) -> None:
        self._update_channel_tabs()
        if mark_dirty:
            self._mark_dirty()
        self._redraw_mapping()

    def _channel_selected(self, index: int) -> None:
        self.channel_buttons[index].setChecked(True)
        self._update_channel_tabs()
        self._redraw_mapping()

    def _update_channel_tabs(self) -> None:
        linked = self.linked.isChecked()
        states = (
            self.left_enabled.isChecked(),
            self.right_enabled.isChecked(),
        )
        for index, (name, enabled) in enumerate(
            zip(("Left click", "Right click"), states)
        ):
            state = "On" if enabled else "Off"
            suffix = f" · {state} · Linked" if linked else f" · {state}"
            self.channel_buttons[index].setText(name + suffix)
        self.editors["left"].settings_content.setVisible(states[0])
        self.editors["left"].linked_notice.setVisible(False)
        self.editors["right"].settings_content.setVisible(states[1] and not linked)
        self.editors["right"].linked_notice.setVisible(states[1] and linked)
        self.channel_tabs.updateGeometry()
        current = self.channel_tabs.currentWidget()
        if current is not None:
            current.updateGeometry()
        QTimer.singleShot(0, self._resize_editor_card)

    def _resize_editor_card(self) -> None:
        current = self.channel_tabs.currentWidget()
        if current is None:
            return
        current_height = max(1, current.sizeHint().height())
        self.channel_tabs.setFixedHeight(current_height)
        margins = self.editor_card.content.contentsMargins()
        card_height = current_height + margins.top() + margins.bottom()
        self.editor_card.setFixedHeight(card_height)
        self.editor_card.updateGeometry()

    def _mark_dirty(self, *_args: Any) -> None:
        if self._loading:
            return
        self.settings_dirty = True
        self.save_button.setText("Apply changes")
        self.save_button.setEnabled(not self.busy)

    def _channel_config(self, channel: str) -> ChannelConfig:
        editor = self.editors[channel]
        deadzone = int(editor.deadzone.value())
        return ChannelConfig(
            output_target=str(editor.output_target.currentData()),
            sensitivity_light=int(editor.sensitivity_light.value()),
            sensitivity_firm=int(editor.sensitivity_firm.value()),
            x_tilt_light=int(editor.output_range_fields["x_tilt"][0].value()),
            x_tilt_firm=int(editor.output_range_fields["x_tilt"][1].value()),
            y_tilt_light=int(editor.output_range_fields["y_tilt"][0].value()),
            y_tilt_firm=int(editor.output_range_fields["y_tilt"][1].value()),
            rotation_light=int(editor.output_range_fields["rotation"][0].value()),
            rotation_firm=int(editor.output_range_fields["rotation"][1].value()),
            raw_min=int(editor.raw_min.value()),
            raw_max=int(editor.raw_max.value()),
            deadzone_low=deadzone,
            deadzone_high=deadzone,
            curve=str(editor.curve.currentData()),
            curve_strength=float(editor.curve_strength_value()),
            actuation_level=int(self.actuation[channel].value()),
            pressure_floor=int(editor.pressure_floor.value()),
            path_stabilization=int(editor.path_stabilization.value()),
            pressure_influence=int(editor.pressure_influence.value()),
        )

    def _settings_draft(self) -> SettingsDraft:
        current = self.service.get_config()
        config = RuntimeConfig(
            schema_version=current.schema_version,
            linked=self.linked.isChecked(),
            left_enabled=self.left_enabled.isChecked(),
            right_enabled=self.right_enabled.isChecked(),
            suppress_lmb=self.editors["left"].suppress.isChecked(),
            suppress_rmb=self.editors["right"].suppress.isChecked(),
            debug_mode=self.debug_mode.isChecked(),
            minimize_to_tray=self.minimize_to_tray.isChecked(),
            session_dpi=self.dpi.value(),
            session_haptic_left=self.haptics["left"].value(),
            session_haptic_right=self.haptics["right"].value(),
            session_device_settings_follow_normal=(
                current.session_device_settings_follow_normal
            ),
            remap_mode=str(self.remap_mode.currentData()),
            remap_hold_hotkey=self.remap_hold_hotkey.binding(),
            activation_hotkey=self._hotkey_text(self.activation_hotkey),
            deactivation_hotkey=self._hotkey_text(self.deactivation_hotkey),
            left=self._channel_config("left"),
            right=self._channel_config("right"),
        )
        return SettingsDraft(
            config=config,
            injection_hz=float(self.injection_hz.currentData()),
            normal_device=self._normal_device,
        )

    @staticmethod
    def _hotkey_text(editor: QKeySequenceEdit) -> str:
        return editor.keySequence().toString(QKeySequence.SequenceFormat.PortableText)

    def _device_settings(self) -> dict[str, int]:
        return {
            "dpi": self.dpi.value(),
            "haptic_left": self.haptics["left"].value(),
            "haptic_right": self.haptics["right"].value(),
            "actuation_left": self.actuation["left"].value(),
            "actuation_right": self.actuation["right"].value(),
        }

    def _apply_settings(self) -> bool:
        previous_hotkey = self.start_hotkey.hotkey.label
        try:
            draft = self._settings_draft()
            draft.validate()
            if not self._replace_start_hotkey(draft.config.activation_hotkey):
                return False
            self.service.apply_config(draft.runtime_patch())
            self.service.launch_config.hz = draft.injection_hz
            self.service.launch_config.backend = "native_synthetic"
        except Exception as exc:
            self._replace_start_hotkey(previous_hotkey)
            self.write_system(f"Settings error: {exc}", level="ERROR")
            if self.debug_mode.isChecked():
                self._select_page(3)
                self.nav_buttons[3].setChecked(True)
            return False
        self.settings_dirty = False
        self.save_button.setText("Applied")
        self.save_button.setEnabled(False)
        QTimer.singleShot(
            1200,
            lambda: (
                self.save_button.setText("Apply changes")
                if not self.settings_dirty
                else None
            ),
        )
        self.write_system("Settings saved.")
        return True

    def _replace_start_hotkey(self, binding: str) -> bool:
        requested = parse_global_hotkey(binding)
        if requested.label == self.start_hotkey.hotkey.label:
            return True
        if self._hotkey_capture_active:
            self.start_hotkey = StartHotkeyListener(
                lambda: self.events.put(("start_hotkey", None)),
                requested.label,
            )
            return True
        candidate = StartHotkeyListener(
            lambda: self.events.put(("start_hotkey", None)),
            requested.label,
        )
        if not candidate.start():
            candidate.close()
            self.write_system(
                f"Shortcut {requested.label} is already in use.",
                level="ERROR",
            )
            return False
        previous = self.start_hotkey
        self.start_hotkey = candidate
        previous.close()
        return True

    def _suspend_start_hotkey(self) -> None:
        if self._hotkey_capture_active:
            return
        self._hotkey_capture_active = True
        self.start_hotkey.close()

    def _resume_start_hotkey(self) -> None:
        if not self._hotkey_capture_active:
            return
        self._hotkey_capture_active = False
        binding = self.start_hotkey.hotkey.label
        listener = StartHotkeyListener(
            lambda: self.events.put(("start_hotkey", None)),
            binding,
        )
        self.start_hotkey = listener
        if not listener.start():
            self.write_system(
                f"{binding} could not be registered.",
                level="WARN",
            )

    def _save_or_apply(self) -> None:
        if self.busy or not self.settings_dirty or not self._apply_settings():
            return
        if self.running:
            self.save_button.setText("Applying…")
            self.save_button.setEnabled(False)
            device = self._device_settings()
            self._watch_future(
                "device_settings_applied",
                self.controller.apply_device_settings(**device),
            )

    def _reset_channel_settings(self, channel: str) -> None:
        channel_label = f"{channel}-click"
        dialog = ConfirmationDialog(
            self,
            title=f"Reset {channel_label} settings?",
            message=(
                f"Restore the recommended pressure settings for {channel_label} only."
            ),
            confirm_text="Reset settings",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        reset = self._settings_draft().reset_channel(channel).config
        source = reset.left if channel == "left" else reset.right
        editor = self.editors[channel]
        self._loading = True
        try:
            editor.raw_min.setValue(source.raw_min)
            editor.raw_max.setValue(source.raw_max)
            editor.output_target.setCurrentIndex(
                max(0, editor.output_target.findData(source.output_target))
            )
            editor.sensitivity_light.setValue(source.sensitivity_light)
            editor.sensitivity_firm.setValue(source.sensitivity_firm)
            for target in ("x_tilt", "y_tilt", "rotation"):
                fields = editor.output_range_fields[target]
                fields[0].setValue(getattr(source, f"{target}_light"))
                fields[1].setValue(getattr(source, f"{target}_firm"))
            editor._update_output_target_controls()
            editor.curve.setCurrentIndex(editor.curve.findData(source.curve))
            editor.curve_strength.setValue(round(source.curve_strength * 10))
            editor.deadzone.setValue(source.deadzone_low)
            editor.pressure_floor.setValue(source.pressure_floor)
            editor.path_stabilization.setValue(source.path_stabilization)
            editor.pressure_influence.setValue(source.pressure_influence)
            editor.suppress.setChecked(
                reset.suppress_lmb if channel == "left" else reset.suppress_rmb
            )
        finally:
            self._loading = False
        self._mark_dirty()
        self._redraw_mapping()
        self.write_system(
            f"{channel_label.title()} settings reset. Apply changes to save them."
        )

    def _load_config(self, config: RuntimeConfig) -> None:
        self._loading = True
        try:
            self.left_enabled.setChecked(config.left_enabled)
            self.right_enabled.setChecked(config.right_enabled)
            self.linked.setChecked(config.linked)
            for channel, ch in (("left", config.left), ("right", config.right)):
                editor = self.editors[channel]
                editor.raw_min.setValue(ch.raw_min)
                editor.raw_max.setValue(ch.raw_max)
                editor.output_target.setCurrentIndex(
                    max(0, editor.output_target.findData(ch.output_target))
                )
                editor.sensitivity_light.setValue(ch.sensitivity_light)
                editor.sensitivity_firm.setValue(ch.sensitivity_firm)
                for target in ("x_tilt", "y_tilt", "rotation"):
                    fields = editor.output_range_fields[target]
                    fields[0].setValue(getattr(ch, f"{target}_light"))
                    fields[1].setValue(getattr(ch, f"{target}_firm"))
                editor._update_output_target_controls()
                editor.curve.setCurrentIndex(max(0, editor.curve.findData(ch.curve)))
                editor.curve_strength.setValue(round(ch.curve_strength * 10))
                self.actuation[channel].setValue(ch.actuation_level)
                editor.deadzone.setValue(ch.deadzone_low)
                editor.pressure_floor.setValue(ch.pressure_floor)
                editor.path_stabilization.setValue(ch.path_stabilization)
                editor.pressure_influence.setValue(ch.pressure_influence)
            self.editors["left"].suppress.setChecked(config.suppress_lmb)
            self.editors["right"].suppress.setChecked(config.suppress_rmb)
            self.debug_mode.setChecked(config.debug_mode)
            self.minimize_to_tray.setChecked(config.minimize_to_tray)
            self.remap_mode.setCurrentIndex(
                max(0, self.remap_mode.findData(config.remap_mode))
            )
            self.remap_hold_hotkey.set_binding(config.remap_hold_hotkey)
            self._update_remap_controls()
            self.activation_hotkey.setKeySequence(
                QKeySequence(config.activation_hotkey)
            )
            self.deactivation_hotkey.setKeySequence(
                QKeySequence(config.deactivation_hotkey)
            )
            self.dpi.setValue(config.session_dpi)
            self.haptics["left"].setValue(config.session_haptic_left)
            self.haptics["right"].setValue(config.session_haptic_right)
        finally:
            self._loading = False
        self._pressure_options_changed(mark_dirty=False)

    def _begin_calibration(self, channel: str) -> None:
        if self.busy or self.calibrating or not self._apply_settings():
            return
        self.calibrating = True
        self._set_status("Calibrating…", "busy")
        self.start_button.setEnabled(False)
        self.save_button.setEnabled(False)
        for editor in self.editors.values():
            editor.calibrate_button.setEnabled(False)

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Calibrate {channel}-click pressure")
        dialog.setModal(True)
        dialog.setMinimumWidth(430)
        dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        dialog_layout = QVBoxLayout(dialog)
        dialog_layout.setContentsMargins(24, 22, 24, 22)
        dialog_layout.setSpacing(12)
        title = QLabel(f"Calibrate {channel}-click pressure")
        title.setObjectName("sectionTitle")
        dialog_layout.addWidget(title)
        self.calibration_instruction = _label(
            "Release the button and get ready.", wrap=True
        )
        dialog_layout.addWidget(self.calibration_instruction)
        self.calibration_step = _label("Preparing…", muted=True)
        dialog_layout.addWidget(self.calibration_step)
        self.calibration_value = _label("Live pressure: —", muted=True)
        dialog_layout.addWidget(self.calibration_value)
        dialog_layout.addWidget(
            _label(
                "The calibration advances automatically after each countdown.",
                muted=True,
                wrap=True,
            )
        )
        self.calibration_dialog = dialog
        dialog.show()

        future = self.controller.calibrate(
            channel,
            config_store=self.config_store,
            progress_cb=lambda payload: self.events.put(
                ("calibration_progress", payload)
            ),
        )
        self._watch_future("calibration_complete", future)

    def _handle_calibration_progress(self, payload: dict[str, Any]) -> None:
        if self.calibration_dialog is None:
            return
        phase = str(payload.get("phase", "prepare"))
        instruction = str(payload.get("instruction", ""))
        if phase == "countdown":
            remaining = int(payload.get("countdown", 0))
            next_phase = str(payload.get("next_phase", "next step")).title()
            self.calibration_instruction.setText(instruction)
            self.calibration_step.setText(f"{next_phase} starts in {remaining}…")
        else:
            phase_labels = {
                "prepare": "Get ready",
                "idle": "Release",
                "light": "Light press",
                "heavy": "Firm press",
                "done": "Complete",
            }
            self.calibration_instruction.setText(instruction)
            self.calibration_step.setText(phase_labels.get(phase, phase.title()))
        value = int(payload.get("value", 0))
        self.calibration_value.setText(f"Live pressure: {value}")

    def _finish_calibration(
        self,
        result: dict[str, dict[str, int]] | None,
        *,
        error: Exception | None = None,
    ) -> None:
        if result:
            self._loading = True
            try:
                for channel, values in result.items():
                    editor = self.editors[channel]
                    editor.raw_min.setValue(int(values["raw_min"]))
                    editor.raw_max.setValue(int(values["raw_max"]))
            finally:
                self._loading = False
            self._mark_dirty()
            self._apply_settings()
            self._redraw_mapping()
            self.write_system("Pressure calibration saved.")
        elif error is not None:
            self.write_system(f"Calibration failed: {error}", level="ERROR")
        if self.calibration_dialog is not None:
            self.calibration_dialog.accept()
            self.calibration_dialog = None
        self.calibrating = False
        for editor in self.editors.values():
            editor.calibrate_button.setEnabled(True)
        self.start_button.setEnabled(not self.detecting)
        self.save_button.setEnabled(self.settings_dirty)
        self._set_status(
            "Running" if self.running else "Stopped",
            "running" if self.running else "stopped",
        )

    def _launch_sandbox(self) -> None:
        executable = Path(sys.executable).resolve()
        candidates = (
            executable.parent / "sandbox" / "MousePressureSandbox.exe",
            Path(__file__).resolve().parents[2]
            / "dist"
            / "windows"
            / "MousePressureSandbox"
            / "MousePressureSandbox.exe",
        )
        try:
            for candidate in candidates:
                if candidate.is_file():
                    subprocess.Popen([str(candidate)])
                    self.write_system(f"Opened pressure sandbox: {candidate}")
                    return
            if not getattr(sys, "frozen", False):
                subprocess.Popen([sys.executable, "-m", "mouse_pressure_sandbox.main"])
                self.write_system("Opened pressure sandbox from source")
                return
        except OSError as exc:
            self.write_system(f"Could not open pressure sandbox: {exc}", level="ERROR")
            return
        self.write_system(
            "The pressure sandbox is not installed. Re-run the Mouse Pressure installer.",
            level="ERROR",
        )

    # ---------- mapping and analysis ----------
    def _visible_mapping_channels(self) -> tuple[str, ...]:
        if self.linked.isChecked():
            return ("left",)
        selected = "left" if self.channel_tabs.currentIndex() == 0 else "right"
        return (selected,)

    @staticmethod
    def _mapped_output_value(settings: ChannelConfig, pressure: int) -> int:
        target = settings.output_target
        if target == "pressure":
            return max(0, min(1024, int(pressure)))
        fraction = max(0.0, min(1.0, int(pressure) / 1024.0))
        if target == "mouse_sensitivity":
            light, firm = settings.sensitivity_light, settings.sensitivity_firm
        elif target == "x_tilt":
            light, firm = settings.x_tilt_light, settings.x_tilt_firm
        elif target == "y_tilt":
            light, firm = settings.y_tilt_light, settings.y_tilt_firm
        elif target == "rotation":
            light, firm = settings.rotation_light, settings.rotation_firm
        else:
            return 0
        return round(light + (firm - light) * fraction)

    def _configure_mapping_axis(self, output_target: str) -> None:
        if output_target == "mouse_sensitivity":
            self.mapping_graph.set_y_axis(
                0, 200, minimum_label="0%", maximum_label="200%"
            )
        elif output_target in {"x_tilt", "y_tilt"}:
            self.mapping_graph.set_y_axis(
                -60, 60, minimum_label="-60°", maximum_label="60°"
            )
        elif output_target == "rotation":
            self.mapping_graph.set_y_axis(
                0, 359, minimum_label="0°", maximum_label="359°"
            )
        else:
            self.mapping_graph.set_y_axis(
                0, 1024, minimum_label="0%", maximum_label="100%"
            )

    def _redraw_mapping(self) -> None:
        selected = "left" if self.channel_tabs.currentIndex() == 0 else "right"
        editor = self.editors[selected]
        output_target = str(editor.output_target.currentData())
        output_label = {
            "mouse_sensitivity": "Output Sensitivity",
            "x_tilt": "Output X-tilt",
            "y_tilt": "Output Y-tilt",
            "rotation": "Output Rotation",
        }.get(output_target, "Output Pressure")
        self.graph_title.setText(output_label)
        if isinstance(self.output_metric_caption, QLabel):
            self.output_metric_caption.setText(output_label)
        self._configure_mapping_axis(output_target)
        series: dict[str, list[tuple[int, int]]] = {}
        raw_ranges: dict[str, tuple[int, int]] = {}
        try:
            draft = self._settings_draft()
        except Exception:
            return
        for channel in ("left", "right"):
            settings = draft.effective_channel(channel)
            if settings.output_target == "pressure":
                pressure_points = draft.mapping_points(
                    channel,
                    raw_start=MappingGraph.RAW_MIN,
                    raw_end=MappingGraph.RAW_MAX,
                    step=4,
                )
            else:
                pressure_points = [
                    (raw, draft.mapped_pressure(channel, raw))
                    for raw in range(
                        MappingGraph.RAW_MIN,
                        MappingGraph.RAW_MAX + 1,
                        4,
                    )
                ]
            series[channel] = [
                (raw, self._mapped_output_value(settings, pressure))
                for raw, pressure in pressure_points
            ]
            raw_ranges[channel] = (settings.raw_min, settings.raw_max)
        self.mapping_graph.set_data(
            series,
            raw_ranges,
            channels=self._visible_mapping_channels(),
        )
        self.mapping_graph.set_actuation_thresholds(
            {
                channel: actuation_raw_estimate(
                    channel,
                    self.actuation[channel].value(),
                )
                for channel in ("left", "right")
            }
        )

    def _trace_directory(self) -> Path:
        configured = self.service.launch_config.trace_dir
        return (
            Path(configured)
            if configured
            else self.config_store.config_dir / "stroke_traces"
        )

    def _refresh_strokes(self, *, select_latest: bool) -> None:
        directory = self._trace_directory()
        paths = sorted(
            directory.glob("stroke-*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:100]
        current = self.stroke_selector.currentText()
        comparison = self.compare_selector.currentText()
        self._trace_paths = {path.name: path for path in paths}
        self.stroke_selector.blockSignals(True)
        self.compare_selector.blockSignals(True)
        self.stroke_selector.clear()
        self.stroke_selector.addItems(self._trace_paths)
        self.compare_selector.clear()
        self.compare_selector.addItem("No comparison")
        self.compare_selector.addItems(self._trace_paths)
        if paths:
            target = (
                paths[0].name
                if select_latest or current not in self._trace_paths
                else current
            )
            self.stroke_selector.setCurrentText(target)
            if comparison in self._trace_paths:
                self.compare_selector.setCurrentText(comparison)
        self.stroke_selector.blockSignals(False)
        self.compare_selector.blockSignals(False)
        self._load_selected_stroke()

    def _load_selected_stroke(self, *_args: Any) -> None:
        path = self._trace_paths.get(self.stroke_selector.currentText())
        if path is None:
            self.stroke_graph.set_analysis(None)
            return
        try:
            analysis = stroke_analysis_data(
                json.loads(path.read_text(encoding="utf-8"))
            )
            comparison_path = self._trace_paths.get(self.compare_selector.currentText())
            comparison = (
                stroke_analysis_data(
                    json.loads(comparison_path.read_text(encoding="utf-8"))
                )
                if comparison_path is not None and comparison_path != path
                else None
            )
        except Exception as exc:
            self.stroke_summary.setText(f"Could not read trace: {exc}")
            self.stroke_graph.set_analysis(None)
            return
        analyses = [("A", analysis)]
        if comparison is not None:
            analyses.append(("B", comparison))
        self.stroke_graph.set_comparison(analyses)
        if comparison is None:
            self.stroke_summary.setText(
                f"{analysis['stationary_dab_points']} stationary points removed"
            )
        else:
            self.stroke_summary.setText("A / B · lower timing values are better")

        def timing(value: float | None) -> str:
            return "—" if value is None else f"{value:.2f} ms"

        def compared(key: str, formatter: Callable[[Any], str] = timing) -> str:
            first = formatter(analysis.get(key))
            if comparison is None:
                return first
            return f"{first} / {formatter(comparison.get(key))}"

        self.onset_metric.setText(compared("onset_ms"))
        self.motion_output_metric.setText(compared("motion_to_output_median_ms"))
        self.delivery_metric.setText(compared("delivery_latency_median_ms"))
        self.jitter_metric.setText(compared("delivery_jitter_ms"))
        self.release_metric.setText(compared("release_ms"))

    # ---------- runtime lifecycle ----------
    def _toggle_bridge(self) -> None:
        if self.busy or self.calibrating:
            return
        if self.running:
            self._begin_stop()
        elif self._apply_settings():
            self._begin_start()

    def _begin_start(self) -> None:
        self.busy = True
        self._set_status("Starting…", "busy")
        self.start_button.setEnabled(False)
        self._watch_future(
            "started", self.controller.start(device_settings=self._device_settings())
        )

    def _begin_stop(self) -> None:
        self.busy = True
        self._set_status("Stopping…", "busy")
        self.start_button.setEnabled(False)
        self._watch_future("stopped", self.controller.stop())

    def _begin_device_detection(self) -> None:
        self.detecting = True
        self.start_button.setEnabled(False)
        self._watch_future(
            "device_settings_detected", self.controller.detect_device_settings()
        )

    def _watch_future(self, name: str, future: Future[Any]) -> None:
        def done(completed: Future[Any]) -> None:
            try:
                result = completed.result()
            except Exception as exc:
                self.events.put((f"{name}_error", exc))
            else:
                self.events.put((name, result))

        future.add_done_callback(done)

    def _set_running(self, running: bool) -> None:
        self.running = running
        self.busy = False
        self.start_button.setText("Stop" if running else "Start")
        self.start_button.setToolTip(
            f"Stop pressure output ({self._hotkey_text(self.deactivation_hotkey)})"
            if running
            else "Apply the visible settings and start pressure output "
            f"({self._hotkey_text(self.activation_hotkey)})"
        )
        self.start_button.setEnabled(not self.detecting)
        self.save_button.setText("Apply changes")
        self.save_button.setEnabled(self.settings_dirty)
        for editor in self.editors.values():
            editor.calibrate_button.setEnabled(not self.calibrating)
        self.injection_hz.setEnabled(not running)
        self.remap_hold_hotkey.setEnabled(not running)
        self.activation_hotkey.setEnabled(not running)
        self.deactivation_hotkey.setEnabled(not running)
        self.mapping_graph.set_live_preview(running)
        self._set_status(
            "Running" if running else "Stopped",
            "running" if running else "stopped",
        )

    def _set_status(self, text: str, state: str) -> None:
        object_name = {
            "running": "statusRunning",
            "busy": "statusBusy",
            "error": "statusError",
        }.get(state, "statusStopped")
        self.status_label.setObjectName(object_name)
        self.status_label.setText(text)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _set_device_connected(self, connected: bool) -> None:
        self.sidebar_backend.setText("Connected" if connected else "Disconnected")
        self.sidebar_backend.setToolTip("Native Windows Ink output")
        self.sidebar_backend_dot.setObjectName(
            "connectionDotConnected" if connected else "connectionDotDisconnected"
        )
        self.sidebar_backend_dot.style().unpolish(self.sidebar_backend_dot)
        self.sidebar_backend_dot.style().polish(self.sidebar_backend_dot)

    def _drain_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._write_log(payload)
            elif kind == "telemetry":
                self._handle_telemetry(payload)
            elif kind == "start_hotkey":
                if (
                    not self.running
                    and not self.busy
                    and not self.detecting
                    and not self.calibrating
                ):
                    self._toggle_bridge()
            elif kind == "started":
                self._set_running(True)
            elif kind == "stopped":
                self._set_running(False)
            elif kind == "device_settings_detected":
                self._handle_device_detected(payload)
            elif kind == "device_settings_applied":
                self.save_button.setText("Applied")
                self.save_button.setEnabled(False)
                self.write_system(
                    f"Mouse settings applied: {payload['dpi']} DPI, "
                    f"haptics L{payload['haptic_left']}/R{payload['haptic_right']}, "
                    f"actuation L{payload['actuation_left']}/R{payload['actuation_right']}."
                )
            elif kind == "calibration_progress":
                self._handle_calibration_progress(payload)
            elif kind == "calibration_complete":
                self._finish_calibration(payload)
            elif kind == "calibration_complete_error":
                self._finish_calibration(None, error=payload)
            elif kind in {"runtime_error", "force_stopped"}:
                self._set_running(False)
                self._set_status(
                    "Driver stopped", "error" if kind == "runtime_error" else "stopped"
                )
                self.write_system(
                    str(payload), level="ERROR" if kind == "runtime_error" else "WARN"
                )
            elif kind.endswith("_error"):
                if kind == "device_settings_detected_error":
                    self.detecting = False
                    self.start_button.setEnabled(True)
                    self._set_device_connected(False)
                    self._set_status("Mouse not detected", "error")
                    self.write_system(
                        f"Could not detect mouse settings: {payload}", level="WARN"
                    )
                else:
                    self._set_running(self.service.stream_active)
                    self._set_status("Start failed", "error")
                    self.write_system(f"Driver error: {payload}", level="ERROR")

    def _handle_device_detected(self, payload: dict[str, int]) -> None:
        self.detecting = False
        MainWindow._set_device_connected(self, True)
        self._normal_device = SessionDeviceSettings.from_mapping(payload)
        self.normal_dpi.setText(str(payload["dpi"]))
        self.normal_haptics["left"].setText(str(payload["haptic_left"]))
        self.normal_haptics["right"].setText(str(payload["haptic_right"]))
        self.normal_actuation["left"].setText(str(payload["actuation_left"]))
        self.normal_actuation["right"].setText(str(payload["actuation_right"]))
        config = self.service.get_config()
        if config.session_device_settings_follow_normal:
            self.dpi.setValue(payload["dpi"])
            self.haptics["left"].setValue(payload["haptic_left"])
            self.haptics["right"].setValue(payload["haptic_right"])
            self.actuation["left"].setValue(payload["actuation_left"])
            self.actuation["right"].setValue(payload["actuation_right"])
        self.start_button.setEnabled(True)
        self._set_status("Stopped", "stopped")
        self.write_system(
            f"Detected mouse: {payload['dpi']} DPI, "
            f"haptics L{payload['haptic_left']}/R{payload['haptic_right']}, "
            f"actuation L{payload['actuation_left']}/R{payload['actuation_right']}."
        )

    def _handle_telemetry(self, payload: dict[str, Any]) -> None:
        effective_by_channel: dict[str, int] = {}
        try:
            draft = self._settings_draft()
        except Exception:
            draft = None
        for channel in ("left", "right"):
            raw = int(payload[f"{channel}_raw"])
            mapped = int(payload[f"{channel}_mapped"])
            self._latest_raw[channel] = raw
            self._latest_mapped[channel] = mapped
            try:
                effective = (
                    draft.effective_pressure(channel, raw)
                    if draft is not None
                    else mapped
                )
            except Exception:
                effective = mapped
            effective_by_channel[channel] = effective
            # The plotted curve includes pressure influence and the configured
            # floor, so its live marker must represent that same output stage.
            settings = draft.effective_channel(channel) if draft is not None else None
            output_input = (
                effective
                if settings is None or settings.output_target == "pressure"
                else mapped
            )
            output_value = (
                MainWindow._mapped_output_value(settings, output_input)
                if settings is not None
                else output_input
            )
            self.mapping_graph.set_current(channel, raw, output_value)
        selected = "left" if self.channel_tabs.currentIndex() == 0 else "right"
        raw = self._latest_raw[selected]
        mapped = self._latest_mapped[selected]
        effective = effective_by_channel[selected]
        self.input_metric.setText(f"{mapped / 1024:.0%}")
        output_target = (
            draft.effective_channel(selected).output_target
            if draft is not None
            else "pressure"
        )
        settings = draft.effective_channel(selected) if draft is not None else None
        output_value = (
            MainWindow._mapped_output_value(
                settings,
                effective if output_target == "pressure" else mapped,
            )
            if settings is not None
            else effective
        )
        if output_target == "mouse_sensitivity":
            output_text = f"{output_value}%"
        elif output_target in {"x_tilt", "y_tilt", "rotation"}:
            output_text = f"{output_value}°"
        else:
            output_text = f"{effective / 1024:.0%}"
        self.output_metric.setText(output_text)
        self.raw_metric.setText(str(raw))
        if self.running:
            self._set_status("Running", "running")

    # ---------- logs, tray, shutdown ----------
    def _write_log(self, entry: LogEntry) -> None:
        stamp = dt.datetime.fromtimestamp(entry.ts / 1000).strftime("%H:%M:%S")
        self.terminal.appendPlainText(f"{stamp} {entry.level:<5} {entry.msg}")
        if (
            entry.msg.startswith("TRACE saved ")
            and self.pages.currentWidget() is self.analysis_page
        ):
            self._refresh_strokes(select_latest=True)

    def write_system(self, message: str, *, level: str = "SYSTEM") -> None:
        self.terminal.appendPlainText(f"> {level}: {message}")

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if (
            event.type() == QEvent.Type.WindowStateChange
            and self.isMinimized()
            and self.minimize_to_tray.isChecked()
        ):
            QTimer.singleShot(0, self._hide_to_tray)
        super().changeEvent(event)

    def _hide_to_tray(self) -> None:
        self.tray.show()
        self.hide()

    def _restore_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.tray.hide()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._allow_close:
            event.accept()
            return
        event.ignore()
        self._quit_application()

    def _quit_application(self) -> None:
        if self.closing:
            return
        self.closing = True
        self._set_status("Closing", "busy")
        self.start_button.setEnabled(False)

        def close_runtime() -> None:
            try:
                self.start_hotkey.close()
                self.controller.close()
            except Exception as exc:
                self._close_error = str(exc)
            finally:
                self._close_complete.set()

        def poll_closed() -> None:
            timed_out = time.monotonic() >= self._close_deadline
            if self._close_complete.is_set() or timed_out:
                self._allow_close = True
                self.event_timer.stop()
                self.tray.hide()
                self.close()
                QApplication.quit()
            else:
                QTimer.singleShot(50, poll_closed)

        self._close_deadline = time.monotonic() + 6.0
        threading.Thread(
            target=close_runtime, name="mouse-pressure-qt-close", daemon=True
        ).start()
        QTimer.singleShot(50, poll_closed)


def main() -> int:
    set_windows_app_identity()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Mouse Pressure")
    app.setOrganizationName("Mouse Pressure")
    app.setWindowIcon(QIcon(str(asset_path("lucide_mouse.ico"))))
    app.setQuitOnLastWindowClosed(False)
    try:
        instance_guard = SingleInstanceGuard()
    except OSError as exc:
        print(f"WARN: could not create the single-instance lock: {exc}")
        instance_guard = None
    if instance_guard is not None and not instance_guard.acquired:
        QMessageBox.information(
            None,
            "Mouse Pressure",
            "Mouse Pressure is already running. Check the taskbar or system tray.",
        )
        return 0
    log_bus = LogBus(maxlen=1000)
    try:
        config_store = ConfigStore()
        service = RuntimeService(
            launch_config=LaunchConfig(
                backend="native_synthetic",
                trace_dir=str(config_store.config_dir / "stroke_traces"),
            ),
            config_store=config_store,
            log_bus=log_bus,
        )
        controller = BridgeController(service)
    except Exception as exc:
        print(f"ERROR: could not initialize control panel: {exc}")
        if instance_guard is not None:
            instance_guard.close()
        return 1
    window = MainWindow(service, controller, log_bus, config_store)
    window.show()
    try:
        return int(app.exec())
    finally:
        if instance_guard is not None:
            instance_guard.close()


if __name__ == "__main__":
    raise SystemExit(main())
