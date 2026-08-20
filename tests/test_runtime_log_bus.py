from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mouse_pressure.runtime.log_bus import LogBus  # noqa: E402


class LogBusTests(unittest.TestCase):
    def test_publish_and_get_recent(self) -> None:
        bus = LogBus(maxlen=10)
        bus.info("hello")
        bus.warn("warned")
        bus.error("failed")

        entries = bus.get_recent(limit=2)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].level, "WARN")
        self.assertEqual(entries[1].level, "ERROR")
        self.assertEqual(entries[1].msg, "failed")
        self.assertIsInstance(entries[1].ts, int)

    def test_maxlen_is_enforced(self) -> None:
        bus = LogBus(maxlen=2)
        bus.info("one")
        bus.info("two")
        bus.info("three")
        entries = bus.get_recent(limit=10)
        self.assertEqual([e.msg for e in entries], ["two", "three"])

    def test_subscriber_receives_entries(self) -> None:
        bus = LogBus(maxlen=5)
        seen: list[str] = []

        def callback(entry) -> None:
            seen.append(f"{entry.level}:{entry.msg}")

        bus.subscribe(callback)
        bus.warn("ping")
        self.assertEqual(seen, ["WARN:ping"])

    def test_limit_zero_returns_empty(self) -> None:
        bus = LogBus(maxlen=5)
        bus.info("x")
        self.assertEqual(bus.get_recent(limit=0), [])


if __name__ == "__main__":
    unittest.main()
