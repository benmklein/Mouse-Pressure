"""Validated global keyboard shortcuts shared by Qt and Win32 adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004

_MOUSE_VIRTUAL_KEYS = frozenset({0x04, 0x05, 0x06})
_MODIFIER_VIRTUAL_KEYS = (
    (MOD_CONTROL, 0x11),
    (MOD_ALT, 0x12),
    (MOD_SHIFT, 0x10),
)

_MODIFIERS = {
    "ctrl": ("Ctrl", MOD_CONTROL),
    "control": ("Ctrl", MOD_CONTROL),
    "alt": ("Alt", MOD_ALT),
    "shift": ("Shift", MOD_SHIFT),
}


@dataclass(frozen=True)
class GlobalHotkey:
    label: str
    modifiers: int
    virtual_key: int


def is_mouse_hotkey(hotkey: GlobalHotkey) -> bool:
    return hotkey.virtual_key in _MOUSE_VIRTUAL_KEYS


def hotkey_is_down(
    hotkey: GlobalHotkey,
    get_key_state: Callable[[int], int],
) -> bool:
    """Return whether an exact keyboard or mouse chord is physically held."""
    for modifier, virtual_key in _MODIFIER_VIRTUAL_KEYS:
        required = bool(hotkey.modifiers & modifier)
        pressed = bool(int(get_key_state(virtual_key)) & 0x8000)
        if pressed != required:
            return False
    return bool(int(get_key_state(hotkey.virtual_key)) & 0x8000)


def parse_global_hotkey(value: str) -> GlobalHotkey:
    """Parse one modifier-plus-key shortcut accepted by RegisterHotKey."""
    return _parse_hotkey(value, require_modifier=True, allow_mouse=True)


def parse_hold_hotkey(value: str) -> GlobalHotkey:
    """Parse one key or key chord that can be sampled while clicking."""
    return _parse_hotkey(value, require_modifier=False, allow_mouse=True)


def _parse_hotkey(
    value: str,
    *,
    require_modifier: bool,
    allow_mouse: bool,
) -> GlobalHotkey:
    parts = [part.strip() for part in str(value).split("+") if part.strip()]
    minimum_parts = 2 if require_modifier else 1
    if len(parts) < minimum_parts:
        if require_modifier:
            raise ValueError("Shortcut must include Ctrl, Alt, or Shift plus one key")
        raise ValueError("Shortcut must include one key")

    modifiers = 0
    labels: set[str] = set()
    for part in parts[:-1]:
        modifier = _MODIFIERS.get(part.casefold())
        if modifier is None:
            raise ValueError(f"Unknown shortcut modifier {part!r}")
        label, flag = modifier
        if label in labels:
            raise ValueError(f"Duplicate shortcut modifier {label}")
        labels.add(label)
        modifiers |= flag
    if require_modifier and not modifiers:
        raise ValueError("Shortcut must include Ctrl, Alt, or Shift")

    key_label, virtual_key = _parse_key(parts[-1], allow_mouse=allow_mouse)
    ordered_modifiers = [name for name in ("Ctrl", "Alt", "Shift") if name in labels]
    return GlobalHotkey(
        label="+".join((*ordered_modifiers, key_label)),
        modifiers=modifiers,
        virtual_key=virtual_key,
    )


def _parse_key(value: str, *, allow_mouse: bool = False) -> tuple[str, int]:
    if allow_mouse:
        mouse_buttons = {
            "middle click": ("Middle click", 0x04),
            "mouse 4": ("Mouse 4", 0x05),
            "mouse 5": ("Mouse 5", 0x06),
        }
        mouse_button = mouse_buttons.get(value.strip().casefold())
        if mouse_button is not None:
            return mouse_button
    key = value.strip().upper()
    if len(key) == 1 and ("A" <= key <= "Z" or "0" <= key <= "9"):
        return key, ord(key)
    if key.startswith("F") and key[1:].isdigit():
        number = int(key[1:])
        if 1 <= number <= 24:
            return f"F{number}", 0x70 + number - 1
    allowed = "A-Z, 0-9, F1-F24, or a supported mouse button" if allow_mouse else "A-Z, 0-9, or F1-F24"
    raise ValueError(f"Shortcut key must be {allowed}")
