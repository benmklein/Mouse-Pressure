# Windows release process

This document describes the first reproducible Windows release path. It does
not authorize redistribution of a third-party VMulti driver.

## Release contents

The installer contains:

- the frozen `SuperstrikePressure.exe` desktop application;
- the independent device-settings restoration watchdog hosted by that same
  executable;
- the Superstrike Raster Ink plugin for the explicitly supported Krita build;
- application and Krita integration uninstall support; and
- VMulti detection with the synthetic Windows Ink backend as the fallback.

The current Krita payload targets **Krita 5.3.3** exactly. A native plugin must
be rebuilt and tested before another Krita version is added to the installer.

## Local build

Install release dependencies into the project environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,release]"
```

Build and test the standalone application without requiring Inno Setup:

```powershell
.\scripts\build_windows.ps1 -SkipInstaller
```

After installing Inno Setup, build the application and the unified installer:

```powershell
.\scripts\build_windows.ps1
```

Outputs:

- `dist/windows/SuperstrikePressure/` — one-folder application
- `dist/installer/SuperstrikePressure-<version>-Setup.exe` — installer

Use `-SkipKritaPlugin` only for a developer smoke build. A public build must
contain `dist/krita/5.3.3/kritatoolsuperstrikeink.dll` and its matching action
and icon files.

## Release checklist

1. Confirm `pyproject.toml` and `superstrike_pressure.__version__` match.
2. Run the complete test suite.
3. Build from a clean checkout through GitHub Actions.
4. Install into a clean Windows 10 VM and a clean Windows 11 VM.
5. Test without VMulti, then with the project-owned signed VMulti package when
   one becomes available.
6. Test wired and Lightspeed Superstrike connections on real hardware.
7. Test Start, Stop, Force Stop, forced process termination, sleep/wake, and
   unplug/reconnect while temporary DPI and haptic overrides are active.
8. Test Krita 5.3.3 installation, shortcut customization, upgrade, and uninstall.
9. Authenticode-sign and timestamp the application files and installer.
10. Generate SHA-256 checksums, release notes, and the software bill of
    materials before publishing the GitHub Release.

## Driver gate

Do not place the currently detected Ugee/Huion VMulti package in this
installer. A public driver must have a verified redistribution license, a
project-specific identity, Secure Boot and Memory Integrity testing, and the
appropriate Microsoft signature/certification.

Until that work is complete, the installer reports whether compatible VMulti
hardware is already present and otherwise leaves the application on its
driverless synthetic backend.

## Windows 11 validation

GitHub-hosted Windows jobs validate the Python application and installer on
Windows Server; they do not replace Windows 11 client testing. Keep VM
snapshots for supported Windows 11 releases and recruit at least one hardware
beta tester with Secure Boot and Memory Integrity enabled.

## Signing

Development builds may remain unsigned. Public builds must sign and timestamp
the executable, bundled DLLs where applicable, and final installer. Driver
signing is a separate Microsoft hardware submission process.
