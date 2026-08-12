"""Native worker-backed Windows synthetic pen injector.

The pressure/contact state machine remains in Python. Only the final ordered
Windows pointer-report queue and ``InjectSyntheticPointerInput`` calls live in
the native relay, keeping the behavioral surface identical to the proven
synthetic backend while moving frame pacing off the Python event loop.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from mouse_pressure.bridge.synthetic_pen import _SyntheticPenInjector


NATIVE_RELAY_FILENAME = "mouse_pressure_synthetic_relay.dll"
NATIVE_RELAY_API_VERSION = 3
NATIVE_FRAME_INTERVAL_US = 120


class NativeRelayStats(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("api_version", ctypes.c_uint32),
        ("submitted", ctypes.c_uint64),
        ("injected", ctypes.c_uint64),
        ("failed", ctypes.c_uint64),
        ("queue_full", ctypes.c_uint64),
        ("max_queue_depth", ctypes.c_uint32),
        ("last_error", ctypes.c_uint32),
        ("total_queue_delay_us", ctypes.c_uint64),
        ("total_inject_call_us", ctypes.c_uint64),
        ("last_queue_delay_us", ctypes.c_uint32),
        ("last_inject_call_us", ctypes.c_uint32),
        ("max_queue_delay_us", ctypes.c_uint32),
        ("max_inject_call_us", ctypes.c_uint32),
        ("completion_dropped", ctypes.c_uint64),
        ("qpc_frequency", ctypes.c_uint64),
    ]


class NativeRelayInput(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("x", ctypes.c_int32),
        ("y", ctypes.c_int32),
        ("pressure", ctypes.c_uint32),
        ("tilt_x", ctypes.c_int32),
        ("tilt_enabled", ctypes.c_uint32),
        ("token", ctypes.c_uint64),
    ]


class NativeRelayCompletion(ctypes.Structure):
    _fields_ = [
        ("token", ctypes.c_uint64),
        ("submitted_qpc", ctypes.c_uint64),
        ("inject_begin_qpc", ctypes.c_uint64),
        ("completed_qpc", ctypes.c_uint64),
        ("qpc_frequency", ctypes.c_uint64),
        ("flags", ctypes.c_uint32),
        ("x", ctypes.c_int32),
        ("y", ctypes.c_int32),
        ("pressure", ctypes.c_uint32),
        ("success", ctypes.c_uint32),
        ("error", ctypes.c_uint32),
        ("queue_delay_us", ctypes.c_uint32),
        ("inject_call_us", ctypes.c_uint32),
    ]


class NativeInputMove(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int32),
        ("y", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
        ("message_time_ms", ctypes.c_uint32),
        ("observed_qpc", ctypes.c_uint64),
        ("qpc_frequency", ctypes.c_uint64),
    ]


class NativeInputStats(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("api_version", ctypes.c_uint32),
        ("captured", ctypes.c_uint64),
        ("drained", ctypes.c_uint64),
        ("dropped", ctypes.c_uint64),
        ("max_queue_depth", ctypes.c_uint32),
        ("last_error", ctypes.c_uint32),
        ("qpc_frequency", ctypes.c_uint64),
    ]


def native_relay_path_candidates() -> list[Path]:
    """Return supported relay locations in priority order."""
    candidates: list[Path] = []
    override = os.environ.get("MOUSE_PRESSURE_NATIVE_RELAY")
    if override:
        candidates.append(Path(override).expanduser())

    package_root = Path(__file__).resolve().parents[1]
    candidates.append(package_root / "native" / NATIVE_RELAY_FILENAME)
    candidates.append(
        Path(__file__).resolve().parents[3]
        / "build"
        / "native"
        / NATIVE_RELAY_FILENAME
    )

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidates.append(
            Path(frozen_root)
            / "mouse_pressure"
            / "native"
            / NATIVE_RELAY_FILENAME
        )

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate.resolve(strict=False)))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def find_native_relay() -> Path | None:
    return next(
        (path for path in native_relay_path_candidates() if path.is_file()),
        None,
    )


def native_relay_available() -> bool:
    return find_native_relay() is not None


class NativeTransformedMouseCapture:
    """Dedicated native low-level hook feeding transformed desktop points.

    Raw Input remains responsible for device identity and release ordering.
    This collector only removes the Python callback from the geometry hot path;
    every returned coordinate has already passed through Windows pointer-speed,
    acceleration, DPI, and virtual-desktop transforms.
    """

    def __init__(
        self,
        log: Callable[[str], None],
        *,
        library_path: str | Path | None = None,
    ) -> None:
        self.log = log
        self._library_path = Path(library_path) if library_path is not None else None
        self._dll: ctypes.WinDLL | None = None
        self._capture = ctypes.c_void_p()

    def _load_library(self) -> tuple[ctypes.WinDLL, Path]:
        path = self._library_path or find_native_relay()
        if path is None:
            raise RuntimeError("The native transformed-input collector is not built.")
        dll = ctypes.WinDLL(str(path), use_last_error=True)
        dll.mp_synth_api_version.argtypes = []
        dll.mp_synth_api_version.restype = ctypes.c_uint32
        dll.mp_input_create.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
        dll.mp_input_create.restype = ctypes.c_void_p
        dll.mp_input_drain_moves.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(NativeInputMove),
            ctypes.c_uint32,
        ]
        dll.mp_input_drain_moves.restype = ctypes.c_uint32
        dll.mp_input_get_stats.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(NativeInputStats),
            ctypes.c_uint32,
        ]
        dll.mp_input_get_stats.restype = ctypes.c_int
        dll.mp_input_destroy.argtypes = [ctypes.c_void_p]
        dll.mp_input_destroy.restype = None
        return dll, path

    def open(self) -> None:
        if self._capture:
            return
        dll, path = self._load_library()
        api_version = int(dll.mp_synth_api_version())
        if api_version != NATIVE_RELAY_API_VERSION:
            raise RuntimeError(
                f"Native transformed-input API {api_version} is incompatible with "
                f"expected API {NATIVE_RELAY_API_VERSION}."
            )
        error = ctypes.c_uint32()
        capture = dll.mp_input_create(ctypes.byref(error))
        if not capture:
            raise RuntimeError(
                f"Native transformed-input startup failed, err={error.value}"
            )
        self._dll = dll
        self._capture = ctypes.c_void_p(capture)
        self.log(f"Native transformed-input capture active path={path}")

    def drain_moves(self, capacity: int = 4096) -> list[dict[str, int | bool | float]]:
        if self._dll is None or not self._capture or capacity <= 0:
            return []
        batch = (NativeInputMove * capacity)()
        count = int(
            self._dll.mp_input_drain_moves(
                self._capture,
                batch,
                max(1, int(capacity)),
            )
        )
        return [
            {
                "x": int(item.x),
                "y": int(item.y),
                "injected": bool(int(item.flags) & 0x00000001),
                "flags": int(item.flags),
                "message_time_ms": int(item.message_time_ms),
                "observed_at": (
                    float(item.observed_qpc) / float(item.qpc_frequency)
                    if item.qpc_frequency
                    else time.perf_counter()
                ),
            }
            for item in batch[:count]
        ]

    def stats(self) -> dict[str, int]:
        if self._dll is None or not self._capture:
            return {}
        stats = NativeInputStats()
        stats.struct_size = ctypes.sizeof(stats)
        ok = bool(
            self._dll.mp_input_get_stats(
                self._capture,
                ctypes.byref(stats),
                ctypes.sizeof(stats),
            )
        )
        if not ok:
            return {}
        return {
            "captured": int(stats.captured),
            "drained": int(stats.drained),
            "dropped": int(stats.dropped),
            "max_queue_depth": int(stats.max_queue_depth),
            "last_error": int(stats.last_error),
        }

    def close(self) -> None:
        dll = self._dll
        capture = self._capture
        if dll is None or not capture:
            return
        stats = self.stats()
        dll.mp_input_destroy(capture)
        self._capture = ctypes.c_void_p()
        self._dll = None
        if stats:
            self.log(
                "Native transformed-input stats "
                f"captured={stats['captured']} drained={stats['drained']} "
                f"dropped={stats['dropped']} max_depth={stats['max_queue_depth']}"
            )


class NativeSyntheticPenInjector:
    """Drop-in injector using a high-priority native report worker."""

    manages_frame_spacing = True

    def __init__(
        self,
        log: Callable[[str], None],
        *,
        library_path: str | Path | None = None,
    ) -> None:
        self.log = log
        self._desktop = _SyntheticPenInjector(log=log)
        self._library_path = Path(library_path) if library_path is not None else None
        self._dll: ctypes.WinDLL | None = None
        self._relay = ctypes.c_void_p()
        self._next_token = 1
        self.last_submission_token: int | None = None
        self._delivery_lock = threading.Lock()
        self._pending_deliveries: dict[int, dict[str, int | bool]] = {}

    def _load_library(self) -> tuple[ctypes.WinDLL, Path]:
        path = self._library_path or find_native_relay()
        if path is None:
            checked = "\n  ".join(str(item) for item in native_relay_path_candidates())
            raise RuntimeError(
                "The native synthetic relay is not built. Checked:\n  " + checked
            )
        dll = ctypes.WinDLL(str(path), use_last_error=True)
        dll.mp_synth_api_version.argtypes = []
        dll.mp_synth_api_version.restype = ctypes.c_uint32
        dll.mp_synth_create.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        dll.mp_synth_create.restype = ctypes.c_void_p
        dll.mp_synth_submit.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_uint32,
            ctypes.c_int32,
            ctypes.c_uint32,
            ctypes.c_uint64,
        ]
        dll.mp_synth_submit.restype = ctypes.c_int
        dll.mp_synth_submit_batch.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(NativeRelayInput),
            ctypes.c_uint32,
        ]
        dll.mp_synth_submit_batch.restype = ctypes.c_int
        dll.mp_synth_drain_completions.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(NativeRelayCompletion),
            ctypes.c_uint32,
        ]
        dll.mp_synth_drain_completions.restype = ctypes.c_uint32
        dll.mp_synth_wait_idle.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        dll.mp_synth_wait_idle.restype = ctypes.c_int
        dll.mp_synth_get_stats.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(NativeRelayStats),
            ctypes.c_uint32,
        ]
        dll.mp_synth_get_stats.restype = ctypes.c_int
        dll.mp_synth_destroy.argtypes = [ctypes.c_void_p]
        dll.mp_synth_destroy.restype = None
        return dll, path

    def open(self) -> None:
        dll, path = self._load_library()
        api_version = int(dll.mp_synth_api_version())
        if api_version != NATIVE_RELAY_API_VERSION:
            raise RuntimeError(
                f"Native synthetic relay API {api_version} is incompatible with "
                f"expected API {NATIVE_RELAY_API_VERSION}."
            )
        error = ctypes.c_uint32()
        relay = dll.mp_synth_create(NATIVE_FRAME_INTERVAL_US, ctypes.byref(error))
        if not relay:
            raise RuntimeError(
                f"Native synthetic relay startup failed, err={error.value}"
            )
        self._dll = dll
        self._relay = ctypes.c_void_p(relay)
        self.log(
            f"SYNTH native relay open api={api_version} "
            f"frame_interval={NATIVE_FRAME_INTERVAL_US}us path={path}"
        )

    def _stats(self) -> NativeRelayStats | None:
        if self._dll is None or not self._relay:
            return None
        stats = NativeRelayStats()
        stats.struct_size = ctypes.sizeof(stats)
        ok = bool(
            self._dll.mp_synth_get_stats(
                self._relay,
                ctypes.byref(stats),
                ctypes.sizeof(stats),
            )
        )
        return stats if ok else None

    def close(self) -> None:
        dll = self._dll
        relay = self._relay
        if dll is None or not relay:
            return
        drained = bool(dll.mp_synth_wait_idle(relay, 3000))
        stats = self._stats()
        if stats is not None:
            injected = max(1, int(stats.injected + stats.failed))
            self.log(
                "SYNTH native relay stats "
                f"submitted={stats.submitted} injected={stats.injected} "
                f"failed={stats.failed} queue_full={stats.queue_full} "
                f"max_depth={stats.max_queue_depth} "
                f"queue_avg={stats.total_queue_delay_us / injected:.1f}us "
                f"queue_max={stats.max_queue_delay_us}us "
                f"inject_avg={stats.total_inject_call_us / injected:.1f}us "
                f"inject_max={stats.max_inject_call_us}us drained={int(drained)}"
                f" completion_dropped={stats.completion_dropped}"
            )
        dll.mp_synth_destroy(relay)
        self._relay = ctypes.c_void_p()
        self._dll = None

    def inject(
        self,
        *,
        flags: int,
        x: int,
        y: int,
        pressure_1024: int,
        tag: str,
        tilt_x: int | None = None,
    ) -> tuple[bool, int]:
        if self._dll is None or not self._relay:
            return False, 6  # ERROR_INVALID_HANDLE
        token = self._next_token
        self._next_token += 1
        self.last_submission_token = token
        ctypes.set_last_error(0)
        ok = bool(
            self._dll.mp_synth_submit(
                self._relay,
                int(flags),
                int(x),
                int(y),
                max(0, min(1024, int(pressure_1024))),
                max(-90, min(90, int(tilt_x or 0))),
                int(tilt_x is not None),
                token,
            )
        )
        error = int(ctypes.get_last_error())
        if not ok:
            self.log(
                f"INJECT {tag} native submit failed err={error} flags=0x{flags:08X} "
                f"x={x} y={y} pressure={pressure_1024}"
            )
        return ok, error

    def inject_batch(
        self,
        reports: list[dict[str, Any]],
    ) -> tuple[bool, int, list[int]]:
        """Submit one Raw Input-derived path to the native scheduler."""
        if self._dll is None or not self._relay:
            return False, 6, []  # ERROR_INVALID_HANDLE
        if not reports:
            return True, 0, []
        tokens = list(range(self._next_token, self._next_token + len(reports)))
        self._next_token += len(reports)
        self.last_submission_token = tokens[-1]
        native_reports = (NativeRelayInput * len(reports))()
        for index, (report, token) in enumerate(zip(reports, tokens, strict=True)):
            tilt_x = report.get("tilt_x")
            native_reports[index] = NativeRelayInput(
                int(report["flags"]),
                int(report["x"]),
                int(report["y"]),
                max(0, min(1024, int(report["pressure_1024"]))),
                max(-90, min(90, int(tilt_x or 0))),
                int(tilt_x is not None),
                token,
            )
        ctypes.set_last_error(0)
        ok = bool(
            self._dll.mp_synth_submit_batch(
                self._relay,
                native_reports,
                len(reports),
            )
        )
        error = int(ctypes.get_last_error())
        if not ok:
            self.log(
                f"INJECT native batch submit failed err={error} "
                f"reports={len(reports)}"
            )
        return ok, error, tokens

    def wait_idle(self, timeout_ms: int = 10) -> bool:
        if self._dll is None or not self._relay:
            return False
        return bool(self._dll.mp_synth_wait_idle(self._relay, max(0, int(timeout_ms))))

    def drain_delivery_events(self, capacity: int = 4096) -> list[dict[str, int | bool]]:
        """Return completed native reports without blocking the input path."""
        if self._dll is None or not self._relay or capacity <= 0:
            return []
        batch = (NativeRelayCompletion * capacity)()
        count = int(
            self._dll.mp_synth_drain_completions(
                self._relay,
                batch,
                capacity,
            )
        )
        drained = [
            {
                "token": int(item.token),
                "submitted_qpc": int(item.submitted_qpc),
                "inject_begin_qpc": int(item.inject_begin_qpc),
                "completed_qpc": int(item.completed_qpc),
                "qpc_frequency": int(item.qpc_frequency),
                "flags": int(item.flags),
                "x": int(item.x),
                "y": int(item.y),
                "pressure": int(item.pressure),
                "success": bool(item.success),
                "error": int(item.error),
                "queue_delay_us": int(item.queue_delay_us),
                "inject_call_us": int(item.inject_call_us),
            }
            for item in batch[:count]
        ]
        with self._delivery_lock:
            pending = list(self._pending_deliveries.values())
            self._pending_deliveries.clear()
        return pending + drained

    def collect_delivery_events(
        self,
        tokens: set[int] | frozenset[int],
        timeout_ms: int = 25,
    ) -> list[dict[str, int | bool]]:
        """Collect selected completions on a diagnostics thread."""
        wanted = {int(token) for token in tokens}
        if not wanted:
            return []
        deadline = time.perf_counter() + max(0, int(timeout_ms)) / 1000.0
        selected: dict[int, dict[str, int | bool]] = {}
        while wanted - selected.keys():
            events = self.drain_delivery_events()
            with self._delivery_lock:
                for event in events:
                    token = int(event["token"])
                    if token in wanted:
                        selected[token] = event
                    else:
                        self._pending_deliveries[token] = event
            if not (wanted - selected.keys()) or time.perf_counter() >= deadline:
                break
            time.sleep(0.0005)
        return [selected[token] for token in sorted(selected)]

    def get_cursor_pos(self) -> tuple[int, int]:
        return self._desktop.get_cursor_pos()

    def is_lmb_down(self) -> bool:
        return self._desktop.is_lmb_down()

    def is_rmb_down(self) -> bool:
        return self._desktop.is_rmb_down()

    def emit_left_click(self) -> None:
        self._desktop.emit_left_click()

    def emit_right_click(self) -> None:
        self._desktop.emit_right_click()
