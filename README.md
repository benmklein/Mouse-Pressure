# Mouse Pressure

[![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows)](docs/compatibility.md)
[![Windows Ink](https://img.shields.io/badge/output-Windows%20Ink-7B61FF)](docs/compatibility.md)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](docs/releasing.md)

Mouse Pressure turns analog mouse-button force into native Windows Ink input.
It lets a compatible mouse behave like a pressure-sensitive pen in drawing
applications, without installing a kernel driver or an application-specific
brush plugin.

## What it includes

- Native, driverless Windows Ink output
- Independent left- and right-button mappings
- Pressure or X-tilt as the output target for either button
- Live calibration and response-curve preview
- Temporary DPI and haptic settings that are restored on Stop
- An included physics sandbox for testing both analog buttons
- Optional local stroke traces for diagnostics

Pressure is the default target for both buttons. For a combined workflow, map
one button to Pressure and the other to X-tilt; the pressure-mapped button owns
the stroke while the X-tilt button acts as an auxiliary control.

## Compatibility

The current hardware integration is intended for compatible Logitech analog
button hardware, including SUPERSTRIKE-compatible devices. That name is used
only to describe compatibility. This project is independent and is not
affiliated with or endorsed by Logitech, Microsoft, Krita, or Qt.

See [compatibility and support scope](docs/compatibility.md) for the current
Windows and application test matrix.

## Install

Download `MousePressure-<version>-Setup.exe` from Releases and run it. The
installer is a per-user application install. It does not:

- install or download VMulti;
- change Windows driver-signing settings; or
- install a Krita plugin or brush.

Start and Stop are also available through `Ctrl+F12` and `Ctrl+Shift+F12`.

## Run from source

```powershell
git clone https://github.com/benmklein/analog_mouse_pressure.git
Set-Location analog_mouse_pressure
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,sandbox]"
.\.venv\Scripts\python.exe -m mouse_pressure.pyside_ui
```

## Build a Windows release

```powershell
.\scripts\build_windows.ps1
```

The build runs tests, compiles the native input relay, packages the application
and sandbox, generates the SBOM and notices, and creates the Inno Setup
installer. See [the release checklist](docs/releasing.md).

## Privacy and recovery

Mouse Pressure does not require an online service. Debug traces stay local and
are disabled by default. See [PRIVACY.md](PRIVACY.md),
[SECURITY.md](SECURITY.md), and [recovery instructions](docs/recovery.md).

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Historical experiments remain in the source tree for research and regression
tests, but they are not exposed by the release UI or installer.

Mouse Pressure is licensed under the [MIT License](LICENSE). Bundled dependency
notices and source availability information are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and
[packaging/legal/SOURCE_OFFER.md](packaging/legal/SOURCE_OFFER.md).
