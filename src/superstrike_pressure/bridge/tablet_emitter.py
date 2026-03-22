"""Superstrike pressure bridge with pluggable tablet backends (VMulti or synthetic pen)."""

from __future__ import annotations

import argparse
import ctypes
import platform
import sys
import time
from dataclasses import dataclass
from ctypes import wintypes
from multiprocessing.connection import Client
from pathlib import Path
from typing import Callable, Literal

import hid

from superstrike_pressure.bridge.curves import PressureConfig, map_normalized_pressure
from superstrike_pressure.sniff.hidpp_pressure import (
    PressureHidppSession,
    normalize_raw_pressure,
)

VMULTI_VID = 0x00FF
VMULTI_PID = 0xBACC
VMULTI_CONTROL_USAGE_PAGE = 0xFF00
VMULTI_CONTROL_USAGE = 0x0001
VMULTI_DEFAULT_COL05_PATH = (
    r"\\?\HID#hid&Col05#1&2d595ca7&0&0004#{4d1e55b2-f16f-11cf-88cb-001111000030}"
)
VMULTI_ALT_COL05_PATH = (
    r"\\?\HID#hid&Col05#1&4784345&0&0004#{4d1e55b2-f16f-11cf-88cb-001111000030}"
)

VMULTI_REPORT_ID_PEN = 0x05
VMULTI_REPORT_ID_CONTROL = 0x40
VMULTI_STATUS_TIP = 0x02
VMULTI_STATUS_IN_RANGE = 0x01
VMULTI_COORD_MAX = 0x7FFF
VMULTI_PRESSURE_MAX = 0x1FFF

VK_LBUTTON = 0x01
SM_CXSCREEN = 0
SM_CYSCREEN = 1

FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

WriteMode = Literal["auto", "write", "write_prefixed", "feature", "feature_prefixed", "writefile"]
ReportFormat = Literal["format_a", "format_b", "report06"]
Backend = Literal["vmulti", "synthetic"]

# Synthetic pointer constants (Win32).
PT_PEN = 3
POINTER_FEEDBACK_DEFAULT = 1
POINTER_FLAG_NEW = 0x00000001
POINTER_FLAG_INRANGE = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
POINTER_FLAG_DOWN = 0x00010000
POINTER_FLAG_UPDATE = 0x00020000
POINTER_FLAG_UP = 0x00040000
PEN_FLAG_NONE = 0x00000000
PEN_MASK_PRESSURE = 0x00000001


def clamp_i(v: int, lo: int, hi: int) -> int:
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def map_range(v: int, in_lo: int, in_hi: int, out_lo: int, out_hi: int) -> int:
    if in_hi <= in_lo:
        return out_lo
    t = (v - in_lo) / float(in_hi - in_lo)
    if t < 0.0:
        t = 0.0
    if t > 1.0:
        t = 1.0
    return int(round(out_lo + t * (out_hi - out_lo)))


def map_1023_to_8191(v: int) -> int:
    v = clamp_i(v, 0, 1023)
    return (v * VMULTI_PRESSURE_MAX) // 1023


def map_1023_to_1024(v: int) -> int:
    v = clamp_i(v, 0, 1023)
    return (v * 1024 + 511) // 1023


def to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="ignore")
        except Exception:
            return repr(value)
    return str(value)


def hex_bytes(data: list[int]) -> str:
    return " ".join(f"{b:02X}" for b in data)


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


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


def _user32():
    return ctypes.windll.user32


def get_cursor_pos() -> tuple[int, int]:
    pt = POINT()
    ok = _user32().GetCursorPos(ctypes.byref(pt))
    if ok == 0:
        raise OSError("GetCursorPos failed")
    return int(pt.x), int(pt.y)


def get_screen_size() -> tuple[int, int]:
    w = int(_user32().GetSystemMetrics(SM_CXSCREEN))
    h = int(_user32().GetSystemMetrics(SM_CYSCREEN))
    return max(1, w), max(1, h)


def is_left_button_down() -> bool:
    return bool(_user32().GetAsyncKeyState(VK_LBUTTON) & 0x8000)


@dataclass(frozen=True)
class VMultiDeviceInfo:
    path: bytes
    vid: int
    pid: int
    usage_page: int | None
    usage: int | None
    manufacturer: str
    product: str

    @property
    def path_text(self) -> str:
        return to_text(self.path)


@dataclass(frozen=True)
class TabletEmission:
    x: int
    y: int
    pressure_8191: int
    status: int
    left_mapped_1023: int


@dataclass(frozen=True)
class WriteResult:
    method: str
    wrote: int
    bytes_sent: list[int]
    win32_error: int | None = None


