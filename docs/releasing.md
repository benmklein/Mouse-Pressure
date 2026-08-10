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
5. Test without VMulti, then with the project-owned signed VMulti package.
6. Test wired and Lightspeed Superstrike connections on real hardware.
7. Test Start, Stop, Force Stop, forced process termination, sleep/wake, and
   unplug/reconnect while temporary DPI and haptic overrides are active.
8. Test Krita 5.3.3 installation, shortcut customization, upgrade, and uninstall.
9. Authenticode-sign and timestamp the application files and installer.
10. Generate SHA-256 checksums, release notes, and the software bill of
    materials before publishing the GitHub Release.

## Virtual tablet driver

The compatible driver currently installed on the development PC is the
Microsoft-signed `Pentablet HID 1.1` package (`vmulti.inf`, hardware ID
`pentablet\hid`). The commonly linked `X9VoiD/vmulti-bin` archive contains the
same package plus legacy DIFx, DevCon, WinTab, and KMDF co-installer files. It
does not include a redistribution notice. Do not copy either package into a
Superstrike release.

The release driver lives in the separate
[`benmklein/superstrike-vmulti`](https://github.com/benmklein/superstrike-vmulti)
repository. It derives its report-forwarding design from the MIT-licensed
original VMulti implementation, uses the project-specific hardware ID
`ROOT\SUPERSTRIKEVMULTI`, and exposes only the digitizer and vendor-control
collections we need. Its x64 build uses Visual Studio 2026 plus the pinned
10.0.28000 WDK and ships a project-owned SetupAPI/NewDev provisioner instead of
redistributing DevCon.

The signed release payload contract is defined by
`packaging/windows/vmulti-driver-manifest.schema.json`. Before Inno Setup can
embed a payload, `scripts/validate_vmulti_payload.ps1` verifies:

- the fixed package and hardware identities;
- SHA-256 hashes for every required file;
- Microsoft hardware-dashboard signatures on the INF, catalog, and SYS;
- a valid Authenticode signature on the project-owned provisioner; and
- the absence of legacy/vendor files such as DevCon, DIFx, WinTab, and the old
  KMDF co-installer.

Build the unified installer with a signed payload:

```powershell
.\scripts\build_windows.ps1 -VMultiPayload C:\release\superstrike-vmulti-x64
```

Without `-VMultiPayload`, the normal installer is still produced, detects an
existing compatible device, and uses the driverless synthetic backend as its
fallback. This keeps ordinary CI builds reproducible while making it impossible
to accidentally package a driver copied from the local driver store.

For a public retail driver, use the Windows Hardware Compatibility Program/HLK
path. Microsoft documents new attestation submissions as testing-only rather
than a retail publication path. The driver currently targets Windows 11 build
22000 or newer and must be tested with Secure Boot and Memory Integrity enabled
before it is passed to the application installer. Windows 10 continues to use
the synthetic fallback unless a separately supported driver is added later.

## Windows 11 validation

GitHub-hosted Windows jobs validate the Python application and installer on
Windows Server; they do not replace Windows 11 client testing. Keep VM
snapshots for supported Windows 11 releases and recruit at least one hardware
beta tester with Secure Boot and Memory Integrity enabled.

## Signing

Development builds may remain unsigned. Public builds must sign and timestamp
the executable, project-owned driver provisioner, bundled DLLs where
applicable, and final installer. Driver signing is a separate Microsoft
hardware submission process.

Primary references:

- https://github.com/djpnewton/vmulti (MIT-licensed original implementation)
- https://github.com/microsoft/Windows-driver-samples/tree/main/hid/vhidmini2
- https://learn.microsoft.com/windows-hardware/drivers/dashboard/driver-signing-offerings
- https://learn.microsoft.com/windows-hardware/drivers/dashboard/code-signing-attestation
