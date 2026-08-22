# Compatibility and support scope

## Supported release target

- Windows 10 x64, currently tested on build 19045
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

## Beta limitations

- Windows 11 x64 is expected to work but has not yet been tested on physical
  hardware. Windows on ARM is also unverified.
- The installer is not Authenticode-signed and may show a Microsoft Defender
  SmartScreen warning. Mouse Pressure does not install a kernel driver or ask
  users to disable Windows security features.
- The first stroke after starting output can occasionally register as a dot.
  Stop and Start are not required; the following strokes normally work.
- Pressure behavior can differ between Windows Ink applications and brush
  presets.
- Anti-cheat compatibility is untested. Mouse Pressure uses a low-level mouse
  hook and synthetic Windows pointer input. Stop it before launching a game
  protected by anti-cheat software.

Additional mice, remote desktop sessions, virtual machines, and drawing
applications not named in a release's test matrix should be described as
unverified unless separately tested.
