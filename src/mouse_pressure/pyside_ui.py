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
from typing import Any

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QSettings, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from mouse_pressure.bridge.config import LaunchConfig, RuntimeConfig
from mouse_pressure.bridge.tablet_emitter import enumerate_vmulti_candidates
from mouse_pressure.dev_ui import (
    BridgeController,
    DevSettings,
    effective_pressure_for_raw,
    parse_dev_settings,
    stroke_analysis_data,
)
from mouse_pressure.ui.qt_theme import Theme, stylesheet, theme_for
from mouse_pressure.ui.qt_widgets import (
    Card,
    LabeledSwitch,
    MappingGraph,
    SliderField,
    StrokeGraph,
    metric_card,
)
from mouse_pressure.ui.windows_shell import (
    SingleInstanceGuard,
    StartHotkeyListener,
    asset_path,
    set_windows_app_identity,
)
from mouse_pressure.web.config_store import ConfigStore
from mouse_pressure.web.log_bus import LogBus, LogEntry
from mouse_pressure.web.runtime_service import RuntimeService


CHANNEL_COLORS = {"left": "#378ADD", "right": "#EF9F27"}


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
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )
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
                            (angle_delta / 120.0)
                            * max(48, bar.pageStep() // 8)
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
        channel_name = "Left" if channel == "left" else "Right"
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        root.setSpacing(14)

        self.enabled = LabeledSwitch(
            f"{channel_name}-click pressure",
            f"Enable pressure output from the {channel.lower()} mouse button.",
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

        raw_values = QGridLayout()
        raw_values.setHorizontalSpacing(12)
        raw_values.setVerticalSpacing(8)
        raw_values.addWidget(_label("Raw activation value", muted=True), 0, 0)
        raw_values.addWidget(_label("Raw full-pressure value", muted=True), 0, 1)
        self.raw_min = QSpinBox()
        self.raw_min.setRange(0, 1022)
        self.raw_min.setValue(config.raw_min)
        self.raw_max = QSpinBox()
        self.raw_max.setRange(1, 1023)
        self.raw_max.setValue(config.raw_max)
        raw_values.addWidget(self.raw_min, 1, 0)
        raw_values.addWidget(self.raw_max, 1, 1)
        settings.addLayout(raw_values)

        settings.addWidget(_label("Calibration", muted=True))
        self.calibrate_button = QPushButton("Calibrate pressure range…")
        settings.addWidget(self.calibrate_button)

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

        settings.addWidget(_label("Press/release behavior", muted=True))
        self.contact = QComboBox()
        for label, value in (
            ("Activates early", "light"),
            ("Balanced", "medium"),
            ("Requires a firmer press", "firm"),
        ):
            self.contact.addItem(label, value)
        self.contact.setCurrentIndex(max(0, self.contact.findData(config.contact_preset)))
        settings.addWidget(self.contact)

        self.suppress = LabeledSwitch(
            "Block the normal mouse click",
        )
        settings.addWidget(self.suppress)

        self.advanced_button = QToolButton()
        self.advanced_button.setText("Advanced settings")
        self.advanced_button.setCheckable(True)
        self.advanced_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_button.setArrowType(Qt.ArrowType.RightArrow)
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
        self.immediate_button_wake = LabeledSwitch(
            "Immediate stroke start (experimental)",
            "Wake output as soon as the exact button-down position is known. Removes up to about 17 ms of intermittent delay; a Raw-Input-first press may wait up to 4 ms for its position.",
            checked=config.immediate_button_wake,
        )
        self.clean_stroke_endings = LabeledSwitch(
            "Clean stroke endings",
            "Holds pressure decreases for 25 ms so a release can end before a thin tail is emitted. Cursor movement and pressure increases remain immediate.",
            checked=config.clean_stroke_endings,
        )
        for widget in (
            self.deadzone,
            self.pressure_floor,
            self.path_stabilization,
            self.pressure_influence,
            self.immediate_button_wake,
            self.clean_stroke_endings,
        ):
            advanced.addWidget(widget)
        self.xtilt: LabeledSwitch | None = None
        if channel == "right":
            self.xtilt = LabeledSwitch(
                "Use right pressure as X-Tilt",
                "While drawing with left pressure, map right pressure to 0–60° X-Tilt.",
            )
            advanced.addWidget(self.xtilt)
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
        self.advanced_button.setArrowType(
            Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow
        )
        self.advanced.updateGeometry()
        self.updateGeometry()

    def curve_strength_value(self) -> float:
        if self.curve.currentData() == "linear":
            return 1.0
        return self.curve_strength.value() / 10.0

    def control_widgets(self) -> list[QWidget]:
        widgets: list[QWidget] = [
            self.enabled,
            self.raw_min,
            self.raw_max,
            self.curve,
            self.curve_strength,
            self.contact,
            self.suppress,
            self.deadzone,
            self.pressure_floor,
            self.path_stabilization,
            self.pressure_influence,
            self.immediate_button_wake,
            self.clean_stroke_endings,
        ]
        if self.xtilt is not None:
            widgets.append(self.xtilt)
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
        self._normal_device = {"dpi": None, "haptic_left": None, "haptic_right": None}
        self._trace_paths: dict[str, Path] = {}
        self._qt_settings = QSettings("Mouse Pressure", "Mouse Pressure")
        self.theme_name = str(self._qt_settings.value("theme", "light"))
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
        service.set_telemetry_callback(lambda sample: self.events.put(("telemetry", sample)))
        service.set_failure_callback(lambda message: self.events.put(("runtime_error", message)))
        service.set_force_stop_callback(lambda message: self.events.put(("force_stopped", message)))

        self.start_hotkey = StartHotkeyListener(
            lambda: self.events.put(("start_hotkey", None))
        )
        if not self.start_hotkey.start():
            self.write_system("Ctrl+F12 could not be registered.", level="WARN")

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
        self.sidebar_backend = QLabel("Connected")
        self.sidebar_backend.setToolTip("VMulti virtual pen output")
        side.addWidget(self.sidebar_backend)
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
            "Apply the visible settings and start pressure output (Ctrl+F12)"
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
        for page in (self.pressure_page, self.mouse_page, self.analysis_page, self.logs_page):
            self.pages.addWidget(page)
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
                lambda _checked=False, selected=index: self.channel_tabs.setCurrentIndex(selected)
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
            "left": ChannelEditor(
                "left", config.left, enabled=config.left_enabled
            ),
            "right": ChannelEditor(
                "right", config.right, enabled=config.right_enabled
            ),
        }
        self.left_enabled = self.editors["left"].enabled
        self.right_enabled = self.editors["right"].enabled
        self.editors["left"].suppress.setChecked(config.suppress_lmb)
        self.editors["right"].suppress.setChecked(config.suppress_rmb)
        if self.editors["right"].xtilt is not None:
            self.editors["right"].xtilt.setChecked(config.rmb_aux_xtilt)
        for channel in ("left", "right"):
            self.channel_tabs.addWidget(self.editors[channel])
            self.editors[channel].calibrate_button.clicked.connect(
                lambda _checked=False, selected=channel: self._begin_calibration(selected)
            )
            self.editors[channel].reset_button.clicked.connect(
                lambda _checked=False, selected=channel: self._reset_channel_settings(selected)
            )
            self.editors[channel].advanced_button.toggled.connect(
                lambda _checked=False: QTimer.singleShot(
                    0, self._resize_editor_card
                )
            )
            self.editors[channel].curve.currentIndexChanged.connect(
                lambda _index: QTimer.singleShot(0, self._resize_editor_card)
            )
        editor_card.content.addWidget(self.channel_tabs)
        body.addWidget(editor_card, 5, Qt.AlignmentFlag.AlignTop)

        graph_column = QVBoxLayout()
        graph_card = Card()
        graph_title = QLabel("Output Pressure")
        graph_title.setObjectName("sectionTitle")
        graph_card.content.addWidget(graph_title)
        self.mapping_graph = MappingGraph()
        self.mapping_graph.setFixedHeight(310)
        graph_card.content.addWidget(self.mapping_graph)
        stats = QHBoxLayout()
        raw_card, self.raw_metric = metric_card("Raw Pressure", "—")
        input_card, self.input_metric = metric_card("Input Pressure", "—")
        output_card, self.output_metric = metric_card("Output Pressure", "—")
        for card in (raw_card, input_card, output_card):
            stats.addWidget(card, 1)
        graph_card.content.addLayout(stats)
        graph_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
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
        content.setMaximumWidth(720)
        layout.setSpacing(16)
        hardware = Card()
        title = QLabel("Hardware while mapping")
        title.setObjectName("sectionTitle")
        hardware.content.addWidget(title)
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        grid.addWidget(_label("Setting", muted=True), 0, 0)
        grid.addWidget(_label("Mapping off", muted=True), 0, 1)
        grid.addWidget(_label("Mapping on", muted=True), 0, 2)
        grid.addWidget(QLabel("DPI"), 1, 0)
        self.normal_dpi = _label("Detecting…", muted=True)
        grid.addWidget(self.normal_dpi, 1, 1)
        self.dpi = QSpinBox()
        self.dpi.setRange(100, 32000)
        self.dpi.setSingleStep(50)
        self.dpi.setValue(config.session_dpi)
        grid.addWidget(self.dpi, 1, 2)
        self.haptics: dict[str, SliderField] = {}
        self.normal_haptics: dict[str, QLabel] = {}
        for row, channel in enumerate(("left", "right"), start=2):
            grid.addWidget(QLabel(f"{channel.title()} haptics"), row, 0)
            normal = _label("—", muted=True)
            self.normal_haptics[channel] = normal
            grid.addWidget(normal, row, 1)
            value = config.session_haptic_left if channel == "left" else config.session_haptic_right
            slider = SliderField("", 0, 5, value)
            slider.title.setVisible(False)
            slider.value_label.setVisible(False)
            slider.description.setText("Haptics off")
            slider.description.setVisible(value == 0)
            slider.valueChanged.connect(
                lambda level, field=slider: field.description.setVisible(level == 0)
            )
            self.haptics[channel] = slider
            grid.addWidget(slider, row, 2)
        hardware.content.addLayout(grid)
        hardware.content.addWidget(
            _label(
                "These values apply only while pressure mapping is active and are restored on Stop.",
                muted=True,
                wrap=True,
            )
        )
        layout.addWidget(hardware)

        output = Card()
        output_title = QLabel("Pen output")
        output_title.setObjectName("sectionTitle")
        output.content.addWidget(output_title)
        self.backend = QComboBox()
        self.backend.addItem("VMulti · lowest latency", "vmulti")
        self.backend.addItem("Synthetic · compatibility fallback", "synthetic")
        self.backend.setCurrentIndex(max(0, self.backend.findData(self.service.launch_config.backend)))
        output.content.addWidget(self.backend)
        output.content.addWidget(
            _label("Prefer VMulti for lowest latency. Use synthetic as a fallback.", muted=True)
        )
        layout.addWidget(output)

        app = Card()
        app_title = QLabel("Application")
        app_title.setObjectName("sectionTitle")
        app.content.addWidget(app_title)
        self.debug_mode = LabeledSwitch(
            "Debug mode",
            "Records stroke traces for analysis. Turn off for potentially reduced latency.",
            checked=config.debug_mode,
        )
        self.minimize_to_tray = LabeledSwitch(
            "Minimize to tray",
            "Keep pressure mapping available when this window is minimized.",
            checked=config.minimize_to_tray,
        )
        app.content.addWidget(self.debug_mode)
        app.content.addWidget(self.minimize_to_tray)
        sandbox_row = QHBoxLayout()
        sandbox_copy = QVBoxLayout()
        sandbox_copy.setSpacing(2)
        sandbox_copy.addWidget(QLabel("Pressure sandbox"))
        sandbox_copy.addWidget(
            _label(
                "Test processed left- and right-click pressure in a physics toy.",
                muted=True,
                wrap=True,
            )
        )
        sandbox_row.addLayout(sandbox_copy, 1)
        self.sandbox_button = QPushButton("Open sandbox")
        self.sandbox_button.clicked.connect(self._launch_sandbox)
        sandbox_row.addWidget(self.sandbox_button)
        app.content.addLayout(sandbox_row)
        layout.addWidget(app)

        advanced = Card()
        advanced_title = QLabel("Advanced backend")
        advanced_title.setObjectName("sectionTitle")
        advanced.content.addWidget(advanced_title)
        hz_row = QHBoxLayout()
        hz_row.addWidget(QLabel("Pen injection rate"))
        hz_row.addStretch(1)
        self.injection_hz = QComboBox()
        for value in (60, 120, 240, 360):
            self.injection_hz.addItem(f"{value} Hz", value)
        desired = round(self.service.launch_config.hz)
        self.injection_hz.setCurrentIndex(max(0, self.injection_hz.findData(desired)))
        hz_row.addWidget(self.injection_hz)
        advanced.content.addLayout(hz_row)
        self.release_teardown = LabeledSwitch(
            "Experimental release teardown",
            "Synthetic only. Compatibility sequence for apps that retain a hover pointer.",
            checked=config.release_teardown,
        )
        advanced.content.addWidget(self.release_teardown)
        layout.addWidget(advanced)
        layout.addStretch(1)
        self.backend.currentIndexChanged.connect(self._backend_changed)
        self._backend_changed()
        return self._scroll_page(content)

    def _build_analysis_page(self) -> QWidget:
        content, layout = _page_container()
        layout.setSpacing(16)
        card = Card()
        toolbar = QHBoxLayout()
        toolbar.addWidget(_label("Recent stroke", muted=True))
        self.stroke_selector = QComboBox()
        self.stroke_selector.setMinimumWidth(280)
        self.stroke_selector.currentIndexChanged.connect(self._load_selected_stroke)
        toolbar.addWidget(self.stroke_selector, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(lambda: self._refresh_strokes(select_latest=False))
        toolbar.addWidget(refresh)
        card.content.addLayout(toolbar)
        self.stroke_summary = _label("Draw with Debug mode enabled, then select a stroke.", muted=True, wrap=True)
        card.content.addWidget(self.stroke_summary)
        self.stroke_graph = StrokeGraph()
        card.content.addWidget(self.stroke_graph, 1)
        metrics = QHBoxLayout()
        _path_card, self.path_metric = metric_card("Path", "—")
        _rate_card, self.motion_metric = metric_card("Motion rate", "—")
        _step_card, self.step_metric = metric_card("Pressure step", "—")
        for metric in (_path_card, _rate_card, _step_card):
            metrics.addWidget(metric, 1)
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
            lambda reason: self._restore_window()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick
            else None
        )

    # ---------- theme and navigation ----------
    def apply_theme(self, name: str) -> None:
        self.theme_name = name
        self.theme = theme_for(name)
        QApplication.instance().setStyleSheet(stylesheet(self.theme))
        self._qt_settings.setValue("theme", name)
        self.theme_selector.blockSignals(True)
        self.theme_selector.setCurrentIndex(
            max(0, self.theme_selector.findData(name))
        )
        self.theme_selector.blockSignals(False)
        switches = [
            self.debug_mode,
            self.minimize_to_tray,
            self.release_teardown,
        ]
        for editor in self.editors.values():
            switches.extend(
                (
                    editor.enabled,
                    editor.suppress,
                    editor.immediate_button_wake,
                    editor.clean_stroke_endings,
                )
            )
            if editor.xtilt is not None:
                switches.append(editor.xtilt)
        for widget in switches:
            widget.set_theme(self.theme)
        self.mapping_graph.set_theme(self.theme)
        self.stroke_graph.set_theme(self.theme)

    def _select_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.page_title.setText(("Pressure", "Mouse", "Stroke analysis", "Logs")[index])
        if index == 2:
            self._refresh_strokes(select_latest=False)

    # ---------- settings ----------
    def _connect_mapping_controls(self) -> None:
        for name, editor in self.editors.items():
            for signal in (
                editor.raw_min.valueChanged,
                editor.raw_max.valueChanged,
                editor.curve.currentIndexChanged,
                editor.curve_strength.valueChanged,
                editor.contact.currentIndexChanged,
                editor.deadzone.valueChanged,
                editor.pressure_floor.valueChanged,
                editor.path_stabilization.valueChanged,
                editor.pressure_influence.valueChanged,
            ):
                signal.connect(lambda *_args, channel=name: self._mapping_control_changed(channel))
            editor.immediate_button_wake.toggled.connect(
                lambda *_args, channel=name: self._mapping_control_changed(channel)
            )
            editor.clean_stroke_endings.toggled.connect(
                lambda *_args, channel=name: self._mapping_control_changed(channel)
            )
            editor.suppress.toggled.connect(
                lambda *_args, channel=name: self._mapping_control_changed(channel)
            )
            if editor.xtilt is not None:
                editor.xtilt.toggled.connect(self._mark_dirty)

    def _connect_non_mapping_controls(self) -> None:
        for signal in (
            self.dpi.valueChanged,
            self.haptics["left"].valueChanged,
            self.haptics["right"].valueChanged,
            self.backend.currentIndexChanged,
            self.debug_mode.toggled,
            self.minimize_to_tray.toggled,
            self.injection_hz.currentIndexChanged,
            self.release_teardown.toggled,
        ):
            signal.connect(self._mark_dirty)

    def _mapping_control_changed(self, channel: str) -> None:
        if self._loading:
            return
        self._mark_dirty()
        self._redraw_mapping()

    def _pressure_options_changed(
        self, *_args: Any, mark_dirty: bool = True
    ) -> None:
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
        for index, (name, enabled) in enumerate(zip(("Left click", "Right click"), states)):
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

    def _channel_settings(self, channel: str) -> DevSettings:
        editor = self.editors[channel]
        return parse_dev_settings(
            raw_min=str(editor.raw_min.value()),
            raw_max=str(editor.raw_max.value()),
            deadzone=str(editor.deadzone.value()),
            curve=str(editor.curve.currentData()),
            curve_strength=str(editor.curve_strength_value()),
            contact_preset=str(editor.contact.currentData()),
            suppress_lmb=editor.suppress.isChecked(),
            release_teardown=self.release_teardown.isChecked(),
            pressure_floor=str(editor.pressure_floor.value()),
            path_stabilization=str(editor.path_stabilization.value()),
            pressure_influence=str(editor.pressure_influence.value()),
            immediate_button_wake=editor.immediate_button_wake.isChecked(),
            clean_stroke_endings=editor.clean_stroke_endings.isChecked(),
            injection_hz=str(self.injection_hz.currentData()),
        )

    def _device_settings(self) -> dict[str, int]:
        return {
            "dpi": self.dpi.value(),
            "haptic_left": self.haptics["left"].value(),
            "haptic_right": self.haptics["right"].value(),
        }

    def _apply_settings(self) -> bool:
        try:
            left = self._channel_settings("left")
            right = self._channel_settings("right")
            device = self._device_settings()
            backend = str(self.backend.currentData())
            normal = self._normal_device
            follows_normal = (
                normal["dpi"] is not None
                and device["dpi"] == normal["dpi"]
                and device["haptic_left"] == normal["haptic_left"]
                and device["haptic_right"] == normal["haptic_right"]
            )
            self.service.apply_config(
                {
                    "linked": self.linked.isChecked(),
                    "left_enabled": self.left_enabled.isChecked(),
                    "right_enabled": self.right_enabled.isChecked(),
                    "suppress_lmb": left.suppress_lmb,
                    "suppress_rmb": right.suppress_lmb,
                    "rmb_aux_xtilt": bool(
                        self.editors["right"].xtilt
                        and self.editors["right"].xtilt.isChecked()
                    ),
                    "debug_mode": self.debug_mode.isChecked(),
                    "minimize_to_tray": self.minimize_to_tray.isChecked(),
                    "release_teardown": (
                        self.release_teardown.isChecked() if backend == "synthetic" else False
                    ),
                    "session_dpi": device["dpi"],
                    "session_haptic_left": device["haptic_left"],
                    "session_haptic_right": device["haptic_right"],
                    "session_device_settings_follow_normal": follows_normal,
                    "left": left.as_runtime_patch()["left"],
                    "right": right.as_runtime_patch()["left"],
                }
            )
            self.service.launch_config.hz = float(self.injection_hz.currentData())
            self.service.launch_config.backend = backend
        except Exception as exc:
            self.write_system(f"Settings error: {exc}", level="ERROR")
            self._select_page(3)
            self.nav_buttons[3].setChecked(True)
            return False
        self.sidebar_backend.setText(
            "Connected"
        )
        self.sidebar_backend.setToolTip(
            "VMulti virtual pen output" if backend == "vmulti" else "Synthetic pen output"
        )
        self.settings_dirty = False
        self.save_button.setText("Applied")
        self.save_button.setEnabled(False)
        QTimer.singleShot(
            1200,
            lambda: self.save_button.setText("Apply changes")
            if not self.settings_dirty
            else None,
        )
        self.write_system("Settings saved.")
        return True

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
        defaults = RuntimeConfig()
        source = defaults.left if channel == "left" else defaults.right
        editor = self.editors[channel]
        self._loading = True
        try:
            editor.raw_min.setValue(source.raw_min)
            editor.raw_max.setValue(source.raw_max)
            editor.curve.setCurrentIndex(editor.curve.findData(source.curve))
            editor.curve_strength.setValue(round(source.curve_strength * 10))
            editor.contact.setCurrentIndex(editor.contact.findData(source.contact_preset))
            editor.deadzone.setValue(source.deadzone_low)
            editor.pressure_floor.setValue(source.pressure_floor)
            editor.path_stabilization.setValue(source.path_stabilization)
            editor.pressure_influence.setValue(source.pressure_influence)
            editor.immediate_button_wake.setChecked(source.immediate_button_wake)
            editor.clean_stroke_endings.setChecked(source.clean_stroke_endings)
            editor.suppress.setChecked(
                defaults.suppress_lmb if channel == "left" else defaults.suppress_rmb
            )
        finally:
            self._loading = False
        self._mark_dirty()
        self._redraw_mapping()
        self.write_system(f"{channel_label.title()} settings reset. Apply changes to save them.")

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
                editor.curve.setCurrentIndex(max(0, editor.curve.findData(ch.curve)))
                editor.curve_strength.setValue(round(ch.curve_strength * 10))
                editor.contact.setCurrentIndex(max(0, editor.contact.findData(ch.contact_preset)))
                editor.deadzone.setValue(ch.deadzone_low)
                editor.pressure_floor.setValue(ch.pressure_floor)
                editor.path_stabilization.setValue(ch.path_stabilization)
                editor.pressure_influence.setValue(ch.pressure_influence)
                editor.immediate_button_wake.setChecked(ch.immediate_button_wake)
                editor.clean_stroke_endings.setChecked(ch.clean_stroke_endings)
            self.editors["left"].suppress.setChecked(config.suppress_lmb)
            self.editors["right"].suppress.setChecked(config.suppress_rmb)
            if self.editors["right"].xtilt:
                self.editors["right"].xtilt.setChecked(config.rmb_aux_xtilt)
            self.debug_mode.setChecked(config.debug_mode)
            self.minimize_to_tray.setChecked(config.minimize_to_tray)
            self.release_teardown.setChecked(config.release_teardown)
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

    def _backend_changed(self, *_args: Any) -> None:
        synthetic = self.backend.currentData() == "synthetic"
        self.release_teardown.setVisible(synthetic)
        self.sidebar_backend.setText("Connected")
        self.sidebar_backend.setToolTip(
            "Synthetic pen output" if synthetic else "VMulti virtual pen output"
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

    def _redraw_mapping(self) -> None:
        series: dict[str, list[tuple[int, int]]] = {}
        raw_ranges: dict[str, tuple[int, int]] = {}
        for channel in ("left", "right"):
            try:
                settings = self._channel_settings(channel)
            except Exception:
                continue
            series[channel] = [
                (raw, effective_pressure_for_raw(settings, raw))
                for raw in range(MappingGraph.RAW_MIN, MappingGraph.RAW_MAX + 1, 4)
            ]
            raw_ranges[channel] = (settings.raw_min, settings.raw_max)
        self.mapping_graph.set_data(
            series,
            raw_ranges,
            channels=self._visible_mapping_channels(),
        )

    def _trace_directory(self) -> Path:
        configured = self.service.launch_config.trace_dir
        return Path(configured) if configured else self.config_store.config_dir / "stroke_traces"

    def _refresh_strokes(self, *, select_latest: bool) -> None:
        directory = self._trace_directory()
        paths = sorted(directory.glob("stroke-*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:100]
        current = self.stroke_selector.currentText()
        self._trace_paths = {path.name: path for path in paths}
        self.stroke_selector.blockSignals(True)
        self.stroke_selector.clear()
        self.stroke_selector.addItems(self._trace_paths)
        if paths:
            target = paths[0].name if select_latest or current not in self._trace_paths else current
            self.stroke_selector.setCurrentText(target)
        self.stroke_selector.blockSignals(False)
        self._load_selected_stroke()

    def _load_selected_stroke(self, *_args: Any) -> None:
        path = self._trace_paths.get(self.stroke_selector.currentText())
        if path is None:
            self.stroke_graph.set_analysis(None)
            return
        try:
            analysis = stroke_analysis_data(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            self.stroke_summary.setText(f"Could not read trace: {exc}")
            self.stroke_graph.set_analysis(None)
            return
        self.stroke_graph.set_analysis(analysis)
        self.stroke_summary.setText(
            f"{analysis['diagnosis']}  ·  {analysis['stationary_dab_points']} stationary points removed"
        )
        self.path_metric.setText(f"{analysis['path_px']:.0f} px")
        self.motion_metric.setText(f"{analysis['motion_hz']:.0f} Hz")
        self.step_metric.setText(f"{analysis['p95_pressure_step']:.0f} / 1024")

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
        self._watch_future("started", self.controller.start(device_settings=self._device_settings()))

    def _begin_stop(self) -> None:
        self.busy = True
        self._set_status("Stopping…", "busy")
        self.start_button.setEnabled(False)
        self._watch_future("stopped", self.controller.stop())

    def _begin_device_detection(self) -> None:
        self.detecting = True
        self.start_button.setEnabled(False)
        self._watch_future("device_settings_detected", self.controller.detect_device_settings())

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
            "Stop pressure output (Ctrl+Shift+F12)"
            if running
            else "Apply the visible settings and start pressure output (Ctrl+F12)"
        )
        self.start_button.setEnabled(not self.detecting)
        self.save_button.setText("Apply changes")
        self.save_button.setEnabled(self.settings_dirty)
        for editor in self.editors.values():
            editor.calibrate_button.setEnabled(not self.calibrating)
        self.backend.setEnabled(not running)
        self.injection_hz.setEnabled(not running)
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
                    f"Mouse settings applied: {payload['dpi']} DPI, haptics L{payload['haptic_left']}/R{payload['haptic_right']}."
                )
            elif kind == "calibration_progress":
                self._handle_calibration_progress(payload)
            elif kind == "calibration_complete":
                self._finish_calibration(payload)
            elif kind == "calibration_complete_error":
                self._finish_calibration(None, error=payload)
            elif kind in {"runtime_error", "force_stopped"}:
                self._set_running(False)
                self._set_status("Driver stopped", "error" if kind == "runtime_error" else "stopped")
                self.write_system(str(payload), level="ERROR" if kind == "runtime_error" else "WARN")
            elif kind.endswith("_error"):
                if kind == "device_settings_detected_error":
                    self.detecting = False
                    self.start_button.setEnabled(True)
                    self._set_status("Mouse not detected", "error")
                    self.write_system(f"Could not detect mouse settings: {payload}", level="WARN")
                else:
                    self._set_running(self.service.stream_active)
                    self._set_status("Start failed", "error")
                    self.write_system(f"Driver error: {payload}", level="ERROR")

    def _handle_device_detected(self, payload: dict[str, int]) -> None:
        self.detecting = False
        self._normal_device = dict(payload)
        self.normal_dpi.setText(str(payload["dpi"]))
        self.normal_haptics["left"].setText(str(payload["haptic_left"]))
        self.normal_haptics["right"].setText(str(payload["haptic_right"]))
        config = self.service.get_config()
        if config.session_device_settings_follow_normal:
            self.dpi.setValue(payload["dpi"])
            self.haptics["left"].setValue(payload["haptic_left"])
            self.haptics["right"].setValue(payload["haptic_right"])
        self.start_button.setEnabled(True)
        self._set_status("Stopped", "stopped")
        self.write_system(
            f"Detected mouse: {payload['dpi']} DPI, haptics L{payload['haptic_left']}/R{payload['haptic_right']}."
        )

    def _handle_telemetry(self, payload: dict[str, Any]) -> None:
        effective_by_channel: dict[str, int] = {}
        for channel in ("left", "right"):
            raw = int(payload[f"{channel}_raw"])
            mapped = int(payload[f"{channel}_mapped"])
            self._latest_raw[channel] = raw
            self._latest_mapped[channel] = mapped
            settings_channel = "left" if self.linked.isChecked() else channel
            try:
                effective = effective_pressure_for_raw(
                    self._channel_settings(settings_channel), raw
                )
            except Exception:
                effective = mapped
            effective_by_channel[channel] = effective
            # The plotted curve includes pressure influence and the configured
            # floor, so its live marker must represent that same output stage.
            self.mapping_graph.set_current(channel, raw, effective)
        selected = "left" if self.channel_tabs.currentIndex() == 0 else "right"
        raw = self._latest_raw[selected]
        mapped = self._latest_mapped[selected]
        effective = effective_by_channel[selected]
        self.input_metric.setText(f"{mapped / 1024:.0%}")
        self.output_metric.setText(f"{effective / 1024:.0%}")
        self.raw_metric.setText(str(raw))
        if self.running:
            self._set_status("Running", "running")

    # ---------- logs, tray, shutdown ----------
    def _write_log(self, entry: LogEntry) -> None:
        stamp = dt.datetime.fromtimestamp(entry.ts / 1000).strftime("%H:%M:%S")
        self.terminal.appendPlainText(f"{stamp} {entry.level:<5} {entry.msg}")
        if entry.msg.startswith("TRACE saved ") and self.pages.currentWidget() is self.analysis_page:
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
        threading.Thread(target=close_runtime, name="mouse-pressure-qt-close", daemon=True).start()
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
        backend = "vmulti" if enumerate_vmulti_candidates() else "synthetic"
        config_store = ConfigStore()
        service = RuntimeService(
            launch_config=LaunchConfig(
                backend=backend,
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
