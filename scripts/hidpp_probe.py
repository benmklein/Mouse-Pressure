"""
HID++ probing utility with mandatory safety cleanup.

This script sends HID++ commands on the Logitech receiver vendor interface
(`MI_02 Col02`) and always attempts to disable MouseButtonSpy (0x8110) on exit.
"""

from __future__ import annotations

import argparse
import atexit
import signal
import sys
import time
from pathlib import Path
from typing import Callable

import hid

VID = 0x046D
PID = 0xC54D
IFACE_NUMBER = 2
USAGE_PAGE_VENDOR = 0xFF00
USAGE_COL01 = 0x0001
USAGE_COL02 = 0x0002

REPORT_LONG = 0x11
DEVICE_INDEX = 0x01
SW_ID = 0x08

ROOT_IDX = 0x00
FEATURE_SET_ID = 0x0001
MOUSE_BUTTON_SPY_ID = 0x8110
FALLBACK_MOUSE_BUTTON_SPY_INDEX = 0x0F


def hex_bytes(data: list[int]) -> str:
    return " ".join(f"{b:02X}" for b in data)


def build_long_report(
    *,
    device_index: int,
    sub_id: int,
    address: int,
    payload: list[int] | None = None,
) -> list[int]:
    body = list(payload or [])[:16]
    body.extend([0] * (16 - len(body)))
    return [REPORT_LONG, device_index, sub_id, address] + body


def function_to_address(function_id: int, sw_id: int = SW_ID) -> int:
    return ((function_id & 0x0F) << 4) | (sw_id & 0x0F)


