from __future__ import annotations

import json
from pathlib import Path

from superstrike_pressure.bridge.stroke_trace import StrokeTraceRecorder


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

    assert output is not None
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["metadata"]["interpolation"] == "time"
    assert [event["kind"] for event in payload["events"]] == [
        "stroke_begin",
        "motion",
        "inject",
        "stroke_end",
    ]
    assert not list(tmp_path.glob("*.tmp"))
    assert any("TRACE saved" in line for line in lines)


def test_trace_without_injection_is_discarded(tmp_path: Path) -> None:
    recorder = StrokeTraceRecorder(str(tmp_path), lambda _line: None)
    recorder.begin()
    recorder.record("motion", x=10, y=20)

    assert recorder.finish("no_contact") is None
    assert list(tmp_path.iterdir()) == []


def test_trace_write_failure_does_not_escape_stroke_cleanup(tmp_path: Path) -> None:
    blocked_directory = tmp_path / "not-a-directory"
    blocked_directory.write_text("occupied", encoding="utf-8")
    lines: list[str] = []
    recorder = StrokeTraceRecorder(str(blocked_directory), lines.append)
    recorder.begin()
    recorder.record("inject", x=1, y=2, pressure=300, flags=4, ok=True)

    assert recorder.finish("release") is None
    assert recorder.active is False
    assert any("TRACE write failed" in line for line in lines)
