<div align="center">
  <img src="src/mouse_pressure/assets/lucide_mouse.png" width="96" alt="Mouse Pressure icon">
  <h1>Mouse Pressure</h1>
  <p>Turn analog mouse button force into Windows Ink pressure and tilt.</p>

<p>
    <a href="docs/compatibility.md"><img src="https://img.shields.io/badge/Windows-10%20tested-0078D4?logo=windows" alt="Windows 10 tested; Windows 11 unverified"></a>
    <a href="docs/compatibility.md"><img src="https://img.shields.io/badge/output-Windows%20Ink-7B61FF" alt="Windows Ink output"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License"></a>
    <a href="docs/releasing.md"><img src="https://img.shields.io/badge/status-alpha-orange.svg" alt="Alpha status"></a>
  </p>

<p>
    <strong><a href="https://github.com/benmklein/analog_mouse_pressure/releases">Download for Windows</a></strong>
    · <a href="docs/compatibility.md">Compatibility</a>
  </p>
</div>

Mouse Pressure lets compatible analog button mice behave like
a pressure sensitive tablet. It uses native Windows Ink and does not
install a kernel driver.

<p align="center">
  <img src="docs/media/interface.png" width="960" alt="Mouse Pressure configuration interface">
</p>

| Pressure sensitive writing                                                                                         | Color control during a stroke                                                                                       |
|:------------------------------------------------------------------------------------------------------------------:|:-------------------------------------------------------------------------------------------------------------------:|
| [![Writing in Krita with mouse-button pressure](docs/media/pressure-writing.gif)](docs/media/pressure-writing.mp4) | [![Changing color in Krita with analog X-tilt](docs/media/pressure-coloring.gif)](docs/media/pressure-coloring.mp4) |
| Button force controls pressure for drawing applications.                                                           | Left and right buttons can be used simultaneously. Here, right button controls hue.                                 |

### Games and other applications

[![Controlling a retractable chain game with analog mouse buttons](docs/media/pressure-game.gif)](docs/media/pressure-game.mp4)

The driver includes a test sandbox demonstrating analog input outside a drawing application. The pressure signal controls a length of a chain, allowing for a simple momentum based game. The sandbox is located in the mouse tab in the driver menu.

## Features

- Native, driverless Windows Ink output
- Independent left and right button mappings
- Pressure or X-tilt output from either button
- Live calibration and response curve preview
- Included analog input physics sandbox

### Using X-tilt

X-tilt gives you a second analog control alongside pressure. For example,
leave the left button mapped to **Pressure**, map the right button to
**X-tilt**, and use right-button force to change another brush property while
you draw. In Krita, open the Brush Editor (`F5`), choose a property such as
**Hue**, **Size**, **Opacity**, or **Rotation**, and select **X-tilt** as its sensor. Other Windows Ink drawing applications may offer similar tilt mappings under their brush or tablet settings.

## Install

1. Download `MousePressure-<version>-Setup.exe` from
   [Releases](https://github.com/benmklein/analog_mouse_pressure/releases).
2. Run the installer, connect a compatible mouse, and select **Start**.
3. Choose a pressure sensitive brush in your drawing application.

Photoshop uses Windows Ink by default. In Krita, select **Windows 8+ Pointer
Input** under **Settings → Configure Krita → Tablet Settings**, then restart
Krita.

Use `Ctrl+F12` to start and `Ctrl+Shift+F12` to stop. See the
[compatibility notes](docs/compatibility.md) for supported hardware and
applications.

> **Alpha note:** This build is tested on Windows 10. Windows 11 is expected to
> work but remains unverified. The unsigned installer may trigger a Microsoft
> Defender SmartScreen warning, and the first stroke after pressing Start can
> occasionally register as a dot. See [compatibility](docs/compatibility.md)
> for the current support scope.

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
