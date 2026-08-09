"""Shared HID++ pressure stream helpers for wired and wireless Superstrike."""

from __future__ import annotations

import atexit
import signal
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import hid

VID = 0x046D
PID = 0xC54D
WIRELESS_PID = PID
WIRED_PID = 0xC0A8
SUPPORTED_PIDS = {WIRELESS_PID, WIRED_PID}
IFACE_NUMBER = 2
USAGE_PAGE_VENDOR = 0xFF00
USAGE_COL02 = 0x0002

REPORT_SHORT = 0x10
REPORT_LONG = 0x11
DEVICE_INDEX = 0x01
WIRED_DEVICE_INDEX = 0xFF
PRESSURE_FEATURE_INDEX = 0x0C
ANALOG_BUTTONS_FEATURE_ID = 0x1B0C
RAW_ADC_MONITORING_FLAG = 0x02
PRESSURE_LEASE_SECONDS = 8
PRESSURE_LEASE_RENEW_INTERVAL_S = 2.0
PRESSURE_RESTORE_LEASE_SECONDS = 60
PRESSURE_FUNCTION_SWID = 0x3C
PRESSURE_NOTIFICATION_SWID = 0x00
PRESSURE_MODE3_ADDR = 0x10
PRESSURE_MODE3_LEFT_PAYLOAD_INDEX = 0
PRESSURE_MODE3_RIGHT_PAYLOAD_INDEX = 2
MOUSE_BUTTON_SPY_INDEX = 0x0F
HIDPP_SW_ID = 0x08
DEVICE_CONFIG_SW_ID = 0x0F
EXTENDED_DPI_FEATURE_INDEX = 0x09
ONBOARD_PROFILES_FEATURE_ID = 0x8100
CONFIG_WRAPPER_FEATURE_INDEX = 0x0F

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


def _build_long_report(
    sub_id: int,
    address: int,
    payload: list[int],
    *,
    device_index: int = DEVICE_INDEX,
) -> list[int]:
    body = list(payload[:16])
    body.extend([0] * (16 - len(body)))
    return [REPORT_LONG, device_index & 0xFF, sub_id, address] + body


def _function_to_address(function_id: int, sw_id: int = HIDPP_SW_ID) -> int:
    return ((function_id & 0x0F) << 4) | (sw_id & 0x0F)


def _short_to_long(short_report: list[int]) -> list[int]:
    if len(short_report) < 4:
        raise ValueError("Short report must contain at least [report, dev, sub, addr]")
    return _build_long_report(
        sub_id=short_report[2],
        address=short_report[3],
        payload=short_report[4:],
        device_index=short_report[1],
    )


def build_monitoring_lease_report(
    *,
    feature_index: int,
    flags: int,
    lease_seconds: int,
    device_index: int = DEVICE_INDEX,
) -> list[int]:
    """Build the HID++ 0x1B0C function-3 monitoring lease request."""
    return _build_long_report(
        sub_id=feature_index,
        address=_function_to_address(3),
        payload=[flags & 0x03, lease_seconds & 0xFF],
        device_index=device_index,
    )


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


