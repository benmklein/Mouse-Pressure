from __future__ import annotations

import json
import zipfile
from pathlib import Path

from mouse_pressure.bridge.config import LaunchConfig, RuntimeConfig
from mouse_pressure.runtime.diagnostic_session import DiagnosticSessionCollector
from mouse_pressure.runtime.log_bus import LogBus


def test_debug_session_builds_one_zip_with_logs_and_new_strokes(tmp_path: Path) -> None:
    trace_dir = tmp_path / "stroke_traces"
    trace_dir.mkdir()
    old_trace = trace_dir / "stroke-old.json"
    old_trace.write_text("{}", encoding="utf-8")
    bus = LogBus()
    collector = DiagnosticSessionCollector(tmp_path, trace_dir, bus)
    collector.start(RuntimeConfig(debug_mode=True), LaunchConfig())
    bus.info("session line")
    new_trace = trace_dir / "stroke-new.json"
    new_trace.write_text('{"events": []}', encoding="utf-8")

    bundle = collector.finish()

    assert bundle is not None
    with zipfile.ZipFile(bundle) as archive:
        assert sorted(archive.namelist()) == [
            "session.json",
            "strokes/stroke-new.json",
        ]
        session = json.loads(archive.read("session.json"))
    assert session["logs"][0]["msg"] == "session line"
    assert session["stroke_files"] == ["stroke-new.json"]


def test_non_debug_session_does_not_write_bundle(tmp_path: Path) -> None:
    bus = LogBus()
    collector = DiagnosticSessionCollector(tmp_path, tmp_path / "traces", bus)
    collector.start(RuntimeConfig(debug_mode=False), LaunchConfig())
    bus.info("not captured")

    assert collector.finish() is None
