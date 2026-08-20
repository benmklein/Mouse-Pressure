"""Canonical synthetic pen runtime."""

from __future__ import annotations

import ctypes
import math
import re
import threading
import time
import winreg
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from mouse_pressure.bridge.curves import PressureConfig, map_normalized_pressure
from mouse_pressure.bridge.stroke_trace import StrokeTraceRecorder
from mouse_pressure.sniff.hidpp_pressure import (
    PRESSURE_MODE3_ADDR,
    PressureHidppSession,
    normalize_raw_pressure,
)

PT_PEN = 3
POINTER_FEEDBACK_DEFAULT = 1
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02

POINTER_FLAG_NEW = 0x00000001
POINTER_FLAG_INRANGE = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
POINTER_FLAG_FIRSTBUTTON = 0x00000010
POINTER_FLAG_PRIMARY = 0x00002000
POINTER_FLAG_DOWN = 0x00010000
POINTER_FLAG_UPDATE = 0x00020000
POINTER_FLAG_UP = 0x00040000

POINTER_CHANGE_NONE = 0
POINTER_CHANGE_FIRSTBUTTON_DOWN = 1
POINTER_CHANGE_FIRSTBUTTON_UP = 2
MK_LBUTTON = 0x0001

PEN_FLAG_NONE = 0x00000000
PEN_MASK_PRESSURE = 0x00000001
PEN_MASK_TILT_X = 0x00000004

WH_MOUSE_LL = 14
HC_ACTION = 0
LLMHF_INJECTED = 0x00000001
WM_LBUTTONDOWN = 0x0201
WM_MOUSEMOVE = 0x0200
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_RBUTTONDBLCLK = 0x0206
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONUP = 0x00A2
WM_NCRBUTTONDOWN = 0x00A4
WM_NCRBUTTONUP = 0x00A5
PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF
MWMO_INPUTAVAILABLE = 0x0004
WM_HOTKEY = 0x0312
WM_INPUT = 0x00FF
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
VK_F12 = 0x7B
EMERGENCY_HOTKEY_ID = 0x5353
SUPPRESSOR_HEARTBEAT_TIMEOUT_S = 3.0
REPORT_LONG = 0x11
ERROR_NOT_READY = 21
# Windows assigns an input timestamp when POINTER_INFO leaves dwTime and
# PerformanceCount at zero. Calls closer than 0.1 ms can therefore collide and
# return ERROR_NOT_READY. A small margin keeps every interpolated path point in
# its own input frame without materially adding stroke latency.
MIN_POINTER_FRAME_INTERVAL_S = 0.00012
# A 32-point cap left 5–7 px gaps on ~4,000 px/s strokes. Forty-eight stays
# below the observed ~16 ms physical-anchor interval while giving Krita a
# materially denser pressure/path ramp. Higher values start delaying the next
# Raw Input anchor more than they improve the reconstructed segment.
MAX_CONTACT_POINTS_PER_UPDATE = 48
MAX_DIRECT_CONTACT_POINTS_PER_UPDATE = 12
# Adaptive batches target these upper bounds before falling back to the hard
# cap above.  This keeps ordinary reports short while spending more reports on
# fast geometry or a large pressure transition where the extra detail matters.
TARGET_CONTACT_SPACING_PX = 2.5
TARGET_CONTACT_PRESSURE_STEP = 18
# VMulti is virtual *hardware*, so its pointer promotion is not guaranteed to
# carry LLMHF_INJECTED.  Match only the immediate, near-identical hook feedback
# from a report we just wrote; a wider window risks swallowing real mouse input.
PEN_FEEDBACK_WINDOW_S = 0.008
PEN_FEEDBACK_TOLERANCE_PX = 2
# The low-level hook and WM_INPUT are delivered on different queues.  Once a
# physical Raw Input device owns the contact, its button-up packet is the only
# event guaranteed to be ordered after that device's final movement packet.
# Keep the hook as a fail-safe for a lost Raw Input release without letting it
# cut off the end of ordinary fast strokes.
RAW_BUTTON_UP_FALLBACK_S = 0.012
# Raw Input and the low-level button hook are separate queues. Experimental
# immediate starts wait briefly for the hook's authoritative desktop anchor
# when WM_INPUT wins that race. The observed tail was 3.1 ms, so 4 ms covers
# it without restoring the old pressure-tick-sized scheduling delay.
BUTTON_ANCHOR_WAIT_S = 0.004
# Delay only falling pressure long enough to distinguish an intentional
# pressure reduction from the mechanical release ramp at the end of a click.
# Pointer motion and rising pressure remain immediate.
CLEAN_STROKE_ENDING_HOLD_S = 0.025
RIM_TYPEMOUSE = 0
RID_INPUT = 0x10000003
RIDI_DEVICENAME = 0x20000007
RIDEV_REMOVE = 0x00000001
RIDEV_INPUTSINK = 0x00000100
RI_MOUSE_LEFT_BUTTON_DOWN = 0x0001
RI_MOUSE_LEFT_BUTTON_UP = 0x0002
RI_MOUSE_RIGHT_BUTTON_DOWN = 0x0004
RI_MOUSE_RIGHT_BUTTON_UP = 0x0008
MOUSE_MOVE_ABSOLUTE = 0x0001
HID_USAGE_PAGE_GENERIC = 0x01
HID_USAGE_GENERIC_MOUSE = 0x02
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


@dataclass
class SyntheticPenConfig:
    contact_threshold: int = 10
    release_threshold: int = 6
    contact_source: str = "lmb_and_pressure"
    pressure_mode: str = "absolute"
    rise_per_frame: int = 256
    fall_per_frame: int = 512
    # Number of high-rate pen ticks used to reach each new hardware pressure.
    # Runtime paths set this from injection_hz / observed_pressure_hz.
    pressure_interp_steps: int = 1
    min_contact_pressure: int = 0
    path_stabilization: int = 0
    pressure_influence: int = 100
    onset_buffer: bool = True
    true_low_latency: bool = False
    # VMulti must build its cursor path from device-scoped Raw Input deltas,
    # because its promoted pointer can look like native mouse feedback.  The
    # Windows synthetic-pointer backend can instead use the OS-transformed
    # mouse coordinates, preserving the user's normal pointer speed/DPI feel.
    allow_raw_direct_motion: bool = True
    stationary_pressure_updates: bool = False
    immediate_button_wake: bool = False
    clean_stroke_endings: bool = False
    suppress_lmb: bool = False
    suppress_rmb: bool = False
    left_output_target: str = "pressure"
    right_output_target: str = "pressure"
    debug_mode: bool = True
    output_backend: str = "synthetic"
    right_contact_threshold: int | None = None
    right_release_threshold: int | None = None
    right_min_contact_pressure: int | None = None
    right_path_stabilization: int | None = None
    right_pressure_influence: int | None = None
    right_onset_buffer: bool | None = None
    right_true_low_latency: bool | None = None
    right_stationary_pressure_updates: bool | None = None
    right_immediate_button_wake: bool | None = None
    right_clean_stroke_endings: bool | None = None
    no_click_through: bool = False
    click_max_ms: int = 220
    click_move_px: int = 6
    click_pressure_max: int = 12
    release_teardown: bool = False
    trace_dir: str | None = None
    trace_raw_min: int | None = None
    trace_raw_max: int | None = None
    trace_curve: str | None = None
    trace_curve_strength: float | None = None
    right_trace_raw_min: int | None = None
    right_trace_raw_max: int | None = None
    right_trace_curve: str | None = None
    right_trace_curve_strength: float | None = None


@dataclass(frozen=True)
class SyntheticPenSample:
    x: int
    y: int
    mapped_1023: int
    pen_1024: int
    state: str
    lmb_down: bool
    lmb_physical: bool
    status: int
    injected: bool
    failed: bool


class POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerType", ctypes.c_uint32),
        ("pointerId", ctypes.c_uint32),
        ("frameId", ctypes.c_uint32),
        ("pointerFlags", ctypes.c_uint32),
        ("sourceDevice", ctypes.c_void_p),
        ("hwndTarget", ctypes.c_void_p),
        ("ptPixelLocation", wintypes.POINT),
        ("ptHimetricLocation", wintypes.POINT),
        ("ptPixelLocationRaw", wintypes.POINT),
        ("ptHimetricLocationRaw", wintypes.POINT),
        ("dwTime", ctypes.c_uint32),
        ("historyCount", ctypes.c_uint32),
        ("InputData", ctypes.c_int32),
        ("dwKeyStates", ctypes.c_uint32),
        ("PerformanceCount", ctypes.c_uint64),
        ("ButtonChangeType", ctypes.c_uint32),
    ]


class POINTER_PEN_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("penFlags", ctypes.c_uint32),
        ("penMask", ctypes.c_uint32),
        ("pressure", ctypes.c_uint32),
        ("rotation", ctypes.c_uint32),
        ("tiltX", ctypes.c_int32),
        ("tiltY", ctypes.c_int32),
    ]


class POINTER_TYPE_UNION(ctypes.Union):
    _fields_ = [("penInfo", POINTER_PEN_INFO)]


class POINTER_TYPE_INFO(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", POINTER_TYPE_UNION)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", ctypes.c_ushort),
        ("usUsage", ctypes.c_ushort),
        ("dwFlags", ctypes.c_uint32),
        ("hwndTarget", ctypes.c_void_p),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", ctypes.c_uint32),
        ("dwSize", ctypes.c_uint32),
        ("hDevice", ctypes.c_void_p),
        ("wParam", ctypes.c_size_t),
    ]


class _RAWMOUSE_BUTTON_FIELDS(ctypes.Structure):
    _fields_ = [
        ("usButtonFlags", ctypes.c_ushort),
        ("usButtonData", ctypes.c_ushort),
    ]


class _RAWMOUSE_BUTTONS(ctypes.Union):
    _anonymous_ = ("fields",)
    _fields_ = [
        ("ulButtons", ctypes.c_uint32),
        ("fields", _RAWMOUSE_BUTTON_FIELDS),
    ]


class RAWMOUSE(ctypes.Structure):
    _anonymous_ = ("buttons",)
    _fields_ = [
        ("usFlags", ctypes.c_ushort),
        ("buttons", _RAWMOUSE_BUTTONS),
        ("ulRawButtons", ctypes.c_uint32),
        ("lLastX", ctypes.c_long),
        ("lLastY", ctypes.c_long),
        ("ulExtraInformation", ctypes.c_uint32),
    ]


class _RAWINPUT_DATA(ctypes.Union):
    _fields_ = [("mouse", RAWMOUSE)]


class RAWINPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("header", RAWINPUTHEADER), ("data", _RAWINPUT_DATA)]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_size_t,
    ctypes.c_ssize_t,
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint32),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def clamp_i(v: int, lo: int, hi: int) -> int:
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def clamp_f(v: float, lo: float, hi: float) -> float:
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def map_1023_to_1024(v: int) -> int:
    v = clamp_i(v, 0, 1023)
    return (v * 1024 + 511) // 1023


def map_1024_to_1023(v: int) -> int:
    """Return the closest internal pressure for a value sent to Windows."""
    v = clamp_i(v, 0, 1024)
    return (v * 1023 + 512) // 1024


class _SyntheticPenInjector:
    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.device = ctypes.c_void_p()
        self.dpi = 96
        self.screen_w = 1
        self.screen_h = 1
        self.pointer_id = 1
        self.pti = POINTER_TYPE_INFO()
        self.pti.type = PT_PEN

        self.user32.CreateSyntheticPointerDevice.argtypes = [ctypes.c_uint32, ctypes.c_ulong, ctypes.c_uint32]
        self.user32.CreateSyntheticPointerDevice.restype = ctypes.c_void_p
        self.user32.InjectSyntheticPointerInput.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(POINTER_TYPE_INFO),
            ctypes.c_uint32,
        ]
        self.user32.InjectSyntheticPointerInput.restype = ctypes.c_int
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user32.GetCursorPos.restype = ctypes.c_int
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user32.GetSystemMetrics.restype = ctypes.c_int
        self.user32.mouse_event.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self.user32.mouse_event.restype = None

        if hasattr(self.user32, "GetDpiForSystem"):
            self.user32.GetDpiForSystem.argtypes = []
            self.user32.GetDpiForSystem.restype = ctypes.c_uint32

        destroy = getattr(self.user32, "DestroySyntheticPointerDevice", None)
        self._destroy = destroy
        if destroy is not None:
            destroy.argtypes = [ctypes.c_void_p]
            destroy.restype = None

    def open(self) -> None:
        ctypes.set_last_error(0)
        dev = self.user32.CreateSyntheticPointerDevice(PT_PEN, 1, POINTER_FEEDBACK_DEFAULT)
        err = ctypes.get_last_error()
        if not dev:
            raise RuntimeError(f"CreateSyntheticPointerDevice failed, err={err}")
        self.device = ctypes.c_void_p(dev)
        if hasattr(self.user32, "GetDpiForSystem"):
            dpi = int(self.user32.GetDpiForSystem())
            if dpi > 0:
                self.dpi = dpi
        self.screen_w = max(1, int(self.user32.GetSystemMetrics(0)))
        self.screen_h = max(1, int(self.user32.GetSystemMetrics(1)))
        self.log(f"SYNTH open handle=0x{int(dev):X} screen={self.screen_w}x{self.screen_h} dpi={self.dpi}")

    def close(self) -> None:
        if self.device and self._destroy is not None:
            try:
                self._destroy(self.device)
            except Exception:
                pass
        self.device = ctypes.c_void_p()

    def get_cursor_pos(self) -> tuple[int, int]:
        pt = wintypes.POINT()
        ok = bool(self.user32.GetCursorPos(ctypes.byref(pt)))
        if not ok:
            return (0, 0)
        return (int(pt.x), int(pt.y))

    def is_lmb_down(self) -> bool:
        return bool(int(self.user32.GetAsyncKeyState(VK_LBUTTON)) & 0x8000)

    def is_rmb_down(self) -> bool:
        return bool(int(self.user32.GetAsyncKeyState(VK_RBUTTON)) & 0x8000)

    def emit_left_click(self) -> None:
        self.user32.mouse_event(0x0002, 0, 0, 0, None)
        self.user32.mouse_event(0x0004, 0, 0, 0, None)

    def emit_right_click(self) -> None:
        self.user32.mouse_event(0x0008, 0, 0, 0, None)
        self.user32.mouse_event(0x0010, 0, 0, 0, None)

    def _to_himetric(self, px: int) -> int:
        return int(round(float(px) * 2540.0 / float(self.dpi)))

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
        pi = self.pti.penInfo.pointerInfo
        pi.pointerType = PT_PEN
        pi.pointerId = self.pointer_id
        pi.frameId = 0
        pi.pointerFlags = flags
        pi.sourceDevice = None
        pi.hwndTarget = None
        pi.ptPixelLocation = wintypes.POINT(x, y)
        pi.ptPixelLocationRaw = wintypes.POINT(x, y)
        hx = self._to_himetric(x)
        hy = self._to_himetric(y)
        pi.ptHimetricLocation = wintypes.POINT(hx, hy)
        pi.ptHimetricLocationRaw = wintypes.POINT(hx, hy)
        pi.dwTime = 0
        pi.historyCount = 1
        pi.InputData = 0
        pi.dwKeyStates = MK_LBUTTON if flags & POINTER_FLAG_FIRSTBUTTON else 0
        pi.PerformanceCount = 0
        if flags & POINTER_FLAG_DOWN:
            pi.ButtonChangeType = POINTER_CHANGE_FIRSTBUTTON_DOWN
        elif flags & POINTER_FLAG_UP:
            pi.ButtonChangeType = POINTER_CHANGE_FIRSTBUTTON_UP
        else:
            pi.ButtonChangeType = POINTER_CHANGE_NONE

        self.pti.penInfo.penFlags = PEN_FLAG_NONE
        self.pti.penInfo.penMask = PEN_MASK_PRESSURE
        self.pti.penInfo.pressure = clamp_i(pressure_1024, 0, 1024)
        self.pti.penInfo.rotation = 0
        if tilt_x is not None:
            self.pti.penInfo.penMask |= PEN_MASK_TILT_X
        self.pti.penInfo.tiltX = clamp_i(int(tilt_x or 0), -90, 90)
        self.pti.penInfo.tiltY = 0

        ctypes.set_last_error(0)
        ok = bool(self.user32.InjectSyntheticPointerInput(self.device, ctypes.byref(self.pti), 1))
        err = ctypes.get_last_error()
        if not ok:
            self.log(
                f"INJECT {tag} failed err={err} flags=0x{flags:08X} x={x} y={y} "
                f"pressure={self.pti.penInfo.pressure} xtilt={self.pti.penInfo.tiltX}"
            )
        return ok, err


