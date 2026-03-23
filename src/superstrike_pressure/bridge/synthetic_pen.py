"""Canonical Superstrike synthetic pen runtime."""

from __future__ import annotations

import ctypes
import threading
import time
from dataclasses import dataclass
from ctypes import wintypes
from pathlib import Path
from typing import Callable

from superstrike_pressure.bridge.curves import PressureConfig, map_normalized_pressure
from superstrike_pressure.sniff.hidpp_pressure import (
    DEVICE_INDEX,
    PRESSURE_FEATURE_INDEX,
    PRESSURE_MODE3_ADDR,
    PressureHidppSession,
    normalize_raw_pressure,
)

PT_PEN = 3
POINTER_FEEDBACK_DEFAULT = 1
VK_LBUTTON = 0x01

POINTER_FLAG_NEW = 0x00000001
POINTER_FLAG_INRANGE = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
POINTER_FLAG_FIRSTBUTTON = 0x00000010
POINTER_FLAG_DOWN = 0x00010000
POINTER_FLAG_UPDATE = 0x00020000
POINTER_FLAG_UP = 0x00040000

PEN_FLAG_NONE = 0x00000000
PEN_MASK_PRESSURE = 0x00000001

WH_MOUSE_LL = 14
HC_ACTION = 0
LLMHF_INJECTED = 0x00000001
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONUP = 0x00A2
PM_REMOVE = 0x0001
REPORT_LONG = 0x11


@dataclass
class SyntheticPenConfig:
    contact_threshold: int = 10
    release_threshold: int = 6
    contact_source: str = "lmb_and_pressure"
    pressure_mode: str = "absolute"
    rise_per_frame: int = 256
    fall_per_frame: int = 512
    min_contact_pressure: int = 0
    suppress_lmb: bool = False
    no_click_through: bool = False
    click_max_ms: int = 220
    click_move_px: int = 6
    click_pressure_max: int = 12
    release_teardown: bool = False


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


def clamp_i(v: int, lo: int, hi: int) -> int:
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def map_1023_to_1024(v: int) -> int:
    v = clamp_i(v, 0, 1023)
    return (v * 1024 + 511) // 1023


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

    def emit_left_click(self) -> None:
        self.user32.mouse_event(0x0002, 0, 0, 0, None)
        self.user32.mouse_event(0x0004, 0, 0, 0, None)

    def _to_himetric(self, px: int) -> int:
        return int(round(float(px) * 2540.0 / float(self.dpi)))

    def inject(self, *, flags: int, x: int, y: int, pressure_1024: int, tag: str) -> tuple[bool, int]:
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
        pi.dwKeyStates = 0
        pi.PerformanceCount = 0
        pi.ButtonChangeType = 0

        self.pti.penInfo.penFlags = PEN_FLAG_NONE
        self.pti.penInfo.penMask = PEN_MASK_PRESSURE
        self.pti.penInfo.pressure = clamp_i(pressure_1024, 0, 1024)
        self.pti.penInfo.rotation = 0
        self.pti.penInfo.tiltX = 0
        self.pti.penInfo.tiltY = 0

        ctypes.set_last_error(0)
        ok = bool(self.user32.InjectSyntheticPointerInput(self.device, ctypes.byref(self.pti), 1))
        err = ctypes.get_last_error()
        if not ok:
            self.log(
                f"INJECT {tag} failed err={err} flags=0x{flags:08X} x={x} y={y} "
                f"pressure={self.pti.penInfo.pressure}"
            )
        return ok, err