def enumerate_vmulti_candidates() -> list[VMultiDeviceInfo]:
    out: list[VMultiDeviceInfo] = []
    for d in hid.enumerate():
        path = d.get("path")
        if not isinstance(path, (bytes, bytearray)):
            continue
        vid = int(d.get("vendor_id", 0))
        pid = int(d.get("product_id", 0))
        usage_page = d.get("usage_page")
        usage = d.get("usage")
        manufacturer = to_text(d.get("manufacturer_string"))
        product = to_text(d.get("product_string"))
        text = " ".join([manufacturer, product, to_text(path)]).lower()

        if vid == VMULTI_VID and pid == VMULTI_PID:
            out.append(
                VMultiDeviceInfo(
                    path=bytes(path),
                    vid=vid,
                    pid=pid,
                    usage_page=usage_page,
                    usage=usage,
                    manufacturer=manufacturer,
                    product=product,
                )
            )
            continue

        if "vmulti" in text or "virtualhid" in text:
            out.append(
                VMultiDeviceInfo(
                    path=bytes(path),
                    vid=vid,
                    pid=pid,
                    usage_page=usage_page,
                    usage=usage,
                    manufacturer=manufacturer,
                    product=product,
                )
            )
    return out


def _path_norm(p: str) -> str:
    return p.replace("/", "\\").lower()


def resolve_vmulti_path(
    *,
    requested_path: str | None,
    log: Callable[[str], None],
) -> bytes:
    candidates = enumerate_vmulti_candidates()
    if not candidates:
        raise RuntimeError("No VMulti HID candidates found")

    if requested_path:
        want = _path_norm(requested_path)
        for c in candidates:
            if _path_norm(c.path_text) == want:
                return c.path

        log("Requested VMulti path not found, falling back to enumerated candidates")

    # Try known-good Col05 control endpoints first.
    for known in (VMULTI_DEFAULT_COL05_PATH, VMULTI_ALT_COL05_PATH):
        want = _path_norm(known)
        for c in candidates:
            if _path_norm(c.path_text) == want:
                return c.path

    ranked = sorted(
        candidates,
        key=lambda c: (
            0 if "col05" in c.path_text.lower() else 1,
            0 if (c.vid == VMULTI_VID and c.pid == VMULTI_PID) else 1,
            0 if (c.usage_page == VMULTI_CONTROL_USAGE_PAGE and c.usage == VMULTI_CONTROL_USAGE) else 1,
            len(c.path_text),
        ),
    )
    best = ranked[0]
    return best.path