class _MouseLmbSuppressor:
    def __init__(
        self,
        log: Callable[[str], None],
        *,
        suppress_left: bool = True,
        suppress_right: bool = False,
        debug_mode: bool = True,
        allow_raw_direct_motion: bool = True,
        left_button_owns_contact: bool = True,
        right_button_owns_contact: bool = True,
    ) -> None:
        self.log = log
        self.suppress_left = bool(suppress_left)
        self.suppress_right = bool(suppress_right)
        self.debug_mode = bool(debug_mode)
        self._left_button_owns_contact = bool(left_button_owns_contact)
        self._right_button_owns_contact = bool(right_button_owns_contact)
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.enabled = False
        self.hook = ctypes.c_void_p()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._input_ready = threading.Event()
        self._proc = None
        self._wnd_proc = None
        self._raw_hwnd = ctypes.c_void_p()
        self._raw_class_atom = 0
        self._raw_class_name = f"MousePressureRawInput_{id(self):X}"
        self._raw_input_active = False
        self._raw_direct_mode = bool(
            allow_raw_direct_motion and self._raw_counts_match_desktop_pixels()
        )
        self._raw_device_handle = 0
        self._raw_motion_device_handle = 0
        self._selected_raw_identity = ""
        self._raw_device_identities: dict[int, str] = {}
        self._raw_contact_active = False
        self._contact_anchor_ready = False
        self._first_contact_pending = True
        self._accepted_motion_count = 0
        self._raw_x = 0
        self._raw_y = 0
        self._logical_position_initialized = False
        self._idle_raw_position_fresh = False
        self._cursor_baseline_x = 0
        self._cursor_baseline_y = 0
        self._cursor_baseline_initialized = False
        self._button_anchor: tuple[float, int, int] | None = None
        self._button_anchor_wait_button: str | None = None
        self._button_anchor_wait_started_at = 0.0
        self._button_anchor_wait_dx = 0
        self._button_anchor_wait_dy = 0
        self._button_anchor_wait_timed_out = False
        self._button_anchor_wait_lock = threading.Lock()
        self._movement_callback: Callable[[], None] | None = None
        self._native_input_capture: Any | None = None
        self._native_input_capture_active = False
        self._button_down_wake_callback: Callable[[str], bool] | None = None
        self._precontact_lock = threading.Lock()
        self._precontact_baseline: dict[str, float | None] = {
            "left": None,
            "right": None,
        }
        self._precontact_started_at = {"left": 0.0, "right": 0.0}
        self._idle_raw_history: deque[
            tuple[float, int, str, int, int]
        ] = deque(maxlen=1024)
        self._timing_callback: (
            Callable[[str, float, dict[str, int | float | str]], None] | None
        ) = None
        self._force_stop_callback: Callable[[str], None] | None = None
        self._force_stop_requested = False
        self._lmb_down = False
        self._rmb_down = False
        self._hook_lmb_up_pending_at = 0.0
        self._hook_rmb_up_pending_at = 0.0
        self._idle_raw_position_fresh = False
        self._last_heartbeat = 0.0
        self._fail_open_logged = False
        self._position_lock = threading.Lock()
        self._motion_diag_lock = threading.Lock()
        self._motion_diag: dict[str, float | int] = {}
        self._hardware_positions: deque[tuple[float, int, int]] = deque(maxlen=512)
        self._recent_injected_positions: deque[tuple[float, int, int]] = deque(maxlen=256)
        self._pending_hook_positions: deque[tuple[float, int, int]] = deque(maxlen=64)
        self._reset_motion_diagnostics()

        self.user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        self.user32.SetWindowsHookExW.restype = ctypes.c_void_p
        self.user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        self.user32.UnhookWindowsHookEx.restype = ctypes.c_int
        self.user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p]
        self.user32.CallNextHookEx.restype = ctypes.c_longlong
        self.user32.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        self.user32.PeekMessageW.restype = ctypes.c_int
        self.user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.TranslateMessage.restype = ctypes.c_int
        self.user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.DispatchMessageW.restype = ctypes.c_longlong
        self.user32.MsgWaitForMultipleObjectsEx.argtypes = [
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self.user32.MsgWaitForMultipleObjectsEx.restype = ctypes.c_uint32
        self.user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
        self.user32.RegisterHotKey.restype = ctypes.c_int
        self.user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.user32.UnregisterHotKey.restype = ctypes.c_int
        self.user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        self.user32.RegisterClassW.restype = ctypes.c_ushort
        self.user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
        self.user32.UnregisterClassW.restype = ctypes.c_int
        self.user32.CreateWindowExW.argtypes = [
            ctypes.c_uint32,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.user32.CreateWindowExW.restype = ctypes.c_void_p
        self.user32.DestroyWindow.argtypes = [ctypes.c_void_p]
        self.user32.DestroyWindow.restype = ctypes.c_int
        self.user32.DefWindowProcW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        self.user32.DefWindowProcW.restype = ctypes.c_ssize_t
        self.user32.RegisterRawInputDevices.argtypes = [
            ctypes.POINTER(RAWINPUTDEVICE),
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self.user32.RegisterRawInputDevices.restype = ctypes.c_int
        self.user32.GetRawInputData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_uint32,
        ]
        self.user32.GetRawInputData.restype = ctypes.c_uint32
        self.user32.GetRawInputBuffer.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_uint32,
        ]
        self.user32.GetRawInputBuffer.restype = ctypes.c_uint32
        self.user32.GetRawInputDeviceInfoW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.user32.GetRawInputDeviceInfoW.restype = ctypes.c_uint32
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user32.GetCursorPos.restype = ctypes.c_int
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user32.GetSystemMetrics.restype = ctypes.c_int
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = ctypes.c_void_p

    @staticmethod
    def _raw_counts_match_desktop_pixels() -> bool:
        """Detect exact one-device-count to one-desktop-pixel Windows motion."""
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Control Panel\Mouse",
            ) as key:
                sensitivity = str(winreg.QueryValueEx(key, "MouseSensitivity")[0])
                acceleration = str(winreg.QueryValueEx(key, "MouseSpeed")[0])
        except OSError:
            return False
        return sensitivity == "10" and acceleration == "0"

    def start(self) -> None:
        if self._thread is not None:
            return
        self.enabled = True
        self._last_heartbeat = time.perf_counter()
        self._fail_open_logged = False
        self._force_stop_requested = False
        self._stop.clear()
        self._ready.clear()
        self._input_ready.clear()
        self._thread = threading.Thread(target=self._run, name="lmb-suppressor", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=2.0):
            self.fail_open("mouse input hook startup timed out")
            raise RuntimeError("The mouse input hook did not become ready")
        if not self.hook:
            self.fail_open("mouse input hook could not be installed")
            raise RuntimeError("The mouse input hook could not be installed")

    def stop(self) -> None:
        self.enabled = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._lmb_down = False
        self._rmb_down = False
        self._hook_lmb_up_pending_at = 0.0
        self._hook_rmb_up_pending_at = 0.0
        with self._button_anchor_wait_lock:
            self._button_anchor_wait_button = None
            self._button_anchor_wait_started_at = 0.0
            self._button_anchor_wait_dx = 0
            self._button_anchor_wait_dy = 0

    def set_force_stop_callback(
        self,
        callback: Callable[[str], None] | None,
    ) -> None:
        self._force_stop_callback = callback

    def set_timing_callback(
        self,
        callback: Callable[[str, float, dict[str, int | float | str]], None] | None,
    ) -> None:
        """Attach an experiment-only observer to native input timestamps.

        The callback never participates in suppression, contact decisions, or
        coordinate publication.  It is used by the isolated timing-capture
        script and is deliberately absent from the normal runtime path.
        """
        self._timing_callback = callback

    def _emit_timing(
        self,
        kind: str,
        observed_at: float,
        **fields: int | float | str,
    ) -> None:
        callback = self._timing_callback
        if callback is None:
            return
        try:
            callback(str(kind), float(observed_at), dict(fields))
        except Exception:
            # Diagnostics must never interfere with the fail-open input path.
            return

    def is_lmb_down(self) -> bool:
        self._resolve_deferred_hook_releases()
        if self._button_anchor_blocked("left"):
            return False
        return self._lmb_down if self.enabled else False

    def is_rmb_down(self) -> bool:
        self._resolve_deferred_hook_releases()
        if self._button_anchor_blocked("right"):
            return False
        return self._rmb_down if self.enabled else False

    def contact_anchor_ready(self, button: str) -> bool:
        """Return whether the current contact has a physical desktop anchor."""
        if button == "right" and not self._right_button_owns_contact:
            return True
        if button != "right" and not self._left_button_owns_contact:
            return True
        return bool(self._contact_anchor_ready)

    def _button_anchor_blocked(self, button: str) -> bool:
        with self._button_anchor_wait_lock:
            if self._button_anchor_wait_button != button:
                return False
            elapsed = time.perf_counter() - self._button_anchor_wait_started_at
            if elapsed < BUTTON_ANCHOR_WAIT_S:
                return True
            self._button_anchor_wait_button = None
            self._button_anchor_wait_started_at = 0.0
            self._button_anchor_wait_dx = 0
            self._button_anchor_wait_dy = 0
            self._button_anchor_wait_timed_out = True

        # Fail open after the bounded wait. GetCursorPos is the safest
        # available fallback at the button instant; queued Raw Input deltas
        # are already reflected in that OS coordinate.
        x, y = self._cursor_position()
        with self._position_lock:
            self._raw_x, self._raw_y = int(x), int(y)
            self._cursor_baseline_x, self._cursor_baseline_y = int(x), int(y)
            self._cursor_baseline_initialized = True
            self._hardware_positions.clear()
        self._add_motion_diagnostics(
            anchor_wait_timeout=1,
            anchor_wait_ms=elapsed * 1000.0,
        )
        return False

    def _resolve_deferred_hook_releases(self, now: float | None = None) -> None:
        """Fail open if a device-scoped Raw Input button-up never arrives."""
        observed_at = time.perf_counter() if now is None else float(now)
        timed_out = False
        if (
            self._hook_lmb_up_pending_at > 0.0
            and observed_at - self._hook_lmb_up_pending_at
            >= RAW_BUTTON_UP_FALLBACK_S
        ):
            self._lmb_down = False
            self._hook_lmb_up_pending_at = 0.0
            timed_out = True
        if (
            self._hook_rmb_up_pending_at > 0.0
            and observed_at - self._hook_rmb_up_pending_at
            >= RAW_BUTTON_UP_FALLBACK_S
        ):
            self._rmb_down = False
            self._hook_rmb_up_pending_at = 0.0
            timed_out = True
        if timed_out:
            self._raw_contact_active = self._contact_button_down()
            self._add_motion_diagnostics(hook_up_timeout=1)

    def _handle_physical_hook_button(
        self,
        msg: int,
        *,
        observed_at: float,
        x: int,
        y: int,
    ) -> None:
        """Update early hook state without outracing device-scoped Raw Input."""
        hook_kind = {
            WM_LBUTTONDOWN: "hook_left_down",
            WM_NCLBUTTONDOWN: "hook_left_down",
            WM_LBUTTONUP: "hook_left_up",
            WM_NCLBUTTONUP: "hook_left_up",
            WM_RBUTTONDOWN: "hook_right_down",
            WM_NCRBUTTONDOWN: "hook_right_down",
            WM_RBUTTONUP: "hook_right_up",
            WM_NCRBUTTONUP: "hook_right_up",
        }.get(int(msg))
        if hook_kind is not None:
            self._emit_timing(hook_kind, observed_at, x=int(x), y=int(y))
        if msg in (WM_LBUTTONDOWN, WM_NCLBUTTONDOWN):
            self._lmb_down = True
            self._hook_lmb_up_pending_at = 0.0
            self._button_anchor = (float(observed_at), int(x), int(y))
            if self._left_button_owns_contact:
                self._reanchor_new_contact_from_hook(observed_at, x, y)
        elif msg in (WM_LBUTTONUP, WM_NCLBUTTONUP):
            if self._raw_input_active and self._raw_contact_active:
                self._hook_lmb_up_pending_at = float(observed_at)
                self._add_motion_diagnostics(hook_up_deferred=1)
            else:
                self._lmb_down = False
        elif msg in (WM_RBUTTONDOWN, WM_NCRBUTTONDOWN):
            self._rmb_down = True
            self._hook_rmb_up_pending_at = 0.0
            self._button_anchor = (float(observed_at), int(x), int(y))
            if self._right_button_owns_contact:
                self._reanchor_new_contact_from_hook(observed_at, x, y)
        elif msg in (WM_RBUTTONUP, WM_NCRBUTTONUP):
            if self._raw_input_active and self._raw_contact_active:
                self._hook_rmb_up_pending_at = float(observed_at)
                self._add_motion_diagnostics(hook_up_deferred=1)
            else:
                self._rmb_down = False

    def _reanchor_new_contact_from_hook(
        self,
        observed_at: float,
        x: int,
        y: int,
    ) -> None:
        """Make the physical button-down coordinate authoritative.

        Raw Input reports relative device counts.  Tracking those counts while
        the pen is out of contact keeps hover responsive, but even a tiny
        mismatch can accumulate over a long move between strokes.  The
        low-level mouse hook supplies the exact Windows desktop coordinate at
        button-down, so snap the logical Raw Input origin to it before any
        motion for the new stroke is accepted.  This also handles the valid
        callback ordering where WM_INPUT button-down reaches us just before
        the hook callback.
        """
        if not self._raw_input_active or not self._raw_contact_active:
            return
        if self._accepted_motion_count != 0:
            return
        with self._button_anchor_wait_lock:
            if self._button_anchor_wait_timed_out:
                self._button_anchor = None
                return
            pending_anchor = self._button_anchor_wait_button is not None
            pending_started_at = self._button_anchor_wait_started_at
            pending_dx = self._button_anchor_wait_dx if pending_anchor else 0
            pending_dy = self._button_anchor_wait_dy if pending_anchor else 0
            with self._position_lock:
                self._raw_x = int(x) + pending_dx
                self._raw_y = int(y) + pending_dy
                self._logical_position_initialized = True
                self._idle_raw_position_fresh = False
                self._cursor_baseline_x = self._raw_x
                self._cursor_baseline_y = self._raw_y
                self._cursor_baseline_initialized = True
                self._pending_hook_positions.clear()
                self._hardware_positions.clear()
            self._button_anchor = (
                float(observed_at),
                int(self._raw_x),
                int(self._raw_y),
            )
            self._button_anchor_wait_button = None
            self._button_anchor_wait_started_at = 0.0
            self._button_anchor_wait_dx = 0
            self._button_anchor_wait_dy = 0
        self._add_motion_diagnostics(contact_anchor_corrected=1)
        self._contact_anchor_ready = True
        if pending_anchor:
            self._add_motion_diagnostics(
                anchor_wait_completed=1,
                anchor_wait_ms=max(
                    0.0,
                    (time.perf_counter() - pending_started_at) * 1000.0,
                ),
            )
            self._signal_input_ready()

    def drain_hardware_positions(
        self,
        max_count: int | None = None,
    ) -> list[tuple[float, int, int]]:
        """Return native mouse positions captured since the previous pen tick."""
        with self._position_lock:
            if max_count is None:
                positions = list(self._hardware_positions)
                self._hardware_positions.clear()
            else:
                positions = [
                    self._hardware_positions.popleft()
                    for _ in range(min(max(0, int(max_count)), len(self._hardware_positions)))
                ]
            if not self._hardware_positions:
                self._input_ready.clear()
        return positions

    def current_hardware_position(self) -> tuple[int, int] | None:
        """Return the freshest physical-mouse anchor without pen feedback.

        VMulti pointer promotion can leave GetCursorPos at the previous pen
        endpoint for a fraction of a frame. The low-level button hook already
        captured the real desktop coordinate, and device-scoped Raw Input then
        maintains the logical position from that same anchor.
        """
        now = time.perf_counter()
        anchor = self._button_anchor
        if anchor is not None and now - anchor[0] <= 0.1:
            return int(anchor[1]), int(anchor[2])
        with self._position_lock:
            if self._raw_contact_active and self._logical_position_initialized:
                return int(self._raw_x), int(self._raw_y)
        return None

    def set_movement_callback(self, callback: Callable[[], None] | None) -> None:
        self._movement_callback = callback

    def set_native_input_capture(self, capture: Any | None) -> None:
        """Use a native hook queue for transformed motion when available."""
        if self._thread is not None:
            raise RuntimeError("Native input capture must be configured before start")
        self._native_input_capture = capture

    def _drain_native_input_moves(self) -> int:
        capture = self._native_input_capture
        if not self._native_input_capture_active or capture is None:
            return 0
        try:
            moves = capture.drain_moves()
        except Exception:
            moves = []
        for move in moves:
            self._handle_native_mouse_move(
                float(move["observed_at"]),
                int(move["x"]),
                int(move["y"]),
                injected=bool(move["injected"]),
            )
        if moves:
            self._add_motion_diagnostics(
                native_capture_drains=1,
                native_capture_moves=len(moves),
            )
        return len(moves)

    def set_button_down_wake_callback(
        self,
        callback: Callable[[str], bool] | None,
    ) -> None:
        """Choose whether an accepted zero-motion button-down wakes output."""
        self._button_down_wake_callback = callback

    def note_precontact_pressure(
        self,
        button: str,
        *,
        raw: int,
        activation_raw: int,
        button_down: bool,
        observed_at: float,
    ) -> None:
        """Track the analog press that can precede the digital click edge.

        The supported mouse may report rising analog pressure one pressure
        frame before its ordinary LMB/RMB down packet.  That interval is the
        only safe pre-click motion to recover: older motion is merely cursor
        approach and must never become ink.
        """
        key = "right" if button == "right" else "left"
        value = float(raw)
        activation = float(max(1, int(activation_raw)))
        with self._precontact_lock:
            baseline = self._precontact_baseline[key]
            if baseline is None:
                baseline = value
            threshold = baseline + max(8.0, (activation - baseline) * 0.60)
            if button_down:
                self._precontact_baseline[key] = baseline
                return
            if value >= threshold and value < activation + 80.0:
                if self._precontact_started_at[key] <= 0.0:
                    self._precontact_started_at[key] = float(observed_at)
            elif value <= threshold - 4.0:
                self._precontact_started_at[key] = 0.0
                # Follow idle drift slowly without letting the start of a
                # press pull the learned rest value upward.
                baseline += (value - baseline) * 0.05
            self._precontact_baseline[key] = baseline

    def _precontact_path_for_down(
        self,
        button: str,
        *,
        observed_at: float,
        device_handle: int,
        device_identity: str,
        anchor_x: int,
        anchor_y: int,
    ) -> list[tuple[float, int, int]]:
        """Reconstruct bounded analog-contact motion ending at button-down."""
        if not self._raw_direct_mode:
            return []
        key = "right" if button == "right" else "left"
        with self._precontact_lock:
            started_at = float(self._precontact_started_at[key])
            self._precontact_started_at[key] = 0.0
            history = list(self._idle_raw_history)
        if started_at <= 0.0 or observed_at - started_at > 0.045:
            return []
        deltas = [
            (at, dx, dy)
            for at, handle, identity, dx, dy in history
            if started_at <= at <= observed_at
            and (
                int(handle) == int(device_handle)
                or bool(device_identity and identity == device_identity)
            )
        ]
        if not deltas:
            return []
        total_dx = sum(dx for _at, dx, _dy in deltas)
        total_dy = sum(dy for _at, _dx, dy in deltas)
        if abs(total_dx) + abs(total_dy) < 2:
            return []
        x = int(anchor_x) - total_dx
        y = int(anchor_y) - total_dy
        path = [(float(started_at), x, y)]
        for at, dx, dy in deltas:
            x += dx
            y += dy
            if path[-1][1:] != (x, y):
                path.append((float(at), x, y))
        return path if len(path) >= 2 else []

    def _contact_button_down(self) -> bool:
        return bool(
            (self._left_button_owns_contact and self._lmb_down)
            or (self._right_button_owns_contact and self._rmb_down)
        )

    def set_button_ownership(self, *, left: bool, right: bool) -> None:
        """Choose which physical buttons may open or extend pen contact."""
        self._left_button_owns_contact = bool(left)
        self._right_button_owns_contact = bool(right)
        self._raw_contact_active = self._contact_button_down()
        if self._raw_contact_active:
            return
        self._idle_raw_position_fresh = False
        with self._button_anchor_wait_lock:
            if self._button_anchor_wait_button is not None:
                self._button_anchor_wait_button = None
                self._button_anchor_wait_started_at = 0.0
                self._button_anchor_wait_dx = 0
                self._button_anchor_wait_dy = 0

    def set_right_button_owns_contact(self, enabled: bool) -> None:
        """Backward-compatible right-button ownership update."""
        self.set_button_ownership(
            left=self._left_button_owns_contact,
            right=enabled,
        )

    def _button_down_wake_enabled(self, flags: int) -> bool:
        callback = self._button_down_wake_callback
        if callback is None:
            return False
        try:
            return bool(
                (flags & RI_MOUSE_LEFT_BUTTON_DOWN and callback("left"))
                or (flags & RI_MOUSE_RIGHT_BUTTON_DOWN and callback("right"))
            )
        except Exception:
            return False

    def _signal_input_ready(self) -> None:
        self._input_ready.set()
        callback = self._movement_callback
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    def wait_for_movement(self, timeout_s: float) -> bool:
        return self._input_ready.wait(timeout=max(0.0, float(timeout_s)))

    @property
    def raw_input_active(self) -> bool:
        return self._raw_input_active

    def _reset_motion_diagnostics(self) -> None:
        with self._motion_diag_lock:
            self._motion_diag = {
                "started_at": time.perf_counter(),
                "last_raw_at": 0.0,
                "raw_seen": 0,
                "raw_selected": 0,
                "raw_dx": 0,
                "raw_dy": 0,
                "raw_distance": 0.0,
                "raw_absolute_ignored": 0,
                "wrong_device": 0,
                "hook_events": 0,
                "hook_injected_hint": 0,
                "hook_correlated": 0,
                "cursor_fallback": 0,
                "published": 0,
                "duplicate": 0,
                "hook_up_deferred": 0,
                "raw_up_received": 0,
                "hook_up_timeout": 0,
            }

    def _add_motion_diagnostics(self, **increments: float | int) -> None:
        with self._motion_diag_lock:
            for key, increment in increments.items():
                self._motion_diag[key] = self._motion_diag.get(key, 0) + increment

    def motion_diagnostics(self) -> dict[str, float | int]:
        with self._motion_diag_lock:
            snapshot = dict(self._motion_diag)
        started_at = float(snapshot.pop("started_at", time.perf_counter()))
        last_raw_at = float(snapshot.pop("last_raw_at", 0.0))
        elapsed = max(0.0, last_raw_at - started_at) if last_raw_at else 0.0
        snapshot["elapsed_ms"] = round(elapsed * 1000.0, 3)
        snapshot["raw_hz"] = (
            round(float(snapshot["raw_selected"]) / elapsed, 2)
            if elapsed > 0.0
            else 0.0
        )
        capture = self._native_input_capture
        if self._native_input_capture_active and capture is not None:
            try:
                stats = capture.stats()
            except Exception:
                stats = {}
            snapshot["native_capture"] = 1
            snapshot["native_capture_dropped"] = int(stats.get("dropped", 0))
            snapshot["native_capture_max_depth"] = int(
                stats.get("max_queue_depth", 0)
            )
        return snapshot

    def _publish_hardware_position(self, observed_at: float, x: int, y: int) -> bool:
        with self._position_lock:
            previous = self._hardware_positions[-1] if self._hardware_positions else None
            position = (float(observed_at), int(x), int(y))
            if previous is not None and previous[1:] == position[1:]:
                return False
            self._hardware_positions.append(position)
            self._input_ready.set()
        callback = self._movement_callback
        if callback is not None:
            try:
                callback()
            except Exception:
                pass
        return True

    def _handle_native_mouse_move(
        self,
        observed_at: float,
        x: int,
        y: int,
        *,
        injected: bool,
    ) -> None:
        """Capture the OS-transformed cursor position for one native move."""
        # Raw Input identifies the physical device, but its lLastX/lLastY values
        # are device counts rather than desktop pixels. The low-level hook gives
        # us the corresponding Windows cursor coordinates after DPI, pointer
        # speed, acceleration, and multi-monitor transforms have been applied.
        bridge_feedback = self._is_recent_bridge_feedback(
            int(x), int(y), float(observed_at)
        )
        if self._raw_input_active:
            if not self._contact_button_down():
                return
            if self._raw_direct_mode:
                # With Windows set to 1:1 and acceleration disabled, Raw Input
                # is already the exact desktop delta. Hook motion is diagnostic
                # only, so VMulti cursor promotion can never enter the path.
                self._add_motion_diagnostics(
                    pen_feedback_filtered=1 if bridge_feedback else 0,
                    hook_events=0 if bridge_feedback else 1,
                    hook_injected_hint=1 if injected else 0,
                )
                return
            if bridge_feedback:
                # VMulti promotion moves the system cursor like hardware and
                # may therefore arrive without LLMHF_INJECTED.  It is an OS
                # baseline change, not physical mouse motion.  Counting it as
                # a delta makes the next real move stall or briefly reverse.
                with self._position_lock:
                    self._cursor_baseline_x = int(x)
                    self._cursor_baseline_y = int(y)
                    self._cursor_baseline_initialized = True
                self._add_motion_diagnostics(pen_feedback_filtered=1)
                return
            # Do not trust LLMHF_INJECTED: Logitech can set it on physical
            # movement, while promoted pen feedback is not always marked. Hold
            # the transformed hook coordinate until a device-scoped Raw Input
            # packet proves that real mouse movement occurred.
            # Synthetic pen promotion moves the Windows cursor. Absolute hook
            # coordinates therefore drift away from the physical mouse path if
            # they are copied directly. Accumulate only the delta since the
            # latest physical-or-injected OS baseline into an independent
            # logical cursor. Raw Input still proves which device moved.
            with self._position_lock:
                if not self._logical_position_initialized:
                    self._raw_x, self._raw_y = int(x), int(y)
                    self._logical_position_initialized = True
                if self._cursor_baseline_initialized:
                    self._raw_x += int(x) - self._cursor_baseline_x
                    self._raw_y += int(y) - self._cursor_baseline_y
                self._cursor_baseline_x = int(x)
                self._cursor_baseline_y = int(y)
                self._cursor_baseline_initialized = True
                self._pending_hook_positions.append(
                    (float(observed_at), self._raw_x, self._raw_y)
                )
            self._add_motion_diagnostics(
                hook_events=1,
                hook_injected_hint=1 if injected else 0,
            )
            return
        else:
            if injected or bridge_feedback or self._is_recent_injected_position(
                int(x), int(y), observed_at
            ):
                return
        self._publish_hardware_position(observed_at, int(x), int(y))

    def _publish_raw_correlated_position(self, observed_at: float) -> None:
        cutoff = float(observed_at) - 0.03
        while self._pending_hook_positions and self._pending_hook_positions[0][0] < cutoff:
            self._pending_hook_positions.popleft()
        if self._pending_hook_positions:
            # A native hook drain can supply several transformed points before
            # buffered Raw Input is consumed. Pair them in order instead of
            # collapsing the entire batch to its final coordinate.
            if self._native_input_capture_active:
                hook_at, x, y = self._pending_hook_positions.popleft()
            else:
                hook_at, x, y = self._pending_hook_positions[-1]
                self._pending_hook_positions.clear()
            publish_at = float(hook_at)
            source = "hook_correlated"
        else:
            # Raw Input can beat its hook callback to this thread. Hold the
            # logical position until a later packet can publish the pending
            # delta; reading GetCursorPos here would reintroduce pen feedback.
            with self._position_lock:
                x, y = self._raw_x, self._raw_y
            publish_at = float(observed_at)
            source = "cursor_fallback"
        self._accepted_motion_count += 1
        if self.debug_mode and self._accepted_motion_count == 1:
            self.log("MOTION correlated hook coordinates with Raw Input device")
        published = self._publish_hardware_position(
            publish_at, int(x), int(y)
        )
        self._add_motion_diagnostics(
            **{
                source: 1,
                "published" if published else "duplicate": 1,
            }
        )

    def _cursor_position(self) -> tuple[int, int]:
        point = wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            return self._raw_x, self._raw_y
        return int(point.x), int(point.y)

    def _get_raw_device_identity(self, device_handle: int) -> str:
        cached = self._raw_device_identities.get(int(device_handle))
        if cached is not None:
            return cached
        size = ctypes.c_uint32(0)
        result = self.user32.GetRawInputDeviceInfoW(
            ctypes.c_void_p(device_handle),
            RIDI_DEVICENAME,
            None,
            ctypes.byref(size),
        )
        identity = ""
        if result != 0xFFFFFFFF and size.value > 0:
            buffer = ctypes.create_unicode_buffer(size.value + 1)
            result = self.user32.GetRawInputDeviceInfoW(
                ctypes.c_void_p(device_handle),
                RIDI_DEVICENAME,
                buffer,
                ctypes.byref(size),
            )
            if result != 0xFFFFFFFF:
                name = buffer.value.upper()
                match = re.search(r"VID_[0-9A-F]{4}&PID_[0-9A-F]{4}", name)
                identity = match.group(0) if match else name
        self._raw_device_identities[int(device_handle)] = identity
        return identity

    def _handle_raw_mouse(self, device_handle: int, mouse: RAWMOUSE) -> None:
        observed_at = time.perf_counter()
        flags = int(mouse.usButtonFlags)
        dx = int(mouse.lLastX)
        dy = int(mouse.lLastY)
        self._emit_timing(
            "raw_mouse",
            observed_at,
            device_handle=int(device_handle),
            button_flags=flags,
            move_flags=int(mouse.usFlags),
            dx=dx,
            dy=dy,
        )
        with self._position_lock:
            coordinate_echo = bool(
                self._raw_direct_mode
                and self._logical_position_initialized
                and max(abs(dx), abs(dy)) >= 64
                and abs(dx - self._raw_x) <= 2
                and abs(dy - self._raw_y) <= 2
            )
        if int(mouse.usFlags) & MOUSE_MOVE_ABSOLUTE or coordinate_echo:
            # Promoted synthetic/VMulti pen motion can re-enter Raw Input as an
            # absolute mouse packet. Its lLastX/lLastY values are coordinates,
            # not deltas; adding them to the logical cursor creates an immediate
            # screen-edge jump and a long painted connector. Some virtual input
            # stacks omit MOUSE_MOVE_ABSOLUTE, so also reject the distinctive
            # large packet that simply echoes the current desktop coordinate.
            # The supported mouse is relative, so neither form is physical path data.
            if dx != 0 or dy != 0 or flags != 0:
                self._add_motion_diagnostics(raw_absolute_ignored=1)
            return
        if not self._raw_contact_active and (dx != 0 or dy != 0):
            idle_identity = self._get_raw_device_identity(int(device_handle))
            with self._precontact_lock:
                self._idle_raw_history.append(
                    (
                        float(observed_at),
                        int(device_handle),
                        idle_identity,
                        int(dx),
                        int(dy),
                    )
                )
        # Auxiliary buttons (for example pressure-to-X-tilt) are held alongside
        # the contact owner. Track their state without letting them keep the
        # Raw Input contact session alive across separate strokes.
        if not self._left_button_owns_contact:
            if flags & RI_MOUSE_LEFT_BUTTON_DOWN:
                self._lmb_down = True
                self._hook_lmb_up_pending_at = 0.0
            if flags & RI_MOUSE_LEFT_BUTTON_UP:
                self._lmb_down = False
                self._hook_lmb_up_pending_at = 0.0
        if not self._right_button_owns_contact:
            if flags & RI_MOUSE_RIGHT_BUTTON_DOWN:
                self._rmb_down = True
                self._hook_rmb_up_pending_at = 0.0
            if flags & RI_MOUSE_RIGHT_BUTTON_UP:
                self._rmb_down = False
                self._hook_rmb_up_pending_at = 0.0

        button_down_flags = 0
        if self._left_button_owns_contact:
            button_down_flags |= RI_MOUSE_LEFT_BUTTON_DOWN
        if self._right_button_owns_contact:
            button_down_flags |= RI_MOUSE_RIGHT_BUTTON_DOWN
        consume_button_packet_motion = False
        if flags & button_down_flags and int(device_handle) != 0:
            incoming_identity = self._get_raw_device_identity(int(device_handle))
            if self._raw_contact_active:
                same_device = int(device_handle) == self._raw_device_handle
                same_identity = bool(
                    self._selected_raw_identity
                    and incoming_identity == self._selected_raw_identity
                )
                if not (same_device or same_identity):
                    # A promoted VMulti contact can surface as a second raw
                    # button-down. Never let that virtual device replace the
                    # physical Logitech device selected for this stroke.
                    self._add_motion_diagnostics(foreign_button_ignored=1)
                    return
                if flags & RI_MOUSE_LEFT_BUTTON_DOWN:
                    self._lmb_down = True
                if flags & RI_MOUSE_RIGHT_BUTTON_DOWN:
                    self._rmb_down = True
                return
            if flags & RI_MOUSE_LEFT_BUTTON_DOWN:
                self._lmb_down = True
                self._hook_lmb_up_pending_at = 0.0
            if flags & RI_MOUSE_RIGHT_BUTTON_DOWN:
                self._rmb_down = True
                self._hook_rmb_up_pending_at = 0.0
            self._raw_device_handle = int(device_handle)
            self._raw_motion_device_handle = 0
            self._accepted_motion_count = 0
            self._pending_hook_positions.clear()
            self._selected_raw_identity = incoming_identity
            self._raw_contact_active = True
            anchor = self._button_anchor
            anchor_ready = bool(
                anchor is not None and time.perf_counter() - anchor[0] <= 0.1
            )
            immediate_wake = self._button_down_wake_enabled(flags)
            consume_button_packet_motion = immediate_wake
            if anchor_ready and anchor is not None:
                # The button hook is an absolute desktop measurement.  Always
                # prefer it over the idle Raw Input estimate so drift cannot
                # carry from one stroke into the next.
                self._raw_x, self._raw_y = anchor[1], anchor[2]
            elif not (
                self._idle_raw_position_fresh
                and self._logical_position_initialized
            ):
                self._raw_x, self._raw_y = self._cursor_position()
            self._logical_position_initialized = True
            self._idle_raw_position_fresh = False
            self._cursor_baseline_x = self._raw_x
            self._cursor_baseline_y = self._raw_y
            self._cursor_baseline_initialized = True
            self._button_anchor = None
            precontact_path = self._precontact_path_for_down(
                "right" if flags & RI_MOUSE_RIGHT_BUTTON_DOWN else "left",
                observed_at=observed_at,
                device_handle=int(device_handle),
                device_identity=incoming_identity,
                anchor_x=int(self._raw_x),
                anchor_y=int(self._raw_y),
            ) if immediate_wake and self._first_contact_pending else []
            self._first_contact_pending = False
            self._contact_anchor_ready = bool(anchor_ready or precontact_path)
            with self._button_anchor_wait_lock:
                self._button_anchor_wait_timed_out = False
                if immediate_wake and not anchor_ready:
                    self._button_anchor_wait_button = (
                        "right"
                        if flags & RI_MOUSE_RIGHT_BUTTON_DOWN
                        else "left"
                    )
                    self._button_anchor_wait_started_at = observed_at
                    self._button_anchor_wait_dx = 0
                    self._button_anchor_wait_dy = 0
                else:
                    self._button_anchor_wait_button = None
                    self._button_anchor_wait_started_at = 0.0
                    self._button_anchor_wait_dx = 0
                    self._button_anchor_wait_dy = 0
            with self._position_lock:
                self._hardware_positions.clear()
                self._hardware_positions.extend(precontact_path)
            self._reset_motion_diagnostics()
            if precontact_path:
                self._add_motion_diagnostics(
                    precontact_recovered=1,
                    precontact_points=len(precontact_path),
                )
            if self.debug_mode:
                self.log(
                    f"RAW button device handle=0x{self._raw_device_handle:X} "
                    f"identity={self._selected_raw_identity or 'unknown'}"
                )
            if immediate_wake and anchor_ready:
                # Raw Input commonly reports the physical down in a packet
                # with no motion. If the hook arrived first, its exact desktop
                # anchor is ready and output can wake immediately. Motion in
                # this same down packet is already represented by that anchor.
                self._add_motion_diagnostics(immediate_button_wake=1)
                self._signal_input_ready()

        if not self._raw_contact_active:
            if (
                self._raw_direct_mode
                and self._logical_position_initialized
                and (dx != 0 or dy != 0)
            ):
                movement_identity = self._get_raw_device_identity(
                    int(device_handle)
                )
                same_device = int(device_handle) == self._raw_motion_device_handle
                same_identity = bool(
                    self._selected_raw_identity
                    and movement_identity == self._selected_raw_identity
                )
                if same_device or same_identity:
                    with self._position_lock:
                        self._raw_x += dx
                        self._raw_y += dy
                        left = int(self.user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
                        top = int(self.user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
                        right = left + max(
                            1,
                            int(self.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
                        ) - 1
                        bottom = top + max(
                            1,
                            int(self.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
                        ) - 1
                        self._raw_x = clamp_i(self._raw_x, left, right)
                        self._raw_y = clamp_i(self._raw_y, top, bottom)
                        self._idle_raw_position_fresh = True
            return

        has_movement = dx != 0 or dy != 0
        if consume_button_packet_motion:
            has_movement = False
        if has_movement:
            self._add_motion_diagnostics(raw_seen=1)
            with self._motion_diag_lock:
                self._motion_diag["last_raw_at"] = time.perf_counter()
            movement_identity = self._get_raw_device_identity(int(device_handle))
            same_device = int(device_handle) == self._raw_device_handle
            same_identity = bool(
                self._selected_raw_identity
                and movement_identity == self._selected_raw_identity
            )
            if self._raw_motion_device_handle == 0 and int(device_handle) != 0:
                if same_device or same_identity:
                    self._raw_motion_device_handle = int(device_handle)
                    if self.debug_mode:
                        self.log(
                            f"RAW motion device handle=0x{self._raw_motion_device_handle:X} "
                            f"identity={movement_identity or 'unknown'}"
                        )
            if int(device_handle) != self._raw_motion_device_handle:
                self._add_motion_diagnostics(wrong_device=1)
                has_movement = False

        if has_movement:
            with self._button_anchor_wait_lock:
                if self._button_anchor_wait_button is not None:
                    # Preserve validated motion arriving after the down packet
                    # while waiting for the hook. It will be applied once to
                    # the authoritative anchor.
                    self._button_anchor_wait_dx += dx
                    self._button_anchor_wait_dy += dy
                    has_movement = False

        if has_movement:
            self._add_motion_diagnostics(
                raw_selected=1,
                raw_dx=dx,
                raw_dy=dy,
                raw_distance=math.hypot(dx, dy),
            )
            if self._raw_direct_mode:
                with self._position_lock:
                    self._raw_x += dx
                    self._raw_y += dy
                    left = int(self.user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
                    top = int(self.user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
                    right = left + max(
                        1,
                        int(self.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
                    ) - 1
                    bottom = top + max(
                        1,
                        int(self.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
                    ) - 1
                    self._raw_x = clamp_i(self._raw_x, left, right)
                    self._raw_y = clamp_i(self._raw_y, top, bottom)
                    x, y = self._raw_x, self._raw_y
                    self._pending_hook_positions.clear()
                published = self._publish_hardware_position(observed_at, x, y)
                self._accepted_motion_count += 1
                self._add_motion_diagnostics(
                    raw_direct=1,
                    **{"published" if published else "duplicate": 1},
                )
            else:
                self._publish_raw_correlated_position(observed_at)

        button_up_flags = RI_MOUSE_LEFT_BUTTON_UP | RI_MOUSE_RIGHT_BUTTON_UP
        if flags & button_up_flags and int(device_handle) == self._raw_device_handle:
            if flags & RI_MOUSE_LEFT_BUTTON_UP:
                self._lmb_down = False
                self._hook_lmb_up_pending_at = 0.0
            if flags & RI_MOUSE_RIGHT_BUTTON_UP:
                self._rmb_down = False
                self._hook_rmb_up_pending_at = 0.0
            with self._button_anchor_wait_lock:
                self._button_anchor_wait_button = None
                self._button_anchor_wait_started_at = 0.0
                self._button_anchor_wait_dx = 0
                self._button_anchor_wait_dy = 0
            self._raw_contact_active = self._contact_button_down()
            if not self._raw_contact_active:
                self._idle_raw_position_fresh = False
                self._contact_anchor_ready = False
            self._add_motion_diagnostics(raw_up_received=1)
            callback = self._movement_callback
            if callback is not None:
                try:
                    callback()
                except Exception:
                    pass

    def mark_injected_position(self, x: int, y: int) -> None:
        """Identify pointer-promotion feedback before it reaches the hook."""
        with self._position_lock:
            self._recent_injected_positions.append((time.perf_counter(), int(x), int(y)))
            if not self._logical_position_initialized:
                self._raw_x, self._raw_y = int(x), int(y)
                self._logical_position_initialized = True
            # Injection changes the OS cursor origin for the next native hook
            # delta, but it must never move the independent physical path.
            self._cursor_baseline_x = int(x)
            self._cursor_baseline_y = int(y)
            self._cursor_baseline_initialized = True

    def _is_recent_injected_position(self, x: int, y: int, now: float) -> bool:
        # Synthetic pen promotion is normally marked LLMHF_INJECTED, but that
        # marker is not reliable through every Windows/app input path. Exact
        # coordinate matching over a very short window catches the feedback
        # without changing the physical mouse trajectory.
        cutoff = now - 0.02
        with self._position_lock:
            while self._recent_injected_positions and self._recent_injected_positions[0][0] < cutoff:
                self._recent_injected_positions.popleft()
            return any(
                recent_x == int(x) and recent_y == int(y)
                for _ts, recent_x, recent_y in self._recent_injected_positions
            )

    def _is_recent_bridge_feedback(self, x: int, y: int, now: float) -> bool:
        """Recognize immediate cursor/button promotion from our pen report."""
        cutoff = float(now) - 0.02
        with self._position_lock:
            while (
                self._recent_injected_positions
                and self._recent_injected_positions[0][0] < cutoff
            ):
                self._recent_injected_positions.popleft()
            return any(
                float(now) - timestamp <= PEN_FEEDBACK_WINDOW_S
                and abs(recent_x - int(x)) <= PEN_FEEDBACK_TOLERANCE_PX
                and abs(recent_y - int(y)) <= PEN_FEEDBACK_TOLERANCE_PX
                for timestamp, recent_x, recent_y in self._recent_injected_positions
            )

    def heartbeat(self) -> None:
        if self.enabled:
            self._last_heartbeat = time.perf_counter()

    @staticmethod
    def _should_block_message(
        msg: int,
        *,
        injected: bool,
        bridge_feedback: bool = False,
        suppress_left: bool = True,
        suppress_right: bool = False,
    ) -> bool:
        """Block configured hardware buttons; movement must pass through.

        Coordinate-based bridge-feedback detection is intentionally not
        allowed to bypass button suppression. A real press can occur at the
        current pen coordinate during the short feedback window; passing its
        down event while suppressing the later up event leaves Windows stuck
        in a held-button state. Only the hook's explicit injected flag is
        authoritative enough to exempt a button message.
        """
        del bridge_feedback
        if injected:
            return False
        left_message = msg in (
            WM_LBUTTONDOWN,
            WM_LBUTTONUP,
            WM_LBUTTONDBLCLK,
            WM_NCLBUTTONDOWN,
            WM_NCLBUTTONUP,
        )
        right_message = msg in (
            WM_RBUTTONDOWN,
            WM_RBUTTONUP,
            WM_RBUTTONDBLCLK,
            WM_NCRBUTTONDOWN,
            WM_NCRBUTTONUP,
        )
        return (suppress_left and left_message) or (
            suppress_right and right_message
        )

    def fail_open(self, reason: str) -> None:
        """Stop blocking hardware clicks without waiting for normal teardown."""
        self.enabled = False
        self._lmb_down = False
        self._rmb_down = False
        self._hook_lmb_up_pending_at = 0.0
        self._hook_rmb_up_pending_at = 0.0
        self._raw_contact_active = False
        self._contact_anchor_ready = False
        self._input_ready.set()
        if not self._fail_open_logged:
            self._fail_open_logged = True
            self.log(f"Mouse button suppressor FAIL-OPEN: {reason}")

    def _drain_buffered_raw_input(self) -> int:
        """Drain high-frequency mouse packets accumulated behind WM_INPUT.

        Microsoft recommends GetRawInputBuffer for 1000 Hz mice. The current
        WM_INPUT packet is still read with GetRawInputData; this method consumes
        only packets that arrived behind it while the message loop was busy.
        """
        raw_size = ctypes.sizeof(RAWINPUT)
        capacity = 256
        storage = ctypes.create_string_buffer(raw_size * capacity)
        total = 0
        alignment = ctypes.sizeof(ctypes.c_void_p)
        for _ in range(8):
            byte_count = ctypes.c_uint32(ctypes.sizeof(storage))
            result = int(
                self.user32.GetRawInputBuffer(
                    storage,
                    ctypes.byref(byte_count),
                    ctypes.sizeof(RAWINPUTHEADER),
                )
            )
            if result in (0, 0xFFFFFFFF):
                if result == 0xFFFFFFFF:
                    self._add_motion_diagnostics(raw_buffer_errors=1)
                break
            self._drain_native_input_moves()
            offset = 0
            processed = 0
            for _index in range(result):
                if offset + ctypes.sizeof(RAWINPUTHEADER) > ctypes.sizeof(storage):
                    break
                raw = ctypes.cast(
                    ctypes.addressof(storage) + offset,
                    ctypes.POINTER(RAWINPUT),
                ).contents
                record_size = max(
                    ctypes.sizeof(RAWINPUTHEADER),
                    int(raw.header.dwSize),
                )
                if offset + record_size > ctypes.sizeof(storage):
                    break
                if int(raw.header.dwType) == RIM_TYPEMOUSE:
                    self._handle_raw_mouse(int(raw.header.hDevice or 0), raw.mouse)
                processed += 1
                offset += (record_size + alignment - 1) & ~(alignment - 1)
            total += processed
            if processed < result:
                self._add_motion_diagnostics(raw_buffer_truncated=1)
                break
        if total:
            self._add_motion_diagnostics(
                raw_buffer_batches=1,
                raw_buffered=total,
            )
        return total

    def _run(self) -> None:
        hook_proc_t = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p)

        native_capture = self._native_input_capture
        if native_capture is not None:
            try:
                native_capture.open()
                self._native_input_capture_active = True
            except Exception as exc:
                self._native_input_capture_active = False
                self.log(
                    "WARN native transformed-input capture unavailable; "
                    f"using Python hook coordinates ({exc})"
                )

        @WNDPROC
        def _raw_wnd_proc(
            hwnd: ctypes.c_void_p,
            message: int,
            w_param: int,
            l_param: int,
        ) -> int:
            if int(message) == WM_INPUT:
                self._drain_native_input_moves()
                size = ctypes.c_uint32(0)
                result = self.user32.GetRawInputData(
                    ctypes.c_void_p(l_param),
                    RID_INPUT,
                    None,
                    ctypes.byref(size),
                    ctypes.sizeof(RAWINPUTHEADER),
                )
                if result != 0xFFFFFFFF and size.value >= ctypes.sizeof(RAWINPUTHEADER):
                    buffer = ctypes.create_string_buffer(size.value)
                    copied = self.user32.GetRawInputData(
                        ctypes.c_void_p(l_param),
                        RID_INPUT,
                        buffer,
                        ctypes.byref(size),
                        ctypes.sizeof(RAWINPUTHEADER),
                    )
                    if copied != 0xFFFFFFFF:
                        raw = ctypes.cast(buffer, ctypes.POINTER(RAWINPUT)).contents
                        if int(raw.header.dwType) == RIM_TYPEMOUSE:
                            self._handle_raw_mouse(int(raw.header.hDevice or 0), raw.mouse)
                self._drain_buffered_raw_input()
            return int(self.user32.DefWindowProcW(hwnd, message, w_param, l_param))

        @hook_proc_t
        def _hook_proc(n_code: int, w_param: int, l_param: int) -> int:
            if n_code == HC_ACTION and self.enabled:
                msg = int(w_param)
                if msg in (
                    WM_MOUSEMOVE,
                    WM_LBUTTONDOWN,
                    WM_LBUTTONUP,
                    WM_LBUTTONDBLCLK,
                    WM_NCLBUTTONDOWN,
                    WM_NCLBUTTONUP,
                    WM_RBUTTONDOWN,
                    WM_RBUTTONUP,
                    WM_RBUTTONDBLCLK,
                    WM_NCRBUTTONDOWN,
                    WM_NCRBUTTONUP,
                ):
                    try:
                        info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                        injected = (int(info.flags) & LLMHF_INJECTED) != 0
                    except Exception:
                        return int(self.user32.CallNextHookEx(self.hook, n_code, w_param, l_param))

                    observed_at = time.perf_counter()
                    bridge_feedback = self._is_recent_bridge_feedback(
                        int(info.pt.x), int(info.pt.y), observed_at
                    )

                    if msg == WM_MOUSEMOVE:
                        if not self._native_input_capture_active:
                            self._handle_native_mouse_move(
                                observed_at,
                                int(info.pt.x),
                                int(info.pt.y),
                                injected=injected,
                            )

                    # IMPORTANT: only hardware events may mutate hook button state.
                    # Injected mouse events (from synthetic pointer promotion, etc.)
                    # must not arm/disarm contact or we can get stuck-down lag.
                    if not injected and not bridge_feedback:
                        self._handle_physical_hook_button(
                            msg,
                            observed_at=observed_at,
                            x=int(info.pt.x),
                            y=int(info.pt.y),
                        )
                    should_block = self._should_block_message(
                        msg,
                        injected=injected,
                        bridge_feedback=bridge_feedback,
                        suppress_left=self.suppress_left,
                        suppress_right=self.suppress_right,
                    )
                    if should_block:
                        return 1
            return int(self.user32.CallNextHookEx(self.hook, n_code, w_param, l_param))

        self._proc = _hook_proc
        self._wnd_proc = _raw_wnd_proc
        hmod = self.kernel32.GetModuleHandleW(None)
        ctypes.set_last_error(0)
        self.hook = ctypes.c_void_p(self.user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, hmod, 0))
        err = ctypes.get_last_error()
        if not self.hook:
            self.log(f"LMB suppressor hook install failed err={err}")
            if self._native_input_capture_active and native_capture is not None:
                try:
                    native_capture.close()
                except Exception:
                    pass
                self._native_input_capture_active = False
            self._ready.set()
            return

        window_class = WNDCLASSW()
        window_class.lpfnWndProc = self._wnd_proc
        window_class.hInstance = hmod
        window_class.lpszClassName = self._raw_class_name
        ctypes.set_last_error(0)
        self._raw_class_atom = int(self.user32.RegisterClassW(ctypes.byref(window_class)))
        raw_error = ctypes.get_last_error()
        if self._raw_class_atom:
            self._raw_hwnd = ctypes.c_void_p(
                self.user32.CreateWindowExW(
                    0,
                    self._raw_class_name,
                    self._raw_class_name,
                    0,
                    0,
                    0,
                    0,
                    0,
                    ctypes.c_void_p(-3),  # HWND_MESSAGE
                    None,
                    hmod,
                    None,
                )
            )
            raw_error = ctypes.get_last_error()
        if self._raw_hwnd:
            raw_device = RAWINPUTDEVICE(
                HID_USAGE_PAGE_GENERIC,
                HID_USAGE_GENERIC_MOUSE,
                RIDEV_INPUTSINK,
                self._raw_hwnd,
            )
            self._raw_input_active = bool(
                self.user32.RegisterRawInputDevices(
                    ctypes.byref(raw_device),
                    1,
                    ctypes.sizeof(RAWINPUTDEVICE),
                )
            )
            raw_error = ctypes.get_last_error()
        if self._raw_input_active:
            coordinate_mode = (
                "device deltas (1:1, acceleration off)"
                if self._raw_direct_mode
                else "Windows-transformed hook correlation"
            )
            self.log(
                "RAW mouse input active; physical device selected on button down; "
                f"coordinates={coordinate_mode}"
            )
        else:
            self.log(f"WARN raw mouse input unavailable err={raw_error}; using hook positions")

        suppressed = "/".join(
            name
            for name, enabled in (
                ("left", self.suppress_left),
                ("right", self.suppress_right),
            )
            if enabled
        )
        if suppressed:
            self.log(f"Mouse button suppressor active ({suppressed})")
        else:
            self.log("Mouse input monitor active (no buttons suppressed)")
        hotkey_registered = bool(
            self.user32.RegisterHotKey(
                None,
                EMERGENCY_HOTKEY_ID,
                MOD_CONTROL | MOD_SHIFT,
                VK_F12,
            )
        )
        if hotkey_registered:
            self.log("Stop hotkey: Ctrl+Shift+F12")
        else:
            self.log("WARN Stop hotkey Ctrl+Shift+F12 could not be registered")
        self._ready.set()

        msg = wintypes.MSG()
        while not self._stop.is_set():
            while self.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if int(msg.message) == WM_HOTKEY and int(msg.wParam) == EMERGENCY_HOTKEY_ID:
                    reason = "Stop hotkey Ctrl+Shift+F12 pressed"
                    self.fail_open(reason)
                    if not self._force_stop_requested:
                        self._force_stop_requested = True
                        callback = self._force_stop_callback
                        if callback is not None:
                            callback(reason)
                    continue
                self.user32.TranslateMessage(ctypes.byref(msg))
                self.user32.DispatchMessageW(ctypes.byref(msg))
            if self.enabled and time.perf_counter() - self._last_heartbeat > SUPPRESSOR_HEARTBEAT_TIMEOUT_S:
                self.fail_open(
                    f"pressure stream heartbeat stopped for {SUPPRESSOR_HEARTBEAT_TIMEOUT_S:.1f}s"
                )
            # Sleep-until-input instead of polling at 1 ms. Native hook timing
            # showed that the old unconditional sleep delayed WM_INPUT by a
            # median 2.4 ms. MWMO_INPUTAVAILABLE wakes for already-queued or
            # newly-arriving Raw Input while the 1 ms timeout still services
            # stop and heartbeat state promptly when idle.
            self.user32.MsgWaitForMultipleObjectsEx(
                0,
                None,
                1,
                QS_ALLINPUT,
                MWMO_INPUTAVAILABLE,
            )

        if hotkey_registered:
            self.user32.UnregisterHotKey(None, EMERGENCY_HOTKEY_ID)
        if self._raw_input_active:
            remove_device = RAWINPUTDEVICE(
                HID_USAGE_PAGE_GENERIC,
                HID_USAGE_GENERIC_MOUSE,
                RIDEV_REMOVE,
                None,
            )
            self.user32.RegisterRawInputDevices(
                ctypes.byref(remove_device),
                1,
                ctypes.sizeof(RAWINPUTDEVICE),
            )
        self._raw_input_active = False
        if self._raw_hwnd:
            self.user32.DestroyWindow(self._raw_hwnd)
            self._raw_hwnd = ctypes.c_void_p()
        if self._raw_class_atom:
            self.user32.UnregisterClassW(self._raw_class_name, hmod)
            self._raw_class_atom = 0
        if self.hook:
            self.user32.UnhookWindowsHookEx(self.hook)
            self.hook = ctypes.c_void_p()
        if self._native_input_capture_active and native_capture is not None:
            try:
                native_capture.close()
            except Exception as exc:
                self.log(f"WARN native transformed-input close failed: {exc}")
        self._native_input_capture_active = False
        self._lmb_down = False
        self._rmb_down = False
        self.log("Mouse button suppressor stopped")


class _StrokePlanner:
    """Own the complete synchronous stroke state machine.

    The emitter owns adapter construction and lifecycle. The planner receives
    those adapters because delivery success is an input to stroke state, and it
    synchronously preserves report order without exposing a plan/commit seam.
    """
    def __init__(
        self,
        config: SyntheticPenConfig,
        log: Callable[[str], None],
        *,
        pen: Any,
        suppressor: Any | None,
        trace: StrokeTraceRecorder | None,
    ) -> None:
        self.config = config
        self.log = log
        self.pen = pen
        self._trace = trace
        self._suppressor = suppressor
        self._pending_native_timing: deque[
            tuple[str, float, dict[str, int | float | str]]
        ] = deque(maxlen=512)
        self._trace_submission_tokens: set[int] = set()
        if config.debug_mode:
            self._suppressor.set_timing_callback(self._observe_native_timing)
        self._suppressor.set_button_down_wake_callback(
            self._button_down_wake_enabled
        )
        self.active_button: str | None = None
        self.state = "idle"
        self.contact_frame_no = 0
        self.prev_contact_pressure = 0
        self.contact_warmup_done = False
        self.precontact_frames = 0
        self.precontact_x = 0
        self.precontact_y = 0
        self.precontact_mapped = 0
        self.contact_start_x = 0
        self.contact_start_y = 0
        self.stroke_base_mapped = 0
        self.onset_catchup_pending = False
        self._last_contact_motion_at = 0.0
        self._last_meaningful_contact_pressure = 0
        self._buffered_contact_path: list[tuple[int, int]] = []
        self._pressure_interp_initialized = False
        self._pressure_interp_value = 0.0
        self._pressure_interp_target = 0.0
        self._pressure_interp_remaining = 0
        self._pressure_interp_start_value = 0.0
        self._pressure_interp_started_at = 0.0
        self._pressure_interp_duration_s = 1.0 / 60.0
        self._last_pressure_target_at = 0.0
        self._pressure_sample_interval_ema = 1.0 / 60.0
        self._clean_ending_output: int | None = None
        self._clean_ending_pending: int | None = None
        self._clean_ending_pending_since = 0.0
        self._last_pointer_injection_at = 0.0
        self._last_contact_position: tuple[int, int] | None = None
        self._contact_path_direction: tuple[float, float] | None = None
        self._stabilized_position: tuple[float, float] | None = None
        self._stabilizer_last_raw: tuple[int, int] | None = None
        self._event_driven_movement = False
        self._last_update_at = 0.0
        self._update_interval_ema = 1.0 / 240.0
        self._last_motion_diag: dict[str, float | int] = {}
        self._aux_tilt_x = 0
        self._last_sent_tilt_x = 0
        self._stationary_anchor_started_at = 0.0
        self._stationary_dab_emitted = False
        self._startup_contact_prime_pending = True

        self.click_candidate_active = False
        self.click_start_t = 0.0
        self.click_start_x = 0
        self.click_start_y = 0
        self.click_peak_mapped = 0

    def set_debug_mode(self, enabled: bool) -> None:
        """Enable or disable detailed stroke diagnostics without restarting."""
        self.config.debug_mode = bool(enabled)
        if enabled:
            if self._trace is None and self.config.trace_dir:
                self._trace = StrokeTraceRecorder(self.config.trace_dir, self.log)
        elif self._trace is not None:
            self._finish_trace("debug_disabled")
            self._trace.close()
            self._trace = None
            self._pending_native_timing.clear()

    def _output_target(self, button: str) -> str:
        return str(
            self.config.right_output_target
            if button == "right"
            else self.config.left_output_target
        )

    def _has_aux_xtilt(self) -> bool:
        return bool(
            self.config.left_output_target == "x_tilt"
            or self.config.right_output_target == "x_tilt"
        )

    def _observe_native_timing(
        self,
        kind: str,
        observed_at: float,
        fields: dict[str, int | float | str],
    ) -> None:
        """Attach original hook/Raw Input times to opt-in stroke traces."""
        trace = self._trace
        if trace is not None and trace.active:
            trace.record(kind, at=observed_at, **fields)
            return
        self._pending_native_timing.append((kind, observed_at, dict(fields)))

    def _finish_trace(self, reason: str) -> None:
        """Queue trace serialization and collect native completions off-thread."""
        trace = self._trace
        if trace is None or not trace.active:
            return
        collector = getattr(self.pen, "collect_delivery_events", None)
        tokens = frozenset(self._trace_submission_tokens)
        deferred = None
        if callable(collector) and tokens:
            def collect_native_delivery() -> list[dict[str, Any]]:
                return [
                    {"kind": "native_delivery", **event}
                    for event in collector(tokens, 25)
                ]

            deferred = collect_native_delivery
        trace.finish(reason, deferred_events=deferred)
        self._trace_submission_tokens.clear()

    def _flush_pending_native_timing(self) -> None:
        trace = self._trace
        if trace is None or not trace.active:
            return
        cutoff = time.perf_counter() - 0.25
        while self._pending_native_timing:
            kind, observed_at, fields = self._pending_native_timing.popleft()
            if observed_at >= cutoff:
                trace.record(kind, at=observed_at, **fields)

    def note_movement_callback(self, callback: Callable[[], None] | None) -> None:
        self._event_driven_movement = callback is not None

    def _button_down_wake_enabled(self, button: str) -> bool:
        if self._output_target(button) != "pressure":
            return False
        if button == "right":
            configured = self.config.right_immediate_button_wake
            if configured is not None:
                return bool(configured)
        return bool(self.config.immediate_button_wake)

    def _reset_clean_stroke_ending(self) -> None:
        self._clean_ending_output = None
        self._clean_ending_pending = None
        self._clean_ending_pending_since = 0.0

    def wait_for_movement(self, timeout_s: float) -> bool:
        if self._suppressor is None:
            time.sleep(max(0.0, float(timeout_s)))
            return False
        return self._suppressor.wait_for_movement(timeout_s)

    def release(self) -> None:
        if self.state == "contact":
            x, y = self.pen.get_cursor_pos()
            self._emit_release_teardown(
                x=x,
                y=y,
                final_contact_pressure=self.prev_contact_pressure,
            )
        self.state = "idle"
        self.contact_frame_no = 0
        self.prev_contact_pressure = 0
        self.contact_warmup_done = False
        self.precontact_frames = 0
        self.precontact_mapped = 0
        self.stroke_base_mapped = 0
        self.onset_catchup_pending = False
        self._last_contact_motion_at = 0.0
        self._last_meaningful_contact_pressure = 0
        self._buffered_contact_path.clear()
        self._last_contact_position = None
        self._contact_path_direction = None
        self.active_button = None
        self._reset_path_stabilizer()
        self._reset_clean_stroke_ending()

    def _read_lmb(self) -> bool:
        # When suppressing native LMB, some systems don't update GetAsyncKeyState
        # reliably for the blocked click. Use hook state for contact gating.
        if self._suppressor is not None and getattr(self._suppressor, "enabled", True):
            return self._suppressor.is_lmb_down()
        return self.pen.is_lmb_down()

    def _read_button(self, button: str) -> bool:
        if button == "right":
            if self._suppressor is not None and getattr(self._suppressor, "enabled", True):
                reader = getattr(self._suppressor, "is_rmb_down", None)
                return bool(reader()) if callable(reader) else False
            reader = getattr(self.pen, "is_rmb_down", None)
            return bool(reader()) if callable(reader) else False
        return self._read_lmb()

    def _physical_button_down(self, button: str) -> bool:
        if button == "right":
            reader = getattr(self.pen, "is_rmb_down", None)
            return bool(reader()) if callable(reader) else False
        return self.pen.is_lmb_down()

    def _channel_setting(self, left_name: str, right_name: str) -> int | bool:
        left_value = getattr(self.config, left_name)
        if self.active_button != "right":
            return left_value
        right_value = getattr(self.config, right_name)
        return left_value if right_value is None else right_value

    def _trace_setting(self, left_name: str, right_name: str) -> int | float | str | None:
        left_value = getattr(self.config, left_name)
        if self.active_button != "right":
            return left_value
        right_value = getattr(self.config, right_name)
        return left_value if right_value is None else right_value

    def _interpolate_pressure(
        self,
        mapped: int,
        *,
        pressure_fresh: bool,
        interpolation_steps: int | None = None,
        now: float | None = None,
        instant: bool = False,
    ) -> int:
        """Distribute hardware pressure changes over synthetic pen reports."""
        value = float(clamp_i(mapped, 0, 1023))
        if instant:
            current_time = time.perf_counter() if now is None else float(now)
            self._pressure_interp_initialized = True
            self._pressure_interp_value = value
            self._pressure_interp_start_value = value
            self._pressure_interp_target = value
            self._pressure_interp_started_at = current_time
            self._pressure_interp_remaining = 0
            if pressure_fresh:
                self._last_pressure_target_at = current_time
            return int(round(value))
        if self._event_driven_movement:
            return self._interpolate_pressure_over_time(
                value,
                pressure_fresh=pressure_fresh,
                now=time.perf_counter() if now is None else float(now),
            )

        if not self._pressure_interp_initialized:
            self._pressure_interp_initialized = True
            self._pressure_interp_value = value
            self._pressure_interp_target = value
            self._pressure_interp_remaining = 0
            return int(round(value))

        if pressure_fresh:
            self._pressure_interp_target = value
            self._pressure_interp_remaining = max(
                1,
                int(
                    self.config.pressure_interp_steps
                    if interpolation_steps is None
                    else interpolation_steps
                ),
            )

        if self._pressure_interp_remaining > 0:
            self._pressure_interp_value += (
                self._pressure_interp_target - self._pressure_interp_value
            ) / self._pressure_interp_remaining
            self._pressure_interp_remaining -= 1
        else:
            self._pressure_interp_value = self._pressure_interp_target

        return clamp_i(int(round(self._pressure_interp_value)), 0, 1023)

    def _interpolate_pressure_over_time(
        self,
        value: float,
        *,
        pressure_fresh: bool,
        now: float,
    ) -> int:
        """Causally ramp between ~60 Hz samples using elapsed time.

        Mouse movement reports are irregular, so a fixed number of callbacks
        can finish too early (visible plateaus) or too late (brush lag). A time
        ramp is independent of mouse polling rate and never predicts position.
        """
        if not self._pressure_interp_initialized:
            self._pressure_interp_initialized = True
            self._pressure_interp_value = value
            self._pressure_interp_start_value = value
            self._pressure_interp_target = value
            self._pressure_interp_started_at = now
            self._pressure_interp_remaining = 0
            if pressure_fresh:
                self._last_pressure_target_at = now
            return clamp_i(int(round(value)), 0, 1023)

        duration = max(0.0001, self._pressure_interp_duration_s)
        progress = clamp_f((now - self._pressure_interp_started_at) / duration, 0.0, 1.0)
        current = self._pressure_interp_start_value + (
            self._pressure_interp_target - self._pressure_interp_start_value
        ) * progress
        self._pressure_interp_value = current

        if pressure_fresh:
            if self._last_pressure_target_at > 0.0:
                sample_interval = now - self._last_pressure_target_at
                if 0.004 <= sample_interval <= 0.05:
                    self._pressure_sample_interval_ema = (
                        self._pressure_sample_interval_ema * 0.75
                        + sample_interval * 0.25
                    )
            self._last_pressure_target_at = now

            if value != self._pressure_interp_target:
                self._pressure_interp_start_value = current
                self._pressure_interp_target = value
                self._pressure_interp_started_at = now
                # Finish slightly ahead of the next expected pressure report.
                # The bounds prevent either a one-frame snap or accumulated lag
                # when a pressure packet arrives unusually early or late.
                self._pressure_interp_duration_s = clamp_f(
                    self._pressure_sample_interval_ema * 0.85,
                    0.006,
                    0.018,
                )

        return clamp_i(int(round(current)), 0, 1023)

    @staticmethod
    def _dedupe_path(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for point in points:
            if not out or point != out[-1]:
                out.append(point)
        return out

    @staticmethod
    def _stationary_dab_path(anchor: tuple[int, int]) -> list[tuple[int, int]]:
        """Create the smallest closed path paint engines treat as a dab.

        Krita's freehand engine ignores pressure-only packets whose desktop
        coordinate never changes. A one-pixel excursion followed immediately
        by the original anchor produces paint without accumulating cursor drift.
        """
        x, y = anchor
        return [(x + 1, y), (x, y)]

    def _reset_path_stabilizer(self) -> None:
        self._stabilized_position = None
        self._stabilizer_last_raw = None

    def _stabilize_contact_path(
        self,
        points: list[tuple[int, int]],
        *,
        strength: int | None = None,
    ) -> list[tuple[int, int]]:
        """Remove small path reversals with a causal filter and bounded lag."""
        strength = clamp_i(
            int(self.config.path_stabilization if strength is None else strength),
            0,
            100,
        )
        if not points or strength <= 0:
            return points

        base_alpha = 1.0 - strength * 0.006
        max_lag = 2.0 + strength * 0.12
        filtered = self._stabilized_position
        previous_raw = self._stabilizer_last_raw
        out: list[tuple[int, int]] = []

        for raw_x, raw_y in points:
            if filtered is None:
                filtered = (float(raw_x), float(raw_y))
            else:
                raw_step = (
                    math.hypot(raw_x - previous_raw[0], raw_y - previous_raw[1])
                    if previous_raw is not None
                    else 0.0
                )
                # Track large intentional moves more closely while smoothing
                # the small alternating deviations that make handwriting look
                # wobbly. The hard lag bound prevents a cursor/pen snap.
                alpha = min(1.0, base_alpha + min(0.15, raw_step / 300.0))
                filtered = (
                    filtered[0] + (raw_x - filtered[0]) * alpha,
                    filtered[1] + (raw_y - filtered[1]) * alpha,
                )
                lag_x = filtered[0] - raw_x
                lag_y = filtered[1] - raw_y
                lag = math.hypot(lag_x, lag_y)
                if lag > max_lag:
                    scale = max_lag / lag
                    filtered = (raw_x + lag_x * scale, raw_y + lag_y * scale)

            stabilized = (round(filtered[0]), round(filtered[1]))
            if not out or stabilized != out[-1]:
                out.append(stabilized)
            previous_raw = (int(raw_x), int(raw_y))

        self._stabilized_position = filtered
        self._stabilizer_last_raw = previous_raw
        if self._trace is not None:
            for point in out:
                self._trace.record("stabilized_motion", x=point[0], y=point[1])
        return out

    def _apply_pressure_influence(
        self,
        mapped: int,
        *,
        influence: int | None = None,
        release_threshold: int | None = None,
    ) -> int:
        """Compress real pressure variation while preserving true pen-up."""
        value = clamp_i(int(mapped), 0, 1023)
        influence = clamp_i(
            int(self.config.pressure_influence if influence is None else influence),
            0,
            100,
        )
        threshold = int(
            self.config.release_threshold
            if release_threshold is None
            else release_threshold
        )
        if value <= threshold or influence >= 100:
            return value
        return clamp_i(round(512 + (value - 512) * influence / 100.0), 0, 1023)

    def _apply_clean_stroke_ending(
        self,
        mapped: int,
        *,
        enabled: bool,
        pressure_fresh: bool,
        button_down: bool,
        now: float,
    ) -> int:
        """Hold falling pressure briefly so button-up can discard release tails."""
        value = clamp_i(int(mapped), 0, 1023)
        if not enabled:
            self._reset_clean_stroke_ending()
            return value

        if self._clean_ending_output is None:
            self._clean_ending_output = value
            return value

        output = self._clean_ending_output
        if not button_down:
            # Release emits pen-up rather than another contact point. Preserve
            # the last visible pressure until that happens and forget the
            # pending mechanical release ramp when the stroke is reset.
            return output

        if value >= output:
            self._clean_ending_output = value
            self._clean_ending_pending = None
            self._clean_ending_pending_since = 0.0
            return value

        if pressure_fresh:
            if self._clean_ending_pending is None:
                self._clean_ending_pending_since = now
            self._clean_ending_pending = value

        if (
            self._clean_ending_pending is not None
            and now - self._clean_ending_pending_since >= CLEAN_STROKE_ENDING_HOLD_S
        ):
            output = self._clean_ending_pending
            self._clean_ending_output = output
            self._clean_ending_pending = None
            self._clean_ending_pending_since = 0.0
        return output

    @staticmethod
    def _limit_path(points: list[tuple[int, int]], max_points: int = 32) -> list[tuple[int, int]]:
        """Uniformly downsample a path while preserving both endpoints."""
        max_points = max(1, int(max_points))
        if len(points) <= max_points:
            return points
        if max_points == 1:
            return [points[-1]]
        last = len(points) - 1
        indices = [round(i * last / (max_points - 1)) for i in range(max_points)]
        return [points[index] for index in indices]

    @staticmethod
    def _contact_point_budget(
        points: list[tuple[int, int]],
        *,
        anchor: tuple[int, int] | None,
        pressure_start: int,
        pressure_end: int,
        max_points: int = MAX_CONTACT_POINTS_PER_UPDATE,
    ) -> int:
        """Choose the smallest useful batch for this known path and pressure ramp."""
        cap = max(1, int(max_points))
        if not points:
            return 0
        distance = 0.0
        previous = anchor
        for point in points:
            if previous is not None:
                distance += math.hypot(point[0] - previous[0], point[1] - previous[1])
            previous = point
        geometry_points = max(1, math.ceil(distance / TARGET_CONTACT_SPACING_PX))
        pressure_points = max(
            1,
            math.ceil(abs(int(pressure_end) - int(pressure_start)) / TARGET_CONTACT_PRESSURE_STEP),
        )
        # Preserve every observed corner when it fits. Interpolation is only
        # between measured positions; the budget never predicts ahead.
        return min(cap, max(len(points), geometry_points, pressure_points))

    def _drain_movement_path(self) -> list[tuple[int, int]]:
        suppressor = self._suppressor
        if suppressor is None:
            return []
        drain = getattr(suppressor, "drain_hardware_positions", None)
        if not callable(drain):
            return []
        cutoff = time.perf_counter() - 0.05
        try:
            # Dense pen injection can take several milliseconds. Drain every
            # Raw Input coordinate that accumulated during that batch so the
            # cursor does not trail behind one queued point at a time. The
            # complete geometry is retained below, then bounded only after its
            # cubic path has been constructed.
            captured = drain()
        except TypeError:
            captured = drain()
        if self._trace is not None:
            for ts, x, y in captured:
                self._trace.record(
                    "motion",
                    at=float(ts),
                    x=int(x),
                    y=int(y),
                )
            self._record_motion_diagnostic_batch()
        return self._dedupe_path(
            [(int(x), int(y)) for ts, x, y in captured if float(ts) >= cutoff]
        )

    def _motion_diagnostic_snapshot(self) -> dict[str, float | int]:
        suppressor = self._suppressor
        if suppressor is None:
            return {}
        reader = getattr(suppressor, "motion_diagnostics", None)
        return dict(reader()) if callable(reader) else {}

    def _record_motion_diagnostic_batch(self) -> None:
        if self._trace is None or not self._trace.active:
            return
        snapshot = self._motion_diagnostic_snapshot()
        if not snapshot:
            return
        cumulative_keys = (
            "raw_seen",
            "raw_selected",
            "raw_dx",
            "raw_dy",
            "raw_distance",
            "raw_absolute_ignored",
            "wrong_device",
            "hook_events",
            "hook_injected_hint",
            "hook_correlated",
            "cursor_fallback",
            "published",
            "duplicate",
            "hook_up_deferred",
            "raw_up_received",
            "hook_up_timeout",
        )
        batch = {
            key: snapshot.get(key, 0) - self._last_motion_diag.get(key, 0)
            for key in cumulative_keys
        }
        self._last_motion_diag = snapshot
        if not any(float(value) != 0.0 for value in batch.values()):
            return
        batch["raw_distance"] = round(float(batch["raw_distance"]), 3)
        batch["raw_hz"] = float(snapshot.get("raw_hz", 0.0))
        self._trace.record("raw_motion_batch", **batch)

    def _finish_motion_diagnostics(self) -> None:
        snapshot = self._motion_diagnostic_snapshot()
        if not snapshot:
            return
        if self._trace is not None and self._trace.active:
            self._trace.record("motion_summary", **snapshot)
        selected = int(snapshot.get("raw_selected", 0))
        if self.config.debug_mode and selected:
            self.log(
                "MOTION DIAG "
                f"raw={selected} ({float(snapshot.get('raw_hz', 0.0)):.1f} Hz) "
                f"published={int(snapshot.get('published', 0))} "
                f"duplicates={int(snapshot.get('duplicate', 0))} "
                f"hook={int(snapshot.get('hook_correlated', 0))} "
                f"cursor={int(snapshot.get('cursor_fallback', 0))} "
                f"hook_injected={int(snapshot.get('hook_injected_hint', 0))}"
            )

    def _buffer_movement_path(self, points: list[tuple[int, int]]) -> None:
        if not points:
            return
        combined = self._dedupe_path(self._buffered_contact_path + points)
        self._buffered_contact_path = self._limit_path(combined, max_points=64)

    @staticmethod
    def _remove_backtracking_spikes(
        points: list[tuple[int, int]],
        *,
        anchor: tuple[int, int] | None,
    ) -> list[tuple[int, int]]:
        """Remove a one-sample detour that immediately reverses direction."""
        if anchor is None or len(points) < 2:
            return points
        out: list[tuple[int, int]] = []
        previous = anchor
        for index, point in enumerate(points):
            if point == previous:
                continue
            if index + 1 < len(points):
                following = points[index + 1]
                incoming = (point[0] - previous[0], point[1] - previous[1])
                outgoing = (following[0] - point[0], following[1] - point[1])
                dot = incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
                direct_sq = (following[0] - previous[0]) ** 2 + (following[1] - previous[1]) ** 2
                incoming_sq = incoming[0] ** 2 + incoming[1] ** 2
                outgoing_sq = outgoing[0] ** 2 + outgoing[1] ** 2
                if dot < 0 and direct_sq < max(incoming_sq, outgoing_sq):
                    continue
            out.append(point)
            previous = point
        return out

    def _prepare_contact_path(
        self,
        candidate: list[tuple[int, int]],
        *,
        endpoint: tuple[int, int],
    ) -> list[tuple[int, int]]:
        """Join a movement batch continuously to the preceding pen report."""
        points = self._dedupe_path(candidate)
        if not points or points[-1] != endpoint:
            points.append(endpoint)
        anchor = self._last_contact_position
        while anchor is not None and points and points[0] == anchor:
            points.pop(0)
        return self._remove_backtracking_spikes(points, anchor=anchor)

    def _prepare_direct_contact_path(
        self,
        candidate: list[tuple[int, int]],
        *,
        endpoint: tuple[int, int],
        latest_only: bool = False,
    ) -> list[tuple[int, int]]:
        """Preserve captured coordinates without curve fitting or prediction."""
        points = self._dedupe_path(candidate)
        if not points or points[-1] != endpoint:
            points.append(endpoint)
        anchor = self._last_contact_position
        while anchor is not None and points and points[0] == anchor:
            points.pop(0)
        # This rejects a one-event injected-pointer feedback detour, not real
        # path smoothing; every remaining hardware coordinate is unchanged.
        points = self._remove_backtracking_spikes(points, anchor=anchor)
        if latest_only:
            return points[-1:]
        return self._limit_path(
            points,
            max_points=MAX_DIRECT_CONTACT_POINTS_PER_UPDATE,
        )

    @staticmethod
    def _densify_linear_path(
        points: list[tuple[int, int]],
        *,
        anchor: tuple[int, int] | None,
        max_spacing_px: float = 1.0,
    ) -> list[tuple[int, int]]:
        """Subdivide measured segments without changing their geometry."""
        if anchor is None or not points:
            return points
        dense: list[tuple[int, int]] = []
        previous = anchor
        spacing = max(1.0, float(max_spacing_px))
        for point in points:
            delta_x = point[0] - previous[0]
            delta_y = point[1] - previous[1]
            distance = math.hypot(delta_x, delta_y)
            if distance <= 0.0:
                continue
            steps = max(1, int(math.ceil(distance / spacing)))
            for step in range(1, steps + 1):
                fraction = step / steps
                dense.append(
                    (
                        round(previous[0] + delta_x * fraction),
                        round(previous[1] + delta_y * fraction),
                    )
                )
            previous = point
        return _StrokePlanner._dedupe_path(dense)

    def _densify_contact_path(
        self,
        points: list[tuple[int, int]],
        *,
        anchor: tuple[int, int] | None,
        max_spacing_px: float = 1.0,
    ) -> list[tuple[int, int]]:
        """Subdivide known segments with a bounded, non-predictive cubic join."""
        if anchor is None or not points:
            return points
        dense: list[tuple[int, int]] = []
        previous = anchor
        spacing = max(1.0, float(max_spacing_px))
        for point in points:
            delta_x = point[0] - previous[0]
            delta_y = point[1] - previous[1]
            distance = math.hypot(delta_x, delta_y)
            if distance <= 0.0:
                continue
            current_direction = (delta_x / distance, delta_y / distance)
            start_direction = self._contact_path_direction or current_direction
            # Avoid a loop if a genuine sharp reversal reaches this layer.
            if (
                start_direction[0] * current_direction[0]
                + start_direction[1] * current_direction[1]
                < 0.0
            ):
                start_direction = current_direction
            handle = min(distance / 3.0, 12.0)
            control1 = (
                previous[0] + start_direction[0] * handle,
                previous[1] + start_direction[1] * handle,
            )
            control2 = (
                point[0] - current_direction[0] * handle,
                point[1] - current_direction[1] * handle,
            )
            steps = max(1, int(math.ceil(distance / spacing)))
            for step in range(1, steps + 1):
                fraction = step / steps
                inverse = 1.0 - fraction
                dense.append(
                    (
                        round(
                            inverse**3 * previous[0]
                            + 3.0 * inverse**2 * fraction * control1[0]
                            + 3.0 * inverse * fraction**2 * control2[0]
                            + fraction**3 * point[0]
                        ),
                        round(
                            inverse**3 * previous[1]
                            + 3.0 * inverse**2 * fraction * control1[1]
                            + 3.0 * inverse * fraction**2 * control2[1]
                            + fraction**3 * point[1]
                        ),
                    )
                )
            self._contact_path_direction = current_direction
            previous = point
        return self._dedupe_path(dense)

    def _wait_for_pointer_frame_slot(self) -> None:
        """Keep adjacent synthetic reports in distinct Windows input frames."""
        deadline = self._last_pointer_injection_at + MIN_POINTER_FRAME_INTERVAL_S
        if time.perf_counter() < deadline:
            # Give the HID reader and mouse-hook threads a scheduling chance
            # before the short high-resolution wait. A positive Windows sleep
            # rounds this 120-us interval to roughly 1 ms on this runtime.
            time.sleep(0)
        while time.perf_counter() < deadline:
            # Keep the final sub-millisecond interval precise; oversleeping it
            # would turn the reconstructed path back into visible batches.
            pass

    def _inject_pen(
        self,
        *,
        flags: int,
        x: int,
        y: int,
        pressure_1024: int,
        tag: str,
    ) -> tuple[bool, int]:
        """Inject one report, retrying timestamp collisions without dropping it."""
        last_error = 0
        native_frame_pacing = bool(
            getattr(self.pen, "manages_frame_spacing", False)
        )
        for attempt in range(3):
            if self._last_pointer_injection_at > 0.0 and not native_frame_pacing:
                self._wait_for_pointer_frame_slot()
            marker = getattr(self._suppressor, "mark_injected_position", None)
            if callable(marker):
                marker(x, y)
            submitted_at = time.perf_counter()
            ok, last_error = self.pen.inject(
                flags=flags,
                x=x,
                y=y,
                pressure_1024=pressure_1024,
                tag=tag,
                tilt_x=(self._aux_tilt_x if self._has_aux_xtilt() else None),
            )
            completed_at = time.perf_counter()
            self._last_pointer_injection_at = completed_at
            if self._trace is not None:
                token = getattr(self.pen, "last_submission_token", None)
                if token is not None:
                    self._trace_submission_tokens.add(int(token))
                self._trace.record(
                    "inject",
                    at=submitted_at,
                    x=int(x),
                    y=int(y),
                    pressure=int(pressure_1024),
                    tilt_x=int(self._aux_tilt_x if self._has_aux_xtilt() else 0),
                    flags=int(flags),
                    tag=str(tag),
                    attempt=attempt + 1,
                    ok=bool(ok),
                    error=int(last_error),
                    submission_token=(
                        int(token)
                        if token is not None
                        else None
                    ),
                    call_duration_us=round(
                        (completed_at - submitted_at) * 1_000_000.0,
                        1,
                    ),
                )
            if ok or last_error != ERROR_NOT_READY:
                return ok, last_error
            if attempt == 0:
                self.log("INJECT frame timestamp collision; retrying without dropping path point")
        return False, last_error

    def _inject_pen_batch(
        self,
        reports: list[dict[str, int | str | None]],
    ) -> tuple[bool, int]:
        """Submit a complete contact path through one native scheduler call."""
        native_batch = getattr(self.pen, "inject_batch", None)
        if not reports or not callable(native_batch):
            return False, 0
        marker = getattr(self._suppressor, "mark_injected_position", None)
        if callable(marker):
            for report in reports:
                marker(int(report["x"]), int(report["y"]))
        submitted_at = time.perf_counter()
        ok, error, tokens = native_batch(reports)
        completed_at = time.perf_counter()
        self._last_pointer_injection_at = completed_at
        if self._trace is not None:
            call_duration_us = round(
                (completed_at - submitted_at) * 1_000_000.0,
                1,
            )
            for index, report in enumerate(reports):
                if index < len(tokens):
                    self._trace_submission_tokens.add(int(tokens[index]))
                self._trace.record(
                    "inject",
                    at=submitted_at,
                    x=int(report["x"]),
                    y=int(report["y"]),
                    pressure=int(report["pressure_1024"]),
                    tilt_x=int(report.get("tilt_x") or 0),
                    flags=int(report["flags"]),
                    tag=str(report["tag"]),
                    attempt=1,
                    ok=bool(ok),
                    error=int(error),
                    submission_token=(
                        int(tokens[index]) if index < len(tokens) else None
                    ),
                    batch_index=index,
                    batch_size=len(reports),
                    call_duration_us=call_duration_us,
                )
        return bool(ok), int(error)

    def _prime_first_native_contact(self, *, flags: int, x: int, y: int) -> None:
        """Complete the native pen's first contact before exposing real ink.

        Krita accepts every report in the first synthetic contact lifecycle but
        renders only its initial dab. A zero-pressure DOWN/UP initializes that
        application path invisibly; the immediately following real lifecycle
        then renders normally. Hover-only priming is not sufficient.
        """
        if (
            not self._startup_contact_prime_pending
            or self.config.output_backend != "native_synthetic"
        ):
            return
        self._startup_contact_prime_pending = False
        prime_reports: list[dict[str, int | str | None]] = [
            {
                "flags": int(flags),
                "x": int(x),
                "y": int(y),
                "pressure_1024": 0,
                "tag": "startup_contact_prime_down",
                "tilt_x": None,
            },
            {
                "flags": POINTER_FLAG_UP | POINTER_FLAG_PRIMARY,
                "x": int(x),
                "y": int(y),
                "pressure_1024": 0,
                "tag": "startup_contact_prime_up",
                "tilt_x": None,
            },
        ]
        ok, error = self._inject_pen_batch(prime_reports)
        wait_idle = getattr(self.pen, "wait_idle", None)
        drained = bool(wait_idle(25)) if ok and callable(wait_idle) else ok
        if not ok or not drained:
            self.log(
                "WARN first native pen contact could not be primed "
                f"error={error} drained={int(drained)}"
            )
            return
        # Let Windows finish routing the completed zero-pressure lifecycle
        # before the real DOWN. This is paid once per Start session.
        time.sleep(0.002)

    def _emit_release_teardown(
        self,
        *,
        x: int,
        y: int,
        final_contact_pressure: int = 0,
    ) -> tuple[bool, bool]:
        # Krita connects a zero-pressure UP at a new coordinate to the previous
        # contact point, creating a thin tail. First carry the last real contact
        # pressure to the release coordinate, then send UP at that same point.
        final_ok = True
        if final_contact_pressure > 0:
            final_ok, _ = self._inject_pen(
                flags=(
                    POINTER_FLAG_UPDATE
                    | POINTER_FLAG_INRANGE
                    | POINTER_FLAG_INCONTACT
                    | POINTER_FLAG_FIRSTBUTTON
                    | POINTER_FLAG_PRIMARY
                ),
                x=x,
                y=y,
                pressure_1024=clamp_i(final_contact_pressure, 1, 1024),
                tag="release_final_contact",
            )

        # The normal release ends contact and range together. Leaving INRANGE on
        # the UP report lets Windows retain a hover pointer at this coordinate;
        # subsequent physical mouse movement can then snap back to it.
        if not self.config.release_teardown:
            ok, _ = self._inject_pen(
                flags=POINTER_FLAG_UP | POINTER_FLAG_PRIMARY,
                x=x,
                y=y,
                pressure_1024=0,
                tag="release_up",
            )
            all_ok = final_ok and ok
            return all_ok, not all_ok

        # Experimental compatibility sequence for apps that otherwise keep a
        # lingering stroke: UP|INRANGE -> hover -> out-of-range.
        ok1, _ = self._inject_pen(
            flags=POINTER_FLAG_UP | POINTER_FLAG_INRANGE | POINTER_FLAG_PRIMARY,
            x=x,
            y=y,
            pressure_1024=0,
            tag="release_up",
        )
        ok2, _ = self._inject_pen(
            flags=POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_PRIMARY,
            x=x,
            y=y,
            pressure_1024=0,
            tag="release_hover",
        )
        ok3, _ = self._inject_pen(
            flags=POINTER_FLAG_UPDATE | POINTER_FLAG_PRIMARY,
            x=x,
            y=y,
            pressure_1024=0,
            tag="release_endhover",
        )
        ok = final_ok and ok1 and ok2 and ok3
        return ok, not ok

    def advance(
        self,
        left_mapped: int,
        right_mapped: int,
        *,
        pressure_fresh: bool = True,
        left_raw: int | None = None,
        right_raw: int | None = None,
    ) -> SyntheticPenSample:
        if self._suppressor is not None:
            self._suppressor.heartbeat()
        update_at = time.perf_counter()
        if self._last_update_at > 0.0:
            interval = update_at - self._last_update_at
            if 0.00005 <= interval <= 0.05:
                self._update_interval_ema = (
                    self._update_interval_ema * 0.9
                    + interval * 0.1
                )
        self._last_update_at = update_at
        prev_state = self.state
        left_down = self._read_button("left")
        right_down = self._read_button("right")
        if self._suppressor is not None:
            if left_raw is not None and self.config.left_output_target == "pressure":
                left_activation_raw = self.config.trace_raw_min
                self._suppressor.note_precontact_pressure(
                    "left",
                    raw=int(left_raw),
                    activation_raw=int(
                        380 if left_activation_raw is None else left_activation_raw
                    ),
                    button_down=bool(left_down),
                    observed_at=update_at,
                )
            if right_raw is not None and self.config.right_output_target == "pressure":
                right_activation_raw = self.config.right_trace_raw_min
                self._suppressor.note_precontact_pressure(
                    "right",
                    raw=int(right_raw),
                    activation_raw=int(
                        380
                        if right_activation_raw is None
                        and self.config.trace_raw_min is None
                        else self.config.trace_raw_min
                        if right_activation_raw is None
                        else right_activation_raw
                    ),
                    button_down=bool(right_down),
                    observed_at=update_at,
                )
            # Raw Input can report DOWN before the low-level hook supplies the
            # new desktop coordinate. Never begin a stroke from the previous
            # pen endpoint while that authoritative anchor is still pending.
            anchor_ready = getattr(self._suppressor, "contact_anchor_ready", None)
            if callable(anchor_ready) and getattr(self._suppressor, "enabled", False):
                if (
                    left_down
                    and self.config.left_output_target == "pressure"
                    and not anchor_ready("left")
                ):
                    left_down = False
                if (
                    right_down
                    and self.config.right_output_target == "pressure"
                    and not anchor_ready("right")
                ):
                    right_down = False
        auxiliary_mapped = (
            left_mapped
            if self.config.left_output_target == "x_tilt"
            else right_mapped
            if self.config.right_output_target == "x_tilt"
            else 0
        )
        self._aux_tilt_x = round(
            clamp_i(int(auxiliary_mapped), 0, 1023) * 60 / 1023
        )
        if self.active_button is None:
            if left_down and self.config.left_output_target == "pressure":
                self.active_button = "left"
            elif right_down and self.config.right_output_target == "pressure":
                self.active_button = "right"
        selected_button = self.active_button or "left"
        lmb_down = bool(
            self.active_button is not None
            and (right_down if selected_button == "right" else left_down)
        )
        lmb_physical = self._physical_button_down(selected_button)
        mapped = clamp_i(
            int(
                0
                if self.active_button is None
                else right_mapped
                if selected_button == "right"
                else left_mapped
            ),
            0,
            1023,
        )
        contact_threshold = int(
            self._channel_setting("contact_threshold", "right_contact_threshold")
        )
        release_threshold = int(
            self._channel_setting("release_threshold", "right_release_threshold")
        )
        min_contact_pressure = int(
            self._channel_setting("min_contact_pressure", "right_min_contact_pressure")
        )
        true_low_latency = bool(
            self._channel_setting("true_low_latency", "right_true_low_latency")
        )
        stationary_pressure_updates = bool(
            self._channel_setting(
                "stationary_pressure_updates",
                "right_stationary_pressure_updates",
            )
        )
        auxiliary_stationary_updates = bool(
            (
                self.config.left_output_target == "x_tilt"
                and self.config.stationary_pressure_updates
            )
            or (
                self.config.right_output_target == "x_tilt"
                and self.config.right_stationary_pressure_updates
            )
        )
        path_stabilization = (
            0
            if true_low_latency
            else int(
                self._channel_setting(
                    "path_stabilization", "right_path_stabilization"
                )
            )
        )
        pressure_influence = int(
            self._channel_setting("pressure_influence", "right_pressure_influence")
        )
        onset_buffer = (
            not true_low_latency
            and bool(self._channel_setting("onset_buffer", "right_onset_buffer"))
        )
        immediate_button_wake = self._button_down_wake_enabled(selected_button)
        clean_stroke_endings = bool(
            self._channel_setting(
                "clean_stroke_endings", "right_clean_stroke_endings"
            )
        )
        if lmb_down and self._trace is not None and not self._trace.active:
            self._trace_submission_tokens.clear()
            self._trace.begin(
                button=selected_button,
                output_backend=str(self.config.output_backend),
                pressure_mode=self.config.pressure_mode,
                contact_source=self.config.contact_source,
                interpolation="time" if self._event_driven_movement else "steps",
                configured_raw_min=self._trace_setting(
                    "trace_raw_min", "right_trace_raw_min"
                ),
                configured_raw_max=self._trace_setting(
                    "trace_raw_max", "right_trace_raw_max"
                ),
                configured_curve=self._trace_setting(
                    "trace_curve", "right_trace_curve"
                ),
                configured_curve_strength=self._trace_setting(
                    "trace_curve_strength", "right_trace_curve_strength"
                ),
                min_contact_pressure=min_contact_pressure,
                path_stabilization=path_stabilization,
                pressure_influence=pressure_influence,
                onset_buffer=onset_buffer,
                true_low_latency=true_low_latency,
                stationary_pressure_updates=stationary_pressure_updates,
                auxiliary_stationary_updates=auxiliary_stationary_updates,
                immediate_button_wake=immediate_button_wake,
                clean_stroke_endings=clean_stroke_endings,
            )
            self._flush_pending_native_timing()
            self._last_motion_diag = {}
        movement_path = self._drain_movement_path()
        if self.state == "contact" and movement_path:
            self._last_contact_motion_at = update_at
        if movement_path and (lmb_down or self.state == "contact"):
            movement_path = self._stabilize_contact_path(
                movement_path,
                strength=path_stabilization,
            )
        elif self.state not in {"contact", "hovering"} and not lmb_down:
            self._reset_path_stabilizer()
        if movement_path:
            self._stationary_anchor_started_at = update_at
            self._stationary_dab_emitted = False
            x, y = movement_path[-1]
        elif (
            self._event_driven_movement
            and self.state == "contact"
            and self._last_contact_position is not None
        ):
            # Pointer promotion can move the OS cursor to a previously injected
            # coordinate. A pressure-only scheduler tick must never treat that
            # feedback as new geometry; remain at the last Raw Input-validated
            # physical point until another movement packet arrives.
            x, y = self._last_contact_position
        else:
            physical_position = None
            if lmb_down and self._suppressor is not None:
                position_reader = getattr(
                    self._suppressor, "current_hardware_position", None
                )
                if callable(position_reader):
                    physical_position = position_reader()
            if physical_position is not None:
                x, y = physical_position
            else:
                x, y = self.pen.get_cursor_pos()

        release_threshold = clamp_i(release_threshold, 0, 1023)
        rise_per_frame = (
            1024
            if true_low_latency
            else clamp_i(int(self.config.rise_per_frame), 0, 1024)
        )
        fall_per_frame = (
            1024
            if true_low_latency
            else clamp_i(int(self.config.fall_per_frame), 0, 1024)
        )
        min_contact_pressure = clamp_i(min_contact_pressure, 0, 1024)
        # LMB + pressure already gives us a debounced contact signal. Requiring
        # a second poll adds a full frame of latency at the default 60 Hz.
        precontact_required = 1

        pressure_input_mapped = self._apply_pressure_influence(
            mapped,
            influence=pressure_influence,
            release_threshold=release_threshold,
        )
        pressure_input_mapped = self._apply_clean_stroke_ending(
            pressure_input_mapped,
            enabled=clean_stroke_endings,
            pressure_fresh=pressure_fresh,
            button_down=lmb_down,
            now=update_at,
        )
        interpolated_mapped = self._interpolate_pressure(
            pressure_input_mapped,
            pressure_fresh=pressure_fresh,
            interpolation_steps=(
                clamp_i(round((1.0 / 60.0) / self._update_interval_ema), 1, 128)
                if self._event_driven_movement
                else None
            ),
            now=update_at,
            instant=true_low_latency,
        )

        # The contact floor is applied to the pressure sent to Windows below.
        # Keep the interpolator at that same visible baseline while contact is
        # held. Otherwise it can remain invisibly near zero and spend the first
        # part of a fresh pressure ramp merely catching up to a floor Krita has
        # already seen, which looks like a delayed width readjustment.
        if self.state == "contact" and min_contact_pressure > 0:
            visible_floor_mapped = map_1024_to_1023(min_contact_pressure)
            if self.config.pressure_mode == "stroke_relative":
                denom = max(1, 1023 - self.stroke_base_mapped)
                visible_floor_mapped = self.stroke_base_mapped + (
                    visible_floor_mapped * denom + 1022
                ) // 1023
            if interpolated_mapped < visible_floor_mapped:
                interpolated_mapped = visible_floor_mapped
                self._pressure_interp_value = float(visible_floor_mapped)
            self._pressure_interp_start_value = max(
                self._pressure_interp_start_value,
                float(visible_floor_mapped),
            )
            self._pressure_interp_target = max(
                self._pressure_interp_target,
                float(visible_floor_mapped),
            )

        pressure_mapped = interpolated_mapped
        if self.config.pressure_mode == "stroke_relative" and self.state == "contact":
            if pressure_mapped <= self.stroke_base_mapped:
                pressure_mapped = 0
            else:
                denom = max(1, 1023 - self.stroke_base_mapped)
                pressure_mapped = ((pressure_mapped - self.stroke_base_mapped) * 1023) // denom
        actual_pen_pressure = map_1023_to_1024(pressure_mapped)
        if self._trace is not None:
            self._trace.record(
                "update",
                state=str(self.state),
                pressure_fresh=bool(pressure_fresh),
                mapped=int(mapped),
                pressure_input_mapped=int(pressure_input_mapped),
                interpolated_mapped=int(interpolated_mapped),
                actual_pressure=int(actual_pen_pressure),
                previous_sent_pressure=int(self.prev_contact_pressure),
                x=int(x),
                y=int(y),
                movement_points=len(movement_path),
                lmb=bool(lmb_down),
                physical_lmb=bool(lmb_physical),
                left_raw=int(left_raw) if left_raw is not None else None,
                right_raw=int(right_raw) if right_raw is not None else None,
            )

        def contact_requested() -> bool:
            if self.config.contact_source == "pressure_only":
                return mapped > contact_threshold
            return lmb_down

        def contact_released() -> bool:
            if self.config.contact_source == "pressure_only":
                return mapped <= release_threshold
            # Device-scoped Raw Input is ordered after the final movement
            # packet and remains the authoritative release signal. The
            # independent physical state can lag by hundreds of milliseconds
            # while the click is suppressed, so waiting for both introduces a
            # visible pen-up delay.
            return not lmb_down

        injected = False
        failed = False
        status = 0
        inject_flags: int | None = None
        inject_pressure = 0
        inject_x = x
        inject_y = y
        injection_path: list[tuple[int, int]] | None = None
        recovered_opening_path: list[tuple[int, int]] = []
        pressure_before_update = self.prev_contact_pressure
        next_state = self.state
        moved_from_contact = 0

        if self.state == "contact":
            release_requested = contact_released()

            if release_requested and movement_path:
                # Button/pressure state can reach this thread before the final
                # Raw Input coordinates. Paint every already-published point as
                # contact, then send UP on the next empty tick. Releasing first
                # discards the closing arc of fast circles.
                release_requested = False

            if release_requested:
                # If release lands between the delayed DOWN and its catch-up
                # update, carry the newest pressure to the release coordinate
                # so the buffered onset still forms a smooth ramp.
                final_contact_pressure = (
                    actual_pen_pressure
                    if self.onset_catchup_pending
                    else self.prev_contact_pressure
                )
                release_x, release_y = x, y
                if self._last_contact_position is not None:
                    # A coordinate not delivered through Raw Input is not
                    # proven physical geometry; the OS cursor can also lag or
                    # be displaced by synthetic pointer promotion. Published
                    # trailing motion was flushed above, so end at the last
                    # injected contact point without stamping another dab.
                    release_x, release_y = self._last_contact_position
                    final_contact_pressure = 0
                if self.config.debug_mode:
                    self.log(
                        "RELEASE reason=button_up "
                        f"mapped={mapped} final={final_contact_pressure} "
                        f"pos=({release_x},{release_y})"
                    )
                inject_pressure = 0
                next_state = "idle"
                self.contact_frame_no = 0
                self.prev_contact_pressure = 0
                self.contact_warmup_done = False
                self.precontact_frames = 0
                self.precontact_mapped = 0
                self.stroke_base_mapped = 0
                self.onset_catchup_pending = False
                self._last_contact_motion_at = 0.0
                self._last_meaningful_contact_pressure = 0
                self._buffered_contact_path.clear()
                self._stationary_dab_emitted = False
                ok, fail = self._emit_release_teardown(
                    x=release_x,
                    y=release_y,
                    final_contact_pressure=final_contact_pressure,
                )
                injected = ok
                failed = fail
                self._last_contact_position = None
                self._contact_path_direction = None
            else:
                self.contact_frame_no += 1
                moved_from_contact = abs(x - self.contact_start_x) + abs(y - self.contact_start_y)

                if self.onset_catchup_pending:
                    # The DOWN was emitted at the buffered first-contact point.
                    # Continue the multi-tick ramp toward the newest pressure at
                    # the current cursor point. This avoids both a held-pressure
                    # tail and a single dramatic catch-up discontinuity.
                    self.onset_catchup_pending = False
                    self.contact_warmup_done = True
                    # Spread the newer real pressure across every coordinate
                    # buffered since the first contact sample. This prevents a
                    # long uniformly thin lead-in on fast strokes.
                    target_mapped = pressure_input_mapped
                    if self.config.pressure_mode == "stroke_relative":
                        if target_mapped <= self.stroke_base_mapped:
                            target_mapped = 0
                        else:
                            denom = max(1, 1023 - self.stroke_base_mapped)
                            target_mapped = (
                                (target_mapped - self.stroke_base_mapped) * 1023
                            ) // denom
                    inject_pressure = map_1023_to_1024(target_mapped)
                    injection_path = self._buffered_contact_path + movement_path
                    self._buffered_contact_path.clear()
                    if injection_path:
                        inject_x, inject_y = injection_path[-1]
                    if not injection_path and (x, y) != (self.contact_start_x, self.contact_start_y):
                        if path_stabilization <= 0:
                            injection_path = [(x, y)]
                        else:
                            steps = max(2, int(self.config.pressure_interp_steps))
                            injection_path = [
                                (
                                    round(
                                        self.contact_start_x
                                        + (x - self.contact_start_x) * i / steps
                                    ),
                                    round(
                                        self.contact_start_y
                                        + (y - self.contact_start_y) * i / steps
                                    ),
                                )
                                for i in range(1, steps + 1)
                            ]
                    self._pressure_interp_value = float(pressure_input_mapped)
                    self._pressure_interp_target = float(pressure_input_mapped)
                    self._pressure_interp_remaining = 0
                    if self._event_driven_movement:
                        # Synchronize the time interpolator as well as its
                        # public value/target. Leaving its start value behind
                        # made the next movement fall back toward an older thin
                        # pressure, producing the apparent onset reversal.
                        self._pressure_interp_start_value = float(pressure_input_mapped)
                        self._pressure_interp_started_at = update_at
                # Briefly suppress stationary click-force transients without
                # making the brush trail the mouse by 12 px / 16 frames.
                elif not self.contact_warmup_done:
                    if moved_from_contact < 2 and self.contact_frame_no <= 2:
                        inject_pressure = 0
                    else:
                        self.contact_warmup_done = True
                        inject_pressure = min(
                            actual_pen_pressure,
                            max(32, self.prev_contact_pressure + rise_per_frame),
                        )
                else:
                    lo = max(0, self.prev_contact_pressure - fall_per_frame)
                    hi = min(1024, self.prev_contact_pressure + rise_per_frame)
                    inject_pressure = clamp_i(actual_pen_pressure, lo, hi)

                # Keep only the first stationary update quiet.
                if self.contact_frame_no <= 2 and moved_from_contact < 2:
                    inject_pressure = min(inject_pressure, 64)

                # While contact is still held, zero is a low pressure sample,
                # not pen-up.  Letting it bypass the configured floor emits an
                # in-contact zero immediately before UP; paint apps interpolate
                # that packet into a long needle tail on fast movement.
                if self.contact_warmup_done and min_contact_pressure > 0:
                    inject_pressure = max(inject_pressure, min_contact_pressure)
                if inject_pressure > min_contact_pressure + 8:
                    self._last_meaningful_contact_pressure = inject_pressure
                self.prev_contact_pressure = inject_pressure
                inject_flags = (
                    POINTER_FLAG_UPDATE
                    | POINTER_FLAG_INRANGE
                    | POINTER_FLAG_INCONTACT
                    | POINTER_FLAG_FIRSTBUTTON
                    | POINTER_FLAG_PRIMARY
                )
                next_state = "contact"
                self.precontact_frames = 0
        else:
            if contact_requested():
                begin_contact = False
                if self.precontact_frames == 0:
                    recovered_opening_path = (
                        list(movement_path)
                        if immediate_button_wake and len(movement_path) >= 2
                        else []
                    )
                    if recovered_opening_path:
                        self.precontact_x = recovered_opening_path[0][0]
                        self.precontact_y = recovered_opening_path[0][1]
                    else:
                        self.precontact_x = x
                        self.precontact_y = y
                    self.precontact_mapped = pressure_input_mapped
                    self._buffered_contact_path.clear()
                    if onset_buffer:
                        # Wait for one newer hardware pressure report. This is
                        # smoother at the cost of roughly one 60-Hz frame.
                        self.precontact_frames = 1
                        next_state = "hovering"
                    else:
                        # Low-latency mode starts immediately. The configured
                        # pressure floor prevents the first dab becoming the
                        # old extended hairline tail.
                        begin_contact = True
                elif pressure_fresh and self.precontact_frames >= precontact_required:
                    begin_contact = True

                if begin_contact:
                    opening_path = recovered_opening_path
                    self._buffer_movement_path(
                        opening_path[1:] if opening_path else movement_path
                    )
                    self.contact_frame_no = 1
                    start_mapped = self.precontact_mapped
                    if self._event_driven_movement:
                        # The interpolator may still be near zero when the first
                        # nonzero hardware report crosses contact. Sending the
                        # raw precontact sample as DOWN and then the interpolated
                        # value on the next point creates a high-pressure stamp
                        # followed by an abrupt collapse. Begin just above the
                        # contact threshold and rise monotonically instead.
                        start_mapped = min(
                            self.precontact_mapped,
                            max(
                                contact_threshold + 1,
                                interpolated_mapped,
                            ),
                        )
                    start_pressure = map_1023_to_1024(start_mapped)
                    if self.config.contact_source != "pressure_only":
                        start_pressure = max(1, start_pressure)
                    inject_pressure = start_pressure
                    if min_contact_pressure > 0 and inject_pressure > 0:
                        inject_pressure = max(inject_pressure, min_contact_pressure)
                    visible_start_mapped = map_1024_to_1023(inject_pressure)
                    if self.config.pressure_mode == "stroke_relative":
                        denom = max(1, 1023 - self.precontact_mapped)
                        visible_start_mapped = self.precontact_mapped + (
                            visible_start_mapped * denom + 1022
                        ) // 1023
                    self.prev_contact_pressure = inject_pressure
                    self._last_meaningful_contact_pressure = inject_pressure
                    self.contact_start_x = self.precontact_x
                    self.contact_start_y = self.precontact_y
                    self.contact_warmup_done = True
                    self.stroke_base_mapped = self.precontact_mapped
                    self.onset_catchup_pending = bool(onset_buffer or opening_path)
                    self._contact_path_direction = None
                    self._stationary_anchor_started_at = update_at
                    self._stationary_dab_emitted = False
                    self._pressure_interp_value = float(visible_start_mapped)
                    self._pressure_interp_target = float(
                        max(pressure_input_mapped, visible_start_mapped)
                    )
                    self._pressure_interp_remaining = (
                        max(1, int(self.config.pressure_interp_steps))
                        if onset_buffer
                        else 0
                    )
                    self._pressure_interp_start_value = float(visible_start_mapped)
                    self._pressure_interp_started_at = update_at
                    inject_x = self.precontact_x
                    inject_y = self.precontact_y
                    inject_flags = (
                        POINTER_FLAG_NEW
                        | POINTER_FLAG_DOWN
                        | POINTER_FLAG_INRANGE
                        | POINTER_FLAG_INCONTACT
                        | POINTER_FLAG_FIRSTBUTTON
                        | POINTER_FLAG_PRIMARY
                    )
                    next_state = "contact"
                    self.precontact_frames = 0
                    if not onset_buffer and not opening_path:
                        self._buffered_contact_path.clear()
                elif self.precontact_frames > 0:
                    self._buffer_movement_path(movement_path)
            elif mapped > 0:
                self.contact_frame_no = 0
                self.prev_contact_pressure = 0
                self.contact_warmup_done = False
                self.precontact_frames = 0
                self.precontact_mapped = 0
                self.stroke_base_mapped = 0
                self.onset_catchup_pending = False
                self._buffered_contact_path.clear()
                self._last_contact_position = None
                self._contact_path_direction = None
                next_state = "hovering"
            else:
                self.contact_frame_no = 0
                self.prev_contact_pressure = 0
                self.contact_warmup_done = False
                self.precontact_frames = 0
                self.precontact_mapped = 0
                self.stroke_base_mapped = 0
                self.onset_catchup_pending = False
                self._buffered_contact_path.clear()
                self._last_contact_position = None
                self._contact_path_direction = None
                next_state = "idle"

        if inject_flags is not None:
            status = inject_flags
            points = [(inject_x, inject_y)]
            stationary_dab_update = False
            if (inject_flags & POINTER_FLAG_UPDATE) and (inject_flags & POINTER_FLAG_INCONTACT):
                candidate_path = injection_path if injection_path is not None else movement_path
                direct_path = path_stabilization <= 0
                if direct_path:
                    prepared_path = self._prepare_direct_contact_path(
                        candidate_path,
                        endpoint=(inject_x, inject_y),
                        latest_only=true_low_latency,
                    )
                    # The first useful pressure sample often arrives tens of
                    # milliseconds after DOWN. Spread that rise spatially over
                    # the measured opening segment instead of applying it as a
                    # single thickness step. Linear subdivision does not alter
                    # the captured path or predict beyond the latest position.
                    onset_pressure_ramp = (
                        not true_low_latency
                        and self._last_contact_position is not None
                        # Raw desktop coordinates can advance 30-60 px between
                        # reports during a fast stroke. Keep the opening ramp
                        # eligible through that first sparse segment; emitted
                        # reports remain capped below, so this does not turn
                        # into an unbounded injection batch.
                        and moved_from_contact <= 96
                        and inject_pressure > pressure_before_update + 8
                    )
                    if onset_pressure_ramp:
                        dense_points = self._densify_linear_path(
                            prepared_path,
                            anchor=self._last_contact_position,
                        )
                        point_budget = self._contact_point_budget(
                            prepared_path,
                            anchor=self._last_contact_position,
                            pressure_start=pressure_before_update,
                            pressure_end=inject_pressure,
                            max_points=MAX_DIRECT_CONTACT_POINTS_PER_UPDATE,
                        )
                        points = self._limit_path(
                            dense_points,
                            max_points=point_budget,
                        )
                    else:
                        dense_points = prepared_path
                        point_budget = len(prepared_path)
                        points = prepared_path
                else:
                    prepared_path = self._prepare_contact_path(
                        candidate_path,
                        endpoint=(inject_x, inject_y),
                    )
                    dense_points = self._densify_contact_path(
                        prepared_path,
                        anchor=self._last_contact_position,
                    )
                    point_budget = self._contact_point_budget(
                        prepared_path,
                        anchor=self._last_contact_position,
                        pressure_start=pressure_before_update,
                        pressure_end=inject_pressure,
                    )
                    points = self._limit_path(dense_points, max_points=point_budget)
                if self._trace is not None:
                    self._trace.record(
                        "path_budget",
                        path_mode="direct" if direct_path else "stabilized",
                        observed_points=len(prepared_path),
                        dense_points=len(dense_points),
                        budget=int(point_budget),
                        emitted_points=len(points),
                        pressure_delta=abs(int(inject_pressure) - int(pressure_before_update)),
                    )
                # A real pen need not resend an identical position and pressure
                # every scheduler tick. Avoid building up a visible dab while
                # preserving stationary pressure changes.
                pressure_changed_enough = (
                    abs(int(inject_pressure) - int(pressure_before_update)) >= 8
                )
                tilt_changed_enough = (
                    abs(int(self._aux_tilt_x) - int(self._last_sent_tilt_x)) >= 2
                )
                legacy_stationary_update = (
                    inject_pressure != pressure_before_update
                    and not self._event_driven_movement
                )
                opted_in_stationary_update = (
                    self._event_driven_movement
                    and update_at - self._stationary_anchor_started_at >= 0.05
                    and (
                        (
                            not self._stationary_dab_emitted
                            and (
                                stationary_pressure_updates
                                or auxiliary_stationary_updates
                            )
                        )
                        or (
                            pressure_fresh
                            and (
                                (
                                    stationary_pressure_updates
                                    and pressure_changed_enough
                                )
                                or (
                                    auxiliary_stationary_updates
                                    and tilt_changed_enough
                                )
                            )
                        )
                    )
                )
                if not points and (
                    legacy_stationary_update or opted_in_stationary_update
                ):
                    anchor = self._last_contact_position or (inject_x, inject_y)
                    points = (
                        self._stationary_dab_path(anchor)
                        if opted_in_stationary_update
                        else [anchor]
                    )
                    stationary_dab_update = opted_in_stationary_update

            all_ok = bool(points)
            count = len(points)
            pressure_fractions: list[float] = []
            if count > 0 and self._last_contact_position is not None:
                cumulative = 0.0
                previous_point = self._last_contact_position
                for point in points:
                    cumulative += math.hypot(
                        point[0] - previous_point[0],
                        point[1] - previous_point[1],
                    )
                    pressure_fractions.append(cumulative)
                    previous_point = point
                if cumulative > 0.0:
                    pressure_fractions = [distance / cumulative for distance in pressure_fractions]
                else:
                    pressure_fractions.clear()
            last_sent_pressure: int | None = None
            scheduled_reports: list[dict[str, int | str | None]] = []
            point_pressures: list[int] = []
            for index, (point_x, point_y) in enumerate(points, start=1):
                point_pressure = inject_pressure
                if pressure_fractions or count > 1:
                    fraction = (
                        pressure_fractions[index - 1]
                        if pressure_fractions
                        else index / count
                    )
                    point_pressure = round(
                        pressure_before_update
                        + (inject_pressure - pressure_before_update) * fraction
                    )
                point_pressures.append(point_pressure)
                scheduled_reports.append(
                    {
                        "flags": int(inject_flags),
                        "x": int(point_x),
                        "y": int(point_y),
                        "pressure_1024": int(point_pressure),
                        "tag": (
                            "stationary_contact"
                            if stationary_dab_update
                            else next_state
                        ),
                        "tilt_x": (
                            self._aux_tilt_x if self._has_aux_xtilt() else None
                        ),
                    }
                )
            native_batch = callable(getattr(self.pen, "inject_batch", None))
            if (
                scheduled_reports
                and native_batch
                and bool(inject_flags & POINTER_FLAG_NEW)
            ):
                self._prime_first_native_contact(
                    flags=int(inject_flags),
                    x=int(points[0][0]),
                    y=int(points[0][1]),
                )
            if scheduled_reports and native_batch:
                ok, _err = self._inject_pen_batch(scheduled_reports)
                all_ok = all_ok and ok
                if ok and (inject_flags & POINTER_FLAG_INCONTACT):
                    self._last_contact_position = points[-1]
                    last_sent_pressure = point_pressures[-1]
                    self._last_sent_tilt_x = self._aux_tilt_x
            else:
                for report, point_pressure in zip(
                    scheduled_reports,
                    point_pressures,
                    strict=True,
                ):
                    ok, _err = self._inject_pen(
                        flags=int(report["flags"]),
                        x=int(report["x"]),
                        y=int(report["y"]),
                        pressure_1024=point_pressure,
                        tag=str(report["tag"]),
                    )
                    all_ok = all_ok and ok
                    if ok and (inject_flags & POINTER_FLAG_INCONTACT):
                        self._last_contact_position = (
                            int(report["x"]),
                            int(report["y"]),
                        )
                        last_sent_pressure = point_pressure
                        self._last_sent_tilt_x = self._aux_tilt_x
            if stationary_dab_update and all_ok:
                self._stationary_dab_emitted = True
            if inject_flags & POINTER_FLAG_INCONTACT:
                self.prev_contact_pressure = (
                    last_sent_pressure
                    if last_sent_pressure is not None
                    else pressure_before_update
                )
            injected = bool(points) and all_ok
            failed = bool(points) and not all_ok

        self.state = next_state
        if self.state == "idle" and not lmb_down:
            self._reset_path_stabilizer()
            self._reset_clean_stroke_ending()

        if (
            self._trace is not None
            and self._trace.active
            and not lmb_down
            and self.state != "contact"
        ):
            self._record_motion_diagnostic_batch()
            self._finish_motion_diagnostics()
            self._finish_trace("release" if prev_state == "contact" else "no_contact")

        if self.config.debug_mode and self.state != prev_state:
            self.log(
                f"STATE {prev_state} -> {self.state} mapped={mapped} pen={inject_pressure} "
                f"button={selected_button} down={int(lmb_down)} phys={int(lmb_physical)} "
                f"frame={self.contact_frame_no} "
                f"fresh={int(pressure_fresh)}"
            )
        elif (
            self.config.debug_mode
            and self.state == "contact"
            and self.contact_frame_no <= 12
        ):
            self.log(
                f"CONTACT frame={self.contact_frame_no} mapped={mapped} actual={actual_pen_pressure} "
                f"sent={inject_pressure} moved={moved_from_contact} warmup={int(self.contact_warmup_done)} "
                f"button={selected_button} down={int(lmb_down)} phys={int(lmb_physical)} "
                f"xtilt={self._aux_tilt_x} points={len(points) if inject_flags else 0}"
            )

        suppress_selected = (
            self.config.suppress_rmb
            if selected_button == "right"
            else self.config.suppress_lmb
        )
        if suppress_selected and (not self.config.no_click_through):
            now = time.perf_counter()
            click_max_s = max(0.01, float(self.config.click_max_ms) / 1000.0)
            click_move_px = max(0, int(self.config.click_move_px))
            click_pressure_max = clamp_i(int(self.config.click_pressure_max), 0, 1023)
            if self.state == "contact":
                self.click_candidate_active = False
            elif lmb_down:
                if not self.click_candidate_active:
                    self.click_candidate_active = True
                    self.click_start_t = now
                    self.click_start_x = x
                    self.click_start_y = y
                    self.click_peak_mapped = mapped
                else:
                    self.click_peak_mapped = max(self.click_peak_mapped, mapped)
            else:
                if self.click_candidate_active:
                    dt = now - self.click_start_t
                    moved = abs(x - self.click_start_x) + abs(y - self.click_start_y)
                    if (
                        dt <= click_max_s
                        and moved <= click_move_px
                        and self.click_peak_mapped <= click_pressure_max
                        and self.state != "contact"
                    ):
                        if selected_button == "right":
                            self.pen.emit_right_click()
                        else:
                            self.pen.emit_left_click()
                self.click_candidate_active = False

        if self.state == "idle" and not lmb_down:
            self.active_button = None

        return SyntheticPenSample(
            x=x,
            y=y,
            mapped_1023=mapped,
            pen_1024=inject_pressure if inject_flags is not None else actual_pen_pressure,
            state=self.state,
            lmb_down=lmb_down,
            lmb_physical=lmb_physical,
            status=status,
            injected=injected,
            failed=failed,
        )


class SyntheticPenEmitter:
    """Own output adapters and expose the synchronous stroke-planning lifecycle.

    ``update`` remains the stable hot-path interface. Adapter construction,
    arming, fail-open behavior, and close ordering stay outside planner state.
    """

    __slots__ = ("_pen", "_planner", "_suppressor_adapter")

    def __init__(self, config: SyntheticPenConfig, log: Callable[[str], None]) -> None:
        pen = _SyntheticPenInjector(log=log)
        trace = (
            StrokeTraceRecorder(config.trace_dir, log)
            if config.debug_mode and config.trace_dir
            else None
        )
        suppressor = _MouseLmbSuppressor(
            log=log,
            suppress_left=config.suppress_lmb,
            suppress_right=config.suppress_rmb,
            debug_mode=config.debug_mode,
            allow_raw_direct_motion=config.allow_raw_direct_motion,
            left_button_owns_contact=(config.left_output_target == "pressure"),
            right_button_owns_contact=(config.right_output_target == "pressure"),
        )
        object.__setattr__(self, "_pen", pen)
        object.__setattr__(self, "_suppressor_adapter", suppressor)
        object.__setattr__(
            self,
            "_planner",
            _StrokePlanner(
                config,
                log,
                pen=pen,
                suppressor=suppressor,
                trace=trace,
            ),
        )

    @property
    def config(self) -> SyntheticPenConfig:
        return self._planner.config

    @config.setter
    def config(self, value: SyntheticPenConfig) -> None:
        self._planner.config = value

    @property
    def pen(self) -> Any:
        return self._pen

    @pen.setter
    def pen(self, value: Any) -> None:
        object.__setattr__(self, "_pen", value)
        self._planner.pen = value

    @property
    def _suppressor(self) -> Any | None:
        return self._suppressor_adapter

    @_suppressor.setter
    def _suppressor(self, value: Any | None) -> None:
        object.__setattr__(self, "_suppressor_adapter", value)
        self._planner._suppressor = value

    def open(self) -> None:
        self.open_unarmed()
        self.arm_input()

    def open_unarmed(self) -> None:
        self._pen.open()

    def _wait_for_clean_button_baseline(self, timeout_s: float = 1.0) -> None:
        """Arm only after the click that started the application is released."""
        deadline = time.perf_counter() + max(0.0, float(timeout_s))
        while True:
            left_down = (
                self.config.left_output_target != "off"
                and bool(self._pen.is_lmb_down())
            )
            right_down = (
                self.config.right_output_target != "off"
                and bool(self._pen.is_rmb_down())
            )
            if not left_down and not right_down:
                return
            if time.perf_counter() >= deadline:
                held = "/".join(
                    name
                    for name, down in (("left", left_down), ("right", right_down))
                    if down
                )
                raise RuntimeError(
                    f"Release the {held} mouse button before starting pressure output"
                )
            time.sleep(0.002)

    def arm_input(self) -> None:
        self._wait_for_clean_button_baseline()
        if self._suppressor is not None:
            self._suppressor.start()

    def close(self) -> None:
        trace = self._planner._trace
        if trace is not None:
            self._planner._finish_trace("bridge_close")
            trace.close()
        if self._suppressor is not None:
            self._suppressor.stop()
        self._pen.close()

    def fail_open(self, reason: str) -> None:
        if self._suppressor is not None:
            self._suppressor.fail_open(reason)

    def set_force_stop_callback(
        self,
        callback: Callable[[str], None] | None,
    ) -> None:
        if self._suppressor is not None:
            self._suppressor.set_force_stop_callback(callback)

    def set_debug_mode(self, enabled: bool) -> None:
        if self._suppressor is not None:
            self._suppressor.debug_mode = bool(enabled)
            self._suppressor.set_timing_callback(
                self._planner._observe_native_timing if enabled else None
            )
        self._planner.set_debug_mode(enabled)

    def sync_button_modes(self) -> None:
        if self._suppressor is not None:
            self._suppressor.set_button_ownership(
                left=self.config.left_output_target == "pressure",
                right=self.config.right_output_target == "pressure",
            )

    def set_movement_callback(self, callback: Callable[[], None] | None) -> None:
        self._planner.note_movement_callback(callback)
        if self._suppressor is not None:
            self._suppressor.set_movement_callback(callback)

    def set_native_input_capture(self, capture: Any | None) -> None:
        if self._suppressor is not None:
            self._suppressor.set_native_input_capture(capture)

    def release(self) -> None:
        self._planner.release()

    def update(
        self,
        left_mapped: int,
        right_mapped: int,
        *,
        pressure_fresh: bool = True,
        left_raw: int | None = None,
        right_raw: int | None = None,
    ) -> SyntheticPenSample:
        """Advance stroke planning and synchronously deliver its reports."""
        return self._planner.advance(
            left_mapped,
            right_mapped,
            pressure_fresh=pressure_fresh,
            left_raw=left_raw,
            right_raw=right_raw,
        )


def _drain_mode3_left_raws(session: PressureHidppSession) -> list[int]:
    out: list[int] = []
    if session.dev is None:
        return out
    session.maintain_pressure_stream()
    while True:
        try:
            data = session.dev.read(64)
        except OSError:
            break
        if not data:
            break
        if (
            len(data) >= 20
            and data[0] == REPORT_LONG
            and data[1] == session.device_index
            and data[2] == session.pressure_feature_index
            and data[3] == PRESSURE_MODE3_ADDR
        ):
            raw_u16 = int.from_bytes(bytes(data[4:6]), byteorder="big")
            out.append(raw_u16 >> 6)
    return out


def run_synthetic_pen_bridge(
    *,
    emitter_config: SyntheticPenConfig,
    pressure_config: PressureConfig,
    mode: int,
    mode_arg: int,
    hz: float,
    duration: float | None,
    log_file: str,
) -> int:
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="ascii") as fh:

        def log(line: str) -> None:
            print(line)
            fh.write(line + "\n")
            fh.flush()

        session = PressureHidppSession(log=log)
        emitter = SyntheticPenEmitter(
            config=replace(
                emitter_config,
                pressure_interp_steps=max(1, int(round(hz / 60.0))),
                trace_raw_min=pressure_config.raw_min,
                trace_raw_max=pressure_config.raw_max,
                trace_curve=str(pressure_config.curve),
                trace_curve_strength=pressure_config.curve_strength,
            ),
            log=log,
        )

        latest_raw = pressure_config.raw_min
        latest_mapped = 0
        frames_decoded = 0
        frames_injected = 0
        failed_injects = 0
        last_status_print = 0.0
        start = time.perf_counter()
        period = 1.0 / max(1.0, hz)
        next_tick = start

        try:
            emitter.set_movement_callback(lambda: None)
            emitter.open()
            session.open()
            session.enable_pressure_stream(mode=mode, mode_arg=mode_arg)
            log(
                f"BRIDGE start hz={hz:.2f} mode=0x{mode:02X} mode_arg=0x{mode_arg:02X} "
                f"raw=[{pressure_config.raw_min},{pressure_config.raw_max}] "
                f"curve={pressure_config.curve} strength={pressure_config.curve_strength:.2f} "
                f"contact_source={emitter_config.contact_source} pressure_mode={emitter_config.pressure_mode} "
                f"threshold={emitter_config.contact_threshold}/{emitter_config.release_threshold} "
                f"suppress_lmb={int(emitter_config.suppress_lmb)} "
                f"click_through={int(not emitter_config.no_click_through)} "
                f"release_teardown={int(emitter_config.release_teardown)}"
            )
            while True:
                now = time.perf_counter()
                if duration is not None and (now - start) >= duration:
                    log("Duration reached")
                    break

                decoded_raws = _drain_mode3_left_raws(session)
                frames_decoded += len(decoded_raws)

                if decoded_raws:
                    # Coalesce a high-rate HID burst into one pointer report per
                    # bridge tick. Replaying the whole burst can flood Windows'
                    # input queue and cause a delayed release-position snap.
                    latest_raw = decoded_raws[-1]
                    norm = normalize_raw_pressure(latest_raw, pressure_config.raw_min, pressure_config.raw_max)
                    latest_mapped = map_normalized_pressure(norm, pressure_config)

                sample = emitter.update(
                    latest_mapped,
                    0,
                    pressure_fresh=bool(decoded_raws),
                    left_raw=latest_raw,
                )
                if sample.injected:
                    frames_injected += 1
                if sample.failed:
                    failed_injects += 1

                if now - last_status_print >= 1.0:
                    last_status_print = now
                    log(
                        f"[{now-start:7.3f}s] raw={latest_raw:3d} mapped={latest_mapped:4d} "
                        f"pen={sample.pen_1024:4d} state={sample.state:8s} "
                        f"lmb={int(sample.lmb_down)} phys={int(sample.lmb_physical)} decoded={frames_decoded} "
                        f"injected={frames_injected} failed={failed_injects}"
                    )

                next_tick += period
                sleep_s = next_tick - time.perf_counter()
                if sleep_s > 0:
                    if emitter.wait_for_movement(sleep_s):
                        next_tick = time.perf_counter()
                else:
                    next_tick = time.perf_counter()

        except KeyboardInterrupt:
            log("Interrupted")
        except Exception as exc:
            log(f"ERROR {type(exc).__name__}: {exc}")
            return 1
        finally:
            emitter.release()
            # Restore native mouse clicks before potentially slow or stalled HID
            # cleanup. The user must never be trapped behind the suppression hook.
            emitter.close()
            session.close()

        elapsed = max(1e-9, time.perf_counter() - start)
        log("")
        log("SUMMARY")
        log(f"elapsed={elapsed:.3f}s")
        log(f"decoded_frames={frames_decoded} ({frames_decoded/elapsed:.2f} Hz)")
        log(f"injected_frames={frames_injected} ({frames_injected/elapsed:.2f} Hz)")
        log(f"failed_injects={failed_injects}")
        log(f"log_file={log_path}")
    return 0
