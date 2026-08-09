from __future__ import annotations

from superstrike_pressure.dev_ui import (
    BridgeController,
    effective_pressure_for_raw,
    parse_dev_settings,
    sensitivity_mapping_points,
)


class _FakeRuntimeService:
    def __init__(self) -> None:
        self.stream_active = False
        self.start_calls = 0
        self.stop_calls = 0

    async def start_stream(self) -> None:
        self.start_calls += 1
        self.stream_active = True

    async def stop_stream(self) -> None:
        self.stop_calls += 1
        self.stream_active = False


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
