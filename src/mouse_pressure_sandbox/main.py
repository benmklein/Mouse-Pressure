"""Executable entry point for the bundled sandbox."""

from mouse_pressure_sandbox.game import PressureSandbox


def main() -> int:
    PressureSandbox().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
