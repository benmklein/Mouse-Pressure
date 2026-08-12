from __future__ import annotations

from pathlib import Path

from mouse_pressure.ui.qt_theme import DARK, LIGHT, stylesheet, theme_for


def test_theme_selection_is_stable() -> None:
    assert theme_for("light") is LIGHT
    assert theme_for("dark") is DARK
    assert theme_for("unexpected") is LIGHT


def test_qt_stylesheet_uses_theme_tokens_and_windows_font() -> None:
    light = stylesheet(LIGHT)
    dark = stylesheet(DARK)

    assert 'font-family: "Segoe UI"' in light
    assert LIGHT.window in light
    assert LIGHT.accent in light
    assert DARK.window in dark
    assert DARK.terminal_text in dark
    assert "QPushButton#channelSegment:checked" in dark
    assert "QComboBox::down-arrow" in dark
    assert Path("src/mouse_pressure/assets/chevron-down.svg").is_file()
