"""Structured per-stroke diagnostics for the synthetic pen pipeline."""

from __future__ import annotations

import json
import queue
import threading
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
        self._write_queue: queue.Queue[
            tuple[
                Path,
                dict[str, Any],
                Callable[[], list[dict[str, Any]]] | None,
            ]
            | None
        ] = queue.Queue()
        self._writer = threading.Thread(
            target=self._writer_main,
            name="mouse-pressure-trace-writer",
            daemon=True,
        )
        self._writer.start()

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

    def finish(
        self,
        reason: str,
        *,
        deferred_events: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> Path | None:
        if not self.active:
            return None
        self.record("stroke_end", reason=str(reason))
        self.active = False
        if not any(event["kind"] == "inject" for event in self._events):
            return None

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = self.directory / f"stroke-{stamp}.json"
        payload = {
            "schema_version": 2,
            "created_at": datetime.now().astimezone().isoformat(),
            "metadata": self._metadata,
            "events": self._events,
        }
        self._write_queue.put((path, payload, deferred_events))
        return path

    def _write_payload(self, path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(".json.tmp")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary.replace(path)
        except (OSError, TypeError, ValueError) as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            self.log(f"TRACE write failed for {path}: {exc}")
            return
        self.log(f"TRACE saved {path.resolve()}")

    def _writer_main(self) -> None:
        while True:
            item = self._write_queue.get()
            try:
                if item is None:
                    return
                path, payload, deferred_events = item
                if deferred_events is not None:
                    try:
                        extra_events = deferred_events()
                    except Exception as exc:  # diagnostics must never stop cleanup
                        self.log(f"TRACE deferred event collection failed: {exc}")
                        extra_events = []
                    events = payload["events"]
                    next_sequence = max(
                        (int(event.get("seq", 0)) for event in events),
                        default=0,
                    )
                    fallback_t_ms = float(events[-1].get("t_ms", 0.0)) if events else 0.0
                    for event in extra_events:
                        next_sequence += 1
                        events.append(
                            {
                                "seq": next_sequence,
                                "t_ms": fallback_t_ms,
                                **event,
                            }
                        )
                self._write_payload(path, payload)
            finally:
                self._write_queue.task_done()

    def flush(self) -> None:
        """Wait for all queued trace writes; never called on the input path."""
        self._write_queue.join()

    def close(self) -> None:
        """Flush diagnostics and stop the background writer."""
        if self._writer.is_alive():
            self.flush()
            self._write_queue.put(None)
            self._writer.join(timeout=2.0)
