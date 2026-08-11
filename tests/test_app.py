from __future__ import annotations

import sys
import types

from mouse_pressure import app


def test_main_dispatches_frozen_watchdog(monkeypatch) -> None:
    watchdog = types.ModuleType(
        "mouse_pressure.web.device_restore_watchdog"
    )
    watchdog.main = lambda args: 7  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules,
        "mouse_pressure.web.device_restore_watchdog",
        watchdog,
    )

    assert app.main(
        [
            app.WATCHDOG_SWITCH,
            "--parent-pid",
            "123",
            "--state-file",
            "state.json",
        ]
    ) == 7


def test_main_starts_desktop_ui(monkeypatch) -> None:
    desktop = types.ModuleType("mouse_pressure.dev_ui")
    desktop.main = lambda: 3  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mouse_pressure.dev_ui", desktop)

    assert app.main([]) == 3
