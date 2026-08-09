"""Small desktop control panel for developing the pressure bridge."""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from superstrike_pressure.bridge.config import LaunchConfig
from superstrike_pressure.bridge.curves import PressureConfig, map_pressure, normalize_curve_name
from superstrike_pressure.web.calibration import run_calibration
from superstrike_pressure.web.config_store import ConfigStore
from superstrike_pressure.web.log_bus import LogBus, LogEntry
from superstrike_pressure.web.models import CURVE_STRENGTH_MAX, CURVE_STRENGTH_MIN
from superstrike_pressure.web.runtime_service import RuntimeService


@dataclass(frozen=True)
class DevSettings:
    raw_min: int
    raw_max: int
    deadzone: int
    curve: str
    curve_strength: float
    contact_preset: str
    suppress_lmb: bool
    release_teardown: bool
    onset_buffer: bool = False
    pressure_floor: int = 12
    path_stabilization: int = 0
    pressure_influence: int = 85
    injection_hz: float = 240.0

    def as_runtime_patch(self) -> dict[str, Any]:
        channel = {
            "raw_min": self.raw_min,
            "raw_max": self.raw_max,
            "deadzone_low": self.deadzone,
            "deadzone_high": self.deadzone,
            "curve": self.curve,
            "curve_strength": self.curve_strength,
            "contact_preset": self.contact_preset,
            "pressure_floor": self.pressure_floor,
            "path_stabilization": self.path_stabilization,
            "pressure_influence": self.pressure_influence,
            "onset_buffer": self.onset_buffer,
        }
        return {
            "linked": True,
            "suppress_lmb": self.suppress_lmb,
            "release_teardown": self.release_teardown,
            "left": channel,
        }


def parse_dev_settings(
    *,
    raw_min: str,
    raw_max: str,
    deadzone: str,
    curve: str,
    curve_strength: str,
    contact_preset: str,
    suppress_lmb: bool,
    release_teardown: bool,
    onset_buffer: bool = False,
    pressure_floor: str = "12",
    path_stabilization: str = "0",
    pressure_influence: str = "85",
    injection_hz: str = "240",
) -> DevSettings:
    """Convert control values into typed settings before backend validation."""
    try:
        parsed = DevSettings(
            raw_min=int(raw_min),
            raw_max=int(raw_max),
            deadzone=int(deadzone),
            curve=curve,
            curve_strength=float(curve_strength),
            contact_preset=contact_preset,
            suppress_lmb=bool(suppress_lmb),
            release_teardown=bool(release_teardown),
            onset_buffer=bool(onset_buffer),
            pressure_floor=int(pressure_floor),
            path_stabilization=int(path_stabilization),
            pressure_influence=int(pressure_influence),
            injection_hz=float(injection_hz),
        )
    except ValueError as exc:
        raise ValueError("Raw range, deadzone, and curve strength must be numeric.") from exc

    if parsed.raw_min >= parsed.raw_max:
        raise ValueError("Raw minimum must be lower than raw maximum.")
    if not CURVE_STRENGTH_MIN <= parsed.curve_strength <= CURVE_STRENGTH_MAX:
        raise ValueError(
            f"Curve strength must be between "
            f"{CURVE_STRENGTH_MIN:g} and {CURVE_STRENGTH_MAX:g}."
        )
    if not 0 <= parsed.pressure_floor <= 100:
        raise ValueError("Pressure floor must be between 0 and 100 percent.")
    if not 0 <= parsed.path_stabilization <= 100:
        raise ValueError("Path stabilization must be between 0 and 100 percent.")
    if not 0 <= parsed.pressure_influence <= 100:
        raise ValueError("Pressure influence must be between 0 and 100 percent.")
    if not 30.0 <= parsed.injection_hz <= 500.0:
        raise ValueError("Pen injection rate must be between 30 and 500 Hz.")
    return parsed


def sensitivity_mapping_points(
    settings: DevSettings,
    *,
    samples: int = 65,
) -> list[tuple[int, int]]:
    """Return raw ADC to effective pen-pressure points for the live graph."""
    count = max(2, int(samples))
    points: list[tuple[int, int]] = []
    for index in range(count):
        raw = round(
            settings.raw_min
            + (settings.raw_max - settings.raw_min) * index / (count - 1)
        )
        points.append((raw, effective_pressure_for_raw(settings, raw)))
    return points


def effective_pressure_for_raw(settings: DevSettings, raw: int) -> int:
    """Map one raw ADC sample through the settings that affect brush size."""
    pressure_config = PressureConfig(
        raw_min=settings.raw_min,
        raw_max=settings.raw_max,
        out_min=0,
        out_max=1023,
        deadzone_low=settings.deadzone / 100.0,
        deadzone_high=1.0 - settings.deadzone / 100.0,
        curve=normalize_curve_name(settings.curve),
        curve_strength=settings.curve_strength,
    )
    floor = round(settings.pressure_floor * 1024 / 100)
    mapped = map_pressure(raw, pressure_config)
    if mapped > 0 and settings.pressure_influence < 100:
        mapped = round(512 + (mapped - 512) * settings.pressure_influence / 100.0)
    if mapped > 0 and floor > 0:
        mapped = max(mapped, floor)
    return max(0, min(1024, int(mapped)))