class HidppProbeSession:
    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log
        self.dev: hid.device | None = None
        self.path_col01: bytes | None = None
        self.path_col02: bytes | None = None
        self.feature_map: dict[int, int] = {}
        self._cleanup_done = False
        self._atexit_registered = False
        self._signal_handlers: dict[signal.Signals, object] = {}

    def discover_paths(self) -> None:
        for d in hid.enumerate():
            if (
                d["vendor_id"] == VID
                and d["product_id"] == PID
                and d.get("interface_number") == IFACE_NUMBER
                and d.get("usage_page") == USAGE_PAGE_VENDOR
            ):
                if d.get("usage") == USAGE_COL01:
                    self.path_col01 = d["path"]
                elif d.get("usage") == USAGE_COL02:
                    self.path_col02 = d["path"]

    def open(self) -> None:
        self.discover_paths()
        if not self.path_col02:
            raise RuntimeError("MI_02 Col02 not found")
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
                # Some environments do not allow replacing handlers.
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

    def transact(
        self,
        *,
        sub_id: int,
        function_id: int,
        payload: list[int] | None = None,
        label: str = "",
        read_window_s: float = 0.6,
    ) -> list[list[int]]:
        if self.dev is None:
            raise RuntimeError("Device not open")

        _ = self.read_for(0.05)  # drain stale packets
        address = function_to_address(function_id)
        pkt = build_long_report(
            device_index=DEVICE_INDEX,
            sub_id=sub_id,
            address=address,
            payload=payload,
        )
        try:
            wrote = self.dev.write(pkt)
            self.log(f"TX {label} wrote={wrote} {hex_bytes(pkt)}")
        except OSError as e:
            self.log(f"TX {label} write_error={e} {hex_bytes(pkt)}")
            return []

        rows = self.read_for(read_window_s)
        if not rows:
            self.log(f"RX {label} <none>")
            return []

        for b in rows:
            if len(b) >= 7:
                self.log(
                    "RX "
                    f"{label} len={len(b)} rep=0x{b[0]:02X} dev=0x{b[1]:02X} "
                    f"sub=0x{b[2]:02X} addr=0x{b[3]:02X} "
                    f"p0=0x{b[4]:02X} p1=0x{b[5]:02X} p2=0x{b[6]:02X} "
                    f"{hex_bytes(b)}"
                )
            else:
                self.log(f"RX {label} len={len(b)} {hex_bytes(b)}")
        return rows

    def root_get_feature(self, feature_id: int) -> int | None:
        payload = [(feature_id >> 8) & 0xFF, feature_id & 0xFF, 0x00]
        rows = self.transact(
            sub_id=ROOT_IDX,
            function_id=0,
            payload=payload,
            label=f"ROOT.GET_FEATURE(0x{feature_id:04X})",
        )
        for b in rows:
            if len(b) >= 7 and b[2] == ROOT_IDX and b[3] == function_to_address(0):
                return b[4]
        return None

    def get_feature_set_index(self) -> int | None:
        idx = self.root_get_feature(FEATURE_SET_ID)
        if idx is not None:
            self.feature_map[FEATURE_SET_ID] = idx
        return idx

    def get_feature_count(self, feature_set_index: int) -> int | None:
        rows = self.transact(
            sub_id=feature_set_index,
            function_id=0,
            payload=[0, 0, 0],
            label="FEATURE_SET.GET_COUNT",
        )
        for b in rows:
            if len(b) >= 5 and b[2] == feature_set_index and b[3] == function_to_address(0):
                return b[4]
        return None

    def get_feature_id(self, feature_set_index: int, feature_index: int) -> tuple[int, int] | None:
        rows = self.transact(
            sub_id=feature_set_index,
            function_id=1,
            payload=[feature_index, 0, 0],
            label=f"FEATURE_SET.GET_FEATURE_ID[0x{feature_index:02X}]",
        )
        for b in rows:
            if len(b) >= 7 and b[2] == feature_set_index and b[3] == function_to_address(1):
                feature_id = (b[4] << 8) | b[5]
                feature_type = b[6]
                return feature_id, feature_type
        return None

    def enumerate_features(self, limit: int | None = None) -> None:
        self.transact(
            sub_id=ROOT_IDX,
            function_id=1,
            payload=[0, 0, 0],
            label="ROOT.GET_PROTOCOL_VERSION",
        )
        feature_set_index = self.get_feature_set_index()
        if feature_set_index is None:
            self.log("PARSE feature_set_index=<none>")
            return

        self.log(f"PARSE feature_set_index=0x{feature_set_index:02X}")
        count = self.get_feature_count(feature_set_index)
        if count is None:
            self.log("PARSE feature_count=<none>")
            return

        self.log(f"PARSE feature_count_raw={count}")
        max_idx = count + 1
        if limit is not None:
            max_idx = min(max_idx, limit)

        for i in range(max_idx):
            result = self.get_feature_id(feature_set_index, i)
            if result is None:
                self.log(f"PARSE index=0x{i:02X} no_direct_response")
                continue
            feature_id, feature_type = result
            self.log(
                f"PARSE index=0x{i:02X} feature_id=0x{feature_id:04X} "
                f"type=0x{feature_type:02X}"
            )
            self.feature_map[feature_id] = i

    def disable_mouse_button_spy(self) -> None:
        if self.dev is None:
            return

        spy_index = self.feature_map.get(MOUSE_BUTTON_SPY_ID)
        if spy_index is None:
            spy_index = self.root_get_feature(MOUSE_BUTTON_SPY_ID)
            if spy_index is not None:
                self.feature_map[MOUSE_BUTTON_SPY_ID] = spy_index
        if spy_index is None:
            spy_index = FALLBACK_MOUSE_BUTTON_SPY_INDEX
            self.log(f"CLEANUP using fallback MouseButtonSpy index 0x{spy_index:02X}")
        else:
            self.log(f"CLEANUP using MouseButtonSpy index 0x{spy_index:02X}")

        # Best-effort disable sequence for 0x8110.
        # We send setter-like zero payloads, then query status.
        self.transact(
            sub_id=spy_index,
            function_id=2,
            payload=[0x00] + [0x00] * 15,
            label="CLEANUP.MouseButtonSpy.func2.disable",
            read_window_s=0.3,
        )
        self.transact(
            sub_id=spy_index,
            function_id=1,
            payload=[0x00] + [0x00] * 15,
            label="CLEANUP.MouseButtonSpy.func1.disable",
            read_window_s=0.3,
        )
        self.transact(
            sub_id=spy_index,
            function_id=0,
            payload=[0x00, 0x00, 0x00],
            label="CLEANUP.MouseButtonSpy.func0.status",
            read_window_s=0.3,
        )

    def cleanup(self) -> None:
        if self._cleanup_done:
            return
        self._cleanup_done = True
        self.log("CLEANUP begin")
        try:
            self.disable_mouse_button_spy()
        except Exception as e:
            self.log(f"CLEANUP error={type(e).__name__}: {e}")
        self.log("CLEANUP end")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Probe HID++ features on Logitech receiver Col02 with mandatory "
            "MouseButtonSpy cleanup."
        )
    )
    p.add_argument(
        "--log-file",
        default="docs/hidpp_probe_safe_log.txt",
        help="Path for probe log output (default: docs/hidpp_probe_safe_log.txt)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of feature indices to enumerate (default: full reported count)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("w", encoding="ascii") as fh:
        def log(line: str) -> None:
            print(line)
            fh.write(line + "\n")
            fh.flush()

        session = HidppProbeSession(log=log)
        try:
            session.open()
            session.enumerate_features(limit=args.limit)
        except KeyboardInterrupt:
            log("Interrupted")
            return 130
        except Exception as e:
            log(f"ERROR {type(e).__name__}: {e}")
            return 1
        finally:
            session.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