def parse_pressure_notification(
    data: list[int],
    timestamp_s: float,
    *,
    device_index: int | None = None,
) -> PressureReport | None:
    if len(data) < 20:
        return None
    if (
        data[0] != REPORT_LONG
        or (device_index is not None and data[1] != device_index)
        or (device_index is None and data[1] not in {DEVICE_INDEX, WIRED_DEVICE_INDEX})
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


def parse_feature_0c_frame(
    data: list[int],
    timestamp_s: float,
    *,
    feature_index: int = PRESSURE_FEATURE_INDEX,
    device_index: int | None = None,
) -> Feature0CFrame | None:
    if len(data) < 20:
        return None
    index_matches = (
        data[1] == device_index
        if device_index is not None
        else data[1] in {DEVICE_INDEX, WIRED_DEVICE_INDEX}
    )
    if data[0] != REPORT_LONG or not index_matches or data[2] != feature_index:
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
    """Return the full 10-bit left ADC code from event 1 channel 0."""
    if frame.addr != PRESSURE_MODE3_ADDR:
        return None
    if len(frame.payload) < PRESSURE_MODE3_LEFT_PAYLOAD_INDEX + 2:
        return None
    offset = PRESSURE_MODE3_LEFT_PAYLOAD_INDEX
    raw_u16 = int.from_bytes(bytes(frame.payload[offset : offset + 2]), byteorder="big")
    return raw_u16 >> 6


def extract_mode3_right_pressure_raw(frame: Feature0CFrame) -> int | None:
    """Return the full 10-bit right ADC code from event 1 channel 1."""
    if frame.addr != PRESSURE_MODE3_ADDR:
        return None
    if len(frame.payload) < PRESSURE_MODE3_RIGHT_PAYLOAD_INDEX + 2:
        return None
    offset = PRESSURE_MODE3_RIGHT_PAYLOAD_INDEX
    raw_u16 = int.from_bytes(bytes(frame.payload[offset : offset + 2]), byteorder="big")
    return raw_u16 >> 6


def extract_mode3_lr_pressure_raw(frame: Feature0CFrame) -> tuple[int | None, int | None]:
    """Return (left_raw, right_raw) for mode-3 frames."""
    left = extract_mode3_left_pressure_raw(frame)
    right = extract_mode3_right_pressure_raw(frame)
    # The physical ADC channels rest around 300+, so a decoded zero is not a
    # real button position. Short lease/monitor transitions can emit an empty
    # event-1 payload; do not turn that transport artifact into pen release.
    if left == 0:
        left = None
    if right == 0:
        right = None
    return left, right


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
        self.device_index = DEVICE_INDEX
        self.transport = "wireless"
        self._candidates: list[dict] = []
        self._candidate_index = 0
        self._cleanup_done = False
        self._atexit_registered = False
        self._signal_handlers: dict[signal.Signals, object] = {}
        self.pressure_feature_index = PRESSURE_FEATURE_INDEX
        self.onboard_profiles_feature_index: int | None = None
        self._previous_monitoring_flags: int | None = None
        self._active_monitoring_flags: int | None = None
        self.lease_renew_interval_s = PRESSURE_LEASE_RENEW_INTERVAL_S
        self._next_lease_renewal = 0.0

    def discover_col02_candidates(self) -> list[dict]:
        candidates: list[dict] = []
        for device in hid.enumerate(VID, 0):
            product = str(device.get("product_string") or "")
            product_id = int(device.get("product_id") or 0)
            if (
                device.get("vendor_id") != VID
                or device.get("interface_number") != IFACE_NUMBER
                or device.get("usage_page") != USAGE_PAGE_VENDOR
                or device.get("usage") != USAGE_COL02
                or (
                    product_id not in SUPPORTED_PIDS
                    and "SUPERSTRIKE" not in product.upper()
                )
            ):
                continue
            wired = product_id == WIRED_PID or "SUPERSTRIKE" in product.upper()
            candidates.append(
                {
                    "path": device["path"],
                    "product_id": product_id,
                    "product": product,
                    "serial": str(device.get("serial_number") or ""),
                    "transport": "wired" if wired else "wireless",
                    "device_index": WIRED_DEVICE_INDEX if wired else DEVICE_INDEX,
                }
            )
        # Prefer the direct device whenever the cable is connected. A receiver
        # can remain enumerated while its paired mouse has switched to USB and
        # will accept writes without returning any HID++ responses.
        candidates.sort(key=lambda item: item["transport"] != "wired")
        return candidates

    def discover_col02_path(self) -> bytes | None:
        candidates = self.discover_col02_candidates()
        return candidates[0]["path"] if candidates else None

    def _open_candidate(self, index: int) -> None:
        candidate = self._candidates[index]
        self._candidate_index = index
        self.path_col02 = candidate["path"]
        self.device_index = int(candidate["device_index"])
        self.transport = str(candidate["transport"])
        self.dev = hid.device()
        self.dev.open_path(self.path_col02)
        self.dev.set_nonblocking(True)
        self.log(
            f"OPEN {self.transport} Col02 pid=0x{candidate['product_id']:04X} "
            f"serial={candidate['serial'] or '-'} path={self.path_col02!r}"
        )

    def _advance_candidate(self) -> bool:
        next_index = self._candidate_index + 1
        if next_index >= len(self._candidates):
            return False
        if self.dev is not None:
            try:
                self.dev.close()
            except OSError:
                pass
        self.dev = None
        self.pressure_feature_index = PRESSURE_FEATURE_INDEX
        self.onboard_profiles_feature_index = None
        self._previous_monitoring_flags = None
        self._active_monitoring_flags = None
        self._next_lease_renewal = 0.0
        self._open_candidate(next_index)
        return True

    def open(self) -> None:
        self._candidates = self.discover_col02_candidates()
        if not self._candidates:
            raise RuntimeError(
                "No wired or wireless Superstrike HID++ command interface was found"
            )
        self._open_candidate(0)
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
        self.maintain_pressure_stream()
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

    def request_long(
        self,
        *,
        feature_index: int,
        address: int,
        payload: list[int],
        label: str,
        timeout_s: float = 0.12,
    ) -> list[int]:
        """Send one HID++ long request and return its matching payload."""
        if self.dev is None:
            raise RuntimeError("Device not open")

        report = _build_long_report(
            feature_index,
            address,
            payload,
            device_index=self.device_index,
        )
        wrote = self.dev.write(report)
        self.log(f"TX {label} write()={wrote} {hex_bytes(report)}")
        if wrote is None or wrote <= 0:
            raise OSError(f"{label} was not accepted by the receiver")

        end = time.perf_counter() + timeout_s
        while time.perf_counter() < end:
            data = self.dev.read(64)
            if not data:
                time.sleep(0.001)
                continue
            row = list(data)
            if (
                len(row) >= 6
                and row[0] == REPORT_LONG
                and row[1] == self.device_index
                and row[2] == 0xFF
                and row[3] == feature_index
                and row[4] == address
            ):
                self.log(f"RX {label} ERROR code=0x{row[5]:02X} {hex_bytes(row)}")
                raise RuntimeError(f"{label} failed with HID++ error 0x{row[5]:02X}")
            if (
                len(row) >= 4
                and row[0] == REPORT_LONG
                and row[1] == self.device_index
                and row[2] == feature_index
                and row[3] == address
            ):
                self.log(f"RX {label} len={len(row)} {hex_bytes(row)}")
                return row[4:20]

        raise TimeoutError(f"Timed out waiting for {label} response")

    def _read_button_settings(self) -> dict[int, list[int]]:
        """Read both HITS button records used by haptics and calibration."""
        current: dict[int, list[int]] = {}
        for button in (0, 1):
            values = self.request_long(
                feature_index=self.pressure_feature_index,
                address=(2 << 4) | DEVICE_CONFIG_SW_ID,
                payload=[button],
                label=f"HAPTIC.read[{button}]",
            )
            if len(values) < 4 or values[0] != button:
                raise RuntimeError(f"Unexpected HITS settings response for button {button}")
            current[button] = values
        return current

    def get_haptic_levels(self) -> tuple[int, int]:
        """Read the current 0..5 haptic level for the left and right buttons."""
        current = self._read_button_settings()
        levels = tuple(
            max(0, min(5, round(current[button][3] / 4.0)))
            for button in (0, 1)
        )
        return int(levels[0]), int(levels[1])

    def set_haptic_levels(self, *, left: int, right: int) -> tuple[int, int]:
        """Set click haptics 0..5 while preserving actuation/rapid-trigger."""
        if not 0 <= left <= 5 or not 0 <= right <= 5:
            raise ValueError("Haptic levels must be in 0..5")

        current = self._read_button_settings()

        self.request_long(
            feature_index=CONFIG_WRAPPER_FEATURE_INDEX,
            address=0x00,
            payload=[0x00, 0x01, 0x00],
            label="HAPTIC.unlock",
        )
        try:
            for button, level in ((0, left), (1, right)):
                values = current[button]
                self.request_long(
                    feature_index=self.pressure_feature_index,
                    address=(1 << 4) | DEVICE_CONFIG_SW_ID,
                    payload=[button, values[1], values[2], level * 4],
                    label=f"HAPTIC.write[{button}]={level}",
                )
        finally:
            self.request_long(
                feature_index=CONFIG_WRAPPER_FEATURE_INDEX,
                address=0x00,
                payload=[0x00, 0x00, 0x00],
                label="HAPTIC.commit_lock",
            )
        return left, right

    @staticmethod
    def _dpi_from_settings(values: list[int]) -> int:
        if len(values) < 5:
            raise RuntimeError("Extended DPI response was too short")
        dpi = int.from_bytes(bytes(values[1:3]), byteorder="big")
        if dpi == 0:
            dpi = int.from_bytes(bytes(values[3:5]), byteorder="big")
        if dpi <= 0:
            raise RuntimeError("Extended DPI response did not contain a valid resolution")
        return dpi

    def get_dpi(self) -> int:
        """Read the current X-axis DPI from feature 0x2202."""
        current = self.request_long(
            feature_index=EXTENDED_DPI_FEATURE_INDEX,
            address=(5 << 4) | DEVICE_CONFIG_SW_ID,
            payload=[0x00],
            label="DPI.read",
        )
        return self._dpi_from_settings(current)

    def set_dpi(self, dpi: int) -> int:
        """Set equal X/Y DPI using feature 0x2202 (index 0x09)."""
        if not 100 <= dpi <= 32000 or dpi % 50 != 0:
            raise ValueError("DPI must be 100..32000 in 50-DPI increments")

        current = self.request_long(
            feature_index=EXTENDED_DPI_FEATURE_INDEX,
            address=(5 << 4) | DEVICE_CONFIG_SW_ID,
            payload=[0x00],
            label="DPI.read",
        )
        if len(current) < 10:
            raise RuntimeError("Extended DPI response was too short")
        lod = current[9]
        high, low = divmod(dpi, 256)
        current_y = int.from_bytes(bytes(current[5:7]), byteorder="big")
        y_high, y_low = (high, low) if current_y > 0 else (0, 0)
        self.request_long(
            feature_index=EXTENDED_DPI_FEATURE_INDEX,
            address=(6 << 4) | DEVICE_CONFIG_SW_ID,
            payload=[0x00, high, low, y_high, y_low, lod],
            label=f"DPI.write={dpi}",
        )

        verified = self.request_long(
            feature_index=EXTENDED_DPI_FEATURE_INDEX,
            address=(5 << 4) | DEVICE_CONFIG_SW_ID,
            payload=[0x00],
            label="DPI.verify",
        )
        actual = self._dpi_from_settings(verified)
        if actual != dpi:
            raise RuntimeError(
                f"DPI remained at {actual}; disable the mouse onboard profile or close G HUB"
            )
        return actual

    def discover_feature_index(self, feature_id: int, *, label: str) -> int:
        """Resolve one HID++ 2.0 feature ID to its current runtime index."""
        payload = self.request_long(
            feature_index=0x00,
            address=_function_to_address(0),
            payload=[(feature_id >> 8) & 0xFF, feature_id & 0xFF],
            label=label,
        )
        if not payload or payload[0] == 0:
            raise RuntimeError(f"Device does not expose HID++ feature 0x{feature_id:04X}")
        return int(payload[0])

    def get_onboard_profile_state(self) -> tuple[bool, int | None]:
        """Return whether onboard profiles own settings and the active sector."""
        if self.onboard_profiles_feature_index is None:
            self.onboard_profiles_feature_index = self.discover_feature_index(
                ONBOARD_PROFILES_FEATURE_ID,
                label="PROFILE.discover.0x8100",
            )
        feature_index = self.onboard_profiles_feature_index
        mode = self.request_long(
            feature_index=feature_index,
            address=_function_to_address(2, DEVICE_CONFIG_SW_ID),
            payload=[],
            label="PROFILE.mode.read",
        )
        enabled = bool(mode and mode[0] == 0x01)
        if not enabled:
            return False, None
        active = self.request_long(
            feature_index=feature_index,
            address=_function_to_address(4, DEVICE_CONFIG_SW_ID),
            payload=[],
            label="PROFILE.active.read",
        )
        if len(active) < 2:
            raise RuntimeError("Onboard profile response was too short")
        return True, int.from_bytes(bytes(active[:2]), byteorder="big")

    def set_onboard_profile_state(
        self,
        *,
        enabled: bool,
        active_sector: int | None = None,
    ) -> tuple[bool, int | None]:
        """Switch between host mode and the saved onboard profile."""
        if self.onboard_profiles_feature_index is None:
            self.onboard_profiles_feature_index = self.discover_feature_index(
                ONBOARD_PROFILES_FEATURE_ID,
                label="PROFILE.discover.0x8100",
            )
        feature_index = self.onboard_profiles_feature_index
        self.request_long(
            feature_index=feature_index,
            address=_function_to_address(1, DEVICE_CONFIG_SW_ID),
            payload=[0x01 if enabled else 0x02],
            label="PROFILE.onboard_mode" if enabled else "PROFILE.host_mode",
        )
        if enabled and active_sector is not None:
            if not 0 <= int(active_sector) <= 0xFFFF:
                raise ValueError("Onboard profile sector must fit in 16 bits")
            high, low = divmod(int(active_sector), 256)
            self.request_long(
                feature_index=feature_index,
                address=_function_to_address(3, DEVICE_CONFIG_SW_ID),
                payload=[high, low],
                label=f"PROFILE.active={int(active_sector)}",
            )
        return bool(enabled), int(active_sector) if enabled and active_sector is not None else None

    def discover_pressure_feature_index(self) -> int:
        """Resolve HID++ feature 0x1B0C instead of assuming index 0x0C."""
        self.pressure_feature_index = self.discover_feature_index(
            ANALOG_BUTTONS_FEATURE_ID,
            label="PRESSURE.discover.0x1B0C",
        )
        self.log(f"PRESSURE feature 0x1B0C index=0x{self.pressure_feature_index:02X}")
        return self.pressure_feature_index

    def enable_pressure_stream(self, mode: int = 0x01, mode_arg: int = 0x00) -> None:
        """Acquire a short event-1 lease while preserving existing monitor flags."""
        del mode, mode_arg  # Retained in the public signature for CLI compatibility.
        while True:
            try:
                self.read_for(0.05)
                feature_index = self.discover_pressure_feature_index()
                current = self.request_long(
                    feature_index=feature_index,
                    address=_function_to_address(4),
                    payload=[],
                    label="PRESSURE.flags.read",
                )
                if not current:
                    raise RuntimeError(
                        "HID++ 0x1B0C function 4 returned no monitoring flags"
                    )
                self._previous_monitoring_flags = current[0] & 0x03
                self._active_monitoring_flags = (
                    self._previous_monitoring_flags | RAW_ADC_MONITORING_FLAG
                )
                self.request_long(
                    feature_index=feature_index,
                    address=_function_to_address(3),
                    payload=[self._active_monitoring_flags, PRESSURE_LEASE_SECONDS],
                    label="PRESSURE.lease.acquire",
                )
                self._next_lease_renewal = (
                    time.perf_counter() + self.lease_renew_interval_s
                )
                return
            except (TimeoutError, RuntimeError, OSError) as exc:
                if not self._advance_candidate():
                    raise
                self.log(
                    f"HID++ probe failed ({type(exc).__name__}); trying next "
                    f"Superstrike interface"
                )

    def maintain_pressure_stream(self) -> bool:
        """Renew the monitoring lease when due; return whether a write occurred."""
        if self._active_monitoring_flags is None:
            return False
        if time.perf_counter() < self._next_lease_renewal:
            return False
        self.refresh_pressure_stream()
        return True

    def refresh_pressure_stream(self, mode: int = 0x01, mode_arg: int = 0x00) -> None:
        """Renew the active stream without consuming pressure reports.

        The Superstrike receiver rejects the short form of this command and
        accepts the 20-byte long report. Recovery used to spend roughly 0.4s
        on the rejected short form and synchronous read windows. A refresh is
        intentionally write-only: the reader loop will consume the echo and
        the resumed pressure frames normally.
        """
        if self.dev is None:
            raise RuntimeError("Device not open")

        del mode, mode_arg
        if self._active_monitoring_flags is None:
            raise RuntimeError("Pressure stream has not been enabled")
        report = build_monitoring_lease_report(
            feature_index=self.pressure_feature_index,
            flags=self._active_monitoring_flags,
            lease_seconds=PRESSURE_LEASE_SECONDS,
            device_index=self.device_index,
        )
        label = "PRESSURE.lease.renew"
        wrote: int | None = None
        try:
            wrote = self.dev.write(report)
            self.log(f"TX {label} write()={wrote} {hex_bytes(report)}")
        except OSError as exc:
            self.log(f"TX {label} write_error={exc} {hex_bytes(report)}")

        if (wrote is None or wrote <= 0) and hasattr(self.dev, "send_feature_report"):
            try:
                wrote = self.dev.send_feature_report(report)
                self.log(f"TX {label} send_feature_report()={wrote}")
            except OSError as exc:
                self.log(f"TX {label} send_feature_report_error={exc}")

        if wrote is None or wrote <= 0:
            raise OSError("Pressure stream refresh was not accepted by the receiver")
        self._next_lease_renewal = time.perf_counter() + self.lease_renew_interval_s

    def disable_pressure_stream(self) -> None:
        if self.dev is None:
            return
        if self._active_monitoring_flags is None:
            return
        if self._previous_monitoring_flags is not None:
            self.request_long(
                feature_index=self.pressure_feature_index,
                address=_function_to_address(3),
                payload=[self._previous_monitoring_flags, PRESSURE_RESTORE_LEASE_SECONDS],
                label="CLEANUP.PRESSURE.flags.restore",
            )
            self._active_monitoring_flags = None
            self._previous_monitoring_flags = None
            self._next_lease_renewal = 0.0
            return
        for i, cmd in enumerate(DISABLE_PRESSURE_STREAM_CANDIDATES, start=1):
            transport_cmd = list(cmd)
            transport_cmd[1] = self.device_index
            self.write_report(
                transport_cmd,
                label=f"CLEANUP.PRESSURE.disable[{i}].short",
                read_window_s=0.1,
            )
            self.write_report(
                _short_to_long(transport_cmd),
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
                device_index=self.device_index,
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
