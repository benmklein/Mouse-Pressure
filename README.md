# Superstrike Pressure Bridge

Use the Logitech G Pro X2 Superstrike's analog HITS button as pressure input
for painting in Krita. The bridge reads the mouse's HID++ pressure stream and
injects Windows synthetic-pen events at the current cursor position.

## Current scope

The barebones release target is intentionally small:

- Windows 10 or 11
- Logitech G Pro X2 Superstrike
- independent left- and right-button pressure mapped to Windows Ink pen pressure
- a command-line interface with adjustable calibration and pressure curves
- Krita as the primary tested drawing application

The WebSocket backend and desktop UI are experimental and are not required for
the first usable release.

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
settings. The **Left button** and **Right button** settings tabs keep separate
calibration, curve, contact, path, pressure, haptic, and native-click suppression
values. Either button can drive the Windows Ink pen using its own pressure map.
The default **Sensitivity mapping** output tab follows the selected settings tab
and marks that button's current click pressure live; **Terminal output** keeps
the full runtime log separately. Less frequently changed pressure, path,
injection, and teardown controls are collapsed under **Advanced settings**. The pressure
floor prevents a fast release from stretching very low pressure into a long
hairline; zero still produces a normal pen-up taper. Stop the bridge before
changing settings that affect the Windows input hook.

Curve strength and left/right haptics use sliders. A haptic level of 0 disables
that button's click haptics. Enabling **Buffer first pressure sample** shows its
roughly 16 ms latency cost. Path stabilization is causal and adds no timed
buffer; its warning reports the maximum spatial trailing distance instead.

The two optional **Ink stabilization** controls are independent. **Path
stabilization** now defaults to 0%: direct mode forwards captured hardware
coordinates without curve fitting or synthetic point expansion so Krita's
Dynamic Brush or Freehand smoothing can own the geometry with lower latency.
Values above 0% enable the experimental causal path filter. **Pressure
influence** defaults to 85% and gently compresses real pressure around
mid-pressure, making width changes less extreme while preserving real pen-up;
set it to 100% for unmodified sensor expression.

**Buffer first pressure sample** is disabled by default for low-latency Krita
Dynamic Brush use. Immediate contact is protected from a hairline start by the
configured pressure floor. Enable the checkbox only when a brush still produces
an objectionable thin lead-in; it deliberately adds roughly one 60-Hz pressure
frame (about 16 ms) before pen-down.

Use **Calibrate raw range (15 sec)** instead of guessing the raw endpoints. A
large highlighted banner stays above the settings and terminal while the
panel gives a three-second countdown before sampling a fully released button,
a light press, and a firm comfortable press. It rejects an unusably small range,
then saves the robust observed range.
The resulting maximum is your useful drawing maximum, not a claim about the
ADC's absolute electrical limit.

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

While the bridge is running, the panel can also apply mouse DPI (100–32,000 in
50-DPI increments) and independent left/right click haptics (0–5) without
restarting the pressure stream. DPI changes may require G HUB to be closed and
the mouse's onboard profile to be disabled; the panel reports a verification
error if the device rejects or overrides the requested value.

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

Each Dev Panel stroke is also saved under `work/stroke_traces`. Analyze the
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
