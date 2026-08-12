"""Compatible-mouse haptic motor write-path test harness.

This script exercises haptic intensity writes discovered from G Hub captures.
All writes are sent on MI_02 Col02 using HID++ long reports and each haptic
write is wrapped with feature 0x0F config unlock/commit packets.
"""

from __future__ import annotations

import atexit
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import hid

VID = 0x046D
PID = 0xC54D
IFACE_NUMBER = 2
USAGE_PAGE_VENDOR = 0xFF00
USAGE_COL02 = 0x0002

REPORT_LONG = 0x11
DEVICE_INDEX = 0x01

FEATURE_CONFIG_WRAPPER = 0x0F
FEATURE_HAPTIC = 0x0C
HAPTIC_FUNCTION1_SWIDF = 0x1F

LEFT_BUTTON = 0x00
LEFT_PARAM1 = 0x04
LEFT_PARAM2 = 0x05

RIGHT_BUTTON = 0x01
RIGHT_PARAM1 = 0x1C
RIGHT_PARAM2 = 0x08

INTENSITY_TO_LEVEL = {
    0x00: 0,
    0x04: 1,
    0x08: 2,
    0x0C: 3,
    0x10: 4,
    0x14: 5,
}
DEFAULT_MAX_INTENSITY = 0x14

TEST1_SEQUENCE = [0x14, 0x10, 0x0C, 0x08, 0x04, 0x00, 0x14]
TEST2_SEQUENCE = [0x02, 0x06, 0x0A, 0x0E, 0x12]
TEST3_SEQUENCE = [0x18, 0x1C, 0x20, 0x28, 0x30, 0x40]

LOG_PATH = Path("docs/haptic_write_test_log.txt")


def hex_bytes(data: Iterable[int]) -> str:
    return " ".join(f"{b:02X}" for b in data)


def build_long_report(sub_id: int, address: int, payload: list[int]) -> list[int]:
    body = list(payload[:16])
    body.extend([0] * (16 - len(body)))
    return [REPORT_LONG, DEVICE_INDEX, sub_id, address] + body


def build_unlock_packet() -> list[int]:
    return build_long_report(FEATURE_CONFIG_WRAPPER, 0x00, [0x00, 0x01, 0x00])


def build_commit_lock_packet() -> list[int]:
    return build_long_report(FEATURE_CONFIG_WRAPPER, 0x00, [0x00, 0x00, 0x00])


def build_haptic_write_packet(button: int, param1: int, param2: int, intensity: int) -> list[int]:
    return build_long_report(
        FEATURE_HAPTIC,
        HAPTIC_FUNCTION1_SWIDF,
        [button & 0xFF, param1 & 0xFF, param2 & 0xFF, intensity & 0xFF],
    )


class HapticTestSession:
    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log
        self.dev: hid.device | None = None
        self.path_col02: bytes | None = None
        self._atexit_registered = False
        self._signal_handlers: dict[signal.Signals, object] = {}
        self._restore_done = False
        self._cleanup_closed = False

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
            raise RuntimeError("Supported receiver MI_02 Col02 not found (046D:C54D Col02)")
        self.dev = hid.device()
        self.dev.open_path(self.path_col02)
        self.dev.set_nonblocking(True)
        self.log(f"OPEN Col02 path={self.path_col02!r}")
        self._register_cleanup_hooks()

    def close(self) -> None:
        if self._cleanup_closed:
            return
        self._cleanup_closed = True
        try:
            if self.dev is not None:
                try:
                    self.dev.close()
                except OSError:
                    pass
        finally:
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
        self.log(f"Signal {signum} received, triggering restore cleanup")
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
                out.append(list(data))
            else:
                time.sleep(0.001)
        return out

    def write_report(self, packet: list[int], *, label: str, read_window_s: float = 0.2) -> list[list[int]]:
        if self.dev is None:
            raise RuntimeError("Device not open")
        wrote = None
        try:
            wrote = self.dev.write(packet)
            self.log(f"TX {label} write()={wrote} {hex_bytes(packet)}")
        except OSError as e:
            self.log(f"TX {label} write_error={e} {hex_bytes(packet)}")
        rows = self.read_for(read_window_s)
        if not rows:
            self.log(f"RX {label} <none>")
        else:
            for b in rows:
                self.log(f"RX {label} len={len(b)} {hex_bytes(b)}")
        return rows

    def has_hidpp_error(self, rows: list[list[int]]) -> bool:
        for b in rows:
            if len(b) >= 6 and b[0] == REPORT_LONG and b[1] == DEVICE_INDEX and b[2] == 0xFF:
                return True
        return False

    def extract_error_code(self, rows: list[list[int]]) -> int | None:
        for b in rows:
            if len(b) >= 6 and b[0] == REPORT_LONG and b[1] == DEVICE_INDEX and b[2] == 0xFF:
                return b[5]
        return None

    def run_wrapped_haptic_write(
        self,
        *,
        button: int,
        param1: int,
        param2: int,
        intensity: int,
        label: str,
    ) -> tuple[bool, str]:
        if self.dev is None:
            raise RuntimeError("Device not open")

        _ = self.read_for(0.04)
        unlock_rows = self.write_report(build_unlock_packet(), label=f"{label}.unlock")
        write_rows = self.write_report(
            build_haptic_write_packet(button, param1, param2, intensity),
            label=f"{label}.haptic_write",
        )
        commit_rows = self.write_report(build_commit_lock_packet(), label=f"{label}.commit_lock")

        all_rows = unlock_rows + write_rows + commit_rows
        is_error = self.has_hidpp_error(all_rows)
        error_code = self.extract_error_code(all_rows)
        if is_error:
            summary = f"ERROR code=0x{(error_code if error_code is not None else 0):02X}"
        else:
            summary = "OK"
        return (not is_error), summary

    def restore_defaults(self) -> None:
        if self._restore_done:
            return
        self._restore_done = True
        if self.dev is None:
            self.log("RESTORE skipped: device not open")
            return

        self.log("RESTORE begin: setting left/right haptic to 0x14")
        self.run_wrapped_haptic_write(
            button=LEFT_BUTTON,
            param1=LEFT_PARAM1,
            param2=LEFT_PARAM2,
            intensity=DEFAULT_MAX_INTENSITY,
            label="RESTORE.left",
        )
        self.run_wrapped_haptic_write(
            button=RIGHT_BUTTON,
            param1=RIGHT_PARAM1,
            param2=RIGHT_PARAM2,
            intensity=DEFAULT_MAX_INTENSITY,
            label="RESTORE.right",
        )
        self.log("RESTORE end")

    def cleanup(self) -> None:
        try:
            self.restore_defaults()
        except Exception as e:
            self.log(f"CLEANUP restore_error={type(e).__name__}: {e}")


