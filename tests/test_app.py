from __future__ import annotations

import sys
import types

from superstrike_pressure import app


def test_main_dispatches_frozen_watchdog(monkeypatch) -> None:
    watchdog = types.ModuleType(
        "superstrike_pressure.web.device_restore_watchdog"
    )
    watchdog.main = lambda args: 7  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules,
        "superstrike_pressure.web.device_restore_watchdog",
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
    desktop = types.ModuleType("superstrike_pressure.dev_ui")
    desktop.main = lambda: 3  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "superstrike_pressure.dev_ui", desktop)

    assert app.main([]) == 3
