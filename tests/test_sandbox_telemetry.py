from __future__ import annotations

import mmap
import time

from mouse_pressure.sandbox_telemetry import (
    TELEMETRY_SIZE,
    SandboxTelemetryReader,
    SandboxTelemetryWriter,
)


def _shared_mapping_pair() -> tuple[mmap.mmap, mmap.mmap]:
    # Anonymous mappings cannot be opened twice portably, so the unit test
    # shares one buffer. Production uses a named Windows mapping.
    mapping = mmap.mmap(-1, TELEMETRY_SIZE)
    return mapping, mapping


def test_round_trip_exposes_processed_pressure() -> None:
    writer_map, reader_map = _shared_mapping_pair()
    writer = SandboxTelemetryWriter(writer_map)
    reader = SandboxTelemetryReader(reader_map)

    writer.publish(
        left_raw=512,
        right_raw=640,
        left_mapped=256,
        right_mapped=1023,
        active=True,
        device_found=True,
    )

    sample = reader.read()
    assert sample is not None
    assert sample.left_raw == 512
    assert sample.right_raw == 640
    assert sample.left_pressure == 256 / 1023
    assert sample.right_pressure == 1.0
    assert sample.active
    assert sample.device_found


def test_inactive_and_stale_frames_are_not_usable_output() -> None:
    writer_map, reader_map = _shared_mapping_pair()
    writer = SandboxTelemetryWriter(writer_map)
    reader = SandboxTelemetryReader(reader_map)

    writer.set_inactive()
    inactive = reader.read()
    assert inactive is not None
    assert not inactive.active

    writer.publish(
        left_raw=500,
        right_raw=500,
        left_mapped=500,
        right_mapped=500,
        active=True,
        device_found=True,
        timestamp_ns=time.perf_counter_ns() - 2_000_000_000,
    )
    assert reader.read(max_age_s=0.5) is None