def make_logger(path: Path) -> Callable[[str], None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a", encoding="ascii")

    def _log(line: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out = f"[{stamp}] {line}"
        print(out)
        fh.write(out + "\n")
        fh.flush()

    def _close() -> None:
        try:
            fh.close()
        except Exception:
            pass

    _log.close = _close  # type: ignore[attr-defined]
    return _log


def countdown(seconds: int, log: Callable[[str], None]) -> None:
    for n in range(seconds, 0, -1):
        log(f"Starting in {n}...")
        time.sleep(1.0)


def run_sequence(
    session: HapticTestSession,
    *,
    phase_name: str,
    button: int,
    param1: int,
    param2: int,
    intensities: list[int],
    delay_s: float,
    stop_on_error: bool,
    log: Callable[[str], None],
) -> bool:
    log(f"WARNING: {phase_name} will write haptic values now")
    for i, intensity in enumerate(intensities, start=1):
        level = INTENSITY_TO_LEVEL.get(intensity)
        level_text = f"level={level}" if level is not None else "level=<custom>"
        label = f"{phase_name}.step{i}"
        ok, summary = session.run_wrapped_haptic_write(
            button=button,
            param1=param1,
            param2=param2,
            intensity=intensity,
            label=label,
        )
        log(
            f"{phase_name} step={i}/{len(intensities)} intensity=0x{intensity:02X} "
            f"{level_text} result={summary}"
        )
        if (not ok) and stop_on_error:
            log(f"{phase_name} stopping early due to error response")
            return False
        time.sleep(delay_s)
    return True


def main() -> int:
    log = make_logger(LOG_PATH)
    session = HapticTestSession(log)
    log("=== HAPTIC WRITE TEST START ===")
    log("This script sends HID++ writes. Press Ctrl+C in countdown to abort.")

    try:
        session.open()
        countdown(3, log)

        run_sequence(
            session,
            phase_name="TEST1.GHubReplay.Left(5->0->5)",
            button=LEFT_BUTTON,
            param1=LEFT_PARAM1,
            param2=LEFT_PARAM2,
            intensities=TEST1_SEQUENCE,
            delay_s=1.0,
            stop_on_error=False,
            log=log,
        )

        run_sequence(
            session,
            phase_name="TEST2.Intermediate.Left",
            button=LEFT_BUTTON,
            param1=LEFT_PARAM1,
            param2=LEFT_PARAM2,
            intensities=TEST2_SEQUENCE,
            delay_s=1.0,
            stop_on_error=False,
            log=log,
        )

        run_sequence(
            session,
            phase_name="TEST3.AboveRange.Left",
            button=LEFT_BUTTON,
            param1=LEFT_PARAM1,
            param2=LEFT_PARAM2,
            intensities=TEST3_SEQUENCE,
            delay_s=1.0,
            stop_on_error=True,
            log=log,
        )
        log("=== HAPTIC WRITE TEST COMPLETE ===")
        return 0
    except KeyboardInterrupt:
        log("Interrupted by user")
        return 130
    except Exception as e:
        log(f"Fatal error: {type(e).__name__}: {e}")
        return 1
    finally:
        try:
            session.cleanup()
        finally:
            session.close()
            close_fn = getattr(log, "close", None)
            if callable(close_fn):
                close_fn()


if __name__ == "__main__":
    sys.exit(main())
