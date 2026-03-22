"""Superstrike pressure -> Windows synthetic pen bridge for Krita/Ink apps."""

from __future__ import annotations

import argparse
import ctypes
import signal
import sys
import threading
import time
from dataclasses import dataclass
from ctypes import wintypes
from pathlib import Path

from superstrike_pressure.bridge.curves import PressureConfig, map_normalized_pressure
from superstrike_pressure.sniff.hidpp_pressure import (
    PressureHidppSession,
    normalize_raw_pressure,
)

# Pointer/input constants.
PT_PEN = 3
POINTER_FEEDBACK_DEFAULT = 1
VK_LBUTTON = 0x01

POINTER_FLAG_NEW = 0x00000001
POINTER_FLAG_INRANGE = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
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
DEVICE_INDEX = 0x01
PRESSURE_FEATURE_INDEX = 0x0C
PRESSURE_MODE3_ADDR = 0x10


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


@dataclass(frozen=True)
class BridgeSample:
    raw: int
    mapped_1023: int
    pen_1024: int
    lmb_down: bool
    state: str


def clamp_i(v: int, lo: int, hi: int) -> int:
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def map_1023_to_1024(v: int) -> int:
    v = clamp_i(v, 0, 1023)
    return (v * 1024 + 511) // 1023


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Read Superstrike pressure (HID++ mode 3) and inject Windows synthetic pen "
            "input with real pressure."
        )
    )
    p.add_argument("--hz", type=float, default=60.0, help="Loop/inject rate (default: 60).")
    p.add_argument("--mode", type=int, default=3, help="Pressure mode byte (default: 3).")
    p.add_argument("--mode-arg", type=int, default=0, help="Pressure mode arg (default: 0).")
    p.add_argument("--raw-min", type=int, default=80, help="Raw min calibration (default: 80).")
    p.add_argument("--raw-max", type=int, default=170, help="Raw max calibration (default: 170).")
    p.add_argument(
        "--curve",
        choices=["linear", "ease_in", "ease_out", "s_curve"],
        default="s_curve",
        help="Pressure curve (default: s_curve).",
    )
    p.add_argument("--curve-strength", type=float, default=2.0, help="Curve strength (default: 2.0).")
    p.add_argument("--deadzone-low", type=float, default=0.05, help="Low deadzone (default: 0.05).")
    p.add_argument("--deadzone-high", type=float, default=0.95, help="High deadzone (default: 0.95).")
    p.add_argument(
        "--contact-threshold",
        type=int,
        default=10,
        help="Mapped pressure threshold (0..1023) for contact (default: 10).",
    )
    p.add_argument(
        "--release-threshold",
        type=int,
        default=6,
        help="Mapped pressure release threshold (0..1023, default: 6).",
    )
    p.add_argument(
        "--contact-source",
        choices=["lmb_and_pressure", "pressure_only"],
        default="lmb_and_pressure",
        help="Contact gating source (default: lmb_and_pressure).",
    )
    p.add_argument("--duration", type=float, default=None, help="Optional runtime duration (seconds).")
    p.add_argument(
        "--suppress-lmb",
        action="store_true",
        help="Suppress native left mouse click events while bridge is running.",
    )
    p.add_argument(
        "--no-click-through",
        action="store_true",
        help="Disable synthetic passthrough clicks when --suppress-lmb is active.",
    )
    p.add_argument(
        "--click-max-ms",
        type=int,
        default=220,
        help="Max hold duration for passthrough click (default: 220ms).",
    )
    p.add_argument(
        "--click-move-px",
        type=int,
        default=6,
        help="Max cursor movement for passthrough click (default: 6px Manhattan).",
    )
    p.add_argument(
        "--click-pressure-max",
        type=int,
        default=12,
        help="Max mapped pressure allowed for passthrough click (default: 12).",
    )
    p.add_argument(
        "--log-file",
        default="docs/pressure_pen_bridge.txt",
        help="Log file path (default: docs/pressure_pen_bridge.txt).",
    )
    return p.parse_args()


class SyntheticPenInjector:
    def __init__(self, log) -> None:
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
        self.user32.mouse_event.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
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
        self.log(
            f"SYNTH open handle=0x{int(dev):X} screen={self.screen_w}x{self.screen_h} dpi={self.dpi}"
        )

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
        # Injected mouse click for UI interaction when native LMB is suppressed.
        self.user32.mouse_event(0x0002, 0, 0, 0, None)  # LEFTDOWN
        self.user32.mouse_event(0x0004, 0, 0, 0, None)  # LEFTUP

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
                f"INJECT {tag} failed err={err} flags=0x{flags:08X} "
                f"x={x} y={y} pressure={self.pti.penInfo.pressure}"
            )
        return ok, err


