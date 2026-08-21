"""Single Python binding for the native synthetic relay DLL."""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NATIVE_RELAY_FILENAME = "mouse_pressure_synthetic_relay.dll"
NATIVE_RELAY_API_VERSION = 4


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
        ("rotation", ctypes.c_uint32),
        ("tilt_x", ctypes.c_int32),
        ("tilt_y", ctypes.c_int32),
        ("pen_mask", ctypes.c_uint32),
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


@dataclass(frozen=True)
class NativeRelayLibrary:
    """A loaded, declared, and version-compatible relay library."""

    dll: Any
    path: Path
    api_version: int

    def create_input_capture(self) -> NativeInputCaptureHandle:
        """Create an owned transformed-input capture handle."""
        error = ctypes.c_uint32()
        handle = self.dll.mp_input_create(ctypes.byref(error))
        if not handle:
            raise RuntimeError(
                f"Native transformed-input startup failed, err={error.value}"
            )
        return NativeInputCaptureHandle(self, ctypes.c_void_p(handle))

    def create_synthetic_relay(
        self,
        frame_interval_us: int,
    ) -> NativeSyntheticRelayHandle:
        """Create an owned synthetic-report relay handle."""
        error = ctypes.c_uint32()
        handle = self.dll.mp_synth_create(
            max(0, int(frame_interval_us)),
            ctypes.byref(error),
        )
        if not handle:
            raise RuntimeError(
                f"Native synthetic relay startup failed, err={error.value}"
            )
        return NativeSyntheticRelayHandle(self, ctypes.c_void_p(handle))


class NativeInputCaptureHandle:
    """Own one native transformed-input handle and its destruction."""

    def __init__(
        self,
        library: NativeRelayLibrary,
        handle: ctypes.c_void_p,
    ) -> None:
        self.library = library
        self._handle = handle

    @property
    def is_open(self) -> bool:
        return bool(self._handle)

    def drain_moves(self, capacity: int) -> list[NativeInputMove]:
        if not self._handle or capacity <= 0:
            return []
        count_requested = max(1, int(capacity))
        batch = (NativeInputMove * count_requested)()
        count = int(
            self.library.dll.mp_input_drain_moves(
                self._handle,
                batch,
                count_requested,
            )
        )
        return list(batch[:count])

    def stats(self) -> NativeInputStats | None:
        if not self._handle:
            return None
        stats = NativeInputStats()
        stats.struct_size = ctypes.sizeof(stats)
        ok = bool(
            self.library.dll.mp_input_get_stats(
                self._handle,
                ctypes.byref(stats),
                ctypes.sizeof(stats),
            )
        )
        return stats if ok else None

    def close(self) -> None:
        handle = self._handle
        if not handle:
            return
        self._handle = ctypes.c_void_p()
        self.library.dll.mp_input_destroy(handle)


class NativeSyntheticRelayHandle:
    """Own one native synthetic relay handle and all operations on it."""

    def __init__(
        self,
        library: NativeRelayLibrary,
        handle: ctypes.c_void_p,
    ) -> None:
        self.library = library
        self._handle = handle

    @property
    def is_open(self) -> bool:
        return bool(self._handle)

    def submit(
        self,
        *,
        flags: int,
        x: int,
        y: int,
        pressure: int,
        rotation: int,
        tilt_x: int,
        tilt_y: int,
        pen_mask: int,
        token: int,
    ) -> tuple[bool, int]:
        if not self._handle:
            return False, 6
        ctypes.set_last_error(0)
        ok = bool(
            self.library.dll.mp_synth_submit(
                self._handle,
                flags,
                x,
                y,
                pressure,
                rotation,
                tilt_x,
                tilt_y,
                pen_mask,
                token,
            )
        )
        return ok, int(ctypes.get_last_error())

    def submit_batch(
        self,
        reports: Any,
        count: int,
    ) -> tuple[bool, int]:
        if not self._handle:
            return False, 6
        ctypes.set_last_error(0)
        ok = bool(
            self.library.dll.mp_synth_submit_batch(
                self._handle,
                reports,
                max(0, int(count)),
            )
        )
        return ok, int(ctypes.get_last_error())

    def drain_completions(self, capacity: int) -> list[NativeRelayCompletion]:
        if not self._handle or capacity <= 0:
            return []
        count_requested = max(1, int(capacity))
        batch = (NativeRelayCompletion * count_requested)()
        count = int(
            self.library.dll.mp_synth_drain_completions(
                self._handle,
                batch,
                count_requested,
            )
        )
        return list(batch[:count])

    def wait_idle(self, timeout_ms: int) -> bool:
        return bool(
            self._handle
            and self.library.dll.mp_synth_wait_idle(
                self._handle,
                max(0, int(timeout_ms)),
            )
        )

    def stats(self) -> NativeRelayStats | None:
        if not self._handle:
            return None
        stats = NativeRelayStats()
        stats.struct_size = ctypes.sizeof(stats)
        ok = bool(
            self.library.dll.mp_synth_get_stats(
                self._handle,
                ctypes.byref(stats),
                ctypes.sizeof(stats),
            )
        )
        return stats if ok else None

    def close(self) -> None:
        handle = self._handle
        if not handle:
            return
        self._handle = ctypes.c_void_p()
        self.library.dll.mp_synth_destroy(handle)


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


def _declare_functions(dll: Any) -> None:
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
        ctypes.c_uint32,
        ctypes.c_int32,
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


def load_native_relay(
    library_path: str | Path | None = None,
) -> NativeRelayLibrary:
    """Discover, declare, and validate the native relay DLL."""
    path = Path(library_path) if library_path is not None else find_native_relay()
    if path is None:
        checked = "\n  ".join(str(item) for item in native_relay_path_candidates())
        raise RuntimeError("The native relay is not built. Checked:\n  " + checked)

    dll = ctypes.WinDLL(str(path), use_last_error=True)
    _declare_functions(dll)
    api_version = int(dll.mp_synth_api_version())
    if api_version != NATIVE_RELAY_API_VERSION:
        raise RuntimeError(
            f"Native relay API {api_version} is incompatible with expected API "
            f"{NATIVE_RELAY_API_VERSION}."
        )
    return NativeRelayLibrary(dll=dll, path=path, api_version=api_version)
