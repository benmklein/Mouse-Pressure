from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

from mouse_pressure.dev_ui import BridgeController, stroke_analysis_data


class _FakeRuntimeService:
    def __init__(self) -> None:
        self.stream_active = False
        self.start_calls = 0
        self.stop_calls = 0
        self.started_with: dict[str, int] | None = None

    async def start_stream(
        self,
        *,
        device_settings: dict[str, int] | None = None,
    ) -> None:
        self.start_calls += 1
        self.started_with = device_settings
        self.stream_active = True

    async def stop_stream(self) -> None:
        self.stop_calls += 1
        self.stream_active = False


class _StalledRuntimeService(_FakeRuntimeService):
    def __init__(self) -> None:
        super().__init__()
        self.stream_active = True

    async def stop_stream(self) -> None:
        self.stop_calls += 1
        await asyncio.Event().wait()


def test_stroke_analysis_exposes_pipeline_and_low_latency_pressure_steps() -> None:
    payload = {
        "metadata": {
            "button": "left",
            "configured_curve": "s_curve",
            "configured_curve_strength": 3.0,
            "true_low_latency": True,
        },
        "events": [
            {
                "kind": "update",
                "t_ms": 0.0,
                "pressure_fresh": True,
                "left_raw": 400,
                "mapped": 100,
                "actual_pressure": 100,
            },
            {
                "kind": "inject",
                "t_ms": 0.1,
                "x": 0,
                "y": 0,
                "pressure": 100,
                "flags": 4,
                "ok": True,
            },
            {
                "kind": "update",
                "t_ms": 16.0,
                "pressure_fresh": True,
                "left_raw": 475,
                "mapped": 700,
                "actual_pressure": 700,
            },
            {
                "kind": "inject",
                "t_ms": 16.1,
                "x": 20,
                "y": 0,
                "pressure": 700,
                "flags": 4,
                "ok": True,
            },
        ],
    }

    result = stroke_analysis_data(payload)

    assert result["raw"] == [(0.0, 400.0), (16.0, 475.0)]
    assert result["mapped"] == [(0.0, 100.0), (16.0, 700.0)]
    assert result["path_px"] == 20.0
    assert result["max_pressure_step"] == 600.0
    assert "True low latency" in result["diagnosis"]


def test_bridge_controller_starts_and_stops_runtime() -> None:
    service = _FakeRuntimeService()
    controller = BridgeController(service)  # type: ignore[arg-type]
    try:
        controller.start().result(timeout=1.0)
        assert service.stream_active is True
        controller.stop().result(timeout=1.0)
        assert service.stream_active is False
        assert service.start_calls == 1
        assert service.stop_calls == 1
    finally:
        controller.close()


def test_bridge_controller_closes_active_runtime() -> None:
    service = _FakeRuntimeService()
    controller = BridgeController(service)  # type: ignore[arg-type]
    controller.start().result(timeout=1.0)

    controller.close()

    assert service.stream_active is False
    assert service.stop_calls == 1


def test_bridge_controller_passes_session_device_settings() -> None:
    service = _FakeRuntimeService()
    controller = BridgeController(service)  # type: ignore[arg-type]
    settings = {"dpi": 1600, "haptic_left": 0, "haptic_right": 3}
    try:
        controller.start(device_settings=settings).result(timeout=1.0)
        assert service.started_with == settings
    finally:
        controller.close()


def test_bridge_controller_close_is_bounded_and_idempotent() -> None:
    service = _StalledRuntimeService()
    controller = BridgeController(service)  # type: ignore[arg-type]

    started = time.monotonic()
    assert controller.close(timeout=0.1) is False
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert service.stop_calls == 1
    controller.close(timeout=0.1)
    assert service.stop_calls == 1


def test_bridge_controller_routes_calibration_on_runtime_loop() -> None:
    service = _FakeRuntimeService()
    controller = BridgeController(service)  # type: ignore[arg-type]
    progress_events: list[dict] = []
    result = {"left": {"raw_min": 410, "raw_max": 690}}

    try:
        with patch(
            "mouse_pressure.dev_ui.run_calibration",
            new=AsyncMock(return_value=result),
        ) as calibrate:
            future = controller.calibrate(
                "left",
                config_store=object(),  # type: ignore[arg-type]
                progress_cb=progress_events.append,
            )

            assert future.result(timeout=1.0) == result
            assert calibrate.await_args.args[0] == "left"
            assert calibrate.await_args.args[1] is service
    finally:
        controller.close()
