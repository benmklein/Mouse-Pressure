from __future__ import annotations

import asyncio
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from superstrike_pressure.bridge.config import ChannelConfig, RuntimeConfig  # noqa: E402
from superstrike_pressure.web import calibration  # noqa: E402


@dataclass
class _MemoryConfigStore:
    saved: RuntimeConfig | None = None

    def save(self, config: RuntimeConfig) -> None:
        self.saved = config


class _FakeRuntimeService:
    def __init__(self, *, active: bool, samples: list[tuple[int, int]]) -> None:
        self.stream_active = active
        self.samples = list(samples)
        self.start_calls = 0
        self.stop_calls = 0
        self.apply_calls: list[dict] = []
        self.config = RuntimeConfig(
            linked=False,
            left=ChannelConfig(raw_min=80, raw_max=180),
            right=ChannelConfig(raw_min=80, raw_max=180),
        )

    async def start_stream(self) -> None:
        self.start_calls += 1
        self.stream_active = True

    async def stop_stream(self) -> None:
        self.stop_calls += 1
        self.stream_active = False

    async def wait_for_raw_sample(self, timeout_s: float = 1.0) -> tuple[int, int]:
        _ = timeout_s
        if self.samples:
            return self.samples.pop(0)
        await asyncio.sleep(0)
        raise TimeoutError

    def apply_config(self, patch: dict) -> RuntimeConfig:
        self.apply_calls.append(patch)
        if "left" in patch:
            self.config.left.raw_min = patch["left"]["raw_min"]
            self.config.left.raw_max = patch["left"]["raw_max"]
        if "right" in patch:
            self.config.right.raw_min = patch["right"]["raw_min"]
            self.config.right.raw_max = patch["right"]["raw_max"]
        return self.config

    def get_config(self) -> RuntimeConfig:
        return self.config


class CalibrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._old_phase_duration = calibration.PHASE_DURATION_S
        self._old_progress_interval = calibration.PROGRESS_INTERVAL_S
        self._old_settle = calibration.CALIBRATION_SETTLE_S
        self._old_countdown = calibration.PHASE_COUNTDOWN_S
        calibration.PHASE_DURATION_S = 0.03
        calibration.PROGRESS_INTERVAL_S = 0.01
        calibration.CALIBRATION_SETTLE_S = 0.0
        calibration.PHASE_COUNTDOWN_S = 0

    async def asyncTearDown(self) -> None:
        calibration.PHASE_DURATION_S = self._old_phase_duration
        calibration.PROGRESS_INTERVAL_S = self._old_progress_interval
        calibration.CALIBRATION_SETTLE_S = self._old_settle
        calibration.PHASE_COUNTDOWN_S = self._old_countdown

    async def test_calibration_starts_and_stops_stream_when_inactive(self) -> None:
        runtime = _FakeRuntimeService(
            active=False,
            samples=[(81, 90), (79, 91), (120, 150), (160, 170), (82, 92), (140, 166)] * 8,
        )
        store = _MemoryConfigStore()
        events: list[dict] = []

        result = await calibration.run_calibration("both", runtime, events.append, store)

        self.assertEqual(runtime.start_calls, 1)
        self.assertEqual(runtime.stop_calls, 1)
        self.assertIn("left", result)
        self.assertIn("right", result)
        self.assertLessEqual(result["left"]["raw_min"], result["left"]["raw_max"])
        self.assertLessEqual(result["right"]["raw_min"], result["right"]["raw_max"])
        self.assertEqual(len(runtime.apply_calls), 1)
        self.assertIsNotNone(store.saved)

        phase_names = {e["phase"] for e in events if e.get("event") == "calibrate.progress"}
        self.assertTrue({"idle", "light", "heavy", "done"}.issubset(phase_names))

    async def test_calibration_preserves_existing_stream_state(self) -> None:
        runtime = _FakeRuntimeService(active=True, samples=[(80, 90), (110, 140), (170, 180)] * 8)
        store = _MemoryConfigStore()
        events: list[dict] = []

        result = await calibration.run_calibration("left", runtime, events.append, store)

        self.assertEqual(runtime.start_calls, 0)
        self.assertEqual(runtime.stop_calls, 0)
        self.assertIn("left", result)
        self.assertEqual(list(result.keys()), ["left"])
        self.assertEqual(runtime.apply_calls[0]["left"]["raw_min"], result["left"]["raw_min"])

    def test_phase_range_uses_light_press_above_idle_noise(self) -> None:
        raw_min, raw_max = calibration._calibrated_range(  # noqa: SLF001
            {
                "idle": [378, 379, 380, 380, 381] * 4 + [999],
                "light": [430, 440, 450],
                "heavy": [620, 625, 630, 632, 635] * 4 + [1000],
            },
            fallback_min=80,
            fallback_max=180,
        )

        self.assertEqual(raw_min, 440)
        self.assertEqual(raw_max, 635)

    async def test_countdown_announces_each_second_before_sampling(self) -> None:
        events: list[dict] = []
        calibration.PHASE_COUNTDOWN_S = 3

        with patch.object(calibration.asyncio, "sleep", new=AsyncMock()) as sleep:
            await calibration._countdown_before_phase(  # noqa: SLF001
                channel="left",
                phase="light",
                progress_cb=events.append,
            )

        self.assertEqual([event["countdown"] for event in events], [3, 2, 1])
        self.assertTrue(all(event["next_phase"] == "light" for event in events))
        self.assertEqual(sleep.await_count, 3)


if __name__ == "__main__":
    unittest.main()
