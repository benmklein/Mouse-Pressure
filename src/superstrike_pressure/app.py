"""Installed application entry point.

The frozen executable also hosts the small crash-recovery watchdog. Keeping
that dispatch here lets the watchdog relaunch the same signed executable
instead of depending on a separately installed Python interpreter.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence


WATCHDOG_SWITCH = "--device-restore-watchdog"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == WATCHDOG_SWITCH:
        from superstrike_pressure.web.device_restore_watchdog import (
            main as watchdog_main,
        )

        return int(watchdog_main(args[1:]))

    from superstrike_pressure.dev_ui import main as ui_main

    return int(ui_main())


if __name__ == "__main__":
    raise SystemExit(main())