class MouseLmbSuppressor:
    """Global low-level mouse hook to swallow LMB events."""

    def __init__(self, log) -> None:
        self.log = log
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.enabled = False
        self.hook = ctypes.c_void_p()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._proc = None

        self.user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        self.user32.SetWindowsHookExW.restype = ctypes.c_void_p
        self.user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        self.user32.UnhookWindowsHookEx.restype = ctypes.c_int
        self.user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p]
        self.user32.CallNextHookEx.restype = ctypes.c_longlong
        self.user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
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

    def _run(self) -> None:
        hook_proc_t = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p)

        @hook_proc_t
        def _hook_proc(nCode: int, wParam: int, lParam: int) -> int:
            if nCode == HC_ACTION and self.enabled:
                msg = int(wParam)
                if msg in (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_NCLBUTTONDOWN, WM_NCLBUTTONUP):
                    try:
                        info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                        if (int(info.flags) & LLMHF_INJECTED) == 0:
                            return 1
                    except Exception:
                        return 1
            return int(self.user32.CallNextHookEx(self.hook, nCode, wParam, lParam))

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
            time.sleep(0.005)

        if self.hook:
            self.user32.UnhookWindowsHookEx(self.hook)
            self.hook = ctypes.c_void_p()
        self.log("LMB suppressor stopped")


def _drain_mode3_left_raws(session: PressureHidppSession) -> list[int]:
    """Drain nonblocking queue and return all mode-3 left raw values in order."""
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


