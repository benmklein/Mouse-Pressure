"""Compatibility wrapper for the installed ``mp-pen`` command."""

from mouse_pressure.bridge.synthetic_pen_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
