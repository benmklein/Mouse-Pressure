from __future__ import annotations

import pytest

from mouse_pressure.ui.hotkeys import parse_global_hotkey, parse_hold_hotkey


def test_parse_global_hotkey_supports_default_bindings() -> None:
    start = parse_global_hotkey("Ctrl+F12")
    stop = parse_global_hotkey("Ctrl+Shift+F12")

    assert start.label == "Ctrl+F12"
    assert start.modifiers == 0x0002
    assert start.virtual_key == 0x7B
    assert stop.label == "Ctrl+Shift+F12"
    assert stop.modifiers == 0x0002 | 0x0004
    assert stop.virtual_key == 0x7B


def test_parse_hold_hotkey_supports_single_keys_and_chords() -> None:
    assert parse_hold_hotkey("F11").label == "F11"
    assert parse_hold_hotkey("Ctrl+F11").label == "Ctrl+F11"
    assert parse_hold_hotkey("Mouse 4").virtual_key == 0x05
    assert parse_hold_hotkey("Mouse 5").virtual_key == 0x06


@pytest.mark.parametrize("value", ["", "F12", "Ctrl", "Ctrl+NotAKey"])
def test_parse_global_hotkey_rejects_unsafe_or_invalid_bindings(value: str) -> None:
    with pytest.raises(ValueError):
        parse_global_hotkey(value)
