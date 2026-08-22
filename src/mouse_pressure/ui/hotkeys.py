"""Validated global keyboard shortcuts shared by Qt and Win32 adapters."""

from __future__ import annotations

from dataclasses import dataclass

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004

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


def parse_global_hotkey(value: str) -> GlobalHotkey:
    """Parse one modifier-plus-key shortcut accepted by RegisterHotKey."""
    return _parse_hotkey(value, require_modifier=True)


def parse_hold_hotkey(value: str) -> GlobalHotkey:
    """Parse one key or key chord that can be sampled while clicking."""
    mouse_buttons = {
        "middle click": ("Middle click", 0x04),
        "mouse 4": ("Mouse 4", 0x05),
        "mouse 5": ("Mouse 5", 0x06),
    }
    mouse_button = mouse_buttons.get(str(value).strip().casefold())
    if mouse_button is not None:
        label, virtual_key = mouse_button
        return GlobalHotkey(label=label, modifiers=0, virtual_key=virtual_key)
    return _parse_hotkey(value, require_modifier=False)


def _parse_hotkey(value: str, *, require_modifier: bool) -> GlobalHotkey:
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

    key_label, virtual_key = _parse_key(parts[-1])
    ordered_modifiers = [name for name in ("Ctrl", "Alt", "Shift") if name in labels]
    return GlobalHotkey(
        label="+".join((*ordered_modifiers, key_label)),
        modifiers=modifiers,
        virtual_key=virtual_key,
    )


def _parse_key(value: str) -> tuple[str, int]:
    key = value.strip().upper()
    if len(key) == 1 and ("A" <= key <= "Z" or "0" <= key <= "9"):
        return key, ord(key)
    if key.startswith("F") and key[1:].isdigit():
        number = int(key[1:])
        if 1 <= number <= 24:
            return f"F{number}", 0x70 + number - 1
    raise ValueError("Shortcut key must be A-Z, 0-9, or F1-F24")
