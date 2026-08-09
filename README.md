# Superstrike Pressure Bridge

[![Windows build](https://github.com/benmklein/analog_mouse_pressure/actions/workflows/windows-build.yml/badge.svg)](https://github.com/benmklein/analog_mouse_pressure/actions/workflows/windows-build.yml)
[![Windows 10/11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows11&logoColor=white)](https://www.microsoft.com/windows/)
[![Krita 5.3.3](https://img.shields.io/badge/Krita-5.3.3-3BABFF?logo=krita&logoColor=white)](https://krita.org/)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-35C46A.svg)](LICENSE)

Use the Logitech G Pro X2 Superstrike's analog HITS button as pressure input
for painting in Krita. The bridge reads the mouse's HID++ pressure stream and
injects Windows synthetic-pen events at the current cursor position.

## Current scope

The first Windows release target is intentionally small:

- Windows 10 or 11
- Logitech G Pro X2 Superstrike
- automatic wired USB or wireless Lightspeed transport detection
- independent left- and right-button pressure mapped to Windows Ink pen pressure
- a desktop interface with adjustable calibration and pressure curves
- Krita as the primary tested drawing application

The standalone application and unified installer are now being prepared for an
alpha release. The installer includes the Krita 5.3.3 tool and detects an
existing compatible VMulti device, but deliberately does not redistribute the
third-party tablet driver currently present on the development machine. The
driverless synthetic Windows Ink backend remains the fallback.

## Release build

The repository now contains a reproducible PyInstaller one-folder build, a
Windows installer definition, and a GitHub Actions packaging workflow. See
[`docs/releasing.md`](docs/releasing.md) for local build commands, supported
Krita versions, signing requirements, Windows 11 validation, and the driver
release gate.

The bridge scans compatible HID++ command interfaces each time it starts. HID
paths and serial numbers are not stored or hardcoded, so another Superstrike
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

## Test pressure in Krita

The drawing program used during development was **Krita**.

1. In Krita, open **Settings > Configure Krita > Tablet Settings** and select
   the Windows Ink / Windows 8+ Pointer Input API. Restart Krita if prompted.
2. Open a document and select a pressure-sensitive brush.
3. Start the bridge from PowerShell:

   ```powershell
   ss-pen --suppress-lmb
   ```

4. Move the pointer over the canvas and press the Superstrike's left HITS
   button. Pressing harder should increase the brush pressure.
5. Press `Ctrl+C` in PowerShell to stop the bridge.

Krita's Tablet Tester is useful when pressure reaches Krita but a particular
brush preset does not respond. Keep native left-click suppression enabled for
Krita so its pressureless mouse stream does not compete with the injected pen
stream:

```powershell
ss-pen --suppress-lmb
```

Leave the experimental release teardown disabled unless Krita visibly keeps
drawing after button-up; its compatibility hover frames can move the cursor.

## Calibration

The defaults reflect the development mouse. First confirm the live raw range:

```powershell
ss-pressure --duration 10
```

Raw readings are now decoded from the complete event payload as 10-bit ADC
codes (`0..1023`) instead of only the first byte. Existing saved `80..185`
style calibration values are migrated automatically to the equivalent
10-bit range when loaded.

Then pass the observed resting and fully pressed values to the bridge:

```powershell
ss-pen --raw-min 320 --raw-max 680 --release-teardown
```

Run `ss-pen --help` for curve, deadzone, contact, smoothing, and click-through
options.

## Development control panel

Launch the small local interface with:

```powershell
ss-dev-ui
```

The panel can start and stop the bridge and persist the mouse and pressure
settings. DPI and left/right haptics live in a global **Mouse hardware** table,
with the detected **Mapping off** restoration values beside the editable
**Mapping on** session values. The **Left button** and **Right button** settings
tabs keep separate raw range, curve, contact, path, pressure, and native-click
suppression values. Either button can drive the Windows Ink pen using its own
pressure map.
The default **Sensitivity mapping** output tab follows the selected settings tab
and marks that button's current click pressure live. The second **Stroke
analysis** tab lists recent Debug-mode traces and graphs raw ADC, mapped,
interpolated, and injected pressure over time plus injected pressure over path
distance. **Terminal output** is the third tab and keeps the full runtime log
separately. Less frequently changed pressure, path,
injection controls are collapsed under **Advanced settings**. The pressure
floor prevents a fast release from stretching very low pressure into a long
hairline; zero still produces a normal pen-up taper. Stop the bridge before
changing settings that affect the Windows input hook.

Curve strength and left/right haptics use sliders. A haptic level of 0 disables
that button's click haptics. The button is always named **Start** or **Stop**;
`Ctrl+F12` starts mapping and `Ctrl+Shift+F12` stops it globally. Before Start is
enabled, the panel detects the
mouse's current DPI and haptic levels for the **Mapping off** column. Values in
the **Mapping on** column initially follow those detected values. Editing any
Mapping-on hardware value turns the profile into an explicit override. Mapping-on
values are applied only for the active bridge session; **Stop**, normal window
shutdown, and automatic failure cleanup restore
the settings detected immediately before startup. The single settings button
saves all settings while stopped and applies the editable mouse hardware values
while running; controls that cannot safely change live remain disabled. Path
stabilization is causal and adds no timed buffer; its warning reports the maximum
spatial trailing distance instead.

When temporary DPI or haptic values differ from the Mapping-off snapshot, an
independent recovery watchdog is armed before the device is changed. If the UI
or bridge process crashes without reaching normal Stop cleanup, the watchdog
reopens the mouse after the process exits and restores DPI, both haptic levels,
and the original onboard-profile state.

**Contact feel** changes only the mapped-pressure thresholds that begin and end
pen contact: Light uses 6/4, Medium 10/6, and Firm 18/12 out of 1023. It does not
change the pressure curve once contact is active.

**Rapid release threshold** is disabled at 0%. Small values such as 1–2% can
help when thin tails are an issue.

The two optional **Ink stabilization** controls are independent. **Path
stabilization** now defaults to 0%: direct mode forwards captured hardware
coordinates without curve fitting or synthetic point expansion so Krita's
Dynamic Brush or Freehand smoothing can own the geometry with lower latency.
Values above 0% enable the experimental causal path filter. **Pressure
influence** defaults to 100% for unmodified sensor expression.

The global **Pressure buttons** section independently enables left and right
pressure mapping. When both are enabled, **Use the same settings for both**
mirrors the left pressure settings onto the right channel and disables the
redundant Right button settings tab until the channels are unlinked.

The Right button's **Advanced settings** include **use right pressure as X-Tilt
modifier** for both VMulti and synthetic output. The verified VMulti pen report
includes signed X/Y Tilt fields, so right pressure can be carried in the same
virtual-HID packet as left pressure. **Show advanced backend settings** appears
only for synthetic output and contains experimental release teardown, because
VMulti does not use the Windows synthetic-pointer teardown sequence. When
X-Tilt is enabled, only the left button starts a
pen stroke: left pressure remains Windows Ink pressure, while the independently
mapped right pressure is sent in the same pen packet as 0–60 degrees of positive
X-Tilt. Native right click is suppressed automatically while the bridge is
active. To try color control in Krita, open the Brush Editor (`F5`) for a Pixel
Brush preset, enable **Hue, Saturation, Value**, select one of those properties,
and choose **X-Tilt** as its sensor. The sensor curve determines how right-button
pressure changes that property. Right calibration, deadzone, and pressure curve
shape the modifier; pressure floor and pressure influence remain specific to
independent pressure strokes. This mode is off by default, and disabling it
restores the independent right-pressure stroke workflow.

The global **Debug mode** toggle appears in Advanced settings and defaults to
on for the current development phase. It records the detailed per-stroke JSON
files under `~/.superstrike/stroke_traces` and prints verbose `RAW`, `CONTACT`,
`RELEASE`, and motion-diagnostic messages used by the analyzer. Turning it off
bypasses the trace recorder entirely while preserving startup, safety, recovery,
error, and cleanup logging, which makes it useful for comparing input latency
without the diagnostic overhead.

## Krita raster ink experiment

`integrations/krita/superstrike_raster_ink` contains the first-stage source for
a native Krita raster tool. It keeps the live path unfiltered by default for
Freehand-like latency, then cancels the temporary stroke on release and replays
a corner-aware refined centerline through the active Krita brush. See
`docs/krita_raster_ink.md` for the staged build, install, and validation plan.

The pressure channel itself reports at roughly 60 Hz. Pen injection defaults
to 240 Hz and resamples cursor position between pressure reports so short, fast
strokes contain more spatial points. At stroke onset, the bridge buffers one
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

The bridge dynamically discovers the HITS feature index, renews its short-lived
monitoring lease every two seconds, and restores the flags that were active
before startup when it closes.

While the bridge is running, the panel can apply another mouse DPI (100–32,000
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

- `ss-pressure` — inspect decoded left and right pressure values
- `ss-pen` — run the supported synthetic-pen path
- `ss-dev-ui` — start/stop dev control panel with settings and live logs
- `ss-sniff` — enumerate Logitech HID interfaces and capture raw reports
- `ss-tablet` — legacy VMulti and synthetic-backend experiments
- `ss-bridge-ws` — experimental WebSocket service

Each traced desktop-app stroke is saved under
`~/.superstrike/stroke_traces`. Analyze the
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

- [x] Decode Superstrike left and right analog pressure
- [x] Inject left pressure through the Windows synthetic-pen API
- [x] Handle clean pen-up teardown for Krita
- [x] Provide an installed CLI and automated regression tests
- [ ] Repeat the Krita hardware smoke test from a clean checkout
- [ ] Package a standalone Windows build
- [ ] Publish installation, troubleshooting, and known-limitations notes
