from __future__ import annotations

import os
from pathlib import Path

import pytest

import mouse_pressure.bridge.native_synthetic as native_synthetic_module
from mouse_pressure.bridge.native_synthetic import (
    NativeSyntheticPenInjector,
    NativeTransformedMouseCapture,
    find_native_relay,
)
from mouse_pressure.bridge.synthetic_pen import POINTER_FLAG_PRIMARY


def test_native_relay_override_is_preferred(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    relay = tmp_path / "relay.dll"
    relay.write_bytes(b"test")
    monkeypatch.setenv("MOUSE_PRESSURE_NATIVE_RELAY", str(relay))
    assert find_native_relay() == relay


def test_native_synthetic_open_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Relay:
        pass

    class _Library:
        api_version = 3
        path = Path("relay.dll")

        def __init__(self) -> None:
            self.created = 0

        def create_synthetic_relay(self, _frame_interval_us: int) -> _Relay:
            self.created += 1
            return _Relay()

    library = _Library()
    monkeypatch.setattr(
        native_synthetic_module,
        "load_native_relay",
        lambda _path=None: library,
    )
    injector = NativeSyntheticPenInjector(log=lambda _line: None)

    injector.open()
    injector.open()

    assert library.created == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows pointer injection only")
def test_built_native_relay_opens_and_drains() -> None:
    relay = find_native_relay()
    if relay is None:
        pytest.skip("native relay has not been built")
    logs: list[str] = []
    injector = NativeSyntheticPenInjector(log=logs.append, library_path=relay)
    injector.open()
    try:
        x, y = injector.get_cursor_pos()
        ok, error = injector.inject(
            flags=POINTER_FLAG_PRIMARY,  # out of range
            x=x,
            y=y,
            pressure_1024=0,
            tag="test",
        )
        assert ok, error
    finally:
        injector.close()
    assert any("native relay stats" in line for line in logs)


@pytest.mark.skipif(os.name != "nt", reason="Windows pointer injection only")
def test_native_relay_batches_and_reports_delivery() -> None:
    relay = find_native_relay()
    if relay is None:
        pytest.skip("native relay has not been built")
    injector = NativeSyntheticPenInjector(log=lambda _line: None, library_path=relay)
    injector.open()
    try:
        x, y = injector.get_cursor_pos()
        ok, error, tokens = injector.inject_batch(
            [
                {
                    "flags": POINTER_FLAG_PRIMARY,
                    "x": x + offset,
                    "y": y,
                    "pressure_1024": 0,
                    "tilt_x": None,
                }
                for offset in range(3)
            ]
        )
        assert ok, error
        assert len(tokens) == 3
        assert injector.wait_idle(1000)
        middle = injector.collect_delivery_events({tokens[1]}, 1000)
        deliveries = (
            injector.collect_delivery_events({tokens[0]}, 1000)
            + middle
            + injector.collect_delivery_events({tokens[2]}, 1000)
        )
        assert [event["token"] for event in deliveries] == tokens
        assert all(event["success"] for event in deliveries)
        assert all(int(event["qpc_frequency"]) > 0 for event in deliveries)
    finally:
        injector.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows input hooks only")
def test_built_native_transformed_input_capture_opens() -> None:
    relay = find_native_relay()
    if relay is None:
        pytest.skip("native relay has not been built")
    logs: list[str] = []
    capture = NativeTransformedMouseCapture(log=logs.append, library_path=relay)
    capture.open()
    try:
        stats = capture.stats()
        assert stats["dropped"] == 0
        assert stats["last_error"] == 0
        assert isinstance(capture.drain_moves(), list)
    finally:
        capture.close()
    assert any("transformed-input capture active" in line for line in logs)