class _MouseLmbSuppressor:
    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.enabled = False
        self.hook = ctypes.c_void_p()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._proc = None
        self._lmb_down = False

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
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = ctypes.c_void_p

    def start(self) -> None:
        if self._thread is not None:
            return
        self.enabled = True
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="lmb-suppressor", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def stop(self) -> None:
        self.enabled = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._lmb_down = False

    def is_lmb_down(self) -> bool:
        return self._lmb_down

    def _run(self) -> None:
        hook_proc_t = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p)

        @hook_proc_t
        def _hook_proc(n_code: int, w_param: int, l_param: int) -> int:
            if n_code == HC_ACTION and self.enabled:
                msg = int(w_param)
                if msg in (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_NCLBUTTONDOWN, WM_NCLBUTTONUP):
                    try:
                        info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                        injected = (int(info.flags) & LLMHF_INJECTED) != 0
                    except Exception:
                        return 1

                    # IMPORTANT: only hardware events may mutate hook button state.
                    # Injected mouse events (from synthetic pointer promotion, etc.)
                    # must not arm/disarm contact or we can get stuck-down lag.
                    if not injected:
                        if msg in (WM_LBUTTONDOWN, WM_NCLBUTTONDOWN):
                            self._lmb_down = True
                        elif msg in (WM_LBUTTONUP, WM_NCLBUTTONUP):
                            self._lmb_down = False
                        return 1
            return int(self.user32.CallNextHookEx(self.hook, n_code, w_param, l_param))

        self._proc = _hook_proc
        hmod = self.kernel32.GetModuleHandleW(None)
        ctypes.set_last_error(0)
        self.hook = ctypes.c_void_p(self.user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, hmod, 0))
        err = ctypes.get_last_error()
        if not self.hook:
            self.log(f"LMB suppressor hook install failed err={err}")
            self._ready.set()
            return

        self.log("LMB suppressor active")
        self._ready.set()

        msg = wintypes.MSG()
        while not self._stop.is_set():
            while self.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                self.user32.TranslateMessage(ctypes.byref(msg))
                self.user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.001)

        if self.hook:
            self.user32.UnhookWindowsHookEx(self.hook)
            self.hook = ctypes.c_void_p()
        self._lmb_down = False
        self.log("LMB suppressor stopped")


