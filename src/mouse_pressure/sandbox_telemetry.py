"""Small local IPC channel for companion pressure-aware applications.

The driver is the sole owner of the mouse HID stream. Companion programs read
the already-calibrated pressure values from a named shared-memory mapping, so
they can run without a second HID lease or a localhost firewall exception.
"""

from __future__ import annotations

import mmap
import os
import struct
import time
from dataclasses import dataclass
from typing import Final


TELEMETRY_TAG: Final = r"Local\MousePressureTelemetryV1"
TELEMETRY_VERSION: Final = 1
TELEMETRY_SIZE: Final = 64
FLAG_ACTIVE: Final = 1 << 0
FLAG_DEVICE_FOUND: Final = 1 << 1

_MAGIC = b"MPS1"
_FRAME = struct.Struct("<4sIIQHHHHI")


@dataclass(frozen=True)
class SandboxTelemetrySample:
    timestamp_ns: int
    left_raw: int
    right_raw: int
    left_mapped: int
    right_mapped: int
    active: bool
    device_found: bool

    @property
    def left_pressure(self) -> float:
        return max(0.0, min(1.0, self.left_mapped / 1023.0))

    @property
    def right_pressure(self) -> float:
        return max(0.0, min(1.0, self.right_mapped / 1023.0))


def _open_named_mapping() -> mmap.mmap | None:
    if os.name != "nt":
        return None
    try:
        return mmap.mmap(-1, TELEMETRY_SIZE, tagname=TELEMETRY_TAG)
    except OSError:
        return None


class SandboxTelemetryWriter:
    """Publish one coherent pressure snapshot with a tiny seqlock."""

    def __init__(self, mapping: mmap.mmap | None = None) -> None:
        self._mapping = mapping if mapping is not None else _open_named_mapping()
        self._sequence = 0

    @property
    def available(self) -> bool:
        return self._mapping is not None

    def publish(
        self,
        *,
        left_raw: int,
        right_raw: int,
        left_mapped: int,
        right_mapped: int,
        active: bool,
        device_found: bool,
        timestamp_ns: int | None = None,
    ) -> None:
        mapping = self._mapping
        if mapping is None:
            return
        flags = (FLAG_ACTIVE if active else 0) | (
            FLAG_DEVICE_FOUND if device_found else 0
        )
        self._sequence = (self._sequence + 2) & 0xFFFFFFFE
        if self._sequence == 0:
            self._sequence = 2
        timestamp = time.perf_counter_ns() if timestamp_ns is None else timestamp_ns
        values = (
            _MAGIC,
            TELEMETRY_VERSION,
            self._sequence,
            max(0, int(timestamp)),
            max(0, min(0xFFFF, int(left_raw))),
            max(0, min(0xFFFF, int(right_raw))),
            max(0, min(1023, int(left_mapped))),
            max(0, min(1023, int(right_mapped))),
            flags,
        )
        # An odd sequence marks an in-progress frame. Readers only accept a
        # matching, even sequence from two consecutive reads.
        odd_values = list(values)
        odd_values[2] = self._sequence | 1
        self._write(tuple(odd_values))
        self._write(values)

    def set_inactive(self) -> None:
        self.publish(
            left_raw=0,
            right_raw=0,
            left_mapped=0,
            right_mapped=0,
            active=False,
            device_found=False,
        )

    def close(self) -> None:
        mapping, self._mapping = self._mapping, None
        if mapping is not None:
            mapping.close()

    def _write(self, values: tuple[object, ...]) -> None:
        mapping = self._mapping
        if mapping is None:
            return
        frame = _FRAME.pack(*values)
        mapping.seek(0)
        mapping.write(frame)
        mapping.write(b"\0" * (TELEMETRY_SIZE - len(frame)))


class SandboxTelemetryReader:
    """Read fresh pressure output from a running Mouse Pressure process."""

    def __init__(self, mapping: mmap.mmap | None = None) -> None:
        self._mapping = mapping if mapping is not None else _open_named_mapping()

    @property
    def available(self) -> bool:
        return self._mapping is not None

    def read(self, *, max_age_s: float = 0.5) -> SandboxTelemetrySample | None:
        mapping = self._mapping
        if mapping is None:
            return None
        for _attempt in range(3):
            first = self._read_frame()
            second = self._read_frame()
            if first != second:
                continue
            magic, version, sequence, timestamp, left_raw, right_raw, left, right, flags = first
            if magic != _MAGIC or version != TELEMETRY_VERSION or sequence & 1:
                return None
            if time.perf_counter_ns() - timestamp > max(0.0, max_age_s) * 1_000_000_000:
                return None
            return SandboxTelemetrySample(
                timestamp_ns=timestamp,
                left_raw=left_raw,
                right_raw=right_raw,
                left_mapped=left,
                right_mapped=right,
                active=bool(flags & FLAG_ACTIVE),
                device_found=bool(flags & FLAG_DEVICE_FOUND),
            )
        return None

    def close(self) -> None:
        mapping, self._mapping = self._mapping, None
        if mapping is not None:
            mapping.close()

    def _read_frame(self) -> tuple[bytes, int, int, int, int, int, int, int, int]:
        mapping = self._mapping
        if mapping is None:
            return (b"", 0, 0, 0, 0, 0, 0, 0, 0)
        mapping.seek(0)
        return _FRAME.unpack(mapping.read(_FRAME.size))
