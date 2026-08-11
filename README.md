<div align="center">
  <img src="src/mouse_pressure/assets/lucide_mouse.png" width="96" alt="Mouse Pressure icon">
  <h1>Mouse Pressure</h1>
  <p>Turn analog mouse-button force into native Windows Ink pressure and tilt.</p>

  <p>
    <a href="docs/compatibility.md"><img src="https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows" alt="Windows 10 and 11"></a>
    <a href="docs/compatibility.md"><img src="https://img.shields.io/badge/output-Windows%20Ink-7B61FF" alt="Windows Ink output"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License"></a>
    <a href="docs/releasing.md"><img src="https://img.shields.io/badge/status-alpha-orange.svg" alt="Alpha status"></a>
  </p>

  <p>
    <strong><a href="https://github.com/benmklein/analog_mouse_pressure/releases">Download for Windows</a></strong>
    · <a href="docs/compatibility.md">Compatibility</a>
  </p>
</div>

Mouse Pressure lets compatible analog-button mice behave like
pressure-sensitive drawing devices. It uses native Windows Ink and does not
install a kernel driver or application-specific brush plugin.

## See it in action

| Pressure-sensitive writing | Color control during a stroke |
| :---: | :---: |
| [![Writing in Krita with mouse-button pressure](docs/media/pressure-writing.gif)](docs/media/pressure-writing.mp4) | [![Changing color in Krita with analog X-tilt](docs/media/pressure-coloring.gif)](docs/media/pressure-coloring.mp4) |
| Button force controls ordinary Krita brushes. | A second analog button moves through a color gradient. |

### Games and other applications

[![Controlling a retractable chain game with analog mouse buttons](docs/media/pressure-game.gif)](docs/media/pressure-game.mp4)

The included sandbox demonstrates continuous analog input outside a drawing
application: one button grabs the rock while the other retracts the chain.

## Features

- Native, driverless Windows Ink output
- Independent left- and right-button mappings
- Pressure or X-tilt output from either button
- Live calibration and response-curve preview
- Included analog-input physics sandbox

## Install

1. Download `MousePressure-<version>-Setup.exe` from
   [Releases](https://github.com/benmklein/analog_mouse_pressure/releases).
2. Run the installer, connect a compatible mouse, and select **Start**.
3. Choose a pressure-sensitive brush in your drawing application.

Photoshop uses Windows Ink by default. In Krita, select **Windows 8+ Pointer
Input** under **Settings → Configure Krita → Tablet Settings**, then restart
Krita.

Use `Ctrl+F12` to start and `Ctrl+Shift+F12` to stop. See the
[compatibility notes](docs/compatibility.md) for supported hardware and
applications.

<details>
<summary><strong>Run from source</strong></summary>

```powershell
git clone https://github.com/benmklein/analog_mouse_pressure.git
Set-Location analog_mouse_pressure
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,sandbox]"
.\.venv\Scripts\python.exe -m mouse_pressure.pyside_ui
```

Run tests with `.\.venv\Scripts\python.exe -m pytest` or build the Windows
installer with `.\scripts\build_windows.ps1`.

</details>

## License

Mouse Pressure is available under the [MIT License](LICENSE). See
[third-party notices](THIRD_PARTY_NOTICES.md), [privacy](PRIVACY.md),
[security](SECURITY.md), and [recovery instructions](docs/recovery.md).
