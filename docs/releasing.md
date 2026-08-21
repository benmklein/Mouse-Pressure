# Windows release checklist

The public release is a driverless, per-user Windows application. It includes
the Mouse Pressure control panel, native Windows Ink relay, and pressure
sandbox. It does not include VMulti or a Krita plugin.

## Build

From a clean checkout with the release dependencies installed:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[release,sandbox]"
.\scripts\build_windows.ps1
```

The build must:

1. pass the complete test suite;
2. compile the native relay;
3. pass the public-artifact privacy scan;
4. generate the CycloneDX SBOM and source revision;
5. package the application and sandbox; and
6. produce the installer and SHA-256 file under `dist/installer`.

The installer lifecycle smoke test performs an install-over-install pass using
the stable application ID. It verifies that stale packaged runtime files are
removed while unrelated files remain intact. Pass the previous release through
`-PreviousInstallerPath` to test a real version upgrade before publishing.

## Manual test matrix

- Clean install, upgrade, and uninstall on Windows 10 x64.
- Clean install, upgrade, and uninstall on Windows 11 x64 before promoting a
  build beyond alpha.
- Wired and supported wireless mouse connections.
- Start, draw, Stop, crash recovery, tray restore, and both global shortcuts.
- Left Pressure / right Pressure, left Pressure / right X-tilt, left Pressure /
  right Y-tilt, left Pressure / right Rotation, left X-tilt / right Pressure,
  left Y-tilt / right Pressure, and left Rotation / right Pressure.
- Sandbox pickup, release, and chain-length control.
- At least one Windows Ink drawing application and Microsoft Paint.
- DPI and haptics restored after Stop and after forced termination recovery.

Record the exact Windows builds, hardware/firmware, and application versions in
the release notes. Do not describe an untested combination as supported.

## Distribution notes

The installer does not require administrator privileges and does not alter
Secure Boot or driver-signature enforcement. Publish the installer, its
`.sha256` file, release notes, known limitations, and links to the source and
privacy documentation together.
