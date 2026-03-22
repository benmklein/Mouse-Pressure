"""Shared HID++ pressure stream helpers for Superstrike over Lightspeed."""

from __future__ import annotations

import atexit
import signal
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import hid

VID = 0x046D
PID = 0xC54D
IFACE_NUMBER = 2
USAGE_PAGE_VENDOR = 0xFF00
USAGE_COL02 = 0x0002

REPORT_SHORT = 0x10
REPORT_LONG = 0x11
DEVICE_INDEX = 0x01
PRESSURE_FEATURE_INDEX = 0x0C
PRESSURE_FUNCTION_SWID = 0x3C
PRESSURE_NOTIFICATION_SWID = 0x00
PRESSURE_MODE3_ADDR = 0x10
PRESSURE_MODE3_LEFT_PAYLOAD_INDEX = 0   # addr 0x10 byte4
PRESSURE_MODE3_RIGHT_PAYLOAD_INDEX = 2  # addr 0x10 byte6
MOUSE_BUTTON_SPY_INDEX = 0x0F
HIDPP_SW_ID = 0x08

DISABLE_PRESSURE_STREAM_CANDIDATES = [
    [
        REPORT_SHORT,
        DEVICE_INDEX,
        PRESSURE_FEATURE_INDEX,
        PRESSURE_FUNCTION_SWID,
        0x00,
        PRESSURE_FUNCTION_SWID,
        0x00,
    ],
    [
        REPORT_SHORT,
        DEVICE_INDEX,
        PRESSURE_FEATURE_INDEX,
        PRESSURE_FUNCTION_SWID,
        0x00,
        0x00,
        0x00,
    ],
]


def hex_bytes(data: Iterable[int]) -> str:
    return " ".join(f"{b:02X}" for b in data)


def _build_long_report(sub_id: int, address: int, payload: list[int]) -> list[int]:
    body = list(payload[:16])
    body.extend([0] * (16 - len(body)))
    return [REPORT_LONG, DEVICE_INDEX, sub_id, address] + body


def _function_to_address(function_id: int, sw_id: int = HIDPP_SW_ID) -> int:
    return ((function_id & 0x0F) << 4) | (sw_id & 0x0F)


def _short_to_long(short_report: list[int]) -> list[int]:
    if len(short_report) < 4:
        raise ValueError("Short report must contain at least [report, dev, sub, addr]")
    return _build_long_report(
        sub_id=short_report[2],
        address=short_report[3],
        payload=short_report[4:],
    )


def build_enable_pressure_stream(mode: int = 0x01, mode_arg: int = 0x00) -> list[int]:
    return [
        REPORT_SHORT,
        DEVICE_INDEX,
        PRESSURE_FEATURE_INDEX,
        PRESSURE_FUNCTION_SWID,
        mode & 0xFF,
        PRESSURE_FUNCTION_SWID,
        mode_arg & 0xFF,
    ]


@dataclass(frozen=True)
class PressureReport:
    timestamp_s: float
    raw: list[int]
    pressure: int
    extra_payload: list[int]


@dataclass(frozen=True)
class Feature0CFrame:
    timestamp_s: float
    raw: list[int]
    addr: int
    payload: list[int]


def parse_pressure_notification(data: list[int], timestamp_s: float) -> PressureReport | None:
    if len(data) < 20:
        return None
    if (
        data[0] != REPORT_LONG
        or data[1] != DEVICE_INDEX
        or data[2] != PRESSURE_FEATURE_INDEX
        or data[3] != PRESSURE_NOTIFICATION_SWID
    ):
        return None
    return PressureReport(
        timestamp_s=timestamp_s,
        raw=list(data),
        pressure=data[4],
        extra_payload=list(data[5:20]),
    )


def parse_feature_0c_frame(data: list[int], timestamp_s: float) -> Feature0CFrame | None:
    if len(data) < 20:
        return None
    if data[0] != REPORT_LONG or data[1] != DEVICE_INDEX or data[2] != PRESSURE_FEATURE_INDEX:
        return None
    return Feature0CFrame(
        timestamp_s=timestamp_s,
        raw=list(data),
        addr=data[3],
        payload=list(data[4:20]),
    )


