from __future__ import annotations

import asyncio
import sys
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from superstrike_pressure.bridge.config import ChannelConfig, LaunchConfig, RuntimeConfig  # noqa: E402
from superstrike_pressure.web.models import StreamAlreadyActiveError, StreamNotActiveError  # noqa: E402
from superstrike_pressure.web.runtime_service import RuntimeService  # noqa: E402


def _frame(left_raw: int, right_raw: int) -> list[int]:
    row = [0x11, 0x01, 0x0C, 0x10, left_raw & 0xFF, 0x00, right_raw & 0xFF]
    row.extend([0x00] * (64 - len(row)))
    return row


@dataclass
class _MemoryConfigStore:
    current: RuntimeConfig

    def load(self) -> RuntimeConfig:
        return self.current

    def save(self, config: RuntimeConfig) -> None:
        self.current = config


class _FakeSession:
    def __init__(self, rows: list[tuple[float, list[int]]]) -> None:
        self.rows = list(rows)
        self.open_calls = 0
        self.close_calls = 0
        self.enable_calls = 0

    def open(self) -> None:
        self.open_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def enable_pressure_stream(self, mode: int, mode_arg: int) -> None:
        _ = mode
        _ = mode_arg
        self.enable_calls += 1

    def read_next(self, timeout_s: float = 0.1):
        _ = timeout_s
        if self.rows:
            return self.rows.pop(0)
        time.sleep(0.002)
        return None


class _FakeEmitter:
    def __init__(self, config, log) -> None:
        self.config = config
        self.log = log
        self.open_calls = 0
        self.close_calls = 0
        self.release_calls = 0
        self.updates: list[tuple[int, int]] = []

    def open(self) -> None:
        self.open_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def release(self) -> None:
        self.release_calls += 1

    def update(self, left_mapped: int, right_mapped: int) -> None:
        self.updates.append((left_mapped, right_mapped))


class RuntimeServiceTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, session: _FakeSession):
        config = RuntimeConfig(
            linked=False,
            left=ChannelConfig(curve="linear", contact_preset="medium"),
            right=ChannelConfig(curve="linear", contact_preset="medium"),
        )
        store = _MemoryConfigStore(config)
        emitter_holder: dict[str, _FakeEmitter] = {}

        def make_session(_log):
            return session

        def make_emitter(cfg, log):
            emitter = _FakeEmitter(cfg, log)
            emitter_holder["emitter"] = emitter
            return emitter

        service = RuntimeService(
            launch_config=LaunchConfig(),
            config_store=store,
            session_factory=make_session,
            emitter_factory=make_emitter,
        )
        return service, store, emitter_holder

    async def test_start_stop_stream_emits_telemetry(self) -> None:
        base = time.perf_counter()
        session = _FakeSession(
            [
                (base + 0.01, _frame(90, 95)),
                (base + 0.03, _frame(120, 130)),
                (base + 0.05, _frame(140, 150)),
            ]
        )
        service, _, holder = self._service(session)
        received: list[dict] = []
        service.set_telemetry_callback(received.append)

        await service.start_stream()
        await asyncio.sleep(0.05)
        await service.stop_stream()

        self.assertFalse(service.stream_active)
        self.assertTrue(service.device_found)
        self.assertGreaterEqual(len(received), 1)
        self.assertIn("left_raw", received[0])
        self.assertIn("right_mapped", received[0])
        self.assertGreater(len(holder["emitter"].updates), 0)
        self.assertEqual(session.open_calls, 1)
        self.assertEqual(session.enable_calls, 1)
        self.assertEqual(session.close_calls, 1)

    async def test_apply_config_updates_emitter_thresholds_without_restart(self) -> None:
        base = time.perf_counter()
        session = _FakeSession([(base + 0.01, _frame(100, 100)), (base + 0.02, _frame(110, 112))])
        service, store, holder = self._service(session)
        await service.start_stream()
        await asyncio.sleep(0.02)

        updated = service.apply_config(
            {
                "linked": True,
                "left": {
                    "curve": "soft",
                    "deadzone_low": 4,
                    "deadzone_high": 15,
                    "contact_preset": "firm",
                },
            }
        )

        self.assertTrue(updated.linked)
        self.assertEqual(updated.left.curve, "soft")
        self.assertEqual(updated.right.curve, "soft")
        self.assertEqual(store.current.left.contact_preset, "firm")
        self.assertEqual(holder["emitter"].config.contact_threshold, 18)
        self.assertEqual(holder["emitter"].config.release_threshold, 12)
        self.assertEqual(session.open_calls, 1)
        self.assertEqual(session.enable_calls, 1)

        await service.stop_stream()

    async def test_start_while_active_raises(self) -> None:
        base = time.perf_counter()
        session = _FakeSession([(base + 0.01, _frame(80, 80))])
        service, _, _ = self._service(session)
        await service.start_stream()
        with self.assertRaises(StreamAlreadyActiveError):
            await service.start_stream()
        await service.stop_stream()

    async def test_stop_while_inactive_raises(self) -> None:
        base = time.perf_counter()
        session = _FakeSession([(base + 0.01, _frame(80, 80))])
        service, _, _ = self._service(session)
        with self.assertRaises(StreamNotActiveError):
            await service.stop_stream()

    async def test_wait_for_raw_sample_reads_stream_data(self) -> None:
        base = time.perf_counter()
        session = _FakeSession([(base + 0.01, _frame(101, 77))])
        service, _, _ = self._service(session)
        await service.start_stream()
        raw = await service.wait_for_raw_sample(timeout_s=0.2)
        self.assertEqual(raw, (101, 77))
        await service.stop_stream()


if __name__ == "__main__":
    unittest.main()
