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
    left_u16 = (left_raw & 0x03FF) << 6
    right_u16 = (right_raw & 0x03FF) << 6
    row = [
        0x11,
        0x01,
        0x0C,
        0x10,
        (left_u16 >> 8) & 0xFF,
        left_u16 & 0xFF,
        (right_u16 >> 8) & 0xFF,
        right_u16 & 0xFF,
    ]
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
        self.refresh_calls = 0
        self.dpi_calls: list[int] = []
        self.haptic_calls: list[tuple[int, int]] = []

    def open(self) -> None:
        self.open_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def enable_pressure_stream(self, mode: int, mode_arg: int) -> None:
        _ = mode
        _ = mode_arg
        self.enable_calls += 1

    def refresh_pressure_stream(self, mode: int, mode_arg: int) -> None:
        _ = mode
        _ = mode_arg
        self.refresh_calls += 1

    def set_dpi(self, dpi: int) -> int:
        self.dpi_calls.append(dpi)
        return dpi

    def set_haptic_levels(self, *, left: int, right: int) -> tuple[int, int]:
        self.haptic_calls.append((left, right))
        return left, right

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
        self.fail_open_calls: list[str] = []
        self.updates: list[tuple[int, int]] = []
        self.raw_updates: list[tuple[int | None, int | None]] = []
        self.movement_callback = None

    def open(self) -> None:
        self.open_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def release(self) -> None:
        self.release_calls += 1

    def fail_open(self, reason: str) -> None:
        self.fail_open_calls.append(reason)

    def set_movement_callback(self, callback) -> None:
        self.movement_callback = callback

    def update(
        self,
        left_mapped: int,
        right_mapped: int,
        *,
        pressure_fresh: bool = True,
        left_raw: int | None = None,
        right_raw: int | None = None,
    ) -> None:
        _ = pressure_fresh
        self.updates.append((left_mapped, right_mapped))
        self.raw_updates.append((left_raw, right_raw))


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
        self.assertTrue(
            any(pair in {(90, 95), (120, 130), (140, 150)} for pair in holder["emitter"].raw_updates)
        )
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
                "release_teardown": True,
                "left": {
                    "curve": "soft",
                    "deadzone_low": 4,
                    "deadzone_high": 15,
                    "contact_preset": "firm",
                    "pressure_floor": 18,
                },
            }
        )

        self.assertTrue(updated.linked)
        self.assertEqual(updated.left.curve, "soft")
        self.assertEqual(updated.right.curve, "soft")
        self.assertEqual(store.current.left.contact_preset, "firm")
        self.assertEqual(holder["emitter"].config.contact_threshold, 18)
        self.assertEqual(holder["emitter"].config.release_threshold, 12)
        self.assertEqual(holder["emitter"].config.min_contact_pressure, 184)
        self.assertEqual(holder["emitter"].config.right_contact_threshold, 18)
        self.assertTrue(holder["emitter"].config.release_teardown)
        self.assertEqual(session.open_calls, 1)
        self.assertEqual(session.enable_calls, 1)

        await service.stop_stream()

    async def test_deadzone_percent_trims_both_ends_of_curve_range(self) -> None:
        session = _FakeSession([])
        service, _, _ = self._service(session)

        service.apply_config(
            {
                "left": {
                    "deadzone_low": 5,
                    "deadzone_high": 5,
                }
            }
        )

        self.assertEqual(service._left_curve_config.deadzone_low, 0.05)
        self.assertEqual(service._left_curve_config.deadzone_high, 0.95)

    async def test_independent_right_channel_updates_right_pen_settings(self) -> None:
        session = _FakeSession([])
        service, store, holder = self._service(session)
        await service.start_stream()

        updated = service.apply_config(
            {
                "linked": False,
                "suppress_rmb": True,
                "right": {
                    "raw_min": 330,
                    "raw_max": 690,
                    "curve": "hard",
                    "contact_preset": "firm",
                    "pressure_floor": 25,
                    "path_stabilization": 40,
                    "pressure_influence": 70,
                    "onset_buffer": True,
                },
            }
        )

        emitter_config = holder["emitter"].config
        self.assertFalse(updated.linked)
        self.assertTrue(updated.suppress_rmb)
        self.assertEqual(store.current.right.curve, "hard")
        self.assertEqual(emitter_config.right_contact_threshold, 18)
        self.assertEqual(emitter_config.right_min_contact_pressure, 256)
        self.assertEqual(emitter_config.right_path_stabilization, 40)
        self.assertEqual(emitter_config.right_pressure_influence, 70)
        self.assertTrue(emitter_config.right_onset_buffer)

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

    async def test_pressure_queue_keeps_only_latest_sample(self) -> None:
        session = _FakeSession([])
        service, _, _ = self._service(session)
        service._sample_queue = asyncio.Queue(maxsize=1)

        service._enqueue_sample((1.0, 90, 91))
        service._enqueue_sample((2.0, 140, 141))

        self.assertEqual(service._sample_queue.qsize(), 1)
        self.assertEqual(service._sample_queue.get_nowait(), (2.0, 140, 141))

    async def test_pen_injection_runs_faster_than_pressure_reports(self) -> None:
        base = time.perf_counter()
        session = _FakeSession([(base, _frame(120, 121))])
        service, _, holder = self._service(session)
        service.launch_config.hz = 240.0

        await service.start_stream()
        await asyncio.sleep(0.06)

        # One pressure report should feed several independent cursor/pen ticks.
        self.assertGreaterEqual(len(holder["emitter"].updates), 3)
        await service.stop_stream()

    async def test_physical_movement_signal_injects_without_waiting_for_timer(self) -> None:
        base = time.perf_counter()
        session = _FakeSession([(base, _frame(120, 121))])
        service, _, holder = self._service(session)
        service.launch_config.hz = 30.0

        await service.start_stream()
        await asyncio.sleep(0.01)
        emitter = holder["emitter"]
        before = len(emitter.updates)
        self.assertIsNotNone(emitter.movement_callback)
        emitter.movement_callback()
        emitter.movement_callback()
        await asyncio.sleep(0.01)

        self.assertGreaterEqual(len(emitter.updates), before + 2)
        await service.stop_stream()

    async def test_device_settings_apply_on_reader_thread_while_active(self) -> None:
        class _StreamingSession(_FakeSession):
            def read_next(self, timeout_s: float = 0.1):
                _ = timeout_s
                time.sleep(0.002)
                return time.perf_counter(), _frame(120, 121)

        session = _StreamingSession([])
        service, _, _ = self._service(session)
        await service.start_stream()

        result = await service.apply_device_settings(
            dpi=1600,
            haptic_left=0,
            haptic_right=3,
        )

        self.assertEqual(result, {"dpi": 1600, "haptic_left": 0, "haptic_right": 3})
        self.assertEqual(session.dpi_calls, [1600])
        self.assertEqual(session.haptic_calls, [(0, 3)])
        self.assertTrue(service.stream_active)
        await service.stop_stream()

    async def test_stalled_stream_fails_open_and_stops(self) -> None:
        session = _FakeSession([])
        service, _, holder = self._service(session)
        service._stream_stall_timeout_s = 0.25
        failures: list[str] = []
        service.set_failure_callback(failures.append)

        await service.start_stream()
        await asyncio.sleep(0.6)

        self.assertFalse(service.stream_active)
        self.assertEqual(len(failures), 1)
        self.assertIn("native clicks were restored", failures[0])
        self.assertEqual(len(holder["emitter"].fail_open_calls), 1)
        self.assertEqual(holder["emitter"].close_calls, 1)
        self.assertEqual(session.close_calls, 1)

    async def test_silent_stream_is_reenabled_before_shutdown(self) -> None:
        class _RecoveringSession(_FakeSession):
            def refresh_pressure_stream(self, mode: int, mode_arg: int) -> None:
                super().refresh_pressure_stream(mode, mode_arg)
                if self.refresh_calls == 1:
                    self.rows.append((time.perf_counter(), _frame(125, 126)))

        session = _RecoveringSession([])
        service, _, holder = self._service(session)
        service._stream_recovery_after_s = 0.1

        await service.start_stream()
        await asyncio.sleep(0.35)

        self.assertTrue(service.stream_active)
        self.assertEqual(session.enable_calls, 1)
        self.assertGreaterEqual(session.refresh_calls, 1)
        self.assertGreater(len(holder["emitter"].updates), 0)
        await service.stop_stream()

    async def test_active_stream_gets_periodic_keepalive(self) -> None:
        class _StreamingSession(_FakeSession):
            def read_next(self, timeout_s: float = 0.1):
                _ = timeout_s
                time.sleep(0.002)
                return time.perf_counter(), _frame(120, 121)

        session = _StreamingSession([])
        service, _, _ = self._service(session)
        service._stream_keepalive_interval_s = 0.05

        await service.start_stream()
        await asyncio.sleep(0.15)

        self.assertTrue(service.stream_active)
        self.assertGreaterEqual(session.refresh_calls, 1)
        await service.stop_stream()


if __name__ == "__main__":
    unittest.main()