class SyntheticPenEmitter:
    def __init__(self, config: SyntheticPenConfig, log: Callable[[str], None]) -> None:
        self.config = config
        self.log = log
        self.pen = _SyntheticPenInjector(log=log)
        self._suppressor = _MouseLmbSuppressor(log=log) if config.suppress_lmb else None
        self.state = "idle"
        self.contact_frame_no = 0
        self.prev_contact_pressure = 0
        self.contact_warmup_done = False
        self.precontact_frames = 0
        self.precontact_x = 0
        self.precontact_y = 0
        self.contact_start_x = 0
        self.contact_start_y = 0
        self.stroke_base_mapped = 0

        self.click_candidate_active = False
        self.click_start_t = 0.0
        self.click_start_x = 0
        self.click_start_y = 0
        self.click_peak_mapped = 0

    def open(self) -> None:
        self.pen.open()
        if self._suppressor is not None:
            self._suppressor.start()

    def close(self) -> None:
        if self._suppressor is not None:
            self._suppressor.stop()
        self.pen.close()

    def release(self) -> None:
        if self.state == "contact":
            x, y = self.pen.get_cursor_pos()
            self._emit_release_teardown(x=x, y=y)
        self.state = "idle"
        self.contact_frame_no = 0
        self.prev_contact_pressure = 0
        self.contact_warmup_done = False
        self.precontact_frames = 0
        self.stroke_base_mapped = 0

    def _read_lmb(self) -> bool:
        # When suppressing native LMB, some systems don't update GetAsyncKeyState
        # reliably for the blocked click. Use hook state for contact gating.
        if self._suppressor is not None:
            return self._suppressor.is_lmb_down()
        return self.pen.is_lmb_down()

    def _emit_release_teardown(self, *, x: int, y: int) -> tuple[bool, bool]:
        # Optional post-UP teardown for apps that keep a lingering in-range pen.
        # Sequence: UP|INRANGE -> UPDATE|INRANGE -> UPDATE(out-of-range).
        ok1, _ = self.pen.inject(
            flags=POINTER_FLAG_UP | POINTER_FLAG_INRANGE,
            x=x,
            y=y,
            pressure_1024=0,
            tag="release_up",
        )
        if not self.config.release_teardown:
            return ok1, not ok1

        ok2, _ = self.pen.inject(
            flags=POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE,
            x=x,
            y=y,
            pressure_1024=0,
            tag="release_hover",
        )
        ok3, _ = self.pen.inject(
            flags=POINTER_FLAG_UPDATE,
            x=x,
            y=y,
            pressure_1024=0,
            tag="release_endhover",
        )
        ok = ok1 and ok2 and ok3
        return ok, not ok

    def update(self, left_mapped: int, right_mapped: int) -> SyntheticPenSample:
        # TODO: right channel injection — eraser mode / haptics / symmetry control
        # Currently telemetry + config only. See thread_backend_v2.md decision 2.
        _ = right_mapped
        prev_state = self.state
        mapped = clamp_i(int(left_mapped), 0, 1023)
        lmb_physical = self.pen.is_lmb_down()
        lmb_down = self._read_lmb()
        x, y = self.pen.get_cursor_pos()

        release_threshold = clamp_i(int(self.config.release_threshold), 0, 1023)
        rise_per_frame = clamp_i(int(self.config.rise_per_frame), 0, 1024)
        fall_per_frame = clamp_i(int(self.config.fall_per_frame), 0, 1024)
        min_contact_pressure = clamp_i(int(self.config.min_contact_pressure), 0, 1024)
        precontact_required = 1 if self.config.pressure_mode == "stroke_relative" else 2

        pressure_mapped = mapped
        if self.config.pressure_mode == "stroke_relative" and self.state == "contact":
            if pressure_mapped <= self.stroke_base_mapped:
                pressure_mapped = 0
            else:
                denom = max(1, 1023 - self.stroke_base_mapped)
                pressure_mapped = ((pressure_mapped - self.stroke_base_mapped) * 1023) // denom
        actual_pen_pressure = map_1023_to_1024(pressure_mapped)

        def contact_requested() -> bool:
            if self.config.contact_source == "pressure_only":
                return mapped > int(self.config.contact_threshold)
            return lmb_down and mapped > int(self.config.contact_threshold)

        def contact_released() -> bool:
            if self.config.contact_source == "pressure_only":
                return mapped <= release_threshold
            # Primary release is button-up. Add pressure fallback to avoid
            # lingering contact if hook-up is delayed/missed under suppression.
            if not lmb_down:
                return True
            if mapped <= release_threshold:
                return True
            # Fast-release fallback: when hook state is stuck down, don't wait for
            # deep pressure decay; drop at/under contact threshold.
            return mapped <= int(self.config.contact_threshold)

        injected = False
        failed = False
        status = 0
        inject_flags: int | None = None
        inject_pressure = 0
        next_state = self.state
        moved_from_contact = 0

        if self.state == "contact":
            if contact_released():
                inject_pressure = 0
                next_state = "idle"
                self.contact_frame_no = 0
                self.prev_contact_pressure = 0
                self.contact_warmup_done = False
                self.precontact_frames = 0
                self.stroke_base_mapped = 0
                ok, fail = self._emit_release_teardown(x=x, y=y)
                injected = ok
                failed = fail
            else:
                self.contact_frame_no += 1
                moved_from_contact = abs(x - self.contact_start_x) + abs(y - self.contact_start_y)

                # Keep startup pressure at zero until there is a minimum cursor movement.
                # This prevents stationary "stamp" blobs caused by click-force transients.
                if not self.contact_warmup_done:
                    if moved_from_contact < 12 and self.contact_frame_no <= 16:
                        inject_pressure = 0
                    else:
                        self.contact_warmup_done = True
                        inject_pressure = min(actual_pen_pressure, max(32, self.prev_contact_pressure + 48))
                elif self.contact_frame_no <= 10:
                    inject_pressure = min(actual_pen_pressure, self.prev_contact_pressure + 64)
                else:
                    lo = max(0, self.prev_contact_pressure - fall_per_frame)
                    hi = min(1024, self.prev_contact_pressure + rise_per_frame)
                    inject_pressure = clamp_i(actual_pen_pressure, lo, hi)

                # Extra guard while near start: keep pressure low for tiny movement.
                if self.contact_frame_no <= 14 and moved_from_contact < 10:
                    inject_pressure = min(inject_pressure, 64)

                if self.contact_warmup_done and min_contact_pressure > 0 and inject_pressure > 0:
                    inject_pressure = max(inject_pressure, min_contact_pressure)
                self.prev_contact_pressure = inject_pressure
                inject_flags = (
                    POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT | POINTER_FLAG_FIRSTBUTTON
                )
                next_state = "contact"
                self.precontact_frames = 0
        else:
            if contact_requested():
                self.precontact_frames += 1
                if self.precontact_frames == 1:
                    self.precontact_x = x
                    self.precontact_y = y
                if self.precontact_frames >= precontact_required:
                    self.contact_frame_no = 1
                    inject_pressure = 0
                    self.prev_contact_pressure = inject_pressure
                    self.contact_start_x = x
                    self.contact_start_y = y
                    self.contact_warmup_done = False
                    self.stroke_base_mapped = mapped
                    inject_flags = (
                        POINTER_FLAG_NEW
                        | POINTER_FLAG_DOWN
                        | POINTER_FLAG_INRANGE
                        | POINTER_FLAG_INCONTACT
                        | POINTER_FLAG_FIRSTBUTTON
                    )
                    next_state = "contact"
                    self.precontact_frames = 0
                else:
                    self.contact_frame_no = 0
                    self.prev_contact_pressure = 0
                    self.contact_warmup_done = False
                    self.stroke_base_mapped = 0
                    next_state = "hovering" if mapped > 0 else "idle"
            elif mapped > 0:
                self.contact_frame_no = 0
                self.prev_contact_pressure = 0
                self.contact_warmup_done = False
                self.precontact_frames = 0
                self.stroke_base_mapped = 0
                next_state = "hovering"
            else:
                self.contact_frame_no = 0
                self.prev_contact_pressure = 0
                self.contact_warmup_done = False
                self.precontact_frames = 0
                self.stroke_base_mapped = 0
                next_state = "idle"

        if inject_flags is not None:
            status = inject_flags
            ok, _err = self.pen.inject(
                flags=inject_flags,
                x=x,
                y=y,
                pressure_1024=inject_pressure,
                tag=next_state,
            )
            injected = ok
            failed = not ok

        self.state = next_state

        if self.state != prev_state:
            self.log(
                f"STATE {prev_state} -> {self.state} mapped={mapped} pen={inject_pressure} "
                f"lmb={int(lmb_down)} phys={int(lmb_physical)} frame={self.contact_frame_no}"
            )
        elif self.state == "contact" and self.contact_frame_no <= 12:
            self.log(
                f"CONTACT frame={self.contact_frame_no} mapped={mapped} actual={actual_pen_pressure} "
                f"sent={inject_pressure} moved={moved_from_contact} warmup={int(self.contact_warmup_done)} "
                f"lmb={int(lmb_down)} phys={int(lmb_physical)}"
            )

        if self.config.suppress_lmb and (not self.config.no_click_through):
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
                        self.pen.emit_left_click()
                self.click_candidate_active = False

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


