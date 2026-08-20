# Privacy

Mouse Pressure is designed to operate locally. The official desktop application
does not contain advertising, analytics, an update tracker, or a cloud service,
and it does not transmit pressure, pointer, or device data to the publisher.

## Data processed while running

To produce virtual pen input, Mouse Pressure observes:

- pressure and button reports from a compatible mouse;
- raw mouse movement and button state needed to correlate physical movement;
- the current desktop pointer position; and
- temporary DPI and haptic values when the user asks the application to change
  those settings while mapping is active.

It does not record keyboard text, inspect document contents, or expose a
network service.

## Data stored locally

By default, persistent files are stored in `%USERPROFILE%\.mouse-pressure`:

- `config.json` contains user-selected settings;
- short-lived `device_restore_*.json` files allow the independent watchdog to
  restore DPI and haptics after an unexpected exit; and
- when Debug mode is enabled, `stroke_traces/stroke-*.json` contains pointer
  coordinates, timing, button state, raw pressure, and mapped pressure for
  recent strokes.

The in-app log is held in memory unless a developer explicitly supplies a log
file. Developer logs can include HID interface paths, process names, screen
coordinates, timing, and hardware settings. Treat logs and stroke traces as
potentially sensitive when attaching them to an issue.

## Controls and deletion

Debug mode is off by default in release settings. A user can delete all stored
data by stopping Mouse Pressure, uninstalling it, and deleting
`%USERPROFILE%\.mouse-pressure`. Deleting configuration resets saved settings.

Before sharing diagnostics publicly, use `scripts/check_public_artifacts.py`
from the source tree and manually review the result for drawing content or other
context that an automated check cannot recognize.

If a future release adds telemetry, crash uploads, or network services, this
notice and the interface must be updated before that collection is enabled.
