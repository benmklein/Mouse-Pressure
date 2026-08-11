# Mouse Pressure Driver

[![Windows build](https://github.com/benmklein/analog_mouse_pressure/actions/workflows/windows-build.yml/badge.svg)](https://github.com/benmklein/analog_mouse_pressure/actions/workflows/windows-build.yml)
[![Windows 10/11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows11&logoColor=white)](https://www.microsoft.com/windows/)
[![Krita 5.3.3](https://img.shields.io/badge/Krita-5.3.3-3BABFF?logo=krita&logoColor=white)](https://krita.org/)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-35C46A.svg)](LICENSE)

**Mouse Pressure** turns analog mouse-button force into pen pressure for drawing
apps. It reads the supported mouse's pressure stream and emits virtual pen input
at the current pointer position.

Compatible with the Logitech G PRO X2 SUPERSTRIKE™. Independent software; not
affiliated with or endorsed by Logitech.

## Current scope

The first Windows release target is intentionally small:

- Windows 10 or 11
- the supported Logitech analog-button mouse listed above
- automatic wired USB or wireless Lightspeed transport detection
- independent left- and right-button pressure mapped to Windows Ink pen pressure
- a desktop interface with adjustable calibration and pressure curves
- a bundled physics sandbox for testing both processed pressure channels
- Krita as the primary tested drawing application

The standalone application and unified installer are now being prepared for an
alpha release. The installer includes the Krita 5.3.3 tool and detects an
existing compatible VMulti device. It can also embed the project-owned virtual
tablet payload once that payload has been signed through Microsoft's hardware
dashboard. The build deliberately rejects the unrelated Pentablet package
currently present on the development machine. The synthetic Windows pen backend
remains the fallback.

## Release build

The repository now contains a reproducible PyInstaller one-folder build, a
Windows installer definition, and a GitHub Actions packaging workflow. See
[`docs/releasing.md`](docs/releasing.md) for local build commands, supported
Krita versions, signing requirements, Windows 11 validation, and the driver
release gate.

The driver scans compatible HID++ command interfaces each time it starts. HID
paths and serial numbers are not stored or hardcoded, so another compatible mouse
does not require a separate device-discovery step. Per-device pressure
calibration is still recommended because physical sensor ranges can vary.

## Install for development

Python 3.12 or newer is required.

```powershell
git clone https://github.com/benmklein/analog_mouse_pressure.git
cd analog_mouse_pressure
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Install the sandbox dependency when developing or creating a release build:

```powershell
python -m pip install -e ".[dev,release,sandbox]"
```

## Pressure sandbox

The Windows installer includes **Pressure Sandbox**, a small chain-and-ball
physics toy for testing the driver's processed left- and right-button output.
Open it from the Mouse page or the Start menu while Mouse Pressure is running.

The sandbox reads calibrated pressure from a versioned local shared-memory
channel. It never opens the mouse HID device itself, so it cannot steal the
pressure lease from the driver and it does not require a firewall prompt. Left
pressure changes the chain length; right pressure grabs and releases the ball.
Right pressure can attach the chain from anywhere in either moving- or
locked-anchor mode; clicking directly on the ball is not required.
Hold `Space` as a keyboard alternative for right pressure. When the driver is
stopped, ordinary mouse controls remain available as a fallback.

## Test pressure in Krita

The drawing program used during development was **Krita**.

1. In Krita, open **Settings > Configure Krita > Tablet Settings** and select
   the Windows Ink / Windows 8+ Pointer Input API. Restart Krita if prompted.
2. Open a document and select a pressure-sensitive brush.
3. Start the driver from PowerShell:

   ```powershell
   mp-pen --suppress-lmb
   ```

4. Move the pointer over the canvas and press the mouse's left analog
   button. Pressing harder should increase the brush pressure.
5. Press `Ctrl+C` in PowerShell to stop the driver.

Krita's Tablet Tester is useful when pressure reaches Krita but a particular
brush preset does not respond. Keep native left-click suppression enabled for
Krita so its pressureless mouse stream does not compete with the injected pen
stream:

```powershell
mp-pen --suppress-lmb
```

Leave the experimental release teardown disabled unless Krita visibly keeps
drawing after button-up; its compatibility hover frames can move the cursor.

## Calibration

The defaults reflect the development mouse. First confirm the live raw range:

```powershell
mp-pressure --duration 10
```

Raw readings are now decoded from the complete event payload as 10-bit ADC
codes (`0..1023`) instead of only the first byte. Existing saved `80..185`
style calibration values are migrated automatically to the equivalent
10-bit range when loaded.

Then pass the observed resting and fully pressed values to the driver:

```powershell
mp-pen --raw-min 320 --raw-max 680 --release-teardown
```

Run `mp-pen --help` for curve, deadzone, contact, smoothing, and click-through
options.

## Desktop control panel

Launch the desktop interface with:

```powershell
mp-dev-ui
```

The PySide6 control panel uses a clean native Windows layout with persistent
light and dark modes. Its sidebar separates the interface into four focused
pages:

- **Pressure** — left/right enable switches, linked settings, channel-specific
  calibration, pressure curves, contact behavior, advanced controls, and a live
  antialiased sensitivity graph.
- **Mouse** — detected Mapping-off DPI/haptics beside the temporary Mapping-on
  values, pen backend selection, debug recording, tray behavior, and backend
  settings.
- **Stroke analysis** — recent Debug-mode traces with mapped, smoothed, and
  injected pressure plotted together.
- **Logs** — full runtime and device output in a high-contrast terminal panel
  in either application theme.

Either button can drive Windows Ink using its own pressure map. Less frequently
changed pressure and path controls stay under **Advanced settings**. The
pressure floor prevents a fast release from stretching very low pressure into a
long hairline; zero still produces a normal pen-up taper.

Curve strength and left/right haptics use sliders. A haptic level of 0 disables
that button's click haptics. The button is always named **Start** or **Stop**;
`Ctrl+F12` starts mapping and `Ctrl+Shift+F12` stops it globally. Before Start is
enabled, the panel detects the
mouse's current DPI and haptic levels for the **Mapping off** column. Values in
the **Mapping on** column initially follow those detected values. Editing any
Mapping-on hardware value turns the profile into an explicit override. Mapping-on
values are applied only for the active driver session; **Stop**, normal window
shutdown, and automatic failure cleanup restore
the settings detected immediately before startup. The single settings button
saves all settings while stopped and applies the editable mouse hardware values
while running; controls that cannot safely change live remain disabled. Path
stabilization is causal and adds no timed buffer; its warning reports the maximum
spatial trailing distance instead.

When temporary DPI or haptic values differ from the Mapping-off snapshot, an
independent recovery watchdog is armed before the device is changed. If the UI
or driver process crashes without reaching normal Stop cleanup, the watchdog
reopens the mouse after the process exits and restores DPI, both haptic levels,
and the original onboard-profile state.

**Contact feel** changes only the mapped-pressure thresholds that begin and end
pen contact: Light uses 6/4, Medium 10/6, and Firm 18/12 out of 1023. It does not
change the pressure curve once contact is active.

**Rapid release threshold** is disabled at 0%. Small values such as 1–2% can
help when thin tails are an issue.

The optional stroke controls are independent. **Path stabilization** now
defaults to 0%: direct mode forwards captured hardware
coordinates without curve fitting or synthetic point expansion so Krita's
Dynamic Brush or Freehand smoothing can own the geometry with lower latency.
Values above 0% enable the experimental causal path filter. **Pressure
influence** defaults to 100% for unmodified sensor expression.

The switches at the top of **Pressure** independently enable left and right
pressure mapping. When both are enabled, **Link settings** mirrors the left
pressure settings onto the right channel and disables the redundant Right
button tab until the channels are unlinked.

The Right button's **Advanced settings** include **use right pressure as X-Tilt
modifier** for both VMulti and synthetic output. The verified VMulti pen report
includes signed X/Y Tilt fields, so right pressure can be carried in the same
virtual-HID packet as left pressure. **Show advanced backend settings** appears
only for synthetic output and contains experimental release teardown, because
VMulti does not use the Windows synthetic-pointer teardown sequence. When
X-Tilt is enabled, only the left button starts a
pen stroke: left pressure remains Windows Ink pressure, while the independently
mapped right pressure is sent in the same pen packet as 0–60 degrees of positive
X-Tilt. Native right click is suppressed automatically while the driver is
active. To try color control in Krita, open the Brush Editor (`F5`) for a Pixel
Brush preset, enable **Hue, Saturation, Value**, select one of those properties,
and choose **X-Tilt** as its sensor. The sensor curve determines how right-button
pressure changes that property. Right calibration, deadzone, and pressure curve
shape the modifier; pressure floor and pressure influence remain specific to
independent pressure strokes. This mode is off by default, and disabling it
restores the independent right-pressure stroke workflow.

The global **Debug mode** toggle appears on the Mouse page and defaults to
on for the current development phase. It records the detailed per-stroke JSON
files under `~/.mouse-pressure/stroke_traces` and prints verbose `RAW`, `CONTACT`,
`RELEASE`, and motion-diagnostic messages used by the analyzer. Turning it off
bypasses the trace recorder entirely while preserving startup, safety, recovery,
error, and cleanup logging, which makes it useful for comparing input latency
without the diagnostic overhead.

## Krita brush integration

`integrations/krita/mouse_pressure_brush` contains the first-stage source for
a native **Mouse Pressure Brush** tool. It keeps the live path unfiltered by
default for Freehand-like latency, then cancels the temporary stroke on release and replays
a corner-aware refined centerline through the active Krita brush. See
`docs/krita_raster_ink.md` for the staged build, install, and validation plan.

The pressure channel itself reports at roughly 60 Hz. Pen injection defaults
to 240 Hz and resamples cursor position between pressure reports so short, fast
strokes contain more spatial points. At stroke onset, the driver buffers one
pressure report (about 16 ms) so Krita can interpolate the initial pressure
ramp across the distance travelled instead of drawing a long, uniformly thin
"cat tail" before the next hardware sample. Large pressure changes are also
distributed linearly across the four 240 Hz pen ticks between 60 Hz hardware
reports, in both the rising and falling directions.

With native left-click suppression enabled, the low-level mouse hook also
captures every hardware movement coordinate between pen ticks. Those points
are replayed as a bounded, deduplicated pen path, preserving the mouse's curved
trajectory instead of connecting only the latest polled positions with long
straight segments. Injection batches use an adaptive budget: ordinary stable
segments stay short for low latency, while fast movement and large pressure
changes receive more interpolation points (up to the safety cap).

The driver dynamically discovers the HITS feature index, renews its short-lived
monitoring lease every two seconds, and restores the flags that were active
before startup when it closes.

While the driver is running, the panel can apply another mouse DPI (100–32,000
in 50-DPI increments) and independent left/right click haptics (0–5) without
restarting the pressure stream. The pre-start snapshot remains the restoration
target. When an onboard profile controls resolution, the panel temporarily
switches the mouse to host mode and restores the previously active profile on
Stop. The panel reports a verification error if the device rejects or overrides
the requested value.

## Development

Install the optional web dependencies only when working on the experimental
WebSocket service:

```powershell
python -m pip install -e ".[dev,web]"
python -m pytest
```

Useful diagnostic commands:

- `mp-pressure` — inspect decoded left and right pressure values
- `mp-pen` — run the supported synthetic-pen path
- `mp-dev-ui` — start/stop dev control panel with settings and live logs
- `mp-sniff` — enumerate Logitech HID interfaces and capture raw reports
- `mp-tablet` — legacy VMulti and synthetic-backend experiments
- `mp-bridge-ws` — experimental WebSocket service

Each traced desktop-app stroke is saved under
`~/.mouse-pressure/stroke_traces`. Analyze the
newest real stroke with:

```powershell
.\.venv\Scripts\python.exe scripts\analyze_stroke_trace.py
```

New traces include raw ADC samples, configured range/curve, motion-to-update
latency, and the adaptive path budget so calibration saturation can be
distinguished from path sparsity or brush-engine behavior.

## Protocol acknowledgement

The full ADC decoder and renewable monitoring-lease lifecycle were
cross-checked against the MIT-licensed
[SUPERSTRIKE SDK](https://github.com/fawhfi/superstrike-sdk) protocol research.

Research captures and historical test logs live under `docs/`. The current
synthetic-pen release behavior is documented in
[`docs/release_teardown.md`](docs/release_teardown.md).

## Barebones release checklist

- [x] Decode left and right analog-button pressure
- [x] Inject left pressure through the Windows synthetic-pen API
- [x] Handle clean pen-up teardown for Krita
- [x] Provide an installed CLI and automated regression tests
- [ ] Repeat the Krita hardware smoke test from a clean checkout
- [ ] Package a standalone Windows build
- [ ] Publish installation, troubleshooting, and known-limitations notes
