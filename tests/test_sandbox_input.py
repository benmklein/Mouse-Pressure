from __future__ import annotations

from mouse_pressure_sandbox.input import SensorSnapshot


def test_sensor_snapshot_defaults_to_safe_fallback() -> None:
    snapshot = SensorSnapshot()
    assert not snapshot.connected
    assert snapshot.left_pressure == 0.0
    assert snapshot.right_pressure == 0.0
