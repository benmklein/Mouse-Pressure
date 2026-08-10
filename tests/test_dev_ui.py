from __future__ import annotations

import asyncio
import time

from superstrike_pressure.dev_ui import (
    BridgeController,
    effective_pressure_for_raw,
    parse_dev_settings,
    sensitivity_mapping_points,
    stroke_analysis_data,
)


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


def test_dev_settings_build_linked_runtime_patch() -> None:
    settings = parse_dev_settings(
        raw_min="80",
        raw_max="170",
        deadzone="5",
        curve="scurve",
        curve_strength="1.5",
        contact_preset="medium",
        suppress_lmb=True,
        release_teardown=True,
    )

    patch = settings.as_runtime_patch()
    assert patch["linked"] is True
    assert patch["suppress_lmb"] is True
    assert patch["release_teardown"] is True
    assert patch["left"]["deadzone_low"] == 5
    assert patch["left"]["deadzone_high"] == 5
    assert patch["left"]["pressure_floor"] == 12
    assert patch["left"]["path_stabilization"] == 0
    assert patch["left"]["pressure_influence"] == 85
    assert patch["left"]["onset_buffer"] is False
    assert patch["left"]["true_low_latency"] is False
    assert patch["left"]["stationary_pressure_updates"] is False
    assert settings.injection_hz == 240.0


def test_dev_settings_reject_invalid_raw_range() -> None:
    try:
        parse_dev_settings(
            raw_min="180",
            raw_max="170",
            deadzone="0",
            curve="linear",
            curve_strength="1.0",
            contact_preset="medium",
            suppress_lmb=False,
            release_teardown=False,
        )
    except ValueError as exc:
        assert "minimum" in str(exc)
    else:
        raise AssertionError("Expected invalid raw range to raise")


def test_dev_settings_accept_curve_strength_three() -> None:
    settings = parse_dev_settings(
        raw_min="320",
        raw_max="670",
        deadzone="0",
        curve="soft",
        curve_strength="3.0",
        contact_preset="medium",
        suppress_lmb=True,
        release_teardown=False,
    )

    assert settings.curve_strength == 3.0


def test_dev_settings_persist_true_low_latency() -> None:
    settings = parse_dev_settings(
        raw_min="320",
        raw_max="670",
        deadzone="0",
        curve="linear",
        curve_strength="1.0",
        contact_preset="medium",
        suppress_lmb=True,
        release_teardown=False,
        onset_buffer=True,
        true_low_latency=True,
    )

    assert settings.true_low_latency is True
    assert settings.as_runtime_patch()["left"]["true_low_latency"] is True


def test_dev_settings_persist_stationary_pressure_updates() -> None:
    settings = parse_dev_settings(
        raw_min="320",
        raw_max="670",
        deadzone="0",
        curve="linear",
        curve_strength="1.0",
        contact_preset="medium",
        suppress_lmb=True,
        release_teardown=False,
        stationary_pressure_updates=True,
    )

    assert settings.stationary_pressure_updates is True
    assert settings.as_runtime_patch()["left"]["stationary_pressure_updates"] is True


def test_sensitivity_mapping_visualizer_uses_effective_pressure_settings() -> None:
    settings = parse_dev_settings(
        raw_min="300",
        raw_max="700",
        deadzone="0",
        curve="linear",
        curve_strength="1.0",
        contact_preset="medium",
        suppress_lmb=True,
        release_teardown=False,
        pressure_floor="15",
        pressure_influence="100",
    )

    points = sensitivity_mapping_points(settings, samples=3)

    assert points[0] == (300, 0)
    assert points[1][0] == 500
    assert 510 <= points[1][1] <= 513
    assert points[2] == (700, 1023)
    assert effective_pressure_for_raw(settings, 301) >= round(15 * 1024 / 100)


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
