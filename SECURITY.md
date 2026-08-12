# Security policy

## Supported releases

Security fixes are provided for the newest published Mouse Pressure release.
Development snapshots are not supported for ordinary end-user installation.

## Reporting a vulnerability

Do not post an unpatched vulnerability, private diagnostic capture, certificate
material, or device identifier in a public issue. Contact the maintainer through
the private security-reporting feature on the GitHub repository:

https://github.com/benmklein/analog_mouse_pressure/security/advisories/new

Include the application version, Windows version, output backend, reproduction
steps, and whether Secure Boot and Memory Integrity are enabled. Redact HID
instance paths, usernames, stroke traces, and unrelated application data.

## Application trust boundary

Mouse Pressure uses the Windows synthetic-pointer API and does not install a
kernel driver. The application reads compatible HID interfaces and emits
pressure or tilt through Windows Ink. It must not download or execute driver
payloads, disable Windows security features, or expose its local developer
interfaces beyond the loopback address.

## Recovery

The application uses fail-open click suppression, a Force Stop shortcut, and an
independent settings-restoration watchdog. See [docs/recovery.md](docs/recovery.md)
for user recovery steps.
