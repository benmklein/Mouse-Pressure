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
        self.disable_calls = 0
        self.refresh_calls = 0
        self.dpi_calls: list[int] = []
        self.haptic_calls: list[tuple[int, int]] = []
        self.current_dpi = 800
        self.current_haptics = (5, 5)
        self.current_profile_enabled = False
        self.current_profile_sector: int | None = None
        self.device_events: list[str] = []

    def open(self) -> None:
        self.open_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def enable_pressure_stream(self, mode: int, mode_arg: int) -> None:
        _ = mode
        _ = mode_arg
        self.enable_calls += 1
        self.device_events.append("pressure_enable")

    def disable_pressure_stream(self) -> None:
        self.disable_calls += 1
        self.device_events.append("pressure_disable")

    def refresh_pressure_stream(self, mode: int, mode_arg: int) -> None:
        _ = mode
        _ = mode_arg
        self.refresh_calls += 1

    def set_dpi(self, dpi: int) -> int:
        self.dpi_calls.append(dpi)
        self.device_events.append(f"dpi={dpi}")
        self.current_dpi = dpi
        return dpi

    def get_dpi(self) -> int:
        return self.current_dpi

    def set_haptic_levels(self, *, left: int, right: int) -> tuple[int, int]:
        self.haptic_calls.append((left, right))
        self.device_events.append(f"haptics={left}/{right}")
        self.current_haptics = (left, right)
        return left, right

    def get_haptic_levels(self) -> tuple[int, int]:
        return self.current_haptics

    def get_onboard_profile_state(self) -> tuple[bool, int | None]:
        return self.current_profile_enabled, self.current_profile_sector

    def set_onboard_profile_state(
        self,
        *,
        enabled: bool,
        active_sector: int | None = None,
    ) -> tuple[bool, int | None]:
        self.current_profile_enabled = bool(enabled)
        self.current_profile_sector = int(active_sector) if enabled and active_sector is not None else None
        self.device_events.append(
            f"profile={self.current_profile_sector}" if enabled else "profile=host"
        )
        return self.current_profile_enabled, self.current_profile_sector

    def read_next(self, timeout_s: float = 0.1):
        _ = timeout_s
        if self.rows:
            return self.rows.pop(0)
        time.sleep(0.002)
        return None


class _TimeoutThenSession(_FakeSession):
    def __init__(self, failures: int) -> None:
        super().__init__([])
        self.failures = failures

    def enable_pressure_stream(self, mode: int, mode_arg: int) -> None:
        super().enable_pressure_stream(mode, mode_arg)
        if self.enable_calls <= self.failures:
            raise TimeoutError("device did not answer")


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


