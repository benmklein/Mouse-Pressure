"""Compatibility wrapper for the installed ``ss-pen`` command."""

from superstrike_pressure.bridge.synthetic_pen_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