class VMultiPenEmitter:
    """Emit control-wrapped pen reports to VMulti Col05 control collection.

    Inner pen payload (9 bytes total):
      [0]  Report ID (0x05)
      [1]  Status (0x01 hover, 0x03 contact)
      [2]  X lo
      [3]  X hi
      [4]  Y lo
      [5]  Y hi
      [6]  Pressure lo
      [7]  Pressure hi
      [8]  Padding/extra (set 0)

    Wrapped control report:
      [0]  0x40 (REPORTID_CONTROL)
      [1]  0x09 (inner report length)
      [2:] inner pen payload bytes above
    """

    def __init__(
        self,
        *,
        device_path: str | None,
        write_mode: WriteMode,
        log: Callable[[str], None],
    ) -> None:
        self.requested_path = device_path
        self.write_mode_requested = write_mode
        self.write_mode_effective: WriteMode | None = None
        self.log = log
        self.dev: hid.device | None = None
        self.file_handle: int | None = None
        self.path: bytes | None = None
        self.screen_w = 1
        self.screen_h = 1

    def open(self) -> None:
        self.path = resolve_vmulti_path(requested_path=self.requested_path, log=self.log)
        self.dev = hid.device()
        open_attempts: list[bytes] = []
        if self.path is not None:
            open_attempts.append(self.path)
        for known in (VMULTI_DEFAULT_COL05_PATH, VMULTI_ALT_COL05_PATH):
            kb = known.encode("utf-8")
            if kb not in open_attempts:
                open_attempts.append(kb)
        for c in enumerate_vmulti_candidates():
            if c.path not in open_attempts and "col05" in c.path_text.lower():
                open_attempts.append(c.path)

        opened = False
        for candidate in open_attempts:
            try:
                self.dev.open_path(candidate)
                self.path = candidate
                opened = True
                break
            except OSError as e:
                self.log(f"open_path failed path={to_text(candidate)!r} error={e}")
        if not opened:
            raise RuntimeError("Unable to open VMulti Col05 control endpoint")

        self.dev.set_nonblocking(True)
        path_text = to_text(self.path)
        handle = int(
            ctypes.windll.kernel32.CreateFileW(
                path_text,
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
        )
        if handle == INVALID_HANDLE_VALUE:
            self.file_handle = None
            self.log("VMULTI CreateFileW(handle for writefile) unavailable")
        else:
            self.file_handle = handle
            self.log(f"VMULTI CreateFileW(handle for writefile)=0x{handle:X}")
        self.screen_w, self.screen_h = get_screen_size()
        self.log(f"VMULTI open path={to_text(self.path)!r}")
        self.log(
            f"VMULTI config control_report=0x{VMULTI_REPORT_ID_CONTROL:02X} "
            f"inner_report=0x{VMULTI_REPORT_ID_PEN:02X} "
            f"screen={self.screen_w}x{self.screen_h}"
        )

    def close(self) -> None:
        if self.file_handle is not None and self.file_handle != INVALID_HANDLE_VALUE:
            try:
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.file_handle))
            except Exception:
                pass
            self.file_handle = None
        if self.dev is not None:
            try:
                self.dev.close()
            except OSError:
                pass
            self.dev = None

    def _build_pen_inner_report(
        self,
        *,
        report_id: int,
        status: int,
        x: int,
        y: int,
        pressure: int,
        pressure_max: int,
        include_extra_byte9: bool,
    ) -> list[int]:
        x = clamp_i(x, 0, VMULTI_COORD_MAX)
        y = clamp_i(y, 0, VMULTI_COORD_MAX)
        pressure = clamp_i(pressure, 0, pressure_max)
        inner = [
            report_id & 0xFF,
            status & 0xFF,
            x & 0xFF,
            (x >> 8) & 0xFF,
            y & 0xFF,
            (y >> 8) & 0xFF,
            pressure & 0xFF,
            (pressure >> 8) & 0xFF,
            0x00,
        ]
        if include_extra_byte9:
            inner.append(0x00)
        return inner

    def _build_control_report(
        self,
        *,
        report_format: ReportFormat,
        status: int,
        x: int,
        y: int,
        pressure: int,
    ) -> list[int]:
        if report_format == "format_a":
            inner = self._build_pen_inner_report(
                report_id=0x05,
                status=status,
                x=x,
                y=y,
                pressure=pressure,
                pressure_max=0x1FFF,
                include_extra_byte9=True,  # 10-byte inner payload
            )
            return [VMULTI_REPORT_ID_CONTROL, 0x0A] + inner

        if report_format == "format_b":
            inner = self._build_pen_inner_report(
                report_id=0x05,
                status=status,
                x=x,
                y=y,
                pressure=pressure,
                pressure_max=0x1FFF,
                include_extra_byte9=False,  # 9-byte inner payload
            )
            packet = [VMULTI_REPORT_ID_CONTROL, 0x09] + inner
            if len(packet) < 65:
                packet.extend([0x00] * (65 - len(packet)))
            return packet

        # report06
        inner = self._build_pen_inner_report(
            report_id=0x06,
            status=status,
            x=x,
            y=y,
            pressure=pressure,
            pressure_max=0x3FFF,
            include_extra_byte9=True,  # 10-byte inner payload
        )
        return [VMULTI_REPORT_ID_CONTROL, 0x0A] + inner

    def _attempt_write(self, packet: list[int], method: WriteMode, *, label: str) -> WriteResult:
        if self.dev is None:
            raise RuntimeError("VMulti not open")
        effective_packet = list(packet)
        win32_error: int | None = None
        try:
            if method == "write":
                ret = int(self.dev.write(packet))
            elif method == "write_prefixed":
                effective_packet = [0x00] + packet
                ret = int(self.dev.write(effective_packet))
            elif method == "feature":
                ret = int(self.dev.send_feature_report(packet))
            elif method == "feature_prefixed":
                effective_packet = [0x00] + packet
                ret = int(self.dev.send_feature_report(effective_packet))
            elif method == "writefile":
                if self.file_handle is None or self.file_handle == INVALID_HANDLE_VALUE:
                    ret = -1
                    win32_error = -1
                else:
                    wf_packet = list(packet)
                    if len(wf_packet) < 65:
                        wf_packet.extend([0x00] * (65 - len(wf_packet)))
                    effective_packet = wf_packet
                    buf = (ctypes.c_ubyte * len(wf_packet))(*wf_packet)
                    written = ctypes.c_ulong(0)
                    ok = ctypes.windll.kernel32.WriteFile(
                        ctypes.c_void_p(self.file_handle),
                        ctypes.byref(buf),
                        len(wf_packet),
                        ctypes.byref(written),
                        None,
                    )
                    if ok:
                        ret = int(written.value)
                        win32_error = 0
                    else:
                        ret = -1
                        win32_error = int(ctypes.get_last_error())
            else:
                raise ValueError(f"Unknown method {method}")
        except OSError as e:
            self.log(f"TX {label} method={method} write_error={e} bytes={hex_bytes(effective_packet)}")
            return WriteResult(method=method, wrote=-1, bytes_sent=effective_packet, win32_error=win32_error)

        self.log(
            f"TX {label} method={method} wrote={ret} win32_error={win32_error} "
            f"bytes={hex_bytes(effective_packet)}"
        )
        return WriteResult(method=method, wrote=ret, bytes_sent=effective_packet, win32_error=win32_error)

    def _write_control_report(
        self,
        control_report: list[int],
        *,
        label: str,
        override_method: WriteMode | None = None,
    ) -> WriteResult:
        if self.dev is None:
            raise RuntimeError("VMulti not open")

        attempts: list[WriteMode]
        selected = override_method or self.write_mode_requested
        if selected == "write":
            attempts = ["write"]
        elif selected == "write_prefixed":
            attempts = ["write_prefixed"]
        elif selected == "feature":
            attempts = ["feature"]
        elif selected == "feature_prefixed":
            attempts = ["feature_prefixed"]
        elif selected == "writefile":
            attempts = ["writefile"]
        else:
            if self.write_mode_effective is not None:
                attempts = [self.write_mode_effective]
            else:
                attempts = ["write", "write_prefixed", "feature", "feature_prefixed", "writefile"]

        for method in attempts:
            result = self._attempt_write(control_report, method, label=label)
            if result.wrote > 0:
                if selected == "auto" and self.write_mode_effective != method:
                    self.write_mode_effective = method
                    self.log(f"VMULTI write method selected: {method}")
                return result

        raise RuntimeError("VMulti write failed (wrote <= 0)")

    def emit_report(
        self,
        *,
        status: int,
        x: int,
        y: int,
        pressure: int,
        label: str,
        report_format: ReportFormat = "format_a",
        write_method: WriteMode | None = None,
    ) -> WriteResult:
        control = self._build_control_report(
            report_format=report_format,
            status=status,
            x=x,
            y=y,
            pressure=pressure,
        )
        return self._write_control_report(control, label=label, override_method=write_method)

    def emit_from_state(
        self,
        *,
        left_mapped_1023: int,
        left_down: bool,
        report_format: ReportFormat,
        write_method: WriteMode | None = None,
    ) -> TabletEmission:
        x_px, y_px = get_cursor_pos()
        x = map_range(x_px, 0, max(1, self.screen_w - 1), 0, VMULTI_COORD_MAX)
        y = map_range(y_px, 0, max(1, self.screen_h - 1), 0, VMULTI_COORD_MAX)

        left_mapped_1023 = clamp_i(left_mapped_1023, 0, 1023)
        if report_format == "report06":
            pressure_8191 = (left_mapped_1023 * 0x3FFF) // 1023
        else:
            pressure_8191 = map_1023_to_8191(left_mapped_1023)

        if left_down and pressure_8191 > 0:
            status = VMULTI_STATUS_IN_RANGE | VMULTI_STATUS_TIP
        else:
            status = VMULTI_STATUS_IN_RANGE

        if status != (VMULTI_STATUS_IN_RANGE | VMULTI_STATUS_TIP):
            pressure_8191 = 0

        self.emit_report(
            status=status,
            x=x,
            y=y,
            pressure=pressure_8191,
            label="pen",
            report_format=report_format,
            write_method=write_method,
        )
        return TabletEmission(
            x=x,
            y=y,
            pressure_8191=pressure_8191,
            status=status,
            left_mapped_1023=left_mapped_1023,
        )

    def send_out_of_range(
        self,
        *,
        report_format: ReportFormat = "format_a",
        write_method: WriteMode | None = None,
    ) -> None:
        # Final "all zero" report requested by user.
        self.emit_report(
            status=0x00,
            x=0,
            y=0,
            pressure=0,
            label="final.out_of_range",
            report_format=report_format,
            write_method=write_method,
        )


