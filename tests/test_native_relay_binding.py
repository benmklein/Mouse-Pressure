from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Any

import pytest

from mouse_pressure.bridge import native_relay_binding
from mouse_pressure.bridge.native_relay_binding import (
    NATIVE_RELAY_API_VERSION,
    NativeInputMove,
    NativeInputStats,
    NativeRelayCompletion,
    NativeRelayInput,
    NativeRelayStats,
    load_native_relay,
)


class _FakeFunction:
    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.argtypes: list[Any] | None = None
        self.restype: Any = None
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        return self.result


class _FakeDll:
    def __init__(self, api_version: int = NATIVE_RELAY_API_VERSION) -> None:
        for name in (
            "mp_synth_api_version",
            "mp_input_create",
            "mp_input_drain_moves",
            "mp_input_get_stats",
            "mp_input_destroy",
            "mp_synth_create",
            "mp_synth_submit",
            "mp_synth_submit_batch",
            "mp_synth_drain_completions",
            "mp_synth_wait_idle",
            "mp_synth_get_stats",
            "mp_synth_destroy",
        ):
            setattr(self, name, _FakeFunction())
        self.mp_synth_api_version.result = api_version


def test_load_native_relay_declares_and_validates_every_export(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dll = _FakeDll()
    loaded_paths: list[str] = []

    def fake_load(path: str, *, use_last_error: bool) -> _FakeDll:
        loaded_paths.append(path)
        assert use_last_error
        return dll

    monkeypatch.setattr(ctypes, "WinDLL", fake_load)
    path = tmp_path / "relay.dll"

    library = load_native_relay(path)

    assert library.dll is dll
    assert library.path == path
    assert library.api_version == NATIVE_RELAY_API_VERSION
    assert loaded_paths == [str(path)]
    for name, value in vars(dll).items():
        assert isinstance(value, _FakeFunction), name
        assert value.argtypes is not None, name
        assert value.restype is not None or name.endswith("destroy"), name


def test_load_native_relay_rejects_an_incompatible_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: _FakeDll(api_version=NATIVE_RELAY_API_VERSION + 1),
    )

    with pytest.raises(RuntimeError, match="incompatible"):
        load_native_relay(tmp_path / "relay.dll")


def test_load_native_relay_reports_every_discovery_location(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.dll"
    second = tmp_path / "second.dll"
    monkeypatch.setattr(native_relay_binding, "find_native_relay", lambda: None)
    monkeypatch.setattr(
        native_relay_binding,
        "native_relay_path_candidates",
        lambda: [first, second],
    )

    with pytest.raises(RuntimeError) as error:
        load_native_relay()

    assert str(first) in str(error.value)
    assert str(second) in str(error.value)


def test_native_relay_structure_layout_sizes_match_the_cpp_abi() -> None:
    assert ctypes.sizeof(NativeRelayStats) == 96
    assert ctypes.sizeof(NativeRelayInput) == 40
    assert ctypes.sizeof(NativeRelayCompletion) == 72
    assert ctypes.sizeof(NativeInputMove) == 32
    assert ctypes.sizeof(NativeInputStats) == 48


def test_library_owns_input_and_relay_handle_lifecycles() -> None:
    dll = _FakeDll()
    dll.mp_input_create.result = 101
    dll.mp_synth_create.result = 202
    library = native_relay_binding.NativeRelayLibrary(
        dll=dll,
        path=Path("relay.dll"),
        api_version=NATIVE_RELAY_API_VERSION,
    )

    capture = library.create_input_capture()
    relay = library.create_synthetic_relay(120)
    capture.close()
    capture.close()
    relay.close()
    relay.close()

    assert len(dll.mp_input_create.calls) == 1
    assert len(dll.mp_input_destroy.calls) == 1
    assert len(dll.mp_synth_create.calls) == 1
    assert len(dll.mp_synth_destroy.calls) == 1
