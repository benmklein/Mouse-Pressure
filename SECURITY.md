# Security policy

## Supported releases

Security fixes are provided for the newest published Mouse Pressure release.
Development snapshots and unsigned driver packages are not supported for
ordinary end-user installation.

## Reporting a vulnerability

Do not post an unpatched vulnerability, private diagnostic capture, certificate
material, or device identifier in a public issue. Contact the maintainer through
the private security-reporting feature on the GitHub repository:

https://github.com/benmklein/analog_mouse_pressure/security/advisories/new

Include the application version, Windows version, output backend, reproduction
steps, and whether Secure Boot and Memory Integrity are enabled. Redact HID
instance paths, usernames, stroke traces, and unrelated application data.

## Driver trust boundary

The optional VMulti package is a kernel-mode virtual HID driver. Official
installers must accept only a payload whose hashes match its manifest, whose
catalog and driver have Microsoft production signatures, and whose project-owned
provisioner has a valid Authenticode signature. Unsigned or test-signed drivers
must not be included in a public installer.

The application communicates with the driver through a deliberately limited
vendor HID collection. The virtual device exposes pressure/tilt pen input; it
does not expose the historical VMulti keyboard, joystick, multitouch, or virtual
mouse interfaces.

## Recovery

The application uses fail-open click suppression, a Force Stop shortcut, and an
independent settings-restoration watchdog. See [docs/recovery.md](docs/recovery.md)
for user recovery steps.
