"""Structured per-stroke diagnostics for the synthetic pen pipeline."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


class StrokeTraceRecorder:
    """Buffer one stroke in memory and atomically write it on release."""

    def __init__(self, directory: str, log: Callable[[str], None]) -> None:
        self.directory = Path(directory)
        self.log = log
        self.active = False
        self._started_at = 0.0
        self._events: list[dict[str, Any]] = []
        self._metadata: dict[str, Any] = {}
        self._sequence = 0

    def begin(self, **metadata: Any) -> None:
        if self.active:
            return
        self.active = True
        self._started_at = time.perf_counter()
        self._events = []
        self._metadata = dict(metadata)
        self._sequence = 0
        self.record("stroke_begin")

    def record(self, kind: str, *, at: float | None = None, **fields: Any) -> None:
        if not self.active or len(self._events) >= 20_000:
            return
        observed_at = time.perf_counter() if at is None else float(at)
        self._sequence += 1
        self._events.append(
            {
                "seq": self._sequence,
                "t_ms": round((observed_at - self._started_at) * 1000.0, 4),
                "kind": str(kind),
                **fields,
            }
        )

    def finish(self, reason: str) -> Path | None:
        if not self.active:
            return None
        self.record("stroke_end", reason=str(reason))
        self.active = False
        if not any(event["kind"] == "inject" for event in self._events):
            return None

        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        path = self.directory / f"stroke-{stamp}.json"
        payload = {
            "schema_version": 1,
            "created_at": datetime.now().astimezone().isoformat(),
            "metadata": self._metadata,
            "events": self._events,
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)
        self.log(f"TRACE saved {path.resolve()}")
        return path