def extract_mode3_primary_pressure_raw(frame: Feature0CFrame) -> int | None:
    """Return mode-3 primary raw pressure (addr 0x10 byte4) when present."""
    return extract_mode3_left_pressure_raw(frame)


def extract_mode3_left_pressure_raw(frame: Feature0CFrame) -> int | None:
    """Return mode-3 LEFT raw pressure (addr 0x10 byte4)."""
    if frame.addr != PRESSURE_MODE3_ADDR:
        return None
    if len(frame.payload) <= PRESSURE_MODE3_LEFT_PAYLOAD_INDEX:
        return None
    return frame.payload[PRESSURE_MODE3_LEFT_PAYLOAD_INDEX]


def extract_mode3_right_pressure_raw(frame: Feature0CFrame) -> int | None:
    """Return mode-3 RIGHT raw pressure (addr 0x10 byte6)."""
    if frame.addr != PRESSURE_MODE3_ADDR:
        return None
    if len(frame.payload) <= PRESSURE_MODE3_RIGHT_PAYLOAD_INDEX:
        return None
    return frame.payload[PRESSURE_MODE3_RIGHT_PAYLOAD_INDEX]


def extract_mode3_lr_pressure_raw(frame: Feature0CFrame) -> tuple[int | None, int | None]:
    """Return (left_raw, right_raw) for mode-3 frames."""
    return (
        extract_mode3_left_pressure_raw(frame),
        extract_mode3_right_pressure_raw(frame),
    )


def normalize_raw_pressure(raw: int, raw_min: int, raw_max: int) -> float:
    if raw_max <= raw_min:
        return 0.0
    t = (raw - raw_min) / float(raw_max - raw_min)
    if t < 0.0:
        return 0.0
    if t > 1.0:
        return 1.0
    return t