class BridgeController:
    """Own a persistent asyncio loop for RuntimeService lifecycle calls."""

    def __init__(self, service: RuntimeService) -> None:
        self.service = service
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="superstrike-dev-runtime",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=2.0):
            raise RuntimeError("Could not start the bridge runtime loop")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def start(self) -> Future[None]:
        return asyncio.run_coroutine_threadsafe(self.service.start_stream(), self._loop)

    def stop(self) -> Future[None]:
        return asyncio.run_coroutine_threadsafe(self.service.stop_stream(), self._loop)

    def calibrate(self, channel: str, progress_cb: Any) -> Future[dict]:
        return asyncio.run_coroutine_threadsafe(
            run_calibration(
                channel,
                self.service,
                progress_cb,
                self.service.config_store,
            ),
            self._loop,
        )

    def apply_device_settings(
        self,
        *,
        dpi: int,
        haptic_left: int,
        haptic_right: int,
    ) -> Future[dict[str, int]]:
        return asyncio.run_coroutine_threadsafe(
            self.service.apply_device_settings(
                dpi=dpi,
                haptic_left=haptic_left,
                haptic_right=haptic_right,
            ),
            self._loop,
        )

    def close(self, timeout: float = 4.0) -> None:
        if not self._thread.is_alive():
            return

        async def stop_if_needed() -> None:
            if self.service.stream_active:
                await self.service.stop_stream()

        future = asyncio.run_coroutine_threadsafe(stop_if_needed(), self._loop)
        future.result(timeout=timeout)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)