class SyntheticPenEmitter:
    """Emit synthetic pen input via InjectSyntheticPointerInput (Windows userspace API)."""

    def __init__(self, *, log: Callable[[str], None], contact_threshold: int = 10) -> None:
        self.log = log
        self.contact_threshold = max(0, int(contact_threshold))
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.device = ctypes.c_void_p()
        self.dpi = 96
        self.screen_w = 1
        self.screen_h = 1
        self.state = "idle"  # idle, hovering, contact
        self.contact_frame_no = 0
        self.prev_contact_pressure = 0
        self.contact_start_t = 0.0
        self.contact_start_x = 0
        self.contact_start_y = 0
        self.contact_warmup_done = False
        self.precontact_frames = 0
        self.precontact_x = 0
        self.precontact_y = 0
        self.precontact_t = 0.0
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

        destroy = getattr(self.user32, "DestroySyntheticPointerDevice", None)
        self._destroy = destroy
        if destroy is not None:
            destroy.argtypes = [ctypes.c_void_p]
            destroy.restype = None

        if hasattr(self.user32, "GetDpiForSystem"):
            self.user32.GetDpiForSystem.argtypes = []
            self.user32.GetDpiForSystem.restype = ctypes.c_uint32

    def open(self) -> None:
        ctypes.set_last_error(0)
        dev = self.user32.CreateSyntheticPointerDevice(PT_PEN, 1, POINTER_FEEDBACK_DEFAULT)
        err = ctypes.get_last_error()
        if not dev:
            raise RuntimeError(f"CreateSyntheticPointerDevice failed err={err}")
        self.device = ctypes.c_void_p(dev)
        if hasattr(self.user32, "GetDpiForSystem"):
            dpi = int(self.user32.GetDpiForSystem())
            if dpi > 0:
                self.dpi = dpi
        self.screen_w, self.screen_h = get_screen_size()
        self.log(
            f"SYNTH open handle=0x{int(dev):X} screen={self.screen_w}x{self.screen_h} "
            f"dpi={self.dpi} threshold={self.contact_threshold}"
        )

    def close(self) -> None:
        if self.device and self._destroy is not None:
            try:
                self._destroy(self.device)
            except Exception:
                pass
        self.device = ctypes.c_void_p()

    def _to_himetric(self, px: int) -> int:
        return int(round(float(px) * 2540.0 / float(max(1, self.dpi))))

    def _inject(self, *, flags: int, x: int, y: int, pressure_1024: int, label: str) -> bool:
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
        if not ok:
            err = ctypes.get_last_error()
            self.log(
                f"SYNTH inject failed label={label} err={err} flags=0x{flags:08X} "
                f"x={x} y={y} pressure={int(self.pti.penInfo.pressure)}"
            )
        return ok

    def emit_from_state(
        self,
        *,
        left_mapped_1023: int,
        left_down: bool,
        sample_fresh: bool = True,
        report_format: ReportFormat | None = None,
        write_method: WriteMode | None = None,
    ) -> TabletEmission:
        _ = report_format
        _ = write_method
        x, y = get_cursor_pos()

        left_mapped_1023 = clamp_i(left_mapped_1023, 0, 1023)
        pressure_1024_actual = map_1023_to_1024(left_mapped_1023)
        should_start_contact = sample_fresh and left_down and left_mapped_1023 > self.contact_threshold

        flags: int | None = None
        status = 0
        emitted_pressure = 0
        next_state = self.state
        if self.state == "contact":
            if not left_down:
                # LMB is authoritative for pen lift.
                flags = POINTER_FLAG_UP
                status = flags
                emitted_pressure = 0
                next_state = "idle"
                self.contact_frame_no = 0
                self.prev_contact_pressure = 0
                self.contact_start_t = 0.0
                self.contact_start_x = x
                self.contact_start_y = y
                self.contact_warmup_done = False
                self.precontact_frames = 0
                self.precontact_x = x
                self.precontact_y = y
                self.precontact_t = 0.0
            else:
                # Keep contact alive while LMB is held; do not wait for pressure decay.
                self.contact_frame_no += 1
                moved = abs(x - self.contact_start_x) + abs(y - self.contact_start_y)
                if not self.contact_warmup_done:
                    if moved < 12 and self.contact_frame_no <= 16:
                        emitted_pressure = 0
                    else:
                        self.contact_warmup_done = True
                        emitted_pressure = min(pressure_1024_actual, max(32, self.prev_contact_pressure + 48))
                elif self.contact_frame_no <= 10:
                    emitted_pressure = min(pressure_1024_actual, self.prev_contact_pressure + 64)
                else:
                    emitted_pressure = pressure_1024_actual
                if moved < 6 and self.contact_frame_no <= 14:
                    emitted_pressure = min(emitted_pressure, 64)
                self.prev_contact_pressure = emitted_pressure
                flags = POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
                status = flags
                next_state = "contact"
                self.precontact_frames = 0
        else:
            if should_start_contact:
                self.precontact_frames += 1
                if self.precontact_frames == 1:
                    self.precontact_x = x
                    self.precontact_y = y
                    self.precontact_t = time.perf_counter()
                moved_from_arm = abs(x - self.precontact_x) + abs(y - self.precontact_y)
                if self.precontact_frames >= 2:
                    # Attack limiter frame 1.
                    self.contact_frame_no = 1
                    emitted_pressure = 0
                    self.prev_contact_pressure = emitted_pressure
                    self.contact_start_t = time.perf_counter()
                    self.contact_start_x = x
                    self.contact_start_y = y
                    self.contact_warmup_done = False
                    flags = POINTER_FLAG_NEW | POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
                    status = flags
                    next_state = "contact"
                    self.precontact_frames = 0
                    self.precontact_t = 0.0
                else:
                    self.contact_frame_no = 0
                    self.prev_contact_pressure = 0
                    self.contact_start_t = 0.0
                    flags = None
                    status = 0
                    emitted_pressure = 0
                    next_state = "hovering" if left_mapped_1023 > 0 else "idle"
            elif left_mapped_1023 > 0:
                self.contact_frame_no = 0
                self.prev_contact_pressure = 0
                self.contact_start_t = 0.0
                self.contact_warmup_done = False
                self.precontact_frames = 0
                self.precontact_t = 0.0
                flags = None
                status = 0
                emitted_pressure = 0
                next_state = "hovering"
            else:
                self.contact_frame_no = 0
                self.prev_contact_pressure = 0
                self.contact_start_t = 0.0
                self.contact_warmup_done = False
                self.precontact_frames = 0
                self.precontact_t = 0.0
                next_state = "idle"

        if flags is not None:
            self._inject(
                flags=flags,
                x=x,
                y=y,
                pressure_1024=emitted_pressure,
                label=next_state,
            )
        self.state = next_state
        return TabletEmission(
            x=x,
            y=y,
            pressure_8191=emitted_pressure,  # synthetic backend uses 0..1024
            status=status,
            left_mapped_1023=left_mapped_1023,
        )

    def send_out_of_range(
        self,
        *,
        report_format: ReportFormat = "format_a",
        write_method: WriteMode | None = None,
    ) -> None:
        _ = report_format
        _ = write_method
        x, y = get_cursor_pos()
        if self.state == "contact":
            self._inject(
                flags=POINTER_FLAG_UP,
                x=x,
                y=y,
                pressure_1024=0,
                label="final_up",
            )
        self.state = "idle"
        self.contact_frame_no = 0
        self.prev_contact_pressure = 0
        self.contact_start_t = 0.0
        self.contact_start_x = x
        self.contact_start_y = y
        self.contact_warmup_done = False
        self.precontact_frames = 0
        self.precontact_x = x
        self.precontact_y = y
        self.precontact_t = 0.0


