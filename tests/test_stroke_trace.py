from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from mouse_pressure.bridge.stroke_trace import StrokeTraceRecorder


def test_trace_recorder_writes_complete_stroke_atomically(tmp_path: Path) -> None:
    lines: list[str] = []
    recorder = StrokeTraceRecorder(str(tmp_path), lines.append)
    recorder.begin(interpolation="time")
    recorder.record("motion", x=10, y=20)
    recorder.record(
        "inject",
        x=10,
        y=20,
        pressure=400,
        flags=0x00000004,
        ok=True,
    )

    output = recorder.finish("release")
    recorder.flush()

    assert output is not None
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["metadata"]["interpolation"] == "time"
    assert [event["kind"] for event in payload["events"]] == [
        "stroke_begin",
        "motion",
        "inject",
        "stroke_end",
    ]
    assert not list(tmp_path.glob("*.tmp"))
    assert any("TRACE saved" in line for line in lines)
    recorder.close()


def test_trace_without_injection_is_discarded(tmp_path: Path) -> None:
    recorder = StrokeTraceRecorder(str(tmp_path), lambda _line: None)
    recorder.begin()
    recorder.record("motion", x=10, y=20)

    assert recorder.finish("no_contact") is None
    assert list(tmp_path.iterdir()) == []
    recorder.close()


def test_trace_write_failure_does_not_escape_stroke_cleanup(tmp_path: Path) -> None:
    blocked_directory = tmp_path / "not-a-directory"
    blocked_directory.write_text("occupied", encoding="utf-8")
    lines: list[str] = []
    recorder = StrokeTraceRecorder(str(blocked_directory), lines.append)
    recorder.begin()
    recorder.record("inject", x=1, y=2, pressure=300, flags=4, ok=True)

    output = recorder.finish("release")
    recorder.flush()
    assert output is not None
    assert not output.exists()
    assert recorder.active is False
    assert any("TRACE write failed" in line for line in lines)
    recorder.close()


def test_trace_serialization_never_blocks_stroke_release(tmp_path: Path) -> None:
    recorder = StrokeTraceRecorder(str(tmp_path), lambda _line: None)
    writer_started = threading.Event()
    allow_writer = threading.Event()
    original_write = recorder._write_payload  # noqa: SLF001

    def delayed_write(path: Path, payload: dict[str, object]) -> None:
        writer_started.set()
        allow_writer.wait(timeout=1.0)
        original_write(path, payload)

    recorder._write_payload = delayed_write  # type: ignore[method-assign]  # noqa: SLF001
    recorder.begin()
    recorder.record("inject", x=1, y=2, pressure=300, flags=4, ok=True)

    started_at = time.perf_counter()
    output = recorder.finish("release")
    elapsed = time.perf_counter() - started_at

    assert output is not None
    assert elapsed < 0.01
    assert writer_started.wait(timeout=1.0)
    assert not output.exists()
    allow_writer.set()
    recorder.flush()
    assert output.exists()
    recorder.close()


def test_deferred_delivery_events_are_collected_off_input_thread(
    tmp_path: Path,
) -> None:
    recorder = StrokeTraceRecorder(str(tmp_path), lambda _line: None)
    recorder.begin(output_backend="native_synthetic")
    recorder.record("inject", x=1, y=2, pressure=300, flags=4, ok=True)

    def collect() -> list[dict[str, object]]:
        time.sleep(0.02)
        return [
            {
                "kind": "native_delivery",
                "token": 7,
                "queue_delay_us": 120,
            }
        ]

    started_at = time.perf_counter()
    output = recorder.finish("release", deferred_events=collect)
    elapsed = time.perf_counter() - started_at

    assert output is not None
    assert elapsed < 0.01
    recorder.flush()
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["events"][-1]["kind"] == "native_delivery"
    assert payload["events"][-1]["token"] == 7
    recorder.close()