class _DeferredArmEmitter(_FakeEmitter):
    def __init__(self, config, log) -> None:
        super().__init__(config, log)
        self.startup_events: list[str] = []

    def open_unarmed(self) -> None:
        self.open_calls += 1
        self.startup_events.append("pen_open")

    def arm_input(self) -> None:
        self.startup_events.append("input_armed")

    def update(self, *args, **kwargs) -> None:
        self.startup_events.append("pressure_update")
        super().update(*args, **kwargs)


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

    async def test_start_retries_transient_pressure_device_timeout(self) -> None:
        session = _TimeoutThenSession(failures=2)
        service, _, _holder = self._service(session)

        await service.start_stream()

        self.assertTrue(service.stream_active)
        self.assertEqual(session.open_calls, 3)
        self.assertEqual(session.enable_calls, 3)
        self.assertEqual(session.close_calls, 2)
        await service.stop_stream()
        self.assertEqual(session.close_calls, 3)

    def test_coordinate_policy_is_backend_specific(self) -> None:
        service, _, _ = self._service(_FakeSession([]))

        service.launch_config.backend = "synthetic"
        self.assertFalse(
            service._emitter_config_from_runtime().allow_raw_direct_motion  # noqa: SLF001
        )

        service.launch_config.backend = "vmulti"
        self.assertTrue(
            service._emitter_config_from_runtime().allow_raw_direct_motion  # noqa: SLF001
        )

    def test_restore_defaults_replaces_saved_configuration(self) -> None:
        service, store, _ = self._service(_FakeSession([]))
        service.apply_config(
            {
                "linked": True,
                "debug_mode": True,
                "minimize_to_tray": False,
                "app_profiles": {"krita.exe": "custom"},
            }
        )
        defaults = RuntimeConfig(
            session_dpi=1200,
            session_haptic_left=4,
            session_haptic_right=4,
        )

        restored = service.restore_defaults(defaults)

        self.assertFalse(restored.linked)
        self.assertTrue(restored.debug_mode)
        self.assertTrue(restored.minimize_to_tray)
        self.assertEqual(restored.session_dpi, 1200)
        self.assertEqual(restored.app_profiles, {})
        self.assertEqual(store.current, restored)

    async def test_start_primes_pressure_before_arming_button_suppression(self) -> None:
        base = time.perf_counter()
        session = _FakeSession([(base, _frame(100, 100))])
        service, _, holder = self._service(session)

        def make_deferred_emitter(cfg, log):
            emitter = _DeferredArmEmitter(cfg, log)
            holder["emitter"] = emitter
            return emitter

        service._emitter_factory = make_deferred_emitter

        await service.start_stream()

        emitter = holder["emitter"]
        self.assertEqual(
            emitter.startup_events[:3],
            ["pen_open", "pressure_update", "input_armed"],
        )
        self.assertIsNotNone(service._latest_emission_sample)
        await service.stop_stream()

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
                    "stationary_pressure_updates": True,
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
        self.assertTrue(emitter_config.right_stationary_pressure_updates)

        await service.stop_stream()

    async def test_auxiliary_right_pressure_mode_is_applied_on_next_start(self) -> None:
        session = _FakeSession([])
        service, store, holder = self._service(session)

        updated = service.apply_config(
            {
                "left_enabled": True,
                "right_enabled": True,
                "rmb_aux_xtilt": True,
            }
        )
        await service.start_stream()

        self.assertTrue(updated.rmb_aux_xtilt)
        self.assertTrue(store.current.rmb_aux_xtilt)
        self.assertTrue(holder["emitter"].config.rmb_aux_xtilt)
        self.assertTrue(holder["emitter"].config.suppress_rmb)

        await service.stop_stream()

    async def test_disabled_pressure_channel_emits_zero_and_does_not_suppress(self) -> None:
        base = time.perf_counter()
        session = _FakeSession([(base + 0.01, _frame(600, 620))])
        service, store, holder = self._service(session)
        service.apply_config(
            {
                "left_enabled": False,
                "right_enabled": True,
                "suppress_lmb": True,
                "suppress_rmb": True,
            }
        )

        await service.start_stream()
        await asyncio.sleep(0.03)

        self.assertFalse(store.current.left_enabled)
        self.assertTrue(store.current.right_enabled)
        self.assertFalse(holder["emitter"].config.suppress_lmb)
        self.assertTrue(holder["emitter"].config.suppress_rmb)
        self.assertTrue(holder["emitter"].updates)
        self.assertEqual(holder["emitter"].updates[-1][0], 0)
        self.assertGreater(holder["emitter"].updates[-1][1], 0)

        await service.stop_stream()

    async def test_debug_mode_is_applied_on_next_start(self) -> None:
        session = _FakeSession([])
        service, store, holder = self._service(session)

        updated = service.apply_config({"debug_mode": False})
        await service.start_stream()

        self.assertFalse(updated.debug_mode)
        self.assertFalse(store.current.debug_mode)
        self.assertFalse(holder["emitter"].config.debug_mode)

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
        deadline = asyncio.get_running_loop().time() + 0.25
        while len(holder["emitter"].updates) < 3:
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.005)

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

    async def test_session_device_settings_restore_on_stop(self) -> None:
        session = _FakeSession([])
        session.current_dpi = 1200
        session.current_haptics = (4, 2)
        session.current_profile_enabled = True
        session.current_profile_sector = 2
        service, _, _ = self._service(session)

        await service.start_stream(
            device_settings={
                "dpi": 1600,
                "haptic_left": 0,
                "haptic_right": 3,
            }
        )

        self.assertEqual(session.current_dpi, 1600)
        self.assertEqual(session.current_haptics, (0, 3))
        self.assertEqual(
            session.device_events,
            [
                "pressure_enable",
                "pressure_disable",
                "profile=host",
                "dpi=1600",
                "haptics=0/3",
                "pressure_enable",
            ],
        )
        await service.stop_stream()
        self.assertEqual(session.current_dpi, 1200)
        self.assertEqual(session.current_haptics, (4, 2))
        self.assertTrue(session.current_profile_enabled)
        self.assertEqual(session.current_profile_sector, 2)
        self.assertEqual(session.dpi_calls, [1600, 1200])
        self.assertEqual(session.haptic_calls, [(0, 3), (4, 2)])
        self.assertEqual(
            session.device_events[-4:],
            ["pressure_disable", "dpi=1200", "haptics=4/2", "profile=2"],
        )

    async def test_session_skips_redundant_device_writes(self) -> None:
        session = _FakeSession([])
        session.current_dpi = 800
        session.current_haptics = (5, 5)
        service, _, _ = self._service(session)

        await service.start_stream(
            device_settings={
                "dpi": 800,
                "haptic_left": 5,
                "haptic_right": 5,
            }
        )
        await service.stop_stream()

        self.assertEqual(session.dpi_calls, [])
        self.assertEqual(session.haptic_calls, [])

    async def test_device_settings_detect_before_start(self) -> None:
        session = _FakeSession([])
        session.current_dpi = 2400
        session.current_haptics = (1, 4)
        service, _, _ = self._service(session)

        detected = await service.detect_device_settings()

        self.assertEqual(
            detected,
            {"dpi": 2400, "haptic_left": 1, "haptic_right": 4},
        )
        self.assertEqual(session.open_calls, 1)
        self.assertEqual(session.close_calls, 1)
        self.assertEqual(session.dpi_calls, [])
        self.assertEqual(session.haptic_calls, [])

    async def test_session_device_settings_restore_when_startup_fails(self) -> None:
        session = _FakeSession([])
        session.current_dpi = 1200
        session.current_haptics = (4, 2)
        service, _, _ = self._service(session)

        def fail_emitter(_config, _log):
            raise RuntimeError("synthetic pen unavailable")

        service._emitter_factory = fail_emitter
        with self.assertRaisesRegex(RuntimeError, "synthetic pen unavailable"):
            await service.start_stream(
                device_settings={
                    "dpi": 1600,
                    "haptic_left": 0,
                    "haptic_right": 3,
                }
            )

        self.assertEqual(session.current_dpi, 1200)
        self.assertEqual(session.current_haptics, (4, 2))
        self.assertEqual(session.close_calls, 1)

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