class DevPanel:
    def __init__(self, root: Any, service: RuntimeService, controller: BridgeController, log_bus: LogBus) -> None:
        import tkinter as tk
        from tkinter import scrolledtext, ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.service = service
        self.controller = controller
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.running = False
        self.busy = False
        self._setting_widgets: list[Any] = []
        self._last_calibration_phase = ""
        self.advanced_visible = {"left": False, "right": False}
        self._latest_raw: dict[str, int | None] = {"left": None, "right": None}
        self._active_calibration_channel = "left"

        root.title("Superstrike Pressure")
        root.geometry("1080x780")
        root.minsize(920, 680)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        self.style = style
        self.theme_var = tk.StringVar(value="light")
        self.theme_colors: dict[str, str] = {}
        style.configure("Status.TLabel", font=("Segoe UI Semibold", 10))
        style.configure("Primary.TButton", font=("Segoe UI Semibold", 10), padding=(16, 8))

        outer = ttk.Frame(root, padding=14)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 12))
        self.status_label = ttk.Label(header, text="● Stopped", style="Status.TLabel", foreground="#a33")
        self.status_label.pack(side="left")
        ttk.Label(header, text="Emergency release: Ctrl+Shift+F12").pack(side="left", padx=18)
        self.toggle_button = ttk.Button(
            header,
            text="Start",
            style="Primary.TButton",
            command=self._toggle,
        )
        self.toggle_button.pack(side="right")
        appearance = ttk.Frame(header)
        appearance.pack(side="right", padx=(0, 14))
        ttk.Label(appearance, text="Theme").pack(side="left", padx=(0, 5))
        ttk.Radiobutton(
            appearance,
            text="Light",
            value="light",
            variable=self.theme_var,
            command=self._apply_theme,
        ).pack(side="left")
        ttk.Radiobutton(
            appearance,
            text="Dark",
            value="dark",
            variable=self.theme_var,
            command=self._apply_theme,
        ).pack(side="left", padx=(4, 0))

        body = ttk.Panedwindow(outer, orient="horizontal")
        self.body = body
        body.pack(fill="both", expand=True)

        self.calibration_frame = tk.Frame(
            outer,
            background="#ffdf80",
            borderwidth=1,
            relief="solid",
            padx=18,
            pady=12,
        )
        self.calibration_title = tk.Label(
            self.calibration_frame,
            text="Calibration",
            background="#ffdf80",
            foreground="#322400",
            font=("Segoe UI Semibold", 13),
        )
        self.calibration_title.pack(anchor="w")
        self.calibration_instruction = tk.Label(
            self.calibration_frame,
            text="",
            background="#ffdf80",
            foreground="#181200",
            font=("Segoe UI Semibold", 16),
            anchor="w",
            justify="left",
            wraplength=850,
        )
        self.calibration_instruction.pack(fill="x", pady=(3, 1))
        self.calibration_value = tk.Label(
            self.calibration_frame,
            text="",
            background="#ffdf80",
            foreground="#594600",
            font=("Cascadia Mono", 10),
            anchor="w",
        )
        self.calibration_value.pack(fill="x")

        settings_frame = ttk.LabelFrame(body, text="Settings", padding=10)
        output_frame = ttk.Frame(body, padding=(8, 0, 0, 0))
        body.add(settings_frame, weight=2)
        body.add(output_frame, weight=3)

        config = service.get_config()
        self.channel_vars: dict[str, dict[str, Any]] = {}
        for channel_name, channel_config in (
            ("left", config.left),
            ("right", config.right),
        ):
            self.channel_vars[channel_name] = {
                "raw_min": tk.StringVar(value=str(channel_config.raw_min)),
                "raw_max": tk.StringVar(value=str(channel_config.raw_max)),
                "deadzone": tk.StringVar(value=str(channel_config.deadzone_low)),
                "curve": tk.StringVar(value=channel_config.curve),
                "curve_strength": tk.DoubleVar(value=channel_config.curve_strength),
                "contact": tk.StringVar(value=channel_config.contact_preset),
                "pressure_floor": tk.StringVar(value=str(channel_config.pressure_floor)),
                "path_stabilization": tk.StringVar(
                    value=str(channel_config.path_stabilization)
                ),
                "pressure_influence": tk.StringVar(
                    value=str(channel_config.pressure_influence)
                ),
                "onset_buffer": tk.BooleanVar(value=channel_config.onset_buffer),
                "suppress": tk.BooleanVar(
                    value=(
                        config.suppress_lmb
                        if channel_name == "left"
                        else getattr(config, "suppress_rmb", False)
                    )
                ),
                "haptic": tk.DoubleVar(value=5.0),
            }
        self.injection_hz_var = tk.StringVar(value=f"{service.launch_config.hz:g}")
        self.dpi_var = tk.StringVar(value="800")
        self.release_teardown_var = tk.BooleanVar(value=config.release_teardown)
        self.calibrate_buttons: dict[str, Any] = {}
        self.device_apply_buttons: list[Any] = []
        self.advanced_buttons: dict[str, Any] = {}
        self.advanced_frames: dict[str, Any] = {}
        self.buffer_warning_labels: dict[str, Any] = {}
        self.path_warning_labels: dict[str, Any] = {}
        self.haptic_note_labels: dict[str, Any] = {}
        self.curve_value_labels: dict[str, Any] = {}
        self.haptic_value_labels: dict[str, Any] = {}

        self.settings_notebook = ttk.Notebook(settings_frame)
        self.settings_notebook.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.channel_tabs: dict[str, Any] = {}
        for channel_name, tab_title in (("left", "Left button"), ("right", "Right button")):
            tab = ttk.Frame(self.settings_notebook, padding=(8, 10))
            self.channel_tabs[channel_name] = tab
            self.settings_notebook.add(tab, text=tab_title)
            self._build_channel_settings_tab(tab, channel_name)
        self.settings_notebook.bind("<<NotebookTabChanged>>", self._on_channel_tab_changed)

        self.apply_button = ttk.Button(
            settings_frame,
            text="Save settings",
            command=self._apply_settings,
        )
        self.apply_button.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self._setting_widgets.append(self.apply_button)
        settings_frame.rowconfigure(0, weight=1)
        settings_frame.columnconfigure(1, weight=1)

        self.output_notebook = ttk.Notebook(output_frame)
        self.output_notebook.pack(fill="both", expand=True)
        visualizer_tab = ttk.Frame(self.output_notebook, padding=12)
        terminal_tab = ttk.Frame(self.output_notebook, padding=8)
        self.output_notebook.add(visualizer_tab, text="Sensitivity mapping")
        self.output_notebook.add(terminal_tab, text="Terminal output")

        self.telemetry_label = ttk.Label(
            visualizer_tab,
            text="Press Start to see sensitivity mapping",
            font=("Segoe UI Semibold", 10),
        )
        self.telemetry_label.pack(fill="x", pady=(0, 8))
        self.sensitivity_canvas = tk.Canvas(
            visualizer_tab,
            background="#f7f9fb",
            highlightthickness=1,
            highlightbackground="#c8d0d8",
            borderwidth=0,
        )
        self.sensitivity_canvas.pack(fill="both", expand=True)
        self.sensitivity_canvas.bind("<Configure>", self._redraw_sensitivity)
        self.mapping_caption = ttk.Label(
            visualizer_tab,
            text="Raw click pressure → effective pen pressure",
            foreground="#56616b",
        )
        self.mapping_caption.pack(anchor="center", pady=(8, 0))

        terminal_toolbar = ttk.Frame(terminal_tab)
        terminal_toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(terminal_toolbar, text="Bridge log").pack(side="left")
        ttk.Button(terminal_toolbar, text="Clear", command=self._clear_terminal).pack(side="right")

        self.terminal = scrolledtext.ScrolledText(
            terminal_tab,
            wrap="word",
            state="disabled",
            background="#101418",
            foreground="#d7e0e7",
            insertbackground="#d7e0e7",
            font=("Cascadia Mono", 9),
            borderwidth=0,
        )
        self.terminal.pack(fill="both", expand=True)
        self.terminal.tag_configure("ERROR", foreground="#ff7b72")
        self.terminal.tag_configure("WARN", foreground="#e3b341")
        self.terminal.tag_configure("INFO", foreground="#d7e0e7")
        self.terminal.tag_configure("SYSTEM", foreground="#79c0ff")

        log_bus.subscribe(lambda entry: self.events.put(("log", entry)))
        service.set_telemetry_callback(lambda sample: self.events.put(("telemetry", sample)))
        service.set_failure_callback(lambda message: self.events.put(("runtime_error", message)))
        self._write_system("Ready. Settings are saved in ~/.superstrike/config.json")
        for variables in self.channel_vars.values():
            for key in (
                "raw_min",
                "raw_max",
                "deadzone",
                "curve",
                "curve_strength",
                "pressure_floor",
                "pressure_influence",
            ):
                variables[key].trace_add("write", self._queue_sensitivity_redraw)
        self._apply_theme()
        root.after_idle(self._redraw_sensitivity)
        root.after(50, self._drain_events)

    def _apply_theme(self) -> None:
        dark = self.theme_var.get() == "dark"
        colors = (
            {
                "background": "#16191e",
                "surface": "#20242b",
                "field": "#292f37",
                "foreground": "#e8edf2",
                "muted": "#a7b0ba",
                "border": "#3b424c",
                "canvas": "#171b21",
                "axis": "#a7b1bc",
                "grid": "#343b45",
                "curve": "#58a6ff",
                "marker": "#f0a43a",
                "marker_text": "#ffc46b",
                "terminal": "#0f1216",
            }
            if dark
            else {
                "background": "#f1f3f5",
                "surface": "#ffffff",
                "field": "#ffffff",
                "foreground": "#20262c",
                "muted": "#606b75",
                "border": "#c8d0d8",
                "canvas": "#f7f9fb",
                "axis": "#68747f",
                "grid": "#dce2e7",
                "curve": "#1677b8",
                "marker": "#f5a623",
                "marker_text": "#7a4800",
                "terminal": "#101418",
            }
        )
        self.theme_colors = colors
        self.root.configure(background=colors["background"])
        style = self.style
        for widget_style in (
            "TFrame",
            "TLabel",
            "TCheckbutton",
            "TRadiobutton",
            "TLabelframe",
            "TLabelframe.Label",
            "Status.TLabel",
        ):
            style.configure(
                widget_style,
                background=colors["background"],
                foreground=colors["foreground"],
            )
        style.configure(
            "TButton",
            background=colors["surface"],
            foreground=colors["foreground"],
            bordercolor=colors["border"],
        )
        style.map(
            "TButton",
            background=[("active", colors["field"]), ("disabled", colors["background"])],
            foreground=[("disabled", colors["muted"])],
        )
        style.configure(
            "Primary.TButton",
            background="#2f81f7" if dark else "#1976b9",
            foreground="#ffffff",
            bordercolor="#2f81f7" if dark else "#1976b9",
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#4a92ee" if dark else "#14679f")],
        )
        style.configure(
            "TEntry",
            fieldbackground=colors["field"],
            foreground=colors["foreground"],
            insertcolor=colors["foreground"],
            bordercolor=colors["border"],
        )
        style.configure(
            "TCombobox",
            fieldbackground=colors["field"],
            background=colors["field"],
            foreground=colors["foreground"],
            arrowcolor=colors["foreground"],
            bordercolor=colors["border"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", colors["field"]), ("disabled", colors["surface"])],
            foreground=[("readonly", colors["foreground"]), ("disabled", colors["muted"])],
        )
        style.configure(
            "TNotebook",
            background=colors["background"],
            bordercolor=colors["border"],
        )
        style.configure(
            "TNotebook.Tab",
            background=colors["surface"],
            foreground=colors["muted"],
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", colors["field"])],
            foreground=[("selected", colors["foreground"])],
        )
        style.configure(
            "TScale",
            background=colors["background"],
            troughcolor=colors["field"],
            bordercolor=colors["border"],
        )

        self.sensitivity_canvas.configure(
            background=colors["canvas"],
            highlightbackground=colors["border"],
        )
        self.mapping_caption.configure(foreground=colors["muted"])
        self.terminal.configure(
            background=colors["terminal"],
            foreground=colors["foreground"],
            insertbackground=colors["foreground"],
        )
        self.terminal.tag_configure("INFO", foreground=colors["foreground"])
        self.terminal.tag_configure("SYSTEM", foreground=colors["curve"])
        for label in self.haptic_note_labels.values():
            label.configure(foreground=colors["muted"])
        for label in self.path_warning_labels.values():
            label.configure(foreground="#f0a43a" if dark else "#a55400")
        for label in self.buffer_warning_labels.values():
            label.configure(foreground="#ff7b72" if dark else "#b42318")

        calibration_background = "#5b4300" if dark else "#ffdf80"
        calibration_foreground = "#fff1bd" if dark else "#181200"
        calibration_muted = "#e6c96d" if dark else "#594600"
        self.calibration_frame.configure(background=calibration_background)
        self.calibration_title.configure(
            background=calibration_background,
            foreground=calibration_foreground,
        )
        self.calibration_instruction.configure(
            background=calibration_background,
            foreground=calibration_foreground,
        )
        self.calibration_value.configure(
            background=calibration_background,
            foreground=calibration_muted,
        )
        self._redraw_sensitivity()

    def _build_channel_settings_tab(self, parent: Any, channel: str) -> None:
        variables = self.channel_vars[channel]
        parent.columnconfigure(1, weight=1)
        row = 0
        row = self._entry_row(parent, row, "Raw minimum", variables["raw_min"])
        row = self._entry_row(parent, row, "Raw maximum", variables["raw_max"])
        calibrate_button = self.ttk.Button(
            parent,
            text=f"Calibrate {channel} button (15 sec)",
            command=lambda name=channel: self._begin_calibration(name),
        )
        calibrate_button.grid(
            row=row,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(3, 8),
        )
        self.calibrate_buttons[channel] = calibrate_button
        row += 1

        row = self._entry_row(
            parent,
            row,
            "Mouse DPI",
            self.dpi_var,
            lock_while_running=False,
        )
        row = self._scale_row(
            parent,
            row,
            f"{channel.title()} click haptics",
            variables["haptic"],
            from_=0.0,
            to=5.0,
            command=lambda value, name=channel: self._set_haptic_value(name, value),
            value_key=("haptic", channel),
            lock_while_running=False,
        )
        haptic_note = self.ttk.Label(parent, text="", foreground="#6b737b")
        haptic_note.grid(
            row=row,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, 4),
        )
        self.haptic_note_labels[channel] = haptic_note
        row += 1

        device_apply_button = self.ttk.Button(
            parent,
            text="Apply mouse settings live",
            command=self._begin_device_apply,
            state="disabled",
        )
        device_apply_button.grid(
            row=row,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(6, 2),
        )
        self.device_apply_buttons.append(device_apply_button)
        row += 1

        advanced_button = self.ttk.Button(
            parent,
            text="Show advanced settings",
            command=lambda name=channel: self._toggle_advanced(name),
        )
        advanced_button.grid(
            row=row,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(10, 4),
        )
        self.advanced_buttons[channel] = advanced_button
        row += 1

        advanced_frame = self.ttk.Frame(parent)
        advanced_frame.grid(row=row, column=0, columnspan=2, sticky="ew")
        advanced_frame.grid_remove()
        advanced_frame.columnconfigure(1, weight=1)
        self.advanced_frames[channel] = advanced_frame
        advanced_row = 0
        advanced_row = self._entry_row(
            advanced_frame,
            advanced_row,
            "Deadzone (%)",
            variables["deadzone"],
        )
        advanced_row = self._combo_row(
            advanced_frame,
            advanced_row,
            "Pressure curve",
            variables["curve"],
            ("linear", "soft", "hard", "scurve"),
        )
        advanced_row = self._scale_row(
            advanced_frame,
            advanced_row,
            "Curve strength",
            variables["curve_strength"],
            from_=CURVE_STRENGTH_MIN,
            to=CURVE_STRENGTH_MAX,
            command=lambda value, name=channel: self._set_curve_strength(name, value),
            value_key=("curve", channel),
        )
        advanced_row = self._combo_row(
            advanced_frame,
            advanced_row,
            "Contact feel",
            variables["contact"],
            ("light", "medium", "firm"),
        )
        advanced_row = self._entry_row(
            advanced_frame,
            advanced_row,
            "Pressure floor (%)",
            variables["pressure_floor"],
        )
        advanced_row = self._entry_row(
            advanced_frame,
            advanced_row,
            "Path stabilization (%)",
            variables["path_stabilization"],
        )
        path_warning = self.ttk.Label(advanced_frame, text="", foreground="#a55400")
        path_warning.grid(
            row=advanced_row,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, 4),
        )
        self.path_warning_labels[channel] = path_warning
        advanced_row += 1
        advanced_row = self._entry_row(
            advanced_frame,
            advanced_row,
            "Pressure influence (%)",
            variables["pressure_influence"],
        )
        onset_buffer = self.ttk.Checkbutton(
            advanced_frame,
            text="Buffer first pressure sample",
            variable=variables["onset_buffer"],
        )
        onset_buffer.grid(
            row=advanced_row,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(4, 0),
        )
        self._setting_widgets.append(onset_buffer)
        advanced_row += 1
        buffer_warning = self.ttk.Label(advanced_frame, text="", foreground="#b42318")
        buffer_warning.grid(
            row=advanced_row,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, 5),
        )
        self.buffer_warning_labels[channel] = buffer_warning
        advanced_row += 1
        advanced_row = self._combo_row(
            advanced_frame,
            advanced_row,
            "Pen injection Hz",
            self.injection_hz_var,
            ("60", "120", "240", "360"),
        )
        teardown = self.ttk.Checkbutton(
            advanced_frame,
            text="Experimental release teardown (may move cursor)",
            variable=self.release_teardown_var,
        )
        teardown.grid(
            row=advanced_row,
            column=0,
            columnspan=2,
            sticky="w",
            pady=2,
        )
        self._setting_widgets.append(teardown)
        row += 1

        suppress = self.ttk.Checkbutton(
            parent,
            text=f"Suppress native {channel} click (required for Krita pressure)",
            variable=variables["suppress"],
        )
        suppress.grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 2))
        self._setting_widgets.append(suppress)

        variables["onset_buffer"].trace_add(
            "write",
            lambda *_args, name=channel: self._update_buffer_warning(name),
        )
        variables["path_stabilization"].trace_add(
            "write",
            lambda *_args, name=channel: self._update_path_warning(name),
        )
        variables["haptic"].trace_add(
            "write",
            lambda *_args, name=channel: self._update_haptic_note(name),
        )
        self._update_buffer_warning(channel)
        self._update_path_warning(channel)
        self._update_haptic_note(channel)

    def _scale_row(
        self,
        parent: Any,
        row: int,
        label: str,
        variable: Any,
        *,
        from_: float,
        to: float,
        command: Any,
        value_key: tuple[str, str],
        lock_while_running: bool = True,
    ) -> int:
        self.ttk.Label(parent, text=label).grid(
            row=row,
            column=0,
            sticky="w",
            padx=(0, 10),
            pady=5,
        )
        control = self.ttk.Frame(parent)
        control.grid(row=row, column=1, sticky="ew", pady=5)
        control.columnconfigure(0, weight=1)
        scale = self.ttk.Scale(
            control,
            variable=variable,
            from_=from_,
            to=to,
            orient="horizontal",
            command=command,
        )
        scale.grid(row=0, column=0, sticky="ew")
        value_label = self.ttk.Label(control, width=4, anchor="e")
        value_label.grid(row=0, column=1, padx=(6, 0))
        kind, channel = value_key
        if kind == "curve":
            self.curve_value_labels[channel] = value_label
            value_label.configure(text=f"{float(variable.get()):.1f}")
        else:
            self.haptic_value_labels[channel] = value_label
            value_label.configure(text=str(round(float(variable.get()))))
        if lock_while_running:
            self._setting_widgets.append(scale)
        return row + 1

    def _set_curve_strength(self, channel: str, value: str) -> None:
        rounded = round(float(value), 1)
        self.channel_vars[channel]["curve_strength"].set(rounded)
        self.curve_value_labels[channel].configure(text=f"{rounded:.1f}")

    def _set_haptic_value(self, channel: str, value: str) -> None:
        rounded = max(0, min(5, round(float(value))))
        self.channel_vars[channel]["haptic"].set(float(rounded))
        self.haptic_value_labels[channel].configure(text=str(rounded))

    def _update_buffer_warning(self, channel: str) -> None:
        enabled = bool(self.channel_vars[channel]["onset_buffer"].get())
        self.buffer_warning_labels[channel].configure(
            text="~16 ms latency increase" if enabled else ""
        )

    def _update_path_warning(self, channel: str) -> None:
        try:
            strength = max(
                0,
                min(100, int(self.channel_vars[channel]["path_stabilization"].get())),
            )
        except ValueError:
            self.path_warning_labels[channel].configure(text="Enter a value from 0 to 100")
            return
        max_lag = 2.0 + strength * 0.12
        self.path_warning_labels[channel].configure(
            text=(
                f"Adds 0 ms buffering; path may trail by up to {max_lag:.1f} px"
                if strength > 0
                else ""
            )
        )

    def _update_haptic_note(self, channel: str) -> None:
        value = round(float(self.channel_vars[channel]["haptic"].get()))
        self.haptic_note_labels[channel].configure(
            text="Haptics off" if value == 0 else ""
        )

    def _selected_channel(self) -> str:
        selected = self.settings_notebook.select()
        return "right" if selected == str(self.channel_tabs["right"]) else "left"

    def _on_channel_tab_changed(self, _event: Any = None) -> None:
        channel = self._selected_channel()
        self.telemetry_label.configure(
            text=(
                "Press Start to see sensitivity mapping"
                if not self.running
                else self.telemetry_label.cget("text")
            )
        )
        self._redraw_sensitivity()

    def _entry_row(
        self,
        parent: Any,
        row: int,
        label: str,
        variable: Any,
        *,
        lock_while_running: bool = True,
    ) -> int:
        self.ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        widget = self.ttk.Entry(parent, textvariable=variable, width=12)
        widget.grid(row=row, column=1, sticky="ew", pady=5)
        if lock_while_running:
            self._setting_widgets.append(widget)
        return row + 1

    def _combo_row(
        self,
        parent: Any,
        row: int,
        label: str,
        variable: Any,
        values: tuple[str, ...],
        *,
        lock_while_running: bool = True,
    ) -> int:
        self.ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        widget = self.ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=12)
        widget.grid(row=row, column=1, sticky="ew", pady=5)
        if lock_while_running:
            self._setting_widgets.append(widget)
        return row + 1

    def _toggle_advanced(self, channel: str) -> None:
        self.advanced_visible[channel] = not self.advanced_visible[channel]
        if self.advanced_visible[channel]:
            self.advanced_frames[channel].grid()
            self.advanced_buttons[channel].configure(text="Hide advanced settings")
        else:
            self.advanced_frames[channel].grid_remove()
            self.advanced_buttons[channel].configure(text="Show advanced settings")

    def _queue_sensitivity_redraw(self, *_args: Any) -> None:
        self.root.after_idle(self._redraw_sensitivity)

    def _redraw_sensitivity(self, _event: Any = None) -> None:
        canvas = self.sensitivity_canvas
        canvas.delete("all")
        width = max(360, canvas.winfo_width())
        height = max(280, canvas.winfo_height())
        left, right, top, bottom = 58, 24, 32, 48
        plot_width = width - left - right
        plot_height = height - top - bottom
        colors = self.theme_colors or {
            "axis": "#68747f",
            "grid": "#dce2e7",
            "foreground": "#3e4953",
            "curve": "#1677b8",
            "marker": "#f5a623",
            "marker_text": "#7a4800",
        }
        axis_color = colors["axis"]
        grid_color = colors["grid"]
        text_color = colors["foreground"]
        curve_color = colors["curve"]

        try:
            settings = self._collect_settings()
            points = sensitivity_mapping_points(settings)
        except Exception as exc:
            canvas.create_text(
                width / 2,
                height / 2,
                text=f"Fix the settings to preview the mapping\n{exc}",
                fill="#a33",
                justify="center",
                font=("Segoe UI", 10),
            )
            return

        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = top + plot_height * (1.0 - fraction)
            canvas.create_line(left, y, width - right, y, fill=grid_color)
            canvas.create_text(
                left - 9,
                y,
                text=f"{round(fraction * 100)}%",
                anchor="e",
                fill=text_color,
                font=("Segoe UI", 8),
            )
        canvas.create_line(left, top, left, height - bottom, fill=axis_color, width=1)
        canvas.create_line(
            left,
            height - bottom,
            width - right,
            height - bottom,
            fill=axis_color,
            width=1,
        )

        raw_span = settings.raw_max - settings.raw_min

        def x_for(raw: int) -> float:
            return left + (raw - settings.raw_min) / raw_span * plot_width

        def y_for(pressure: int) -> float:
            return top + (1.0 - pressure / 1024.0) * plot_height

        coordinates: list[float] = []
        for raw, pressure in points:
            coordinates.extend((x_for(raw), y_for(pressure)))
        canvas.create_line(
            *coordinates,
            fill=curve_color,
            width=3,
            smooth=True,
            splinesteps=16,
        )

        for raw, anchor in (
            (settings.raw_min, "w"),
            (round((settings.raw_min + settings.raw_max) / 2), "center"),
            (settings.raw_max, "e"),
        ):
            canvas.create_text(
                x_for(raw),
                height - bottom + 18,
                text=str(raw),
                anchor=anchor,
                fill=text_color,
                font=("Cascadia Mono", 8),
            )

        curve_title = f"{settings.curve} · strength {settings.curve_strength:g}"
        canvas.create_text(
            width - right,
            14,
            text=curve_title,
            anchor="e",
            fill=text_color,
            font=("Segoe UI Semibold", 9),
        )

        channel = self._selected_channel()
        latest_raw = self._latest_raw[channel]
        if latest_raw is not None:
            raw = max(settings.raw_min, min(settings.raw_max, latest_raw))
            pressure = effective_pressure_for_raw(settings, raw)
            x, y = x_for(raw), y_for(pressure)
            canvas.create_line(x, y, x, height - bottom, fill=colors["marker"], dash=(4, 3))
            canvas.create_line(left, y, x, y, fill=colors["marker"], dash=(4, 3))
            canvas.create_oval(
                x - 6,
                y - 6,
                x + 6,
                y + 6,
                fill=colors["marker"],
                outline=colors["marker_text"],
                width=2,
            )
            canvas.create_text(
                x,
                max(top + 12, y - 16),
                text=f"{round(pressure / 1024 * 100)}%",
                anchor="s",
                fill=colors["marker_text"],
                font=("Segoe UI Semibold", 9),
            )

    def _collect_settings(self, channel: str | None = None) -> DevSettings:
        channel = self._selected_channel() if channel is None else channel
        variables = self.channel_vars[channel]
        return parse_dev_settings(
            raw_min=variables["raw_min"].get(),
            raw_max=variables["raw_max"].get(),
            deadzone=variables["deadzone"].get(),
            curve=variables["curve"].get(),
            curve_strength=str(variables["curve_strength"].get()),
            contact_preset=variables["contact"].get(),
            suppress_lmb=variables["suppress"].get(),
            release_teardown=self.release_teardown_var.get(),
            onset_buffer=variables["onset_buffer"].get(),
            pressure_floor=variables["pressure_floor"].get(),
            path_stabilization=variables["path_stabilization"].get(),
            pressure_influence=variables["pressure_influence"].get(),
            injection_hz=self.injection_hz_var.get(),
        )

    def _apply_settings(self) -> bool:
        if self.running:
            self._write_system("Stop the bridge before changing settings.", level="WARN")
            return False
        try:
            left = self._collect_settings("left")
            right = self._collect_settings("right")
            self.service.apply_config(
                {
                    "linked": False,
                    "suppress_lmb": left.suppress_lmb,
                    "suppress_rmb": right.suppress_lmb,
                    "release_teardown": self.release_teardown_var.get(),
                    "left": left.as_runtime_patch()["left"],
                    "right": right.as_runtime_patch()["left"],
                }
            )
            self.service.launch_config.hz = left.injection_hz
        except Exception as exc:
            self._write_system(f"Settings error: {exc}", level="ERROR")
            return False
        self._write_system("Settings saved.")
        return True

    def _toggle(self) -> None:
        if self.busy:
            return
        if self.running:
            self._begin_stop()
        elif self._apply_settings():
            self._begin_start()

    def _begin_start(self) -> None:
        self.busy = True
        self.toggle_button.configure(text="Starting…", state="disabled")
        self.status_label.configure(text="● Starting", foreground="#b78103")
        self._watch_future("started", self.controller.start())

    def _begin_stop(self) -> None:
        self.busy = True
        self.toggle_button.configure(text="Stopping…", state="disabled")
        self.status_label.configure(text="● Stopping", foreground="#b78103")
        self._watch_future("stopped", self.controller.stop())

    def _begin_calibration(self, channel: str) -> None:
        if self.busy:
            return
        if not self.running and not self._apply_settings():
            return
        self.busy = True
        self._active_calibration_channel = channel
        self._last_calibration_phase = "prepare"
        self.toggle_button.configure(state="disabled")
        self.calibrate_buttons[channel].configure(text="Calibrating…", state="disabled")
        self.status_label.configure(text="● Calibration: release button", foreground="#b78103")
        self._show_calibration_prompt(
            phase="prepare",
            instruction=f"Release the {channel} button and get ready.",
            value=0,
        )
        self._write_system(
            f"{channel.title()} calibration starting: release the button, then hold a light press, "
            "then a firm comfortable press when prompted."
        )
        self._watch_future(
            "calibrated",
            self.controller.calibrate(
                channel,
                lambda event: self.events.put(("calibration_progress", event)),
            ),
        )

    def _show_calibration_prompt(
        self,
        *,
        phase: str,
        instruction: str,
        value: int,
        next_phase: str = "",
        countdown: int = 0,
    ) -> None:
        titles = {
            "prepare": "Get ready",
            "idle": "Step 1 of 3 — Release",
            "light": "Step 2 of 3 — Light press",
            "heavy": "Step 3 of 3 — Firm press",
            "done": "Calibration complete",
        }
        if phase == "countdown":
            upcoming_title = titles.get(next_phase, "Next step")
            self.calibration_title.configure(
                text=f"{upcoming_title} starts in {countdown}…"
            )
            self.calibration_instruction.configure(text=f"Get ready: {instruction}")
            self.calibration_value.configure(text="Sampling has not started yet")
        else:
            self.calibration_title.configure(text=titles.get(phase, "Calibration"))
            active_instruction = (
                f"NOW — {instruction}" if phase in {"idle", "light", "heavy"} else instruction
            )
            self.calibration_instruction.configure(text=active_instruction)
            if phase in {"idle", "light", "heavy"}:
                detail = f"Sampling now · raw value: {value}"
            elif phase == "done":
                detail = "The measured range is being saved"
            else:
                detail = "A countdown will appear before every sampling step"
            self.calibration_value.configure(text=detail)
        if not self.calibration_frame.winfo_ismapped():
            self.calibration_frame.pack(
                fill="x",
                pady=(0, 12),
                before=self.body,
            )

    def _begin_device_apply(self) -> None:
        if not self.running:
            self._write_system("Start the bridge before applying mouse settings.", level="WARN")
            return
        try:
            dpi = int(self.dpi_var.get())
            left = round(float(self.channel_vars["left"]["haptic"].get()))
            right = round(float(self.channel_vars["right"]["haptic"].get()))
            if not 100 <= dpi <= 32000 or dpi % 50 != 0:
                raise ValueError("DPI must be 100..32000 in 50-DPI increments.")
            if not 0 <= left <= 5 or not 0 <= right <= 5:
                raise ValueError("Haptics must be between 0 and 5.")
        except ValueError as exc:
            self._write_system(f"Mouse settings error: {exc}", level="ERROR")
            return

        for button in self.device_apply_buttons:
            button.configure(text="Applying…", state="disabled")
        self._watch_future(
            "device_settings_applied",
            self.controller.apply_device_settings(
                dpi=dpi,
                haptic_left=left,
                haptic_right=right,
            ),
        )

    def _watch_future(self, event_name: str, future: Future[Any]) -> None:
        def done(completed: Future[Any]) -> None:
            try:
                result = completed.result()
            except Exception as exc:
                self.events.put((f"{event_name}_error", exc))
            else:
                self.events.put((event_name, result))

        future.add_done_callback(done)

    def _set_running(self, running: bool) -> None:
        self.running = running
        self.busy = False
        self.calibration_frame.pack_forget()
        self._last_calibration_phase = ""
        state = "disabled" if running else "normal"
        for widget in self._setting_widgets:
            if isinstance(widget, self.ttk.Combobox):
                widget.configure(state="disabled" if running else "readonly")
            else:
                widget.configure(state=state)
        self.toggle_button.configure(
            text="Stop" if running else "Start",
            state="normal",
        )
        self.status_label.configure(
            text="● Running" if running else "● Stopped",
            foreground="#238636" if running else "#a33",
        )
        for button in self.device_apply_buttons:
            button.configure(
                text="Apply mouse settings live",
                state="normal" if running else "disabled",
            )
        for channel, button in self.calibrate_buttons.items():
            button.configure(
                text=f"Calibrate {channel} button (15 sec)",
                state="normal",
            )
        if not running:
            self.telemetry_label.configure(text="Press Start to see sensitivity mapping")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._write_log(payload)
                elif kind == "telemetry":
                    self._latest_raw["left"] = int(payload["left_raw"])
                    self._latest_raw["right"] = int(payload["right_raw"])
                    channel = self._selected_channel()
                    raw = int(payload[f"{channel}_raw"])
                    mapped = int(payload[f"{channel}_mapped"])
                    try:
                        effective = effective_pressure_for_raw(
                            self._collect_settings(channel),
                            raw,
                        )
                        effective_text = f"effective {effective / 1024:.0%}"
                    except Exception:
                        effective_text = "effective —"
                    self.telemetry_label.configure(
                        text=(
                            f"{channel.title()} raw {raw:3d}  ·  "
                            f"mapped {mapped / 1024:.0%}  ·  "
                            f"{effective_text}  ·  {payload['hz']:4.1f} Hz"
                        )
                    )
                    self._redraw_sensitivity()
                elif kind == "started":
                    self._set_running(True)
                elif kind == "stopped":
                    self._set_running(False)
                elif kind == "device_settings_applied":
                    for button in self.device_apply_buttons:
                        button.configure(text="Apply mouse settings live", state="normal")
                    self._write_system(
                        f"Mouse settings applied: {payload['dpi']} DPI, "
                        f"haptics L{payload['haptic_left']}/R{payload['haptic_right']}."
                    )
                elif kind == "calibration_progress":
                    phase = str(payload.get("phase", ""))
                    instruction = str(payload.get("instruction", phase))
                    value = int(payload.get("value", 0))
                    next_phase = str(payload.get("next_phase", ""))
                    countdown = int(payload.get("countdown", 0))
                    self.status_label.configure(
                        text=(
                            f"● Calibration: {next_phase} in {countdown}"
                            if phase == "countdown"
                            else f"● Calibration: {phase}"
                        ),
                        foreground="#b78103",
                    )
                    self._show_calibration_prompt(
                        phase=phase,
                        instruction=instruction,
                        value=value,
                        next_phase=next_phase,
                        countdown=countdown,
                    )
                    self._last_calibration_phase = phase
                elif kind == "calibrated":
                    channel_name = self._active_calibration_channel
                    channel = payload[channel_name]
                    self.channel_vars[channel_name]["raw_min"].set(str(channel["raw_min"]))
                    self.channel_vars[channel_name]["raw_max"].set(str(channel["raw_max"]))
                    self._set_running(self.service.stream_active)
                    self._write_system(
                        f"{channel_name.title()} calibration saved: "
                        f"raw {channel['raw_min']}–{channel['raw_max']}."
                    )
                elif kind == "runtime_error":
                    self._set_running(False)
                    self._write_system(str(payload), level="ERROR")
                elif kind.endswith("_error"):
                    self._set_running(self.service.stream_active)
                    self._write_system(f"Bridge error: {payload}", level="ERROR")
                elif kind == "closed":
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(50, self._drain_events)

    def _write_log(self, entry: LogEntry) -> None:
        import datetime

        stamp = datetime.datetime.fromtimestamp(entry.ts / 1000).strftime("%H:%M:%S")
        self._append_terminal(f"{stamp} {entry.level:<5} {entry.msg}\n", entry.level)

    def _write_system(self, message: str, *, level: str = "SYSTEM") -> None:
        self._append_terminal(f"> {message}\n", level)

    def _append_terminal(self, text: str, tag: str) -> None:
        self.terminal.configure(state="normal")
        self.terminal.insert("end", text, tag)
        self.terminal.see("end")
        self.terminal.configure(state="disabled")

    def _clear_terminal(self) -> None:
        self.terminal.configure(state="normal")
        self.terminal.delete("1.0", "end")
        self.terminal.configure(state="disabled")

    def _on_close(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.toggle_button.configure(state="disabled")
        self.status_label.configure(text="● Closing", foreground="#b78103")

        def close_runtime() -> None:
            try:
                self.controller.close()
            finally:
                self.events.put(("closed", None))

        threading.Thread(target=close_runtime, name="superstrike-dev-close", daemon=True).start()


def main() -> int:
    if sys.platform != "win32":
        print("ERROR: the Superstrike dev control panel is Windows-only.")
        return 1

    import tkinter as tk

    log_bus = LogBus(maxlen=1000)
    try:
        service = RuntimeService(
            launch_config=LaunchConfig(trace_dir="work/stroke_traces"),
            config_store=ConfigStore(),
            log_bus=log_bus,
        )
        controller = BridgeController(service)
    except Exception as exc:
        print(f"ERROR: could not initialize dev control panel: {exc}")
        return 1

    root = tk.Tk()
    DevPanel(root, service, controller, log_bus)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
