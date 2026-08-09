"""Small desktop control panel for developing the pressure bridge."""

from __future__ import annotations

import asyncio
import ctypes
import json
import math
import queue
import sys
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from superstrike_pressure.bridge.config import LaunchConfig, RuntimeConfig
from superstrike_pressure.bridge.curves import PressureConfig, map_pressure, normalize_curve_name
from superstrike_pressure.bridge.tablet_emitter import enumerate_vmulti_candidates
from superstrike_pressure.web.config_store import ConfigStore
from superstrike_pressure.web.log_bus import LogBus, LogEntry
from superstrike_pressure.web.models import CURVE_STRENGTH_MAX, CURVE_STRENGTH_MIN
from superstrike_pressure.web.runtime_service import RuntimeService


def _asset_path(name: str) -> Path:
    return Path(__file__).resolve().parent / "assets" / name


class _TrayController:
    """Small lazy-loaded notification-area wrapper for the Tk control panel."""

    def __init__(self, image_path: Path) -> None:
        self.image_path = image_path
        self._icon: Any | None = None
        self._thread: threading.Thread | None = None

    def show(self, on_show: Any, on_quit: Any) -> bool:
        if self._icon is not None:
            return True
        try:
            import pystray
            from PIL import Image

            image = Image.open(self.image_path).convert("RGBA")
            icon = pystray.Icon(
                "superstrike_pressure",
                image,
                "Superstrike Pressure",
                menu=pystray.Menu(
                    pystray.MenuItem(
                        "Show",
                        lambda _icon, _item: on_show(),
                        default=True,
                    ),
                    pystray.MenuItem(
                        "Quit",
                        lambda _icon, _item: on_quit(),
                    ),
                ),
            )
        except Exception:
            return False
        self._icon = icon
        self._thread = threading.Thread(
            target=icon.run,
            name="superstrike-tray",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        icon = self._icon
        self._icon = None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass


class _StartHotkeyListener:
    """Register Ctrl+F12 independently of focus while the control panel runs."""

    HOTKEY_ID = 0x5354
    WM_HOTKEY = 0x0312
    PM_REMOVE = 0x0001
    MOD_CONTROL = 0x0002
    VK_F12 = 0x7B

    def __init__(self, callback: Any) -> None:
        self._callback = callback
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._registered = False
        self._thread = threading.Thread(
            target=self._run,
            name="superstrike-start-hotkey",
            daemon=True,
        )

    def start(self) -> bool:
        self._thread.start()
        self._ready.wait(timeout=1.0)
        return self._registered

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._registered = bool(
            user32.RegisterHotKey(
                None,
                self.HOTKEY_ID,
                self.MOD_CONTROL,
                self.VK_F12,
            )
        )
        self._ready.set()
        if not self._registered:
            return
        message = wintypes.MSG()
        try:
            while not self._stop.is_set():
                while user32.PeekMessageW(
                    ctypes.byref(message),
                    None,
                    0,
                    0,
                    self.PM_REMOVE,
                ):
                    if (
                        int(message.message) == self.WM_HOTKEY
                        and int(message.wParam) == self.HOTKEY_ID
                    ):
                        self._callback()
                        continue
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
                time.sleep(0.01)
        finally:
            user32.UnregisterHotKey(None, self.HOTKEY_ID)


def _ui_percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[round((len(ordered) - 1) * fraction)])