class PipePressureSource:
    def __init__(self, pipe_name: str, log: Callable[[str], None]) -> None:
        self.pipe_name = pipe_name
        self.log = log
        self.conn = None

    def open(self) -> None:
        self.conn = Client(self.pipe_name, family="AF_PIPE")
        self.log(f"PIPE connected to {self.pipe_name}")

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def poll_latest_left_mapped(self) -> int | None:
        if self.conn is None:
            return None
        latest = None
        while self.conn.poll(0):
            payload = self.conn.recv()
            if isinstance(payload, dict) and "left_mapped" in payload:
                latest = int(payload["left_mapped"])
        return latest


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Run Superstrike pressure bridge on Windows (VMulti or synthetic pen backend). "
            "Supports --test mode for VMulti diagnostics."
        )
    )
    p.add_argument(
        "--backend",
        choices=["vmulti", "synthetic"],
        default="synthetic",
        help="Tablet output backend (default: synthetic).",
    )
    p.add_argument(
        "--test",
        action="store_true",
        help="Run VMulti-only phased test (5s hover, 3s half-pressure contact, 2s hover).",
    )
    p.add_argument(
        "--source",
        choices=["direct", "pipe"],
        default="direct",
        help="Pressure source for non-test mode (default: direct).",
    )
    p.add_argument(
        "--pipe-name",
        default=r"\\.\pipe\superstrike_pressure",
        help=r"Named pipe when --source=pipe (default: \\.\pipe\superstrike_pressure).",
    )
    p.add_argument(
        "--vmulti-path",
        default=VMULTI_DEFAULT_COL05_PATH,
        help="Preferred VMulti Col05 control path.",
    )
    p.add_argument(
        "--vmulti-write-mode",
        choices=["auto", "write", "write_prefixed", "feature", "feature_prefixed", "writefile"],
        default="auto",
        help=(
            "hidapi write method (default: auto; tries write, write+0x00, "
            "send_feature_report, send_feature_report+0x00, WriteFile)."
        ),
    )
    p.add_argument(
        "--report-format",
        choices=["format_a", "format_b", "report06"],
        default="format_a",
        help=(
            "VMulti inner report format: format_a(len=0x0A,id=0x05), "
            "format_b(len=0x09,id=0x05,pad65), report06(len=0x0A,id=0x06)."
        ),
    )
    p.add_argument("--raw-min", type=int, default=80, help="Left calibration min (default: 80).")
    p.add_argument("--raw-max", type=int, default=170, help="Left calibration max (default: 170).")
    p.add_argument("--mode", type=int, default=3, help="Superstrike pressure mode byte (default: 3).")
    p.add_argument("--mode-arg", type=int, default=0, help="Superstrike pressure mode arg (default: 0).")
    p.add_argument("--hz", type=float, default=60.0, help="Target update rate (default: 60Hz).")
    p.add_argument(
        "--curve",
        choices=["linear", "ease_in", "ease_out", "s_curve"],
        default="s_curve",
        help="Pressure curve (default: s_curve).",
    )
    p.add_argument("--curve-strength", type=float, default=2.0, help="Curve gamma (default: 2.0).")
    p.add_argument("--deadzone-low", type=float, default=0.05, help="Deadzone low (default: 0.05).")
    p.add_argument("--deadzone-high", type=float, default=0.95, help="Deadzone high (default: 0.95).")
    p.add_argument(
        "--contact-threshold",
        type=int,
        default=10,
        help="Synthetic backend contact threshold in mapped 0..1023 (default: 10).",
    )
    p.add_argument("--duration", type=float, default=None, help="Optional runtime limit in seconds.")
    p.add_argument(
        "--log-file",
        default=None,
        help="Log path. Defaults: test->docs/vmulti_emitter_test.txt, normal->docs/tablet_bridge_log.txt.",
    )
    return p.parse_args()


