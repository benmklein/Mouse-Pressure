"""In-process log bus used by WS modules."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Callable


@dataclass(frozen=True)
class LogEntry:
    level: str
    ts: int
    msg: str

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


class LogBus:
    def __init__(self, maxlen: int = 500) -> None:
        self._entries: deque[LogEntry] = deque(maxlen=maxlen)
        self._subscribers: list[Callable[[LogEntry], None]] = []

    def info(self, msg: str) -> LogEntry:
        return self._append("INFO", msg)

    def warn(self, msg: str) -> LogEntry:
        return self._append("WARN", msg)

    def error(self, msg: str) -> LogEntry:
        return self._append("ERROR", msg)

    def _append(self, level: str, msg: str) -> LogEntry:
        entry = LogEntry(level=level, ts=int(time.time() * 1000), msg=str(msg))
        self._entries.append(entry)
        for callback in list(self._subscribers):
            try:
                callback(entry)
            except Exception:
                continue
        return entry

    def get_recent(self, limit: int = 100) -> list[LogEntry]:
        safe_limit = max(0, int(limit))
        if safe_limit == 0:
            return []
        return list(self._entries)[-safe_limit:]

    def subscribe(self, callback: Callable[[LogEntry], None]) -> None:
        self._subscribers.append(callback)


GLOBAL_LOG_BUS = LogBus()