def _ui_geometry_injections(
    injected: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Remove intentional stationary-dab loops from stroke geometry."""
    cleaned: list[dict[str, Any]] = []
    removed = 0
    for event in injected:
        if event.get("tag") == "stationary_contact":
            removed += 1
            continue
        cleaned.append(event)
        while len(cleaned) >= 3:
            first, middle, last = cleaned[-3:]
            excursion = math.hypot(
                int(middle["x"]) - int(first["x"]),
                int(middle["y"]) - int(first["y"]),
            )
            if (
                (first["x"], first["y"]) == (last["x"], last["y"])
                and 0.0 < excursion <= 1.01
            ):
                cleaned.pop()
                cleaned.pop()
                removed += 2
            else:
                break
    return cleaned, removed


def stroke_analysis_data(payload: dict[str, Any]) -> dict[str, Any]:
    """Build compact metrics and graph series for the desktop analyzer."""
    events = list(payload.get("events", []))
    metadata = dict(payload.get("metadata", {}))
    updates = [event for event in events if event.get("kind") == "update"]
    fresh = [event for event in updates if event.get("pressure_fresh")]
    motions = [event for event in events if event.get("kind") == "motion"]
    injected = [
        event
        for event in events
        if event.get("kind") == "inject"
        and event.get("ok")
        and int(event.get("flags", 0)) & 0x00000004
    ]
    geometry, stationary_points = _ui_geometry_injections(injected)
    raw_key = "right_raw" if metadata.get("button") == "right" else "left_raw"
    raw_series = [
        (float(event.get("t_ms", 0.0)), float(event[raw_key]))
        for event in fresh
        if event.get(raw_key) is not None
    ]
    mapped_series = [
        (float(event.get("t_ms", 0.0)), float(event.get("mapped", 0)))
        for event in fresh
    ]
    interpolated_series = [
        (
            float(event.get("t_ms", 0.0)),
            float(event.get("actual_pressure", event.get("interpolated_mapped", 0))),
        )
        for event in updates
    ]
    injected_time = [
        (float(event.get("t_ms", 0.0)), float(event.get("pressure", 0)))
        for event in injected
    ]
    injected_distance: list[tuple[float, float]] = []
    distance = 0.0
    previous: dict[str, Any] | None = None
    for event in geometry:
        if previous is not None:
            distance += math.hypot(
                int(event["x"]) - int(previous["x"]),
                int(event["y"]) - int(previous["y"]),
            )
        injected_distance.append((distance, float(event.get("pressure", 0))))
        previous = event

    motion_distances = [
        math.hypot(
            int(current["x"]) - int(previous_event["x"]),
            int(current["y"]) - int(previous_event["y"]),
        )
        for previous_event, current in zip(motions, motions[1:])
    ]
    motion_duration = (
        float(motions[-1].get("t_ms", 0.0)) - float(motions[0].get("t_ms", 0.0))
        if len(motions) >= 2
        else 0.0
    )
    motion_hz = (
        (len(motions) - 1) * 1000.0 / motion_duration
        if motion_duration > 0.0
        else 0.0
    )
    pressure_steps = [
        abs(float(current.get("pressure", 0)) - float(previous_event.get("pressure", 0)))
        for previous_event, current in zip(geometry, geometry[1:])
    ]
    mapped_steps = [
        abs(float(current.get("mapped", 0)) - float(previous_event.get("mapped", 0)))
        for previous_event, current in zip(fresh, fresh[1:])
    ]
    max_pressure_step = max(pressure_steps, default=0.0)
    p95_pressure_step = _ui_percentile(pressure_steps, 0.95)
    max_mapped_step = max(mapped_steps, default=0.0)
    p95_motion_segment = _ui_percentile(motion_distances, 0.95)
    true_low_latency = bool(metadata.get("true_low_latency", False))
    if max_pressure_step > 128 and true_low_latency:
        diagnosis = (
            "Pressure steps reach the pen unchanged because True low latency "
            "disables pressure interpolation."
        )
    elif max_pressure_step > 128:
        diagnosis = "Large pressure steps exist before Krita receives the stroke."
    elif motion_hz and motion_hz < 90 and p95_motion_segment > 12:
        diagnosis = "Position anchors are sparse enough to make fast curves angular."
    else:
        diagnosis = "The injected path and pressure are comparatively smooth."

    return {
        "metadata": metadata,
        "diagnosis": diagnosis,
        "motion_hz": motion_hz,
        "p95_motion_segment": p95_motion_segment,
        "p95_pressure_step": p95_pressure_step,
        "max_pressure_step": max_pressure_step,
        "max_mapped_step": max_mapped_step,
        "path_px": distance,
        "stationary_dab_points": stationary_points,
        "raw": raw_series,
        "mapped": mapped_series,
        "interpolated": interpolated_series,
        "injected_time": injected_time,
        "injected_distance": injected_distance,
    }


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
    true_low_latency: bool = False
    stationary_pressure_updates: bool = False
    rapid_release_threshold: int = 0
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
            "true_low_latency": self.true_low_latency,
            "stationary_pressure_updates": self.stationary_pressure_updates,
            "rapid_release_threshold": self.rapid_release_threshold,
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
    true_low_latency: bool = False,
    stationary_pressure_updates: bool = False,
    rapid_release_threshold: str = "0",
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
            true_low_latency=bool(true_low_latency),
            stationary_pressure_updates=bool(stationary_pressure_updates),
            rapid_release_threshold=int(rapid_release_threshold),
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
    if not 0 <= parsed.rapid_release_threshold <= 30:
        raise ValueError("Rapid release threshold must be between 0 and 30 percent.")
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
    apply_pressure_shaping: bool = True,
) -> list[tuple[int, int]]:
    """Return raw ADC to effective pen-pressure points for the live graph."""
    count = max(2, int(samples))
    points: list[tuple[int, int]] = []
    for index in range(count):
        raw = round(
            settings.raw_min
            + (settings.raw_max - settings.raw_min) * index / (count - 1)
        )
        points.append(
            (
                raw,
                (
                    effective_pressure_for_raw(settings, raw)
                    if apply_pressure_shaping
                    else curve_pressure_for_raw(settings, raw)
                ),
            )
        )
    return points


def curve_pressure_for_raw(settings: DevSettings, raw: int) -> int:
    """Map one raw ADC sample through calibration, deadzone, and curve."""
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
    return max(0, min(1024, int(map_pressure(raw, pressure_config))))


def effective_pressure_for_raw(settings: DevSettings, raw: int) -> int:
    """Map one raw ADC sample through the settings that affect brush size."""
    floor = round(settings.pressure_floor * 1024 / 100)
    mapped = curve_pressure_for_raw(settings, raw)
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

    def start(self, *, device_settings: dict[str, int] | None = None) -> Future[None]:
        operation = (
            self.service.start_stream()
            if device_settings is None
            else self.service.start_stream(device_settings=device_settings)
        )
        return asyncio.run_coroutine_threadsafe(
            operation,
            self._loop,
        )

    def stop(self) -> Future[None]:
        return asyncio.run_coroutine_threadsafe(self.service.stop_stream(), self._loop)

    def detect_device_settings(self) -> Future[dict[str, int]]:
        return asyncio.run_coroutine_threadsafe(
            self.service.detect_device_settings(),
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
        from tkinter import messagebox, scrolledtext, ttk

        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.root = root
        self.service = service
        self.controller = controller
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.running = False
        self.busy = False
        self.detecting_device_settings = True
        self._setting_widgets: list[Any] = []
        self._closing = False
        self._tray_hidden = False
        self._tray = _TrayController(_asset_path("lucide_mouse.png"))
        self.advanced_visible = {"left": False, "right": False}
        self._latest_raw: dict[str, int | None] = {"left": None, "right": None}

        root.title("Superstrike Pressure")
        root.geometry("1080x780")
        root.minsize(920, 680)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.bind("<Unmap>", self._on_window_unmapped, add="+")
        try:
            root.iconbitmap(default=str(_asset_path("lucide_mouse.ico")))
            self._window_icon = tk.PhotoImage(
                file=str(_asset_path("lucide_mouse.png"))
            )
            root.iconphoto(True, self._window_icon)
        except Exception:
            self._window_icon = None

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
        self.status_label = ttk.Label(
            header,
            text="● Detecting mouse",
            style="Status.TLabel",
            foreground="#b78103",
        )
        self.status_label.pack(side="left")
        ttk.Label(
            header,
            text="Start: Ctrl+F12  ·  Stop: Ctrl+Shift+F12",
        ).pack(side="left", padx=18)
        self.toggle_button = ttk.Button(
            header,
            text="Start",
            style="Primary.TButton",
            command=self._toggle,
            state="disabled",
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
                # Retain these compatibility variables for older profiles and
                # the runtime schema, but keep the temporarily removed UI
                # features disabled.
                "onset_buffer": tk.BooleanVar(value=False),
                "true_low_latency": tk.BooleanVar(value=False),
                "stationary_pressure_updates": tk.BooleanVar(value=False),
                "rapid_release_threshold": tk.StringVar(
                    value=str(
                        getattr(channel_config, "rapid_release_threshold", 0)
                    )
                ),
                "suppress": tk.BooleanVar(
                    value=(
                        config.suppress_lmb
                        if channel_name == "left"
                        else getattr(config, "suppress_rmb", False)
                    )
                ),
                "haptic": tk.DoubleVar(
                    value=float(
                        config.session_haptic_left
                        if channel_name == "left"
                        else config.session_haptic_right
                    )
                ),
            }
        self.injection_hz_var = tk.StringVar(value=f"{service.launch_config.hz:g}")
        self.output_backend_var = tk.StringVar(value=service.launch_config.backend)
        self.dpi_var = tk.StringVar(value=str(config.session_dpi))
        self.follow_normal_device_settings = bool(
            getattr(config, "session_device_settings_follow_normal", True)
        )
        self.normal_dpi_var = tk.StringVar(value="Detecting…")
        self.normal_haptic_vars = {
            "left": tk.StringVar(value="—"),
            "right": tk.StringVar(value="—"),
        }
        self.left_pressure_enabled_var = tk.BooleanVar(
            value=getattr(config, "left_enabled", True)
        )
        self.right_pressure_enabled_var = tk.BooleanVar(
            value=getattr(config, "right_enabled", False)
        )
        self.linked_pressure_var = tk.BooleanVar(
            value=(
                bool(config.linked)
                and self.left_pressure_enabled_var.get()
                and self.right_pressure_enabled_var.get()
            )
        )
        self.rmb_aux_xtilt_var = tk.BooleanVar(
            value=getattr(config, "rmb_aux_xtilt", False)
        )
        self.debug_mode_var = tk.BooleanVar(
            value=getattr(config, "debug_mode", True)
        )
        self.minimize_to_tray_var = tk.BooleanVar(
            value=getattr(config, "minimize_to_tray", True)
        )
        self.release_teardown_var = tk.BooleanVar(value=config.release_teardown)
        self.advanced_buttons: dict[str, Any] = {}
        self.advanced_frames: dict[str, Any] = {}
        self.path_warning_labels: dict[str, Any] = {}
        self.haptic_note_labels: dict[str, Any] = {}
        self.curve_value_labels: dict[str, Any] = {}
        self.haptic_value_labels: dict[str, Any] = {}
        self.normal_device_value_labels: list[Any] = []
        self._refreshing_settings_scrollregion = False

        self.settings_canvas = tk.Canvas(
            settings_frame,
            borderwidth=0,
            highlightthickness=0,
        )
        settings_scrollbar = ttk.Scrollbar(
            settings_frame,
            orient="vertical",
            command=self.settings_canvas.yview,
        )
        self.settings_canvas.configure(yscrollcommand=settings_scrollbar.set)
        self.settings_canvas.grid(row=0, column=0, sticky="nsew")
        settings_scrollbar.grid(row=0, column=1, sticky="ns")
        self.settings_content = ttk.Frame(self.settings_canvas)
        self._settings_canvas_window = self.settings_canvas.create_window(
            (0, 0),
            window=self.settings_content,
            anchor="nw",
        )
        self.settings_content.bind(
            "<Configure>",
            lambda _event: self._refresh_settings_scrollregion(),
        )
        self.settings_canvas.bind("<Configure>", self._resize_settings_content)
        self.root.bind_all("<MouseWheel>", self._on_settings_mousewheel, add="+")

        self.mouse_hardware_frame = ttk.LabelFrame(
            self.settings_content,
            text="Mouse hardware",
            padding=(8, 6),
        )
        self.mouse_hardware_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._build_mouse_hardware_settings(self.mouse_hardware_frame)

        self.pressure_options_frame = ttk.LabelFrame(
            self.settings_content,
            text="Pressure buttons",
            padding=(8, 6),
        )
        self.pressure_options_frame.grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(0, 10),
        )
        self._build_pressure_options(self.pressure_options_frame)

        self.settings_notebook = ttk.Notebook(self.settings_content)
        self.settings_notebook.grid(row=2, column=0, sticky="nsew")
        self.channel_tabs: dict[str, Any] = {}
        for channel_name, tab_title in (("left", "Left button"), ("right", "Right button")):
            tab = ttk.Frame(self.settings_notebook, padding=(8, 10))
            self.channel_tabs[channel_name] = tab
            self.settings_notebook.add(tab, text=tab_title)
            self._build_channel_settings_tab(tab, channel_name)
        self.settings_notebook.bind("<<NotebookTabChanged>>", self._on_channel_tab_changed)

        self.backend_advanced_visible = False
        self.backend_advanced_container = ttk.Frame(self.settings_content)
        self.backend_advanced_container.grid(
            row=3,
            column=0,
            sticky="ew",
            pady=(10, 0),
        )
        self.backend_advanced_container.columnconfigure(0, weight=1)
        self.backend_advanced_button = ttk.Button(
            self.backend_advanced_container,
            text="Show advanced backend settings",
            command=self._toggle_backend_advanced,
        )
        self.backend_advanced_button.grid(row=0, column=0, sticky="ew")

        self.backend_advanced_frame = ttk.LabelFrame(
            self.backend_advanced_container,
            text="Advanced backend settings",
            padding=(8, 6),
        )
        self.backend_advanced_frame.grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(6, 0),
        )
        self.backend_advanced_frame.grid_remove()
        self._build_backend_advanced_settings(self.backend_advanced_frame)

        app_behavior = ttk.Frame(self.settings_content)
        app_behavior.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        minimize_to_tray = ttk.Checkbutton(
            app_behavior,
            text="Minimize to tray",
            variable=self.minimize_to_tray_var,
        )
        minimize_to_tray.pack(side="left")

        settings_actions = ttk.Frame(self.settings_content)
        settings_actions.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        settings_actions.columnconfigure(0, weight=1)
        settings_actions.columnconfigure(1, weight=1)
        self.apply_button = ttk.Button(
            settings_actions,
            text="Save settings",
            command=self._apply_all_settings,
        )
        self.apply_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.restore_defaults_button = ttk.Button(
            settings_actions,
            text="Restore defaults",
            command=self._confirm_restore_defaults,
        )
        self.restore_defaults_button.grid(
            row=0,
            column=1,
            sticky="ew",
            padx=(5, 0),
        )
        settings_frame.rowconfigure(0, weight=1)
        settings_frame.columnconfigure(0, weight=1)
        self.settings_content.columnconfigure(0, weight=1)

        self.output_notebook = ttk.Notebook(output_frame)
        self.output_notebook.pack(fill="both", expand=True)
        visualizer_tab = ttk.Frame(self.output_notebook, padding=12)
        analysis_tab = ttk.Frame(self.output_notebook, padding=10)
        terminal_tab = ttk.Frame(self.output_notebook, padding=8)
        self.analysis_tab = analysis_tab
        self.terminal_tab = terminal_tab
        self.output_notebook.add(visualizer_tab, text="Sensitivity mapping")
        self.output_notebook.add(analysis_tab, text="Stroke analysis")
        self.output_notebook.add(terminal_tab, text="Terminal output")
        self.output_notebook.bind("<<NotebookTabChanged>>", self._on_output_tab_changed)

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

        analysis_toolbar = ttk.Frame(analysis_tab)
        analysis_toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(analysis_toolbar, text="Recent stroke").pack(side="left")
        self.stroke_trace_var = tk.StringVar(value="")
        self.stroke_selector = ttk.Combobox(
            analysis_toolbar,
            textvariable=self.stroke_trace_var,
            state="readonly",
            width=34,
        )
        self.stroke_selector.pack(side="left", fill="x", expand=True, padx=(8, 6))
        self.stroke_selector.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._load_selected_stroke(),
        )
        ttk.Button(
            analysis_toolbar,
            text="Refresh",
            command=lambda: self._refresh_stroke_list(select_latest=False),
        ).pack(side="right")
        self.stroke_analysis_summary = ttk.Label(
            analysis_tab,
            text="Draw a stroke with Debug mode on, then choose it here.",
            wraplength=620,
            justify="left",
            font=("Segoe UI", 9),
        )
        self.stroke_analysis_summary.pack(fill="x", pady=(0, 8))
        self.stroke_analysis_canvas = tk.Canvas(
            analysis_tab,
            background="#f7f9fb",
            highlightthickness=1,
            highlightbackground="#c8d0d8",
            borderwidth=0,
        )
        self.stroke_analysis_canvas.pack(fill="both", expand=True)
        self.stroke_analysis_canvas.bind(
            "<Configure>",
            lambda _event: self._draw_stroke_analysis(),
        )
        self._stroke_trace_paths: dict[str, Path] = {}
        self._stroke_analysis: dict[str, Any] | None = None

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
        service.set_force_stop_callback(
            lambda message: self.events.put(("force_stopped", message))
        )
        self._write_system("Ready. Settings are saved in ~/.superstrike/config.json")
        self.start_hotkey = _StartHotkeyListener(
            lambda: self.events.put(("start_hotkey", None))
        )
        if not self.start_hotkey.start():
            self._write_system(
                "Ctrl+F12 could not be registered; use the Start button instead.",
                level="WARN",
            )
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
        self.rmb_aux_xtilt_var.trace_add("write", self._queue_sensitivity_redraw)
        self.output_backend_var.trace_add(
            "write",
            lambda *_args: self._update_backend_setting_visibility(),
        )
        for key in (
            "raw_min",
            "raw_max",
            "deadzone",
            "curve",
            "curve_strength",
            "contact",
            "pressure_floor",
            "path_stabilization",
            "pressure_influence",
            "onset_buffer",
            "true_low_latency",
            "stationary_pressure_updates",
            "rapid_release_threshold",
        ):
            self.channel_vars["left"][key].trace_add(
                "write",
                lambda *_args, setting=key: self._mirror_linked_setting(setting),
            )
        self._apply_theme()
        self._update_backend_setting_visibility()
        self._on_pressure_options_changed()
        root.after_idle(self._redraw_sensitivity)
        root.after_idle(self._begin_device_detection)
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
                "terminal_foreground": "#e8edf2",
                "terminal_info": "#d7e0e7",
                "terminal_system": "#79c0ff",
                "terminal_warn": "#e3b341",
                "terminal_error": "#ff7b72",
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
                "terminal": "#ffffff",
                "terminal_foreground": "#20262c",
                "terminal_info": "#303840",
                "terminal_system": "#0067a8",
                "terminal_warn": "#8a5700",
                "terminal_error": "#b42318",
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
        self.stroke_analysis_canvas.configure(
            background=colors["canvas"],
            highlightbackground=colors["border"],
        )
        self.settings_canvas.configure(background=colors["background"])
        self.mapping_caption.configure(foreground=colors["muted"])
        self.stroke_analysis_summary.configure(foreground=colors["muted"])
        self.terminal.configure(
            background=colors["terminal"],
            foreground=colors["terminal_foreground"],
            insertbackground=colors["terminal_foreground"],
        )
        self.terminal.tag_configure("INFO", foreground=colors["terminal_info"])
        self.terminal.tag_configure("SYSTEM", foreground=colors["terminal_system"])
        self.terminal.tag_configure("WARN", foreground=colors["terminal_warn"])
        self.terminal.tag_configure("ERROR", foreground=colors["terminal_error"])
        for label in self.haptic_note_labels.values():
            label.configure(foreground=colors["muted"])
        for label in self.normal_device_value_labels:
            label.configure(foreground=colors["muted"])
        for label in self.path_warning_labels.values():
            label.configure(foreground="#f0a43a" if dark else "#a55400")
        self._draw_stroke_analysis()

        self._redraw_sensitivity()

    def _build_pressure_options(self, parent: Any) -> None:
        left = self.ttk.Checkbutton(
            parent,
            text="Left pressure",
            variable=self.left_pressure_enabled_var,
            command=self._on_pressure_options_changed,
        )
        left.grid(row=0, column=0, sticky="w", padx=(0, 14))
        right = self.ttk.Checkbutton(
            parent,
            text="Right pressure",
            variable=self.right_pressure_enabled_var,
            command=self._on_pressure_options_changed,
        )
        right.grid(row=0, column=1, sticky="w")
        self.link_pressure_checkbox = self.ttk.Checkbutton(
            parent,
            text="Use the same settings for both",
            variable=self.linked_pressure_var,
            command=self._on_pressure_options_changed,
        )
        self.link_pressure_checkbox.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(6, 0),
        )
        self._setting_widgets.extend((left, right, self.link_pressure_checkbox))

    def _build_mouse_hardware_settings(self, parent: Any) -> None:
        parent.columnconfigure(0, weight=0)
        parent.columnconfigure(1, weight=0)
        parent.columnconfigure(2, weight=1)
        self.ttk.Label(parent, text="Setting").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 5)
        )
        self.ttk.Label(parent, text="Mapping off").grid(
            row=0, column=1, sticky="w", padx=(0, 14), pady=(0, 5)
        )
        self.ttk.Label(parent, text="Mapping on").grid(
            row=0, column=2, sticky="w", pady=(0, 5)
        )

        self.ttk.Label(parent, text="DPI").grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=5
        )
        normal_dpi = self.ttk.Label(
            parent,
            textvariable=self.normal_dpi_var,
            foreground="#606b75",
        )
        normal_dpi.grid(row=1, column=1, sticky="w", padx=(0, 14), pady=5)
        self.normal_device_value_labels.append(normal_dpi)
        dpi_entry = self.ttk.Entry(parent, textvariable=self.dpi_var, width=10)
        dpi_entry.grid(row=1, column=2, sticky="ew", pady=5)

        for offset, channel in enumerate(("left", "right"), start=2):
            self.ttk.Label(parent, text=f"{channel.title()} haptics").grid(
                row=offset,
                column=0,
                sticky="w",
                padx=(0, 10),
                pady=5,
            )
            normal_haptic = self.ttk.Label(
                parent,
                textvariable=self.normal_haptic_vars[channel],
                foreground="#606b75",
            )
            normal_haptic.grid(
                row=offset,
                column=1,
                sticky="w",
                padx=(0, 14),
                pady=5,
            )
            self.normal_device_value_labels.append(normal_haptic)

            control = self.ttk.Frame(parent)
            control.grid(row=offset, column=2, sticky="ew", pady=5)
            control.columnconfigure(0, weight=1)
            scale = self.ttk.Scale(
                control,
                variable=self.channel_vars[channel]["haptic"],
                from_=0.0,
                to=5.0,
                orient="horizontal",
                command=lambda value, name=channel: self._set_haptic_value(
                    name, value
                ),
            )
            scale.grid(row=0, column=0, sticky="ew")
            value_label = self.ttk.Label(control, width=4, anchor="e")
            value_label.grid(row=0, column=1, padx=(6, 0))
            value_label.configure(
                text=str(round(float(self.channel_vars[channel]["haptic"].get())))
            )
            self.haptic_value_labels[channel] = value_label

            haptic_note = self.ttk.Label(control, text="", foreground="#6b737b")
            haptic_note.grid(
                row=1,
                column=0,
                columnspan=2,
                sticky="w",
                pady=(0, 2),
            )
            self.haptic_note_labels[channel] = haptic_note

        self.ttk.Label(parent, text="Pen output").grid(
            row=4, column=0, sticky="w", padx=(0, 10), pady=5
        )
        backend_combo = self.ttk.Combobox(
            parent,
            textvariable=self.output_backend_var,
            values=("vmulti", "synthetic"),
            state="readonly",
            width=12,
        )
        backend_combo.grid(row=4, column=2, sticky="ew", pady=5)
        self._setting_widgets.append(backend_combo)

        note = self.ttk.Label(
            parent,
            text="Prefer VMulti for lowest latency. Use synthetic as a fallback.",
            foreground="#606b75",
            wraplength=360,
            justify="left",
        )
        note.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.normal_device_value_labels.append(note)

    def _build_backend_advanced_settings(self, parent: Any) -> None:
        self.synthetic_teardown_frame = self.ttk.Frame(parent)
        self.synthetic_teardown_frame.grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(4, 0),
        )
        teardown = self.ttk.Checkbutton(
            self.synthetic_teardown_frame,
            text="Experimental release teardown (may move cursor)",
            variable=self.release_teardown_var,
        )
        teardown.grid(row=0, column=0, sticky="w", pady=(2, 0))
        self._setting_widgets.append(teardown)
        teardown_note = self.ttk.Label(
            self.synthetic_teardown_frame,
            text=(
                "Synthetic backend only. Compatibility sequence for apps that keep "
                "a Windows synthetic pointer in hover after release."
            ),
            foreground="#6b737b",
            wraplength=360,
            justify="left",
        )
        teardown_note.grid(row=1, column=0, sticky="w", pady=(0, 2))
        self.normal_device_value_labels.append(teardown_note)

    def _update_backend_setting_visibility(self) -> None:
        if self.output_backend_var.get().strip().lower() == "synthetic":
            self.backend_advanced_container.grid()
            self.synthetic_teardown_frame.grid()
        else:
            self.synthetic_teardown_frame.grid_remove()
            self.backend_advanced_container.grid_remove()
        if (
            self.backend_advanced_visible
            and self.output_backend_var.get().strip().lower() == "synthetic"
        ):
            self.backend_advanced_frame.grid()
        else:
            self.backend_advanced_frame.grid_remove()
        self._redraw_sensitivity()
        self.root.after_idle(self._refresh_settings_scrollregion)

    def _toggle_backend_advanced(self) -> None:
        self.backend_advanced_visible = not self.backend_advanced_visible
        if self.backend_advanced_visible:
            self.backend_advanced_frame.grid()
            self.backend_advanced_button.configure(
                text="Hide advanced backend settings"
            )
        else:
            self.backend_advanced_frame.grid_remove()
            self.backend_advanced_button.configure(
                text="Show advanced backend settings"
            )
        self.root.after_idle(self._refresh_settings_scrollregion)

    def _build_channel_settings_tab(self, parent: Any, channel: str) -> None:
        variables = self.channel_vars[channel]
        parent.columnconfigure(1, weight=1)
        row = 0
        row = self._entry_row(parent, row, "Raw minimum", variables["raw_min"])
        row = self._entry_row(parent, row, "Raw maximum", variables["raw_max"])

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
        contact_note = self.ttk.Label(
            advanced_frame,
            text="Controls when contact begins and releases.",
            foreground="#6b737b",
            wraplength=330,
            justify="left",
        )
        contact_note.grid(
            row=advanced_row,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, 5),
        )
        self.normal_device_value_labels.append(contact_note)
        advanced_row += 1
        advanced_row = self._entry_row(
            advanced_frame,
            advanced_row,
            "Pressure floor (%)",
            variables["pressure_floor"],
        )
        advanced_row = self._entry_row(
            advanced_frame,
            advanced_row,
            "Rapid release threshold (%)",
            variables["rapid_release_threshold"],
        )
        rapid_release_note = self.ttk.Label(
            advanced_frame,
            text="0% is off. Use small values (1-2%) if thin tails are an issue.",
            foreground="#6b737b",
            wraplength=330,
            justify="left",
        )
        rapid_release_note.grid(
            row=advanced_row,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, 5),
        )
        self.normal_device_value_labels.append(rapid_release_note)
        advanced_row += 1
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
        advanced_row = self._combo_row(
            advanced_frame,
            advanced_row,
            "Pen injection Hz",
            self.injection_hz_var,
            ("60", "120", "240", "360"),
        )
        debug_mode = self.ttk.Checkbutton(
            advanced_frame,
            text="Debug mode",
            variable=self.debug_mode_var,
        )
        debug_mode.grid(
            row=advanced_row,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(5, 0),
        )
        self._setting_widgets.append(debug_mode)
        advanced_row += 1
        debug_note = self.ttk.Label(
            advanced_frame,
            text=(
                "Records stroke traces for analysis. Turn off for potentially "
                "reduced latency."
            ),
            foreground="#6b737b",
            wraplength=330,
            justify="left",
        )
        debug_note.grid(
            row=advanced_row,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, 5),
        )
        self.normal_device_value_labels.append(debug_note)
        advanced_row += 1

        if channel == "right":
            self.rmb_aux_xtilt_checkbox = self.ttk.Checkbutton(
                advanced_frame,
                text="Use right pressure as X-Tilt modifier",
                variable=self.rmb_aux_xtilt_var,
            )
            self.rmb_aux_xtilt_checkbox.grid(
                row=advanced_row,
                column=0,
                columnspan=2,
                sticky="w",
                pady=(6, 0),
            )
            self._setting_widgets.append(self.rmb_aux_xtilt_checkbox)
            advanced_row += 1
            aux_note = self.ttk.Label(
                advanced_frame,
                text=(
                    "During a left-pressure stroke, right pressure becomes 0–60° "
                    "X-Tilt instead of starting its own stroke. Requires both pressure buttons."
                ),
                foreground="#6b737b",
                wraplength=330,
                justify="left",
            )
            aux_note.grid(
                row=advanced_row,
                column=0,
                columnspan=2,
                sticky="w",
                pady=(0, 5),
            )
            self.normal_device_value_labels.append(aux_note)
            advanced_row += 1
        row += 1

        suppress = self.ttk.Checkbutton(
            parent,
            text=f"Suppress native {channel} click (required for Krita pressure)",
            variable=variables["suppress"],
        )
        suppress.grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 2))
        self._setting_widgets.append(suppress)

        variables["path_stabilization"].trace_add(
            "write",
            lambda *_args, name=channel: self._update_path_warning(name),
        )
        variables["haptic"].trace_add(
            "write",
            lambda *_args, name=channel: self._update_haptic_note(name),
        )
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

    def _mirror_linked_setting(self, setting: str) -> None:
        if not self.linked_pressure_var.get():
            return
        self.channel_vars["right"][setting].set(
            self.channel_vars["left"][setting].get()
        )

    def _copy_left_settings_to_right(self) -> None:
        for setting in (
            "raw_min",
            "raw_max",
            "deadzone",
            "curve",
            "curve_strength",
            "contact",
            "pressure_floor",
            "path_stabilization",
            "pressure_influence",
            "onset_buffer",
            "true_low_latency",
            "stationary_pressure_updates",
            "rapid_release_threshold",
        ):
            self._mirror_linked_setting(setting)

    def _on_pressure_options_changed(self) -> None:
        both_enabled = (
            self.left_pressure_enabled_var.get()
            and self.right_pressure_enabled_var.get()
        )
        if not both_enabled and self.linked_pressure_var.get():
            self.linked_pressure_var.set(False)
        if both_enabled and self.linked_pressure_var.get():
            self._copy_left_settings_to_right()
        self._update_pressure_option_states()
        self._redraw_sensitivity()
        self.root.after_idle(self._refresh_settings_scrollregion)

    def _update_pressure_option_states(self) -> None:
        both_enabled = (
            self.left_pressure_enabled_var.get()
            and self.right_pressure_enabled_var.get()
        )
        self.link_pressure_checkbox.configure(
            state=("normal" if both_enabled and not self.running else "disabled")
        )
        linked = both_enabled and self.linked_pressure_var.get()
        if linked and self._selected_channel() == "right":
            self.settings_notebook.select(self.channel_tabs["left"])
        self.settings_notebook.tab(
            self.channel_tabs["right"],
            state=("disabled" if linked else "normal"),
        )
        self.rmb_aux_xtilt_checkbox.configure(
            state=("normal" if both_enabled and not self.running else "disabled")
        )

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
        self.root.after_idle(self._refresh_settings_scrollregion)

    def _resize_settings_content(self, event: Any) -> None:
        self.settings_canvas.itemconfigure(
            self._settings_canvas_window,
            width=max(1, int(event.width)),
        )
        self._refresh_settings_scrollregion()

    def _refresh_settings_scrollregion(self) -> None:
        # ttk.Notebook normally reserves the height of its tallest tab, even
        # when that tab is not selected. Size it to the visible channel so a
        # collapsed Advanced section cannot leave a large blank scroll range.
        if self._refreshing_settings_scrollregion:
            return
        self._refreshing_settings_scrollregion = True
        try:
            self.root.update_idletasks()
            selected = self.settings_notebook.select()
            if selected:
                selected_tab = self.root.nametowidget(selected)
                self.settings_notebook.configure(
                    height=max(1, int(selected_tab.winfo_reqheight()))
                )
                self.root.update_idletasks()

            content_height = max(1, int(self.settings_content.winfo_reqheight()))
            content_width = max(1, int(self.settings_canvas.winfo_width()))
            self.settings_canvas.configure(
                scrollregion=(0, 0, content_width, content_height)
            )
            viewport_height = max(1, int(self.settings_canvas.winfo_height()))
            if content_height <= viewport_height:
                self.settings_canvas.yview_moveto(0.0)
            else:
                current_top = self.settings_canvas.yview()[0]
                maximum_top = max(0.0, 1.0 - viewport_height / content_height)
                if current_top > maximum_top:
                    self.settings_canvas.yview_moveto(maximum_top)
        finally:
            self._refreshing_settings_scrollregion = False

    def _on_settings_mousewheel(self, event: Any) -> str | None:
        x = self.root.winfo_pointerx()
        y = self.root.winfo_pointery()
        left = self.settings_canvas.winfo_rootx()
        top = self.settings_canvas.winfo_rooty()
        if not (
            left <= x < left + self.settings_canvas.winfo_width()
            and top <= y < top + self.settings_canvas.winfo_height()
        ):
            return None
        delta = int(getattr(event, "delta", 0))
        if delta:
            self.settings_canvas.yview_scroll(-1 if delta > 0 else 1, "units")
            return "break"
        return None

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
        self.root.after_idle(self._refresh_settings_scrollregion)

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
        channel = self._selected_channel()
        auxiliary_xtilt = channel == "right" and self.rmb_aux_xtilt_var.get()
        self.mapping_caption.configure(
            text=(
                "Raw right-button pressure → X-Tilt (0–60°)"
                if auxiliary_xtilt
                else "Raw click pressure → effective pen pressure"
            )
        )

        try:
            settings = self._collect_settings()
            points = sensitivity_mapping_points(
                settings,
                apply_pressure_shaping=not auxiliary_xtilt,
            )
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

        latest_raw = self._latest_raw[channel]
        if latest_raw is not None:
            raw = max(settings.raw_min, min(settings.raw_max, latest_raw))
            pressure = (
                curve_pressure_for_raw(settings, raw)
                if auxiliary_xtilt
                else effective_pressure_for_raw(settings, raw)
            )
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

    def _on_output_tab_changed(self, _event: Any = None) -> None:
        if self.output_notebook.select() == str(self.analysis_tab):
            self._refresh_stroke_list(select_latest=not bool(self._stroke_trace_paths))

    def _trace_directory(self) -> Path:
        configured = self.service.launch_config.trace_dir or "work/stroke_traces"
        return Path(configured)

    def _refresh_stroke_list(self, *, select_latest: bool) -> None:
        directory = self._trace_directory()
        paths = (
            sorted(
                directory.glob("stroke-*.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:40]
            if directory.exists()
            else []
        )
        previous = self.stroke_trace_var.get()
        self._stroke_trace_paths = {path.name: path for path in paths}
        names = list(self._stroke_trace_paths)
        self.stroke_selector.configure(values=names)
        if not names:
            self.stroke_trace_var.set("")
            self._stroke_analysis = None
            self.stroke_analysis_summary.configure(
                text=(
                    "No traces found. Enable Debug mode, start the bridge, and "
                    "draw a stroke."
                )
            )
            self._draw_stroke_analysis()
            return
        selection = previous if previous in self._stroke_trace_paths else names[0]
        if select_latest:
            for name in names:
                try:
                    payload = json.loads(
                        self._stroke_trace_paths[name].read_text(encoding="utf-8")
                    )
                    if float(stroke_analysis_data(payload)["path_px"]) >= 25.0:
                        selection = name
                        break
                except (OSError, ValueError, TypeError):
                    continue
        self.stroke_trace_var.set(selection)
        self._load_selected_stroke()

    def _load_selected_stroke(self) -> None:
        path = self._stroke_trace_paths.get(self.stroke_trace_var.get())
        if path is None:
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            analysis = stroke_analysis_data(payload)
        except (OSError, ValueError, TypeError) as exc:
            self._stroke_analysis = None
            self.stroke_analysis_summary.configure(text=f"Could not analyze stroke: {exc}")
            self._draw_stroke_analysis()
            return
        self._stroke_analysis = analysis
        metadata = analysis["metadata"]
        curve = str(metadata.get("configured_curve", "unknown"))
        strength = metadata.get("configured_curve_strength", "—")
        self.stroke_analysis_summary.configure(
            text=(
                f"Motion {analysis['motion_hz']:.1f} Hz · p95 position gap "
                f"{analysis['p95_motion_segment']:.1f} px · p95/max pen-pressure "
                f"step {analysis['p95_pressure_step']:.0f}/{analysis['max_pressure_step']:.0f} "
                f"· max mapped step {analysis['max_mapped_step']:.0f} · "
                f"curve {curve} {strength}"
            )
        )
        self._draw_stroke_analysis()

    def _draw_stroke_analysis(self) -> None:
        canvas = self.stroke_analysis_canvas
        canvas.delete("all")
        width = max(420, canvas.winfo_width())
        height = max(360, canvas.winfo_height())
        colors = self.theme_colors or {
            "canvas": "#f7f9fb",
            "axis": "#68747f",
            "grid": "#dce2e7",
            "foreground": "#20262c",
        }
        if self._stroke_analysis is None:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Select a recent stroke to display its pressure pipeline.",
                fill=colors["foreground"],
                justify="center",
            )
            return

        dark = self.theme_var.get() == "dark"
        series_colors = {
            "Raw ADC": "#f0a43a" if dark else "#c46a00",
            "Mapped sample": "#58a6ff" if dark else "#1677b8",
            "Interpolated": "#56d364" if dark else "#238636",
            "Injected pen": "#ff7b72" if dark else "#c33b32",
        }
        left, right = 55, 18
        panel_gap = 42
        top = 28
        bottom = 42
        panel_height = max(110, (height - top - bottom - panel_gap) / 2)

        def draw_panel(
            panel_top: float,
            title: str,
            x_label: str,
            graph_series: list[tuple[str, list[tuple[float, float]]]],
        ) -> None:
            panel_bottom = panel_top + panel_height
            plot_width = width - left - right
            all_x = [x for _name, values in graph_series for x, _y in values]
            x_max = max(all_x, default=1.0)
            if x_max <= 0.0:
                x_max = 1.0
            for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
                y = panel_bottom - fraction * panel_height
                canvas.create_line(left, y, width - right, y, fill=colors["grid"])
                canvas.create_text(
                    left - 7,
                    y,
                    text=str(round(fraction * 1024)),
                    anchor="e",
                    fill=colors["foreground"],
                    font=("Segoe UI", 8),
                )
            canvas.create_line(
                left,
                panel_top,
                left,
                panel_bottom,
                fill=colors["axis"],
            )
            canvas.create_line(
                left,
                panel_bottom,
                width - right,
                panel_bottom,
                fill=colors["axis"],
            )
            canvas.create_text(
                left,
                panel_top - 13,
                text=title,
                anchor="w",
                fill=colors["foreground"],
                font=("Segoe UI Semibold", 9),
            )
            canvas.create_text(
                width - right,
                panel_bottom + 17,
                text=x_label,
                anchor="e",
                fill=colors["foreground"],
                font=("Segoe UI", 8),
            )
            legend_x = left + 8
            for name, values in graph_series:
                if not values:
                    continue
                step = max(1, math.ceil(len(values) / 900))
                sampled = values[::step]
                coordinates: list[float] = []
                for x_value, y_value in sampled:
                    coordinates.extend(
                        (
                            left + x_value / x_max * plot_width,
                            panel_bottom
                            - max(0.0, min(1024.0, y_value)) / 1024.0 * panel_height,
                        )
                    )
                if len(coordinates) >= 4:
                    canvas.create_line(
                        *coordinates,
                        fill=series_colors[name],
                        width=2,
                    )
                legend_y = panel_top + 7
                canvas.create_line(
                    legend_x,
                    legend_y,
                    legend_x + 14,
                    legend_y,
                    fill=series_colors[name],
                    width=3,
                )
                canvas.create_text(
                    legend_x + 18,
                    legend_y,
                    text=name,
                    anchor="w",
                    fill=colors["foreground"],
                    font=("Segoe UI", 8),
                )
                legend_x += 92

        analysis = self._stroke_analysis
        draw_panel(
            top,
            "Pressure pipeline",
            "time (ms)",
            [
                ("Raw ADC", analysis["raw"]),
                ("Mapped sample", analysis["mapped"]),
                ("Interpolated", analysis["interpolated"]),
                ("Injected pen", analysis["injected_time"]),
            ],
        )
        draw_panel(
            top + panel_height + panel_gap,
            "Pressure attached to the drawn path",
            "path distance (px)",
            [("Injected pen", analysis["injected_distance"])],
        )

    def _set_controls_from_config(self, config: RuntimeConfig) -> None:
        # Disable linking while assigning both channels so their distinct
        # factory curve strengths are not accidentally mirrored.
        self.linked_pressure_var.set(False)
        for channel, channel_config in (
            ("left", config.left),
            ("right", config.right),
        ):
            variables = self.channel_vars[channel]
            variables["raw_min"].set(str(channel_config.raw_min))
            variables["raw_max"].set(str(channel_config.raw_max))
            variables["deadzone"].set(str(channel_config.deadzone_low))
            variables["curve"].set(channel_config.curve)
            variables["curve_strength"].set(channel_config.curve_strength)
            variables["contact"].set(channel_config.contact_preset)
            variables["pressure_floor"].set(str(channel_config.pressure_floor))
            variables["path_stabilization"].set(
                str(channel_config.path_stabilization)
            )
            variables["pressure_influence"].set(
                str(channel_config.pressure_influence)
            )
            variables["onset_buffer"].set(channel_config.onset_buffer)
            variables["true_low_latency"].set(channel_config.true_low_latency)
            variables["stationary_pressure_updates"].set(
                channel_config.stationary_pressure_updates
            )
            variables["rapid_release_threshold"].set(
                str(channel_config.rapid_release_threshold)
            )
            variables["suppress"].set(
                config.suppress_lmb if channel == "left" else config.suppress_rmb
            )
            variables["haptic"].set(
                float(
                    config.session_haptic_left
                    if channel == "left"
                    else config.session_haptic_right
                )
            )
            self.curve_value_labels[channel].configure(
                text=f"{channel_config.curve_strength:.1f}"
            )
            self.haptic_value_labels[channel].configure(
                text=str(round(float(variables["haptic"].get())))
            )
            self._update_haptic_note(channel)
            self._update_path_warning(channel)

        self.left_pressure_enabled_var.set(config.left_enabled)
        self.right_pressure_enabled_var.set(config.right_enabled)
        self.linked_pressure_var.set(config.linked)
        self.rmb_aux_xtilt_var.set(config.rmb_aux_xtilt)
        self.debug_mode_var.set(config.debug_mode)
        self.minimize_to_tray_var.set(config.minimize_to_tray)
        self.release_teardown_var.set(config.release_teardown)
        self.follow_normal_device_settings = (
            config.session_device_settings_follow_normal
        )
        self.dpi_var.set(str(config.session_dpi))
        self.injection_hz_var.set("240")
        self.output_backend_var.set(
            "vmulti" if enumerate_vmulti_candidates() else "synthetic"
        )
        self._on_pressure_options_changed()
        self._update_backend_setting_visibility()
        self._redraw_sensitivity()

    def _confirm_restore_defaults(self) -> None:
        if self.running or self.busy:
            return
        confirmed = self.messagebox.askyesno(
            "Restore default settings?",
            (
                "Restore every pressure, mouse, backend, and app setting to "
                "its default value?\n\nThis cannot be undone."
            ),
            icon="warning",
            parent=self.root,
        )
        if not confirmed:
            return

        defaults = RuntimeConfig()
        if defaults.session_device_settings_follow_normal:
            try:
                defaults.session_dpi = int(self.normal_dpi_var.get())
                defaults.session_haptic_left = int(
                    self.normal_haptic_vars["left"].get()
                )
                defaults.session_haptic_right = int(
                    self.normal_haptic_vars["right"].get()
                )
            except ValueError:
                pass
        try:
            self.service.restore_defaults(defaults)
            self._set_controls_from_config(defaults)
        except Exception as exc:
            self._write_system(f"Could not restore defaults: {exc}", level="ERROR")
            return
        self._write_system("Default settings restored.")

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
            true_low_latency=variables["true_low_latency"].get(),
            stationary_pressure_updates=variables[
                "stationary_pressure_updates"
            ].get(),
            rapid_release_threshold=variables["rapid_release_threshold"].get(),
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
            device_settings = self._collect_device_settings()
            backend = self.output_backend_var.get().strip().lower()
            synthetic_backend = backend == "synthetic"
            self.service.apply_config(
                {
                    "linked": (
                        self.linked_pressure_var.get()
                        and self.left_pressure_enabled_var.get()
                        and self.right_pressure_enabled_var.get()
                    ),
                    "left_enabled": self.left_pressure_enabled_var.get(),
                    "right_enabled": self.right_pressure_enabled_var.get(),
                    "suppress_lmb": left.suppress_lmb,
                    "suppress_rmb": right.suppress_lmb,
                    "rmb_aux_xtilt": self.rmb_aux_xtilt_var.get(),
                    "debug_mode": self.debug_mode_var.get(),
                    "minimize_to_tray": self.minimize_to_tray_var.get(),
                    "release_teardown": (
                        self.release_teardown_var.get() if synthetic_backend else False
                    ),
                    "session_dpi": device_settings["dpi"],
                    "session_haptic_left": device_settings["haptic_left"],
                    "session_haptic_right": device_settings["haptic_right"],
                    "session_device_settings_follow_normal": (
                        self.follow_normal_device_settings
                    ),
                    "left": left.as_runtime_patch()["left"],
                    "right": right.as_runtime_patch()["left"],
                }
            )
            self.service.launch_config.hz = left.injection_hz
            self.service.launch_config.backend = backend
        except Exception as exc:
            self._write_system(f"Settings error: {exc}", level="ERROR")
            return False
        self._write_system("Settings saved.")
        return True

    def _apply_all_settings(self) -> None:
        if self.busy:
            return
        if self.running:
            self._begin_device_apply()
        else:
            self._apply_settings()

    def _toggle(self) -> None:
        if self.busy:
            return
        if self.running:
            self._begin_stop()
        elif self._apply_settings():
            self._begin_start()

    def _begin_start(self) -> None:
        try:
            device_settings = self._collect_device_settings()
        except ValueError as exc:
            self._write_system(f"Mouse settings error: {exc}", level="ERROR")
            return
        self.busy = True
        self.toggle_button.configure(text="Start", state="disabled")
        self.status_label.configure(text="● Starting", foreground="#b78103")
        self._watch_future(
            "started",
            self.controller.start(device_settings=device_settings),
        )

    def _begin_stop(self) -> None:
        self.busy = True
        self.toggle_button.configure(text="Stop", state="disabled")
        self.status_label.configure(text="● Stopping", foreground="#b78103")
        self._watch_future("stopped", self.controller.stop())

    def _begin_device_apply(self) -> None:
        if not self.running:
            self._write_system("Start the bridge before applying mouse settings.", level="WARN")
            return
        try:
            settings = self._collect_device_settings()
        except ValueError as exc:
            self._write_system(f"Mouse settings error: {exc}", level="ERROR")
            return

        try:
            self.service.apply_config(
                {
                    "minimize_to_tray": self.minimize_to_tray_var.get(),
                    "session_dpi": settings["dpi"],
                    "session_haptic_left": settings["haptic_left"],
                    "session_haptic_right": settings["haptic_right"],
                    "session_device_settings_follow_normal": (
                        self.follow_normal_device_settings
                    ),
                }
            )
        except Exception as exc:
            self._write_system(f"Settings error: {exc}", level="ERROR")
            return

        self.apply_button.configure(text="Applying…", state="disabled")
        self._watch_future(
            "device_settings_applied",
            self.controller.apply_device_settings(
                dpi=settings["dpi"],
                haptic_left=settings["haptic_left"],
                haptic_right=settings["haptic_right"],
            ),
        )

    def _collect_device_settings(self) -> dict[str, int]:
        try:
            dpi = int(self.dpi_var.get())
            left = round(float(self.channel_vars["left"]["haptic"].get()))
            right = round(float(self.channel_vars["right"]["haptic"].get()))
        except ValueError as exc:
            raise ValueError("DPI and haptic levels must be numeric.") from exc
        if not 100 <= dpi <= 32000 or dpi % 50 != 0:
            raise ValueError("DPI must be 100..32000 in 50-DPI increments.")
        if not 0 <= left <= 5 or not 0 <= right <= 5:
            raise ValueError("Haptics must be between 0 and 5.")
        try:
            normal = (
                int(self.normal_dpi_var.get()),
                int(self.normal_haptic_vars["left"].get()),
                int(self.normal_haptic_vars["right"].get()),
            )
        except ValueError:
            normal = None
        if normal is not None and (dpi, left, right) != normal:
            self.follow_normal_device_settings = False
        return {
            "dpi": dpi,
            "haptic_left": left,
            "haptic_right": right,
        }

    def _begin_device_detection(self) -> None:
        if self.running or self.busy:
            return
        self.detecting_device_settings = True
        self.toggle_button.configure(text="Start", state="disabled")
        self.status_label.configure(text="● Detecting mouse", foreground="#b78103")
        self._watch_future(
            "device_settings_detected",
            self.controller.detect_device_settings(),
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
        state = "disabled" if running else "normal"
        for widget in self._setting_widgets:
            if isinstance(widget, self.ttk.Combobox):
                widget.configure(state="disabled" if running else "readonly")
            else:
                widget.configure(state=state)
        self._update_pressure_option_states()
        self.toggle_button.configure(
            text="Stop" if running else "Start",
            state="normal",
        )
        self.status_label.configure(
            text="● Running" if running else "● Stopped",
            foreground="#238636" if running else "#a33",
        )
        self.apply_button.configure(
            text="Apply settings live" if running else "Save settings",
            state="normal",
        )
        self.restore_defaults_button.configure(
            state="disabled" if running else "normal"
        )
        if not running:
            self.telemetry_label.configure(text="Press Start to see sensitivity mapping")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "tray_restore":
                    self._restore_from_tray()
                elif kind == "tray_quit":
                    self._on_close()
                elif kind == "start_hotkey":
                    if not self.running and not self.busy and not self.detecting_device_settings:
                        if self._apply_settings():
                            self._begin_start()
                elif kind == "log":
                    self._write_log(payload)
                elif kind == "telemetry":
                    self._latest_raw["left"] = int(payload["left_raw"])
                    self._latest_raw["right"] = int(payload["right_raw"])
                    channel = self._selected_channel()
                    raw = int(payload[f"{channel}_raw"])
                    mapped = int(payload[f"{channel}_mapped"])
                    try:
                        if (
                            channel == "right"
                            and self.rmb_aux_xtilt_var.get()
                        ):
                            effective_text = f"X-Tilt {round(mapped * 60 / 1023)}°"
                        else:
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
                elif kind == "device_settings_detected":
                    self.detecting_device_settings = False
                    self.normal_dpi_var.set(str(payload["dpi"]))
                    for channel in ("left", "right"):
                        key = f"haptic_{channel}"
                        self.normal_haptic_vars[channel].set(str(payload[key]))
                    if self.follow_normal_device_settings:
                        self.dpi_var.set(str(payload["dpi"]))
                        for channel in ("left", "right"):
                            value = int(payload[f"haptic_{channel}"])
                            self.channel_vars[channel]["haptic"].set(float(value))
                            self.haptic_value_labels[channel].configure(text=str(value))
                            self._update_haptic_note(channel)
                    self.toggle_button.configure(text="Start", state="normal")
                    self.status_label.configure(text="● Stopped", foreground="#a33")
                    self._write_system(
                        f"Detected normal mouse settings: {payload['dpi']} DPI, "
                        f"haptics L{payload['haptic_left']}/R{payload['haptic_right']}."
                    )
                elif kind == "device_settings_applied":
                    self.apply_button.configure(text="Apply settings live", state="normal")
                    self._write_system(
                        f"Mouse settings applied: {payload['dpi']} DPI, "
                        f"haptics L{payload['haptic_left']}/R{payload['haptic_right']}."
                    )
                elif kind == "runtime_error":
                    self._set_running(False)
                    self.output_notebook.select(self.terminal_tab)
                    self.status_label.configure(text="● Bridge stopped", foreground="#a33")
                    self._write_system(str(payload), level="ERROR")
                elif kind == "force_stopped":
                    self._set_running(False)
                    self.status_label.configure(text="● Stopped", foreground="#a33")
                    self._write_system(
                        "Stop completed. Pressure mapping is off and normal mouse settings were restored.",
                        level="WARN",
                    )
                elif kind.endswith("_error"):
                    if kind == "device_settings_detected_error":
                        self.detecting_device_settings = False
                        self.normal_dpi_var.set("Unavailable")
                        for variable in self.normal_haptic_vars.values():
                            variable.set("—")
                        self.toggle_button.configure(text="Start", state="normal")
                        self.status_label.configure(text="● Mouse not detected", foreground="#a33")
                        self._write_system(
                            f"Could not detect normal mouse settings: {payload}",
                            level="WARN",
                        )
                        continue
                    self._set_running(self.service.stream_active)
                    self.output_notebook.select(self.terminal_tab)
                    if not self.service.stream_active:
                        self.status_label.configure(text="● Start failed", foreground="#a33")
                    self._write_system(f"Bridge error: {payload}", level="ERROR")
                elif kind == "closed":
                    self._tray.stop()
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(50, self._drain_events)

    def _write_log(self, entry: LogEntry) -> None:
        import datetime

        stamp = datetime.datetime.fromtimestamp(entry.ts / 1000).strftime("%H:%M:%S")
        self._append_terminal(f"{stamp} {entry.level:<5} {entry.msg}\n", entry.level)
        if (
            entry.msg.startswith("TRACE saved ")
            and self.output_notebook.select() == str(self.analysis_tab)
        ):
            self.root.after_idle(
                lambda: self._refresh_stroke_list(select_latest=True)
            )

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

    def _on_window_unmapped(self, event: Any) -> None:
        if event.widget is self.root:
            self.root.after_idle(self._maybe_minimize_to_tray)

    def _maybe_minimize_to_tray(self) -> None:
        if (
            self._closing
            or self._tray_hidden
            or not self.minimize_to_tray_var.get()
            or self.root.state() != "iconic"
        ):
            return
        shown = self._tray.show(
            lambda: self.events.put(("tray_restore", None)),
            lambda: self.events.put(("tray_quit", None)),
        )
        if not shown:
            self._write_system(
                "Could not create the tray icon; the window remains minimized.",
                level="WARN",
            )
            return
        self._tray_hidden = True
        self.root.withdraw()

    def _restore_from_tray(self) -> None:
        if self._closing:
            return
        self._tray.stop()
        self._tray_hidden = False
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def _on_close(self) -> None:
        if self.busy or self._closing:
            return
        self._closing = True
        self._tray.stop()
        self.busy = True
        self.toggle_button.configure(state="disabled")
        self.status_label.configure(text="● Closing", foreground="#b78103")

        def close_runtime() -> None:
            try:
                self.start_hotkey.close()
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
        # Prefer the virtual HID tablet when its signed driver is installed,
        # while keeping the driverless Windows synthetic pointer as a fallback.
        backend = "vmulti" if enumerate_vmulti_candidates() else "synthetic"
        service = RuntimeService(
            launch_config=LaunchConfig(
                backend=backend,
                trace_dir="work/stroke_traces",
            ),
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