def _run_test_mode(emitter: VMultiPenEmitter, *, hz: float, log: Callable[[str], None]) -> None:
    log("TEST mode begin")
    log("Ensure Krita is open with a brush and Windows Ink enabled.")

    period = 1.0 / max(1.0, hz)
    sw, sh = get_screen_size()
    x = map_range(sw // 2, 0, max(1, sw - 1), 0, VMULTI_COORD_MAX)
    y = map_range(sh // 2, 0, max(1, sh - 1), 0, VMULTI_COORD_MAX)

    def run_phase(
        *,
        name: str,
        seconds: float,
        status: int,
        pressure: int,
        report_format: ReportFormat,
        write_method: WriteMode,
    ) -> None:
        log("")
        log(
            f"ACTIVE {name} duration={seconds:.1f}s status=0x{status:02X} "
            f"pressure={pressure} format={report_format} method={write_method}"
        )
        first = True
        phase_error_logged = False
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            try:
                result = emitter.emit_report(
                    status=status,
                    x=x,
                    y=y,
                    pressure=pressure,
                    label=f"test.{name}",
                    report_format=report_format,
                    write_method=write_method,
                )
            except Exception as e:
                if not phase_error_logged:
                    phase_error_logged = True
                    log(f"PHASE {name} write failure: {type(e).__name__}: {e}")
                time.sleep(period)
                continue
            if first:
                first = False
                log(
                    f"FIRST {name}: method={result.method} wrote={result.wrote} "
                    f"win32_error={result.win32_error} bytes={hex_bytes(result.bytes_sent)}"
                )
            time.sleep(period)

    # Requested 5-phase matrix with 1s hover reset between phases.
    run_phase(
        name="phase1_formatA_write_contact",
        seconds=3.0,
        status=0x03,
        pressure=4096,
        report_format="format_a",
        write_method="write",
    )
    run_phase(
        name="reset_after_phase1_hover",
        seconds=1.0,
        status=0x01,
        pressure=0,
        report_format="format_a",
        write_method="write",
    )

    run_phase(
        name="phase2_formatB_write_contact",
        seconds=3.0,
        status=0x03,
        pressure=4096,
        report_format="format_b",
        write_method="write",
    )
    run_phase(
        name="reset_after_phase2_hover",
        seconds=1.0,
        status=0x01,
        pressure=0,
        report_format="format_b",
        write_method="write",
    )

    run_phase(
        name="phase3_report06_write_contact",
        seconds=3.0,
        status=0x03,
        pressure=4096,
        report_format="report06",
        write_method="write",
    )
    run_phase(
        name="reset_after_phase3_hover",
        seconds=1.0,
        status=0x01,
        pressure=0,
        report_format="report06",
        write_method="write",
    )

    run_phase(
        name="phase4_formatA_writefile_contact",
        seconds=3.0,
        status=0x03,
        pressure=4096,
        report_format="format_a",
        write_method="writefile",
    )
    run_phase(
        name="reset_after_phase4_hover",
        seconds=1.0,
        status=0x01,
        pressure=0,
        report_format="format_a",
        write_method="write",
    )

    run_phase(
        name="phase5_formatA_feature_contact",
        seconds=3.0,
        status=0x03,
        pressure=4096,
        report_format="format_a",
        write_method="feature",
    )

    log("TEST mode complete")


def run_tablet_bridge() -> int:
    args = parse_args()
    if platform.system().lower() != "windows":
        print("ERROR: Tablet emitter is Windows-only")
        return 1

    log_file = args.log_file
    if not log_file:
        if args.test:
            log_file = "docs/vmulti_emitter_test.txt"
        elif args.backend == "synthetic":
            log_file = "docs/pressure_pen_bridge.txt"
        else:
            log_file = "docs/tablet_bridge_log.txt"
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    left_cfg = PressureConfig(
        raw_min=args.raw_min,
        raw_max=args.raw_max,
        out_min=0,
        out_max=1023,
        deadzone_low=args.deadzone_low,
        deadzone_high=args.deadzone_high,
        curve=args.curve,
        curve_strength=args.curve_strength,
    )

    with log_path.open("w", encoding="ascii") as fh:

        def log(line: str) -> None:
            print(line)
            fh.write(line + "\n")
            fh.flush()

        if args.backend == "vmulti":
            emitter = VMultiPenEmitter(
                device_path=args.vmulti_path,
                write_mode=args.vmulti_write_mode,
                log=log,
            )
        else:
            emitter = SyntheticPenEmitter(
                log=log,
                contact_threshold=args.contact_threshold,
            )
        session: PressureHidppSession | None = None
        pipe_source: PipePressureSource | None = None

        latest_left_raw = 0
        latest_left_mapped = 0
        last_print_t = 0.0
        frames_decoded = 0
        emits = 0
        start = time.perf_counter()
        period = 1.0 / max(1.0, args.hz)
        next_tick = start

        try:
            emitter.open()

            if args.test:
                if args.backend != "vmulti":
                    log("TEST mode currently supports backend=vmulti only.")
                    return 1
                _run_test_mode(emitter, hz=args.hz, log=log)
                return 0

            if args.source == "direct":
                session = PressureHidppSession(log=log)
                session.open()
                session.enable_pressure_stream(mode=args.mode, mode_arg=args.mode_arg)
                log(
                    f"PIPELINE backend={args.backend} source=direct "
                    f"mode=0x{args.mode:02X} mode_arg=0x{args.mode_arg:02X} "
                    f"curve={left_cfg.curve} strength={left_cfg.curve_strength:.2f} "
                    f"raw_range=[{left_cfg.raw_min},{left_cfg.raw_max}]"
                )
            else:
                pipe_source = PipePressureSource(args.pipe_name, log=log)
                pipe_source.open()
                log(f"PIPELINE backend={args.backend} source=pipe")

            while True:
                now = time.perf_counter()
                if args.duration is not None and (now - start) >= args.duration:
                    log("Duration reached")
                    break

                decoded_samples: list[tuple[int, int]] = []
                if session is not None:
                    # Drain nonblocking queue; keep all decoded samples for synthetic backend.
                    while True:
                        if session.dev is None:
                            break
                        try:
                            data = session.dev.read(64)
                        except OSError as e:
                            log(f"RX read_error={e}")
                            break
                        if not data:
                            break
                        if (
                            len(data) >= 20
                            and data[0] == 0x11
                            and data[1] == 0x01
                            and data[2] == 0x0C
                            and data[3] == 0x10
                        ):
                            left_raw = int(data[4])
                            left_norm = normalize_raw_pressure(left_raw, left_cfg.raw_min, left_cfg.raw_max)
                            left_mapped = map_normalized_pressure(left_norm, left_cfg)
                            decoded_samples.append((left_raw, left_mapped))
                            latest_left_mapped = left_mapped
                            latest_left_raw = left_raw
                            frames_decoded += 1
                elif pipe_source is not None:
                    v = pipe_source.poll_latest_left_mapped()
                    if v is not None:
                        latest_left_mapped = clamp_i(v, 0, 1023)

                emission: TabletEmission | None = None
                left_down = is_left_button_down()
                if args.backend == "synthetic":
                    if decoded_samples:
                        for raw, mapped in decoded_samples:
                            latest_left_raw = raw
                            latest_left_mapped = mapped
                            left_down = is_left_button_down()
                            emission = emitter.emit_from_state(
                                left_mapped_1023=latest_left_mapped,
                                left_down=left_down,
                                sample_fresh=True,
                                report_format=args.report_format,
                                write_method=args.vmulti_write_mode,
                            )
                            emits += 1
                    else:
                        # Still run state machine each tick so LMB-up releases immediately.
                        left_down = is_left_button_down()
                        emission = emitter.emit_from_state(
                            left_mapped_1023=latest_left_mapped,
                            left_down=left_down,
                            sample_fresh=False,
                            report_format=args.report_format,
                            write_method=args.vmulti_write_mode,
                        )
                        emits += 1
                else:
                    emission = emitter.emit_from_state(
                        left_mapped_1023=latest_left_mapped,
                        left_down=left_down,
                        report_format=args.report_format,
                        write_method=args.vmulti_write_mode,
                    )
                    emits += 1

                if now - last_print_t >= 0.25:
                    last_print_t = now
                    pressure_label = "p8191"
                    if args.backend == "synthetic":
                        pressure_label = "p1024"
                    if emission is None:
                        emission = TabletEmission(
                            x=0,
                            y=0,
                            pressure_8191=0,
                            status=0,
                            left_mapped_1023=latest_left_mapped,
                        )
                    log(
                        f"[{now-start:8.3f}s] left_raw={latest_left_raw:3d} "
                        f"left_mapped={latest_left_mapped:4d} left_down={int(left_down)} "
                        f"status=0x{emission.status:02X} {pressure_label}={emission.pressure_8191:4d} "
                        f"x={emission.x:5d} y={emission.y:5d}"
                    )

                next_tick += period
                sleep_s = next_tick - time.perf_counter()
                if sleep_s > 0:
                    time.sleep(sleep_s)
                else:
                    next_tick = time.perf_counter()

        except KeyboardInterrupt:
            log("Interrupted")
        except Exception as e:
            log(f"ERROR {type(e).__name__}: {e}")
            return 1
        finally:
            try:
                emitter.send_out_of_range(
                    report_format=args.report_format,
                    write_method=args.vmulti_write_mode,
                )
            except Exception as e:
                log(f"Final out-of-range send failed: {e}")

            if pipe_source is not None:
                pipe_source.close()
            if session is not None:
                session.close()
            emitter.close()

        elapsed = max(1e-9, time.perf_counter() - start)
        log("")
        log("SUMMARY")
        log(f"emits={emits} emit_rate={emits/elapsed:.2f}Hz")
        log(f"decoded_pressure_frames={frames_decoded}")
        log(f"log_file={log_path}")

    return 0


if __name__ == "__main__":
    sys.exit(run_tablet_bridge())
