"""Modern PySide6 control panel for Superstrike Pressure."""

from __future__ import annotations

import datetime as dt
import json
import queue
import sys
import threading
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
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from superstrike_pressure.bridge.config import LaunchConfig, RuntimeConfig
from superstrike_pressure.bridge.tablet_emitter import enumerate_vmulti_candidates
from superstrike_pressure.dev_ui import (
    BridgeController,
    DevSettings,
    effective_pressure_for_raw,
    parse_dev_settings,
    sensitivity_mapping_points,
    stroke_analysis_data,
)
from superstrike_pressure.ui.qt_theme import Theme, stylesheet, theme_for
from superstrike_pressure.ui.qt_widgets import (
    Card,
    LabeledSwitch,
    MappingGraph,
    SliderField,
    StrokeGraph,
    metric_card,
)
from superstrike_pressure.ui.windows_shell import StartHotkeyListener, asset_path
from superstrike_pressure.web.config_store import ConfigStore
from superstrike_pressure.web.log_bus import LogBus, LogEntry
from superstrike_pressure.web.runtime_service import RuntimeService


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


class ChannelEditor(QWidget):
    """One channel's primary and advanced pressure controls."""

    def __init__(self, channel: str, config: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsEditor")
        self.channel = channel
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        root.setSpacing(14)

        calibration = QGridLayout()
        calibration.setHorizontalSpacing(12)
        calibration.setVerticalSpacing(8)
        calibration.addWidget(_label("Raw minimum", muted=True), 0, 0)
        calibration.addWidget(_label("Raw maximum", muted=True), 0, 1)
        self.raw_min = QSpinBox()
        self.raw_min.setRange(0, 1022)
        self.raw_min.setValue(config.raw_min)
        self.raw_max = QSpinBox()
        self.raw_max.setRange(1, 1023)
        self.raw_max.setValue(config.raw_max)
        calibration.addWidget(self.raw_min, 1, 0)
        calibration.addWidget(self.raw_max, 1, 1)
        root.addLayout(calibration)

        root.addWidget(_label("Pressure curve", muted=True))
        self.curve = QComboBox()
        self.curve.addItem("Soft", "soft")
        self.curve.addItem("Linear", "linear")
        self.curve.addItem("Hard", "hard")
        self.curve.addItem("S-Curve", "scurve")
        index = self.curve.findData(config.curve)
        self.curve.setCurrentIndex(max(0, index))
        root.addWidget(self.curve)

        self.curve_strength = SliderField(
            "Curve strength",
            5,
            40,
            round(config.curve_strength * 10),
            description="Controls how strongly the selected curve reshapes pressure.",
        )
        self.curve_strength.spin.setVisible(False)
        self.curve_strength.value_label.setText(f"{config.curve_strength:.1f}")
        self.curve_strength.valueChanged.connect(
            lambda value: self.curve_strength.value_label.setText(f"{value / 10:.1f}")
        )
        root.addWidget(self.curve_strength)

        contact_row = QGridLayout()
        contact_row.setHorizontalSpacing(12)
        contact_row.addWidget(_label("Contact feel", muted=True), 0, 0)
        contact_row.addWidget(_label("Native click", muted=True), 0, 1)
        self.contact = QComboBox()
        for label, value in (("Light", "light"), ("Medium", "medium"), ("Firm", "firm")):
            self.contact.addItem(label, value)
        self.contact.setCurrentIndex(max(0, self.contact.findData(config.contact_preset)))
        contact_row.addWidget(self.contact, 1, 0)
        self.suppress = QCheckBox("Suppress")
        contact_row.addWidget(self.suppress, 1, 1)
        root.addLayout(contact_row)
        root.addWidget(
            _label("Controls when contact begins and releases.", muted=True, wrap=True)
        )

        self.advanced_button = QToolButton()
        self.advanced_button.setText("Advanced settings")
        self.advanced_button.setCheckable(True)
        self.advanced_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_button.setArrowType(Qt.ArrowType.RightArrow)
        root.addWidget(self.advanced_button, 0, Qt.AlignmentFlag.AlignLeft)

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
        self.xtilt: LabeledSwitch | None = None
        if channel == "right":
            self.xtilt = LabeledSwitch(
                "Use right pressure as X-Tilt",
                "While drawing with left pressure, map right pressure to 0–60° X-Tilt.",
            )
            advanced.addWidget(self.xtilt)
        self.advanced.setVisible(False)
        root.addWidget(self.advanced)
        root.addStretch(1)
        self.advanced_button.toggled.connect(self._show_advanced)

    def _show_advanced(self, visible: bool) -> None:
        self.advanced.setVisible(visible)
        self.advanced_button.setArrowType(
            Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow
        )
        self.advanced.updateGeometry()
        self.updateGeometry()

    def curve_strength_value(self) -> float:
        return self.curve_strength.value() / 10.0

    def control_widgets(self) -> list[QWidget]:
        widgets: list[QWidget] = [
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
        self._loading = False
        self._latest_raw = {"left": 0, "right": 0}
        self._latest_mapped = {"left": 0, "right": 0}
        self._normal_device = {"dpi": None, "haptic_left": None, "haptic_right": None}
        self._trace_paths: dict[str, Path] = {}
        self._qt_settings = QSettings("Superstrike", "Pressure")
        self.theme_name = str(self._qt_settings.value("theme", "light"))
        self.theme: Theme = theme_for(self.theme_name)

        self.setWindowTitle("Superstrike Pressure")
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
        self.write_system("Ready. Settings are stored in ~/.superstrike/config.json")
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
        brand = QLabel("Superstrike")
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
        self.sidebar_backend = _label("VMulti output", muted=True)
        side.addWidget(self.sidebar_backend)
        self.theme_button = QPushButton()
        self.theme_button.clicked.connect(self._toggle_theme)
        side.addWidget(self.theme_button)
        layout.addWidget(sidebar)

        content = QVBoxLayout()
        content.setContentsMargins(24, 18, 24, 20)
        content.setSpacing(16)
        header = QHBoxLayout()
        self.status_label = QLabel("● Detecting mouse")
        self.status_label.setObjectName("statusBusy")
        header.addWidget(self.status_label)
        self.hz_label = _label("— Hz", muted=True)
        header.addWidget(self.hz_label)
        header.addStretch(1)
        shortcut = _label("Ctrl+F12 start  ·  Ctrl+Shift+F12 stop", muted=True)
        header.addWidget(shortcut)
        self.save_button = QPushButton("Save settings")
        self.save_button.clicked.connect(self._save_or_apply)
        header.addWidget(self.save_button)
        self.start_button = QPushButton("Start")
        self.start_button.setObjectName("primary")
        self.start_button.setToolTip("Start pressure mapping (Ctrl+F12)")
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

    def _scroll_page(self, content: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(content)
        return area

    def _build_pressure_page(self, config: RuntimeConfig) -> QWidget:
        content, layout = _page_container()
        layout.setSpacing(16)
        options = Card(padding=16)
        option_row = QHBoxLayout()
        self.left_enabled = LabeledSwitch(
            "Left pressure", checked=config.left_enabled, compact=True
        )
        self.right_enabled = LabeledSwitch(
            "Right pressure", checked=config.right_enabled, compact=True
        )
        self.linked = LabeledSwitch(
            "Link settings", checked=config.linked, compact=True
        )
        option_row.addWidget(self.left_enabled, 1)
        option_row.addWidget(self.right_enabled, 1)
        option_row.addWidget(self.linked, 1)
        options.content.addLayout(option_row)
        layout.addWidget(options)

        body = QHBoxLayout()
        body.setSpacing(16)
        editor_card = Card()
        editor_card.setMinimumWidth(390)
        title = QLabel("Button settings")
        title.setObjectName("sectionTitle")
        editor_card.content.addWidget(title)
        self.channel_tabs = QTabWidget()
        self.editors = {
            "left": ChannelEditor("left", config.left),
            "right": ChannelEditor("right", config.right),
        }
        self.editors["left"].suppress.setChecked(config.suppress_lmb)
        self.editors["right"].suppress.setChecked(config.suppress_rmb)
        if self.editors["right"].xtilt is not None:
            self.editors["right"].xtilt.setChecked(config.rmb_aux_xtilt)
        self.channel_scroll_areas: dict[str, QScrollArea] = {}
        for channel, label in (("left", "Left button"), ("right", "Right button")):
            scroll = QScrollArea()
            scroll.setObjectName("settingsScroll")
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(self.editors[channel])
            self.channel_scroll_areas[channel] = scroll
            self.channel_tabs.addTab(scroll, label)
        editor_card.content.addWidget(self.channel_tabs)
        body.addWidget(editor_card, 5)

        graph_column = QVBoxLayout()
        graph_card = Card()
        graph_header = QHBoxLayout()
        graph_title = QLabel("Sensitivity mapping")
        graph_title.setObjectName("sectionTitle")
        graph_header.addWidget(graph_title)
        graph_header.addStretch(1)
        self.mapping_state = _label("Press Start for live input", muted=True)
        graph_header.addWidget(self.mapping_state)
        graph_card.content.addLayout(graph_header)
        self.mapping_graph = MappingGraph()
        graph_card.content.addWidget(self.mapping_graph, 1)
        stats = QHBoxLayout()
        raw_card, self.raw_metric = metric_card("Raw", "—")
        mapped_card, self.mapped_metric = metric_card("Mapped", "—")
        effective_card, self.effective_metric = metric_card("Effective", "—")
        for card in (raw_card, mapped_card, effective_card):
            stats.addWidget(card, 1)
        graph_card.content.addLayout(stats)
        graph_column.addWidget(graph_card, 1)

        restore_card = Card(padding=14)
        restore_row = QHBoxLayout()
        restore_row.addWidget(_label("Default settings"), 1)
        self.restore_button = QPushButton("Restore defaults")
        self.restore_button.clicked.connect(self._restore_defaults)
        restore_row.addWidget(self.restore_button)
        restore_card.content.addLayout(restore_row)
        graph_column.addWidget(restore_card)
        body.addLayout(graph_column, 7)
        layout.addLayout(body, 1)

        self._connect_mapping_controls()
        self.left_enabled.toggled.connect(self._pressure_options_changed)
        self.right_enabled.toggled.connect(self._pressure_options_changed)
        self.linked.toggled.connect(self._pressure_options_changed)
        self.channel_tabs.currentChanged.connect(lambda _index: self._redraw_mapping())
        self._pressure_options_changed()
        return content

    def _build_mouse_page(self, config: RuntimeConfig) -> QScrollArea:
        content, layout = _page_container()
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
        self.tray.setToolTip("Superstrike Pressure")
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
        self.theme_button.setText("☀  Light mode" if name == "dark" else "◐  Dark mode")
        switches = [
            self.left_enabled,
            self.right_enabled,
            self.linked,
            self.debug_mode,
            self.minimize_to_tray,
            self.release_teardown,
        ]
        for editor in self.editors.values():
            if editor.xtilt is not None:
                switches.append(editor.xtilt)
        for widget in switches:
            widget.set_theme(self.theme)
        self.mapping_graph.set_theme(self.theme)
        self.stroke_graph.set_theme(self.theme)

    def _toggle_theme(self) -> None:
        self.apply_theme("light" if self.theme_name == "dark" else "dark")

    def _select_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
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

    def _mapping_control_changed(self, channel: str) -> None:
        if self._loading:
            return
        if self.linked.isChecked() and channel == "left":
            self._copy_left_to_right()
        self._redraw_mapping()

    def _copy_left_to_right(self) -> None:
        left, right = self.editors["left"], self.editors["right"]
        self._loading = True
        try:
            right.raw_min.setValue(left.raw_min.value())
            right.raw_max.setValue(left.raw_max.value())
            right.curve.setCurrentIndex(right.curve.findData(left.curve.currentData()))
            right.curve_strength.setValue(left.curve_strength.value())
            right.contact.setCurrentIndex(right.contact.findData(left.contact.currentData()))
            right.deadzone.setValue(left.deadzone.value())
            right.pressure_floor.setValue(left.pressure_floor.value())
            right.path_stabilization.setValue(left.path_stabilization.value())
            right.pressure_influence.setValue(left.pressure_influence.value())
        finally:
            self._loading = False

    def _pressure_options_changed(self, *_args: Any) -> None:
        both = self.left_enabled.isChecked() and self.right_enabled.isChecked()
        self.linked.setEnabled(both)
        if not both and self.linked.isChecked():
            self.linked.setChecked(False)
        self.channel_tabs.setTabEnabled(0, self.left_enabled.isChecked())
        self.channel_tabs.setTabEnabled(
            1,
            self.right_enabled.isChecked() and not self.linked.isChecked(),
        )
        if self.linked.isChecked():
            self._copy_left_to_right()
            if self.channel_tabs.currentIndex() == 1:
                self.channel_tabs.setCurrentIndex(0)
        self._redraw_mapping()

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
            "VMulti output" if backend == "vmulti" else "Synthetic output"
        )
        self.write_system("Settings saved.")
        return True

    def _save_or_apply(self) -> None:
        if self.busy or not self._apply_settings():
            return
        if self.running:
            self.save_button.setText("Applying…")
            self.save_button.setEnabled(False)
            device = self._device_settings()
            self._watch_future(
                "device_settings_applied",
                self.controller.apply_device_settings(**device),
            )

    def _restore_defaults(self) -> None:
        answer = QMessageBox.question(
            self,
            "Restore default settings?",
            "This will replace the current pressure and mouse settings with the recommended defaults.",
            QMessageBox.StandardButton.RestoreDefaults | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.RestoreDefaults:
            return
        try:
            config = self.service.restore_defaults()
            self._load_config(config)
            self.write_system("Default settings restored.")
        except Exception as exc:
            self.write_system(f"Could not restore defaults: {exc}", level="ERROR")

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
        self._pressure_options_changed()

    def _backend_changed(self, *_args: Any) -> None:
        synthetic = self.backend.currentData() == "synthetic"
        self.release_teardown.setVisible(synthetic)
        self.sidebar_backend.setText("Synthetic output" if synthetic else "VMulti output")

    # ---------- mapping and analysis ----------
    def _visible_mapping_channels(self) -> tuple[str, ...]:
        if self.linked.isChecked() and self.left_enabled.isChecked() and self.right_enabled.isChecked():
            return ("left", "right")
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
            series[channel] = sensitivity_mapping_points(settings)
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
        if self.busy:
            return
        if self.running:
            self._begin_stop()
        elif self._apply_settings():
            self._begin_start()

    def _begin_start(self) -> None:
        self.busy = True
        self._set_status("Starting", "busy")
        self.start_button.setEnabled(False)
        self._watch_future("started", self.controller.start(device_settings=self._device_settings()))

    def _begin_stop(self) -> None:
        self.busy = True
        self._set_status("Stopping", "busy")
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
        self.start_button.setEnabled(not self.detecting)
        self.save_button.setText("Apply settings live" if running else "Save settings")
        self.save_button.setEnabled(True)
        self.restore_button.setEnabled(not running)
        self.backend.setEnabled(not running)
        self.injection_hz.setEnabled(not running)
        self._set_status("Running" if running else "Stopped", "running" if running else "stopped")
        if not running:
            self.mapping_state.setText("Press Start for live input")

    def _set_status(self, text: str, state: str) -> None:
        object_name = {
            "running": "statusRunning",
            "busy": "statusBusy",
            "error": "statusError",
        }.get(state, "statusStopped")
        self.status_label.setObjectName(object_name)
        self.status_label.setText(f"● {text}")
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
                if not self.running and not self.busy and not self.detecting:
                    self._toggle_bridge()
            elif kind == "started":
                self._set_running(True)
            elif kind == "stopped":
                self._set_running(False)
            elif kind == "device_settings_detected":
                self._handle_device_detected(payload)
            elif kind == "device_settings_applied":
                self.save_button.setText("Apply settings live")
                self.save_button.setEnabled(True)
                self.write_system(
                    f"Mouse settings applied: {payload['dpi']} DPI, haptics L{payload['haptic_left']}/R{payload['haptic_right']}."
                )
            elif kind in {"runtime_error", "force_stopped"}:
                self._set_running(False)
                self._set_status("Bridge stopped", "error" if kind == "runtime_error" else "stopped")
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
                    self.write_system(f"Bridge error: {payload}", level="ERROR")

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
        for channel in ("left", "right"):
            raw = int(payload[f"{channel}_raw"])
            mapped = int(payload[f"{channel}_mapped"])
            self._latest_raw[channel] = raw
            self._latest_mapped[channel] = mapped
            self.mapping_graph.set_current(channel, raw, mapped)
        selected = "left" if self.channel_tabs.currentIndex() == 0 else "right"
        raw = self._latest_raw[selected]
        mapped = self._latest_mapped[selected]
        try:
            effective = effective_pressure_for_raw(self._channel_settings(selected), raw)
        except Exception:
            effective = mapped
        hz = float(payload.get("hz", 0.0))
        self.raw_metric.setText(str(raw))
        self.mapped_metric.setText(f"{mapped / 1024:.0%}")
        self.effective_metric.setText(f"{effective / 1024:.0%}")
        self.mapping_state.setText(f"{selected.title()} button · live")
        self.hz_label.setText(f"{hz:.1f} Hz")

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
            finally:
                self.events.put(("closed", None))

        def poll_closed() -> None:
            drained = False
            retained: list[tuple[str, Any]] = []
            while True:
                try:
                    item = self.events.get_nowait()
                except queue.Empty:
                    break
                if item[0] == "closed":
                    drained = True
                else:
                    retained.append(item)
            for item in retained:
                self.events.put(item)
            if drained:
                self._allow_close = True
                self.tray.hide()
                QApplication.quit()
            else:
                QTimer.singleShot(50, poll_closed)

        threading.Thread(target=close_runtime, name="superstrike-qt-close", daemon=True).start()
        QTimer.singleShot(50, poll_closed)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Superstrike Pressure")
    app.setOrganizationName("Superstrike")
    app.setQuitOnLastWindowClosed(False)
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
        return 1
    window = MainWindow(service, controller, log_bus, config_store)
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