def _drain_mode3_left_raws(session: PressureHidppSession) -> list[int]:
    out: list[int] = []
    if session.dev is None:
        return out
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
            and data[1] == DEVICE_INDEX
            and data[2] == PRESSURE_FEATURE_INDEX
            and data[3] == PRESSURE_MODE3_ADDR
        ):
            out.append(int(data[4]))
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
        emitter = SyntheticPenEmitter(config=emitter_config, log=log)

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
                    for raw in decoded_raws:
                        latest_raw = raw
                        norm = normalize_raw_pressure(raw, pressure_config.raw_min, pressure_config.raw_max)
                        latest_mapped = map_normalized_pressure(norm, pressure_config)
                        sample = emitter.update(latest_mapped, 0)
                        if sample.injected:
                            frames_injected += 1
                        if sample.failed:
                            failed_injects += 1
                else:
                    sample = emitter.update(latest_mapped, 0)
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
                    time.sleep(sleep_s)
                else:
                    next_tick = time.perf_counter()

        except KeyboardInterrupt:
            log("Interrupted")
        except Exception as exc:
            log(f"ERROR {type(exc).__name__}: {exc}")
            return 1
        finally:
            emitter.release()
            session.close()
            emitter.close()

        elapsed = max(1e-9, time.perf_counter() - start)
        log("")
        log("SUMMARY")
        log(f"elapsed={elapsed:.3f}s")
        log(f"decoded_frames={frames_decoded} ({frames_decoded/elapsed:.2f} Hz)")
        log(f"injected_frames={frames_injected} ({frames_injected/elapsed:.2f} Hz)")
        log(f"failed_injects={failed_injects}")
        log(f"log_file={log_path}")
    return 0