def main() -> int:
    if sys.platform != "win32":
        print("ERROR: this bridge is Windows-only.")
        return 1

    args = parse_args()
    cfg = PressureConfig(
        raw_min=args.raw_min,
        raw_max=args.raw_max,
        out_min=0,
        out_max=1023,
        deadzone_low=args.deadzone_low,
        deadzone_high=args.deadzone_high,
        curve=args.curve,
        curve_strength=args.curve_strength,
    )

    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="ascii") as fh:

        def log(line: str) -> None:
            print(line)
            fh.write(line + "\n")
            fh.flush()

        stop_requested = False

        def _signal_handler(signum: int, _frame) -> None:
            nonlocal stop_requested
            stop_requested = True
            log(f"Signal {signum} received; stopping")

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _signal_handler)
            except Exception:
                pass

        session = PressureHidppSession(log=log)
        pen = SyntheticPenInjector(log=log)
        lmb_suppressor = MouseLmbSuppressor(log=log) if args.suppress_lmb else None

        latest_raw = cfg.raw_min
        latest_mapped = 0
        state = "idle"  # idle, hovering, contact
        contact_frame_no = 0
        prev_contact_pressure = 0
        contact_start_t = 0.0
        contact_start_x = 0
        contact_start_y = 0
        contact_warmup_done = False
        precontact_frames = 0
        precontact_x = 0
        precontact_y = 0
        precontact_t = 0.0
        click_candidate_active = False
        click_start_t = 0.0
        click_start_x = 0
        click_start_y = 0
        click_peak_mapped = 0
        frames_injected = 0
        frames_decoded = 0
        failed_injects = 0
        last_status_print = 0.0
        start = time.perf_counter()
        period = 1.0 / max(1.0, args.hz)
        next_tick = start
        release_threshold = clamp_i(args.release_threshold, 0, 1023)
        click_through_enabled = args.suppress_lmb and (not args.no_click_through)
        click_max_s = max(0.01, float(args.click_max_ms) / 1000.0)
        click_move_px = max(0, int(args.click_move_px))
        click_pressure_max = clamp_i(args.click_pressure_max, 0, 1023)

        def contact_requested(lmb_down: bool, mapped: int) -> bool:
            if args.contact_source == "pressure_only":
                return mapped > args.contact_threshold
            return lmb_down and mapped > args.contact_threshold

        def contact_released(lmb_down: bool, mapped: int) -> bool:
            if args.contact_source == "pressure_only":
                return mapped <= release_threshold
            return not lmb_down

        try:
            pen.open()
            session.open()
            session.enable_pressure_stream(mode=args.mode, mode_arg=args.mode_arg)
            if lmb_suppressor is not None:
                lmb_suppressor.start()
            log(
                f"BRIDGE start hz={args.hz:.2f} mode=0x{args.mode:02X} mode_arg=0x{args.mode_arg:02X} "
                f"raw=[{cfg.raw_min},{cfg.raw_max}] curve={cfg.curve} strength={cfg.curve_strength:.2f} "
                f"deadzone=[{cfg.deadzone_low:.3f},{cfg.deadzone_high:.3f}] threshold={args.contact_threshold} "
                f"release_threshold={release_threshold} contact_source={args.contact_source} "
                f"suppress_lmb={int(args.suppress_lmb)} click_through={int(click_through_enabled)} "
                f"click_max_ms={args.click_max_ms} click_move_px={click_move_px} click_pressure_max={click_pressure_max}"
            )

            while not stop_requested:
                now = time.perf_counter()
                if args.duration is not None and (now - start) >= args.duration:
                    log("Duration reached")
                    break

                decoded_raws = _drain_mode3_left_raws(session)
                frames_decoded += len(decoded_raws)

                lmb_down_state = pen.is_lmb_down()

                # LMB is authoritative for pen lift: release immediately on button-up.
                if state == "contact" and contact_released(lmb_down_state, latest_mapped):
                    x, y = pen.get_cursor_pos()
                    ok, _err = pen.inject(
                        flags=POINTER_FLAG_UP,
                        x=x,
                        y=y,
                        pressure_1024=0,
                        tag="release",
                    )
                    if ok:
                        frames_injected += 1
                    else:
                        failed_injects += 1
                    next_state = "idle"
                    log(
                        f"STATE {state} -> {next_state} transition=release "
                        f"raw={latest_raw} mapped={latest_mapped} pen=0 lmb={int(lmb_down_state)}"
                    )
                    state = next_state
                    contact_frame_no = 0
                    prev_contact_pressure = 0
                    contact_start_t = 0.0
                    contact_start_x = x
                    contact_start_y = y
                    contact_warmup_done = False
                    precontact_frames = 0
                    precontact_x = x
                    precontact_y = y
                    precontact_t = 0.0

                for raw in decoded_raws:
                    latest_raw = raw
                    norm = normalize_raw_pressure(latest_raw, cfg.raw_min, cfg.raw_max)
                    latest_mapped = map_normalized_pressure(norm, cfg)
                    actual_pen_pressure = map_1023_to_1024(latest_mapped)
                    lmb_down = pen.is_lmb_down()
                    lmb_down_state = lmb_down
                    x, y = pen.get_cursor_pos()

                    inject_flags: int | None = None
                    inject_pressure = 0
                    next_state = state
                    transition = None

                    if state == "contact":
                        if contact_released(lmb_down, latest_mapped):
                            inject_flags = POINTER_FLAG_UP
                            inject_pressure = 0
                            next_state = "idle"
                            transition = "release"
                            contact_frame_no = 0
                            prev_contact_pressure = 0
                            contact_start_t = 0.0
                            contact_start_x = x
                            contact_start_y = y
                            contact_warmup_done = False
                            precontact_frames = 0
                            precontact_x = x
                            precontact_y = y
                            precontact_t = 0.0
                        else:
                            contact_frame_no += 1
                            moved = abs(x - contact_start_x) + abs(y - contact_start_y)
                            if not contact_warmup_done:
                                if moved < 12 and contact_frame_no <= 16:
                                    inject_pressure = 0
                                else:
                                    contact_warmup_done = True
                                    inject_pressure = min(actual_pen_pressure, max(32, prev_contact_pressure + 48))
                            elif contact_frame_no <= 10:
                                inject_pressure = min(actual_pen_pressure, prev_contact_pressure + 64)
                            else:
                                inject_pressure = actual_pen_pressure
                            if moved < 6 and contact_frame_no <= 14:
                                inject_pressure = min(inject_pressure, 64)
                            prev_contact_pressure = inject_pressure
                            inject_flags = POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
                            next_state = "contact"
                            precontact_frames = 0
                    else:
                        if contact_requested(lmb_down, latest_mapped):
                            precontact_frames += 1
                            if precontact_frames == 1:
                                precontact_x = x
                                precontact_y = y
                                precontact_t = time.perf_counter()
                            moved_from_arm = abs(x - precontact_x) + abs(y - precontact_y)
                            if precontact_frames >= 2:
                                contact_frame_no = 1
                                inject_pressure = 0
                                prev_contact_pressure = inject_pressure
                                contact_start_t = time.perf_counter()
                                contact_start_x = x
                                contact_start_y = y
                                contact_warmup_done = False
                                inject_flags = POINTER_FLAG_NEW | POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
                                next_state = "contact"
                                transition = "contact_down"
                                precontact_frames = 0
                                precontact_t = 0.0
                            else:
                                contact_frame_no = 0
                                prev_contact_pressure = 0
                                contact_start_t = 0.0
                                next_state = "hovering" if latest_mapped > 0 else "idle"
                                transition = "precontact_arm"
                        elif latest_mapped > 0:
                            contact_frame_no = 0
                            prev_contact_pressure = 0
                            contact_start_t = 0.0
                            inject_flags = None
                            inject_pressure = 0
                            next_state = "hovering"
                            contact_warmup_done = False
                            precontact_frames = 0
                            precontact_t = 0.0
                            if state != "hovering":
                                transition = "hover_enter"
                        else:
                            contact_frame_no = 0
                            prev_contact_pressure = 0
                            contact_start_t = 0.0
                            next_state = "idle"
                            contact_warmup_done = False
                            precontact_frames = 0
                            precontact_t = 0.0
                            if state != "idle":
                                transition = "idle_enter"

                    if inject_flags is not None:
                        ok, _err = pen.inject(
                            flags=inject_flags,
                            x=x,
                            y=y,
                            pressure_1024=inject_pressure,
                            tag=next_state,
                        )
                        if ok:
                            frames_injected += 1
                        else:
                            failed_injects += 1

                    if transition is not None or next_state != state:
                        log(
                            f"STATE {state} -> {next_state} transition={transition or 'state_change'} "
                            f"raw={latest_raw} mapped={latest_mapped} pen={inject_pressure if inject_flags is not None else actual_pen_pressure} "
                            f"lmb={int(lmb_down)}"
                        )
                    state = next_state

                if click_through_enabled:
                    cx, cy = pen.get_cursor_pos()
                    if state == "contact":
                        click_candidate_active = False
                    elif lmb_down_state:
                        if not click_candidate_active:
                            click_candidate_active = True
                            click_start_t = now
                            click_start_x = cx
                            click_start_y = cy
                            click_peak_mapped = latest_mapped
                        else:
                            click_peak_mapped = max(click_peak_mapped, latest_mapped)
                    else:
                        if click_candidate_active:
                            dt = now - click_start_t
                            moved = abs(cx - click_start_x) + abs(cy - click_start_y)
                            if (
                                dt <= click_max_s
                                and moved <= click_move_px
                                and click_peak_mapped <= click_pressure_max
                                and state != "contact"
                            ):
                                pen.emit_left_click()
                                log(
                                    f"CLICK passthrough dt_ms={dt*1000.0:.0f} moved={moved} "
                                    f"peak_mapped={click_peak_mapped}"
                                )
                        click_candidate_active = False

                if now - last_status_print >= 1.0:
                    last_status_print = now
                    pen_pressure = map_1023_to_1024(latest_mapped)
                    log(
                        f"[{now-start:7.3f}s] raw={latest_raw:3d} mapped={latest_mapped:4d} "
                        f"pen={pen_pressure:4d} state={state:8s} lmb={int(lmb_down_state)} "
                        f"decoded={frames_decoded} injected={frames_injected} failed={failed_injects}"
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
            try:
                if state == "contact":
                    x, y = pen.get_cursor_pos()
                    pen.inject(
                        flags=POINTER_FLAG_UP,
                        x=x,
                        y=y,
                        pressure_1024=0,
                        tag="final_up",
                    )
            except Exception as exc:
                log(f"Final pen release failed: {exc}")

            if lmb_suppressor is not None:
                lmb_suppressor.stop()
            session.close()
            pen.close()

            elapsed = max(1e-9, time.perf_counter() - start)
            log("")
            log("SUMMARY")
            log(f"elapsed={elapsed:.3f}s")
            log(f"decoded_frames={frames_decoded} ({frames_decoded/elapsed:.2f} Hz)")
            log(f"injected_frames={frames_injected} ({frames_injected/elapsed:.2f} Hz)")
            log(f"failed_injects={failed_injects}")
            log(f"log_file={log_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
