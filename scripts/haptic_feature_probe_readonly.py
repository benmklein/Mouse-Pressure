"""Read-only HID++ function-0 probe for candidate haptic features.

This script performs ONLY function 0 queries with address byte 0x00:
  11 01 [feature_index] 00 00 ...
No feature enable/disable writes are sent.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import hid

VID = 0x046D
PID = 0xC54D
IFACE_NUMBER = 2
USAGE_PAGE_VENDOR = 0xFF00
USAGE_COL02 = 0x0002

REPORT_LONG = 0x11
DEVICE_INDEX = 0x01
FUNC0_ADDR_NO_SWID = 0x00

TARGET_FEATURE_IDS = [0x80E0, 0x8061, 0x9403, 0x1890, 0x18B1, 0x18A1]
FEATURE_TABLE_PATH = Path("docs/feature_table_full.txt")
LOG_PATH = Path("docs/haptic_feature_probe_log.txt")


def hex_bytes(data: list[int]) -> str:
    return " ".join(f"{b:02X}" for b in data)


def read_feature_map_from_table(path: Path) -> dict[int, int]:
    pattern = re.compile(r"index=0x([0-9A-Fa-f]{2})\s+feature_id=0x([0-9A-Fa-f]{4})")
    out: dict[int, int] = {}
    text = path.read_text(encoding="ascii", errors="ignore")
    for line in text.splitlines():
        m = pattern.search(line)
        if not m:
            continue
        idx = int(m.group(1), 16)
        feature_id = int(m.group(2), 16)
        out[feature_id] = idx
    return out


def find_col02_path() -> bytes | None:
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


def read_for(dev: hid.device, seconds: float) -> list[tuple[float, list[int]]]:
    end = time.perf_counter() + seconds
    rows: list[tuple[float, list[int]]] = []
    while time.perf_counter() < end:
        data = dev.read(64)
        if data:
            rows.append((time.perf_counter(), list(data)))
        else:
            time.sleep(0.001)
    return rows


def main() -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    feature_map = read_feature_map_from_table(FEATURE_TABLE_PATH)

    missing = [fid for fid in TARGET_FEATURE_IDS if fid not in feature_map]
    if missing:
        print(
            "ERROR: missing feature IDs in feature table: "
            + ", ".join(f"0x{x:04X}" for x in missing)
        )
        return 1

    with LOG_PATH.open("w", encoding="ascii") as fh:
        def log(line: str) -> None:
            print(line)
            fh.write(line + "\n")
            fh.flush()

        path = find_col02_path()
        if not path:
            log("ERROR: MI_02 Col02 not found")
            return 1

        log("READ-ONLY HID++ function0 probe begin")
        log(f"OPEN Col02 path={path!r}")

        targets = [(fid, feature_map[fid]) for fid in TARGET_FEATURE_IDS]
        for fid, idx in targets:
            log(f"MAP feature_id=0x{fid:04X} -> index=0x{idx:02X}")

        dev = hid.device()
        try:
            dev.open_path(path)
            dev.set_nonblocking(True)

            # Drain stale packets once before probing.
            stale = read_for(dev, 0.05)
            if stale:
                log(f"DRAIN stale_packets={len(stale)}")

            for fid, idx in targets:
                # Exactly as requested: 11 01 [index] 00 00...
                pkt = [REPORT_LONG, DEVICE_INDEX, idx, FUNC0_ADDR_NO_SWID] + [0x00] * 16
                wrote = dev.write(pkt)
                log(
                    f"TX feature_id=0x{fid:04X} index=0x{idx:02X} "
                    f"wrote={wrote} {hex_bytes(pkt)}"
                )

                rows = read_for(dev, 0.35)
                if not rows:
                    log(f"RX feature_id=0x{fid:04X} index=0x{idx:02X} <none>")
                    continue
                for ts, data in rows:
                    log(
                        f"RX feature_id=0x{fid:04X} index=0x{idx:02X} "
                        f"t={ts:.6f} len={len(data)} {hex_bytes(data)}"
                    )

        finally:
            try:
                dev.close()
            except Exception:
                pass

        log("READ-ONLY HID++ function0 probe end")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
