"""Build one shareable diagnostics bundle after a debug session stops."""

from __future__ import annotations

import ctypes
import json
import os
import platform
import threading
import zipfile
from ctypes import wintypes
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from mouse_pressure import __version__
from mouse_pressure.bridge.config import LaunchConfig, RuntimeConfig
from mouse_pressure.runtime.config_store import runtime_config_to_dict
from mouse_pressure.runtime.log_bus import LogBus, LogEntry


def _display_diagnostics() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    result: dict[str, Any] = {
        "primary_width": int(user32.GetSystemMetrics(0)),
        "primary_height": int(user32.GetSystemMetrics(1)),
        "virtual_left": int(user32.GetSystemMetrics(76)),
        "virtual_top": int(user32.GetSystemMetrics(77)),
        "virtual_width": int(user32.GetSystemMetrics(78)),
        "virtual_height": int(user32.GetSystemMetrics(79)),
    }
    monitors: list[dict[str, Any]] = []

    class PointL(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class MonitorInfoExW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    class DevModeW(ctypes.Structure):
        _fields_ = [
            ("dmDeviceName", wintypes.WCHAR * 32),
            ("dmSpecVersion", wintypes.WORD),
            ("dmDriverVersion", wintypes.WORD),
            ("dmSize", wintypes.WORD),
            ("dmDriverExtra", wintypes.WORD),
            ("dmFields", wintypes.DWORD),
            ("dmPosition", PointL),
            ("dmDisplayOrientation", wintypes.DWORD),
            ("dmDisplayFixedOutput", wintypes.DWORD),
            ("dmColor", wintypes.SHORT),
            ("dmDuplex", wintypes.SHORT),
            ("dmYResolution", wintypes.SHORT),
            ("dmTTOption", wintypes.SHORT),
            ("dmCollate", wintypes.SHORT),
            ("dmFormName", wintypes.WCHAR * 32),
            ("dmLogPixels", wintypes.WORD),
            ("dmBitsPerPel", wintypes.DWORD),
            ("dmPelsWidth", wintypes.DWORD),
            ("dmPelsHeight", wintypes.DWORD),
            ("dmDisplayFlags", wintypes.DWORD),
            ("dmDisplayFrequency", wintypes.DWORD),
            ("dmICMMethod", wintypes.DWORD),
            ("dmICMIntent", wintypes.DWORD),
            ("dmMediaType", wintypes.DWORD),
            ("dmDitherType", wintypes.DWORD),
            ("dmReserved1", wintypes.DWORD),
            ("dmReserved2", wintypes.DWORD),
            ("dmPanningWidth", wintypes.DWORD),
            ("dmPanningHeight", wintypes.DWORD),
        ]

    monitor_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )
    user32.GetMonitorInfoW.argtypes = [
        wintypes.HMONITOR,
        ctypes.POINTER(MonitorInfoExW),
    ]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.EnumDisplaySettingsW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(DevModeW),
    ]
    user32.EnumDisplaySettingsW.restype = wintypes.BOOL
    user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        monitor_proc,
        wintypes.LPARAM,
    ]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL

    @monitor_proc
    def collect_monitor(
        monitor: int,
        _dc: int,
        _rect: ctypes.POINTER(wintypes.RECT),
        _data: int,
    ) -> bool:
        info = MonitorInfoExW()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return True
        mode = DevModeW()
        mode.dmSize = ctypes.sizeof(mode)
        has_mode = bool(user32.EnumDisplaySettingsW(info.szDevice, -1, ctypes.byref(mode)))
        item: dict[str, Any] = {
            "device": info.szDevice,
            "primary": bool(info.dwFlags & 1),
            "bounds": {
                "left": int(info.rcMonitor.left),
                "top": int(info.rcMonitor.top),
                "right": int(info.rcMonitor.right),
                "bottom": int(info.rcMonitor.bottom),
            },
        }
        if has_mode:
            item["mode"] = {
                "width": int(mode.dmPelsWidth),
                "height": int(mode.dmPelsHeight),
                "refresh_hz": int(mode.dmDisplayFrequency),
            }
        try:
            shcore = ctypes.WinDLL("shcore", use_last_error=True)
            shcore.GetDpiForMonitor.argtypes = [
                wintypes.HMONITOR,
                ctypes.c_int,
                ctypes.POINTER(wintypes.UINT),
                ctypes.POINTER(wintypes.UINT),
            ]
            dpi_x = wintypes.UINT()
            dpi_y = wintypes.UINT()
            if int(shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))) == 0:
                item["effective_dpi"] = {"x": int(dpi_x.value), "y": int(dpi_y.value)}
                item["scale_percent"] = round(int(dpi_x.value) * 100 / 96)
        except (AttributeError, OSError):
            pass
        monitors.append(item)
        return True

    try:
        user32.EnumDisplayMonitors(None, None, collect_monitor, 0)
    except (AttributeError, OSError):
        pass
    result["monitors"] = monitors
    return result


class DiagnosticSessionCollector:
    """Capture cheap in-memory logs, then write files after input has stopped."""

    def __init__(self, config_dir: Path, trace_dir: Path, log_bus: LogBus) -> None:
        self.output_dir = Path(config_dir) / "logs"
        self.trace_dir = Path(trace_dir)
        self._lock = threading.Lock()
        self._active = False
        self._started_at: datetime | None = None
        self._baseline_traces: set[Path] = set()
        self._entries: list[dict[str, Any]] = []
        self._config: dict[str, Any] = {}
        self._launch: dict[str, Any] = {}
        log_bus.subscribe(self._record)

    def _record(self, entry: LogEntry) -> None:
        with self._lock:
            if self._active and len(self._entries) < 100_000:
                self._entries.append(entry.as_dict())

    def start(self, config: RuntimeConfig, launch: LaunchConfig) -> None:
        with self._lock:
            self._active = bool(config.debug_mode)
            self._started_at = datetime.now().astimezone()
            self._entries = []
            self._config = runtime_config_to_dict(config)
            self._launch = asdict(launch)
            self._baseline_traces = set(self.trace_dir.glob("stroke-*.json"))

    def finish(self) -> Path | None:
        with self._lock:
            if not self._active or self._started_at is None:
                self._active = False
                return None
            self._active = False
            started_at = self._started_at
            entries = list(self._entries)
            config = dict(self._config)
            launch = dict(self._launch)
            baseline = set(self._baseline_traces)

        trace_paths = sorted(
            set(self.trace_dir.glob("stroke-*.json")) - baseline,
            key=lambda path: path.stat().st_mtime,
        )
        finished_at = datetime.now().astimezone()
        stamp = started_at.strftime("%Y%m%d-%H%M%S")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        destination = self.output_dir / f"mouse-pressure-session-{stamp}.zip"
        summary = {
            "schema_version": 1,
            "app_version": __version__,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "system": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "display": _display_diagnostics(),
            },
            "runtime_config": config,
            "launch_config": launch,
            "logs": entries,
            "stroke_files": [path.name for path in trace_paths],
        }
        temporary = destination.with_suffix(".zip.tmp")
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            archive.writestr("session.json", json.dumps(summary, indent=2))
            for path in trace_paths:
                try:
                    archive.write(path, f"strokes/{path.name}")
                except OSError:
                    continue
        temporary.replace(destination)
        return destination
