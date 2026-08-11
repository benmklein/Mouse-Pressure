# Compatibility and support scope

## Supported release target

- Windows 11 x64 on versions explicitly listed in the release notes
- A compatible Logitech analog-button mouse connected by supported wired USB
  or Lightspeed transport
- Windows Ink applications; Krita and Microsoft Paint are tested examples

The hardware name is used only to identify compatibility. Mouse Pressure is
independent software and is not affiliated with or endorsed by Logitech, Krita,
Microsoft, or Qt.

## Output

Mouse Pressure uses its native, driverless Windows Ink output. The installer
does not add a kernel driver, require driver-signing changes, or install an
application-specific brush plugin. Behavior can still differ between Windows
Ink applications.

## Explicit non-guarantees

Mouse Pressure does not replace the mouse firmware or Logitech software and
does not guarantee compatibility with future firmware, G HUB, Windows, Krita,
or other application updates. Pressure calibration can vary between physical
devices. Release notes must identify the combinations actually tested.

Windows 10, Windows on ARM, additional mice, remote desktop sessions, virtual
machines, and drawing applications not named in a release's test matrix should
be described as unverified unless separately tested.