class PressureHidppSession:
    """HID++ session with mandatory cleanup for pressure stream enablement."""

    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log
        self.dev: hid.device | None = None
        self.path_col02: bytes | None = None
        self._cleanup_done = False
        self._atexit_registered = False
        self._signal_handlers: dict[signal.Signals, object] = {}

    def discover_col02_path(self) -> bytes | None:
        for d in hid.enumerate():
            if (
                d["vendor_id"] == VID
                and d["product_id"] == PID
                and d.get("interface_number") == IFACE_NUMBER
                and d.get("usage_page") == USAGE_PAGE_VENDOR
                and d.get("usage") == USAGE_COL02
            ):
                return d["path"]
        return None

    def open(self) -> None:
        self.path_col02 = self.discover_col02_path()
        if not self.path_col02:
            raise RuntimeError(
                "Superstrike receiver MI_02 Col02 not found (VID:PID 046D:C54D usage 0x0002)"
            )

        self.dev = hid.device()
        self.dev.open_path(self.path_col02)
        self.dev.set_nonblocking(True)
        self.log(f"OPEN Col02 path={self.path_col02!r}")
        self._register_cleanup_hooks()

    def close(self) -> None:
        try:
            self.cleanup()
        finally:
            if self.dev is not None:
                try:
                    self.dev.close()
                except OSError:
                    pass
            self._unregister_cleanup_hooks()

    def _register_cleanup_hooks(self) -> None:
        if not self._atexit_registered:
            atexit.register(self.cleanup)
            self._atexit_registered = True
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._signal_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._signal_handler)
            except Exception:
                pass

    def _unregister_cleanup_hooks(self) -> None:
        if self._atexit_registered:
            try:
                atexit.unregister(self.cleanup)
            except Exception:
                pass
            self._atexit_registered = False
        for sig, handler in self._signal_handlers.items():
            try:
                signal.signal(sig, handler)
            except Exception:
                pass
        self._signal_handlers.clear()

    def _signal_handler(self, signum: int, _frame: object) -> None:
        self.log(f"Signal {signum} received, running cleanup")
        self.cleanup()
        raise KeyboardInterrupt

    def read_for(self, seconds: float) -> list[list[int]]:
        if self.dev is None:
            return []
        end = time.perf_counter() + seconds
        out: list[list[int]] = []
        while time.perf_counter() < end:
            try:
                data = self.dev.read(64)
            except OSError as e:
                self.log(f"RX read_error={e}")
                break
            if data:
                out.append(data)
            else:
                time.sleep(0.001)
        return out

    def read_next(self, timeout_s: float = 0.1) -> tuple[float, list[int]] | None:
        if self.dev is None:
            return None
        end = time.perf_counter() + timeout_s
        while time.perf_counter() < end:
            try:
                data = self.dev.read(64)
            except OSError as e:
                self.log(f"RX read_error={e}")
                return None
            if data:
                return time.perf_counter(), list(data)
            time.sleep(0.001)
        return None

    def write_report(
        self,
        report: list[int],
        *,
        label: str,
        read_window_s: float = 0.15,
    ) -> list[list[int]]:
        if self.dev is None:
            raise RuntimeError("Device not open")
        wrote = None
        try:
            wrote = self.dev.write(report)
            self.log(f"TX {label} write()={wrote} {hex_bytes(report)}")
        except OSError as e:
            self.log(f"TX {label} write_error={e} {hex_bytes(report)}")

        if (wrote is None or wrote <= 0) and hasattr(self.dev, "send_feature_report"):
            try:
                feature_wrote = self.dev.send_feature_report(report)
                self.log(f"TX {label} send_feature_report()={feature_wrote}")
            except OSError as e:
                self.log(f"TX {label} send_feature_report_error={e}")

        rows = self.read_for(read_window_s)
        for b in rows:
            self.log(f"RX {label} len={len(b)} {hex_bytes(b)}")
        return rows

    def enable_pressure_stream(self, mode: int = 0x01, mode_arg: int = 0x00) -> None:
        enable_cmd = build_enable_pressure_stream(mode=mode, mode_arg=mode_arg)
        self.read_for(0.05)  # Drain stale packets first.
        self.write_report(
            enable_cmd,
            label=f"PRESSURE.enable.short(mode=0x{mode:02X},arg=0x{mode_arg:02X})",
            read_window_s=0.2,
        )
        self.write_report(
            _short_to_long(enable_cmd),
            label=f"PRESSURE.enable.long_fallback(mode=0x{mode:02X},arg=0x{mode_arg:02X})",
            read_window_s=0.2,
        )

    def disable_pressure_stream(self) -> None:
        if self.dev is None:
            return
        for i, cmd in enumerate(DISABLE_PRESSURE_STREAM_CANDIDATES, start=1):
            self.write_report(
                cmd,
                label=f"CLEANUP.PRESSURE.disable[{i}].short",
                read_window_s=0.1,
            )
            self.write_report(
                _short_to_long(cmd),
                label=f"CLEANUP.PRESSURE.disable[{i}].long_fallback",
                read_window_s=0.1,
            )

    def disable_mouse_button_spy(self) -> None:
        if self.dev is None:
            return

        def tx(function_id: int, payload: list[int], label: str) -> None:
            pkt = _build_long_report(
                sub_id=MOUSE_BUTTON_SPY_INDEX,
                address=_function_to_address(function_id),
                payload=payload,
            )
            self.write_report(pkt, label=label, read_window_s=0.08)

        tx(2, [0x00] + [0x00] * 15, "CLEANUP.MouseButtonSpy.func2.disable")
        tx(1, [0x00] + [0x00] * 15, "CLEANUP.MouseButtonSpy.func1.disable")
        tx(0, [0x00, 0x00, 0x00], "CLEANUP.MouseButtonSpy.func0.status")

    def cleanup(self) -> None:
        if self._cleanup_done:
            return
        self._cleanup_done = True
        self.log("CLEANUP begin")
        try:
            self.disable_pressure_stream()
            self.disable_mouse_button_spy()
        except Exception as e:
            self.log(f"CLEANUP error={type(e).__name__}: {e}")
        self.log("CLEANUP end")
