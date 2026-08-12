# Input backend feasibility investigation

This investigation was performed from `benmklein/analog_mouse_pressure` at commit
`bfd527598094a7e38fe695710b36863eb6114f3d` (the requested
`cleanup/barebones-release` worktree, preserved with its existing uncommitted changes) on
the separate branch `codex/input-backend-investigation`. The VMulti repository was
inspected read-only at `E:\Projects\mouse-pressure-vmulti`, commit
`4cc86a851bc911dc76220e9335204ab065a53c47`.

## Decision table

| Hypothesis | Current evidence | Result | Confidence | Remaining unknown | Cheapest next experiment | Likely engineering cost |
|---|---|---|---|---|---|---|
| The digital click can improve stroke onset. | In the G Hub USB capture, MouseButtonSpy and ordinary mouse down differ by a median **+0.091 ms** (14 presses), while the next analog frame arrives **0.835–14.854 ms** later. | **Yes, as the immediate contact trigger and timing anchor.** It cannot provide force by itself. | High | Exact low-level hook vs Raw Input vs mode-3 timing on this runtime. | Run the 100-press timing capture below. | Low |
| Current analog pressure can be read immediately on click. | Known feature `0x1B0C` function 0/2 probes return static/configuration-looking data; function 3 is the lease and function 4 is flags. No known read returns a newer ADC value. | **No evidence that it is possible. Probably not through the known interface.** | Medium | An undocumented read-only field could exist. | Capture known reads while pressed; do not fuzz writes. | Low to investigate; unknown to implement |
| A zero-intentional-latency estimated start is good enough. | In 112 new presses, offline click-edge pressure was median 378.9 raw, effectively identical to configured activation raw 379 and therefore 0% mapped output. Predicting the next sample had large p90 error. | **A learned click value alone cannot remove the hairline. A deliberately nonzero output floor can; future/body-pressure prediction is too unreliable.** | High | Whether the current first injected frame consistently honors the configured floor. | Inspect new end-to-end Debug traces at the first injected contact. | Low |
| Waiting for one post-click pressure sample is viable. | The new capture measured median 8.7 ms, p90 14.8 ms, p99 16.8 ms. Motion occurred before the sample in 48/112 presses; path length p90 was 16.3 mouse counts. | **Accurate but likely to reproduce the felt latency/shortened opening. Not recommended as the default.** | High | Whether generic replay can hide that wait in every app. | Use only as an opt-in quality comparison after delivery proof. | Medium |
| A buffered opening can be replayed generically. | Synthetic pen accepts one pen contact per call; Windows may coalesce move messages, with history recoverable only if the receiver asks for it. VMulti has no driver-side input FIFO. | **Plausible, not proven.** Replay must be a sequence of calls/reports, not a history batch, and applications may not preserve it as intended. | Low to medium | Krita, Photoshop, Qt, and VMulti delivery under rapid catch-up. | Run the neutral receiver first, then identical Krita/Photoshop tests. | Medium |
| `InjectSyntheticPointerInput` is broadly usable. | It is a generic Windows pen API and works in the current Krita workflow. For `PT_PEN`, `count` must be 1. Current code supplies neither `dwTime` nor `PerformanceCount`. | **Usable as a generic Windows Ink fallback; not yet certified for catch-up replay.** | Medium | App-specific treatment of injected pointers and coalescing/history behavior. | Run the proof receiver at 0.25, 1, 4, and 8 ms spacing. | Medium |
| The current VMulti device is broadly usable. | Windows 10 recognizes it as a pen and its reports contain tip, in-range, X/Y, pressure, and tilt. The descriptor lacks required X/Y physical range/unit metadata, and each write completes one pending HID read rather than entering a FIFO. | **Promising, more hardware-like, but not yet standards-complete or broadly validated.** | Medium | Windows 11/HLK, Photoshop, report loss at catch-up rates. | Fix descriptor metadata in the driver repo, then run Win11 + neutral receiver + Photoshop matrix. | Medium to high |
| Photoshop can work without a port. | Adobe documents Windows Ink as the default Windows tablet path and supports Pen Pressure in brush Shape Dynamics. Both generic backends present a Windows pen rather than a Krita-only API. | **Likely for ordinary pressure strokes; not measured here.** A port would only be needed for app-native postprocessing/replay semantics. | Medium | Whether Photoshop recognizes each backend and preserves rapid opening updates. | Four-outcome manual Photoshop procedure below. | Low to test |
| The Krita DLL can cover multiple Krita versions. | It imports many C++ Krita internals. Krita's own CMake file explicitly says it provides no C++ binary compatibility between releases. The installed/tested build is exactly 5.3.3/Qt 5.15.7. | **Treat the DLL as exact-build-specific.** One source tree with versioned builds is realistic; one binary across patches is not a supportable promise. Krita 6 needs a separate Qt 6 build and compatibility code. | High | How many 5.3.x patches compile/load unchanged. | CI source builds against selected tags; no installed-copy mutation. | Medium |
| The next architecture should be generic-first. | Device acquisition, click/contact, mapping, movement, and pen reports do not require Krita internals. Perfect-Ink replay and preset-aware rendering do. | **Test a generic immediate estimated onset first; retain optional app plugins only for rendering enhancements.** | High | Quality of the estimate and generic delivery proof. | 100-press capture plus offline comparison, then one opt-in runtime experiment. | Medium |

## Plain-language conclusion

What probably works: use the independent click signal immediately, begin the generic pen
contact at a deliberately nonzero pressure floor, and replace it with real pressure as soon as
the next device frame arrives. The new capture shows that a learned actuation value itself maps
to 0%; the floor, rather than an actuation estimate, is what removes the hairline without waiting.

What probably does not work: treating addresses `0x20` and `0x30` as two extra pressure
samples, requesting a known “current pressure” value on click, sending a historical batch
through `InjectSyntheticPointerInput`, or promising one Krita DLL for multiple releases.

What remains uncertain: whether rapid catch-up frames are preserved by each Windows Ink
application, how accurately click pressure can be estimated, and whether the current VMulti
descriptor/report queue behaves reliably on Windows 11 and Photoshop.

The 112-press click/HID timing capture is complete. The highest-value next test is now the same
stroke mix with normal Debug traces, verifying whether the **first injected pen-down actually
contains the configured 15% floor** and whether Windows/the target app receives that frame. This
distinguishes a backend emission bug from the unavoidable real sensor ramp.

## Confirmed facts and inferences

### Confirmed by repository data or source

- Mode 2 address `0x10` contains 522 frames at 60.058 Hz mean; its median interval is
  17 ms and p99 is 19 ms. Mode 3 contains 251 frames at 60.082 Hz mean with the same
  median and a 19 ms p99.
- Addresses `0x10`, `0x20`, and `0x30` each arrive near 60 Hz, phase-shifted by roughly
  1 ms. The combined packet count is about 180 Hz, but only `0x10` contains both decoded
  pressure channels. Existing captures show deterministic transforms between `0x20` and
  `0x30`; they do not provide a newer independent pressure sample.
- The reader has no 60 Hz polling sleep before the device read. It waits for reports and
  returns them as they arrive. The 33 ms UI refresh only drains telemetry and does not drive
  acquisition.
- The current decode is 10-bit: `(byte4 << 2) | (byte5 >> 6)` for left and the equivalent
  byte pair for right. All four low-bit codes occur in both mode logs. Those bits add
  amplitude resolution; they do not increase temporal resolution.
- In 15 G Hub presses, the next pressure notification arrived 0.835–14.854 ms after ordinary
  mouse down (median 5.880 ms). In 14 aligned presses, MouseButtonSpy differed from ordinary
  mouse down by -0.106–+0.128 ms (median +0.091 ms).
- The newest 500 structured stroke traces show emitter-observed button proxy to virtual down
  median 18.732 ms, p90 31.735 ms, p99 48.992 ms; proxy to next fresh pressure median
  12.999 ms, p90 18.637 ms; proxy to a mapped rise of 32 median 20.644 ms, p90 33.086 ms.
  These are runtime observations, but the proxy is not the original hardware hook timestamp.
- `InjectSyntheticPointerInput` requires `count == 1` for a pen. Its `count` therefore cannot
  represent pen history. Windows permits injection timestamps via `dwTime` or
  `PerformanceCount`, but the current injector leaves both zero.
- The VMulti descriptor includes a pen application collection and tip, in-range, X/Y,
  pressure, barrel/eraser, and tilt fields. Its X/Y fields do not include the physical
  minimum/maximum, unit, and exponent metadata required by Microsoft's integrated-pen
  guidance.
- The VMulti driver completes one pending HID read for each vendor write. It does not keep a
  catch-up FIFO when no read is pending.
- The Krita plugin imports private C++ symbols from `libkritaui`, `libkritaimage`,
  `libkritaflake`, `libkritacommand`, `libkritawidgetutils`, and `libkritaglobal`, in addition
  to Qt 5 and KDE Frameworks libraries.

### Inferences that still require measurement

- Approximately 60 Hz is almost certainly the device/firmware stream limit for independent
  analog values, not a Python/UI bottleneck. A faster undocumented hardware mode is not
  disproven, but none appears in the captures or known feature functions.
- The 27–39 ms “first meaningful pressure” delay is consistent with: waiting up to one
  16.7 ms period for an early, still-low ramp sample; waiting another period for body
  pressure; then up to about 4.2 ms for the 240 Hz emission tick plus thread/app scheduling.
  The USB capture contains examples such as pressure values at +4.760, +21.758, and
  +38.755 ms. This explanation is strongly supported but is not a direct sensor-internal
  measurement.
- A learned actuation estimate should outperform a fixed global floor for the average press,
  but only click-aligned mode-3 traces can establish its error distribution.
- A normal Windows Ink application should accept the generic backends for basic pressure.
  Photoshop was not installed or tested during this investigation.

## Current pipeline and timing

```mermaid
sequenceDiagram
    participant Mouse as Physical mouse
    participant Hook as Low-level hook thread
    participant Raw as Raw Input window/thread
    participant HID as HID++ device (~60 Hz pressure)
    participant Reader as mouse-pressure-reader thread
    participant Loop as asyncio loop / latest-only queue
    participant Emit as contact + interpolation emitter (240 Hz fallback)
    participant Pen as Synthetic pointer or VMulti
    participant Win as Windows pointer stack
    participant App as Krita / Photoshop

    Mouse->>Hook: digital down (perf_counter)
    Hook->>Emit: button state / suppression decision
    Mouse->>Raw: high-rate dx/dy + button packets
    Raw->>Loop: movement events (queue up to 512)
    Note over HID: Wait 0–16.7 ms for next addr 0x10 frame
    HID->>Reader: feature 0x0C report
    Reader->>Reader: decode 10-bit L/R pressure
    Reader->>Loop: call_soon_threadsafe → queue(maxsize=1)
    Note over Loop,Emit: Up to ~4.2 ms fallback emission tick; movement may emit sooner
    Loop->>Emit: fresh pressure + latest path
    Emit->>Pen: pen down/update/up
    Pen->>Win: PT_PEN call or HID report
    Win->>App: WM_POINTER / Qt tablet event
    Note over App: OS/app may coalesce moves; history requires receiver support
```

All newly added diagnostics use `time.perf_counter`, which is QueryPerformanceCounter-backed
on Windows. Existing structured traces also use one monotonic runtime clock. The UI timer is
outside the critical acquisition path.

The latest-only pressure queue may discard an intermediate frame if the event loop is blocked,
but it cannot explain a stable 60 Hz source rate: the raw mode logs already show that cadence.
The emission loop advances its next deadline after each tick rather than maintaining an absolute
phase, so scheduler delay can add a few milliseconds. The synthetic injector also enforces a
small distinct-frame spacing during dense replay. Neither creates the initial 16.7 ms sensor
period, but both matter to the tail of the latency distribution.

## Existing capture analysis

The reusable analysis command is:

```powershell
.\.venv\Scripts\python.exe scripts\analyze_input_backend_evidence.py --trace-limit 500
```

Raw summary: [`results/input_evidence_summary.json`](results/input_evidence_summary.json).

The old G Hub stream is change-driven, so its “preceding pressure age” (median 321 ms) must not
be interpreted as the current mode-3 sample age. It remains useful for aligning ordinary mouse,
MouseButtonSpy, and the first subsequent analog change. A new continuous mode-3 capture is
required for last-sample, slope, and stable-body calculations.

### Why `0x20`/`0x30` do not create 180 Hz pressure

They are interleaved packets, but packet rate is not information rate. The observed relationships
include `addr30.byte5 = (addr20.byte5 + 0x40) mod 256` and
`addr30.byte7 = (addr20.byte7 + 0xC0) mod 256`. Neither packet provides the two independent
10-bit left/right values decoded from address `0x10`. Their phase is useful for understanding
firmware scheduling, not for interpolating new force measurements.

### New 112-press physical capture

The user-run capture at `work/input_timing/input-timing.json` contains 112 matched Raw Input
downs/ups and 7,023 address-`0x10` frames over 117.8 seconds. It ended with normal cleanup. The
low-level hook was deliberately absent because native suppression was disabled, and
MouseButtonSpy was not enabled; device-scoped Raw Input still supplies the exact button edge used
for this analysis.

| Measurement | Median | p90 | p99 | Maximum |
|---|---:|---:|---:|---:|
| Address-`0x10` interval | 16.03 ms | 18.01 ms | 18.14 ms | 28.01 ms |
| Button to next pressure frame | 8.71 ms | 14.78 ms | 16.77 ms | 17.77 ms |
| Offline interpolated raw at click | 378.89 | 397.90 | 410.98 | 435.58 |
| Button to configured 15% floor | 16.87 ms | 26.20 ms | 30.98 ms | 352.43 ms |
| Path before next frame | 0 counts | 16.32 | 37.98 | 39.36 |

The interpolated click median of 378.89 matching `raw_min=379` is the central result. It means
the physical digital contact happens where calibration says 0% output should begin. This is not
a missing low bit or reader delay. A thicker zero-latency start must intentionally depart from
instantaneous sensor truth.

Only 25.0% of presses received a new frame within 4 ms, 42.9% within 8 ms, and 93.8% within
16 ms. Motion occurred before that frame in 48/112 presses. Waiting is therefore not free even
when the buffered path is eventually replayed.

## Immediate-onset strategies

The existing files do not contain exact, continuous pre-click mode-3 history on the same clock as
the original hook edge. Consequently, numeric error/blob/hairline results for strategies B–G
would be invented. The table below records what can be concluded causally now; the capture tool
was added to make the missing comparison measurable offline.

| Strategy | Intentional latency | Likely onset | Principal failure mode | Causal? | Current assessment |
|---|---:|---|---|---|---|
| A. Current behavior | Configuration-dependent | Real pressure/floor, sometimes after fresh-frame gating | Hairline, delayed down, or first frame still in early ramp | Yes | Baseline only |
| B. Last analog sample | 0 ms | Usually rest/baseline pressure | Underestimates nearly every clean press; stale sample after idle | Yes | Not promising alone |
| C. Fixed contact floor | 0 ms | Consistent minimum width | Blob for a deliberately light mark; discontinuity when real pressure crosses it | Yes | Useful safety bound, not a full estimator |
| D. Learned actuation pressure | 0 ms | Accurately predicts the click edge, but that edge maps to 0% | Does not solve the visible hairline | Yes | Useful for calibration validation, not onset thickness |
| E. Bounded slope extrapolation | 0 ms | Median next-frame error 4.9 mapped percentage points | p90 error 44.7 points; overshoot/blob on varied press speeds | Yes | **Reject as the primary estimator** |
| F. One fresh sample + replay | 0.8–14.9 ms typical hardware wait; sometimes more runtime | Measured but possibly still early-ramp | Visible lag; app coalesces catch-up; short tap ends before sample | Yes | Viable quality mode, not lowest latency |
| G. 4–8 ms adaptive wait | Fixed upper bound | Fresh sample when lucky, otherwise D/C estimate | Mixed behavior and a smaller but still perceptible delay | Yes | Best bounded-wait comparison |

For click-only actions, D/C can emit a pen tap immediately; F may never receive a useful sample
before release. For slow ramps, D can overstate intended pressure and therefore needs a conservative
cap plus fast convergence to real data. For flicks, F/G require path buffering and must prove that
the backend/application preserves the opening sequence. None of these approaches can causally
correct already-rendered generic ink without an application-specific repaint/undo mechanism.

## Can pressure be queried on click?

No known request returns a fresher ADC value than the unsolicited stream:

- Feature `0x1B0C` function 0/2 probe responses are stable/configuration-shaped.
- Function 3 is already used to acquire/renew the stream lease.
- Function 4 is used for flags.
- MouseButtonSpy is a digital event and contains no decoded analog force.
- HITS button records preserve actuation/rapid-trigger configuration when haptics are changed,
  but the current code does not know how to map those fields to the mode-3 raw ADC scale.

A read request would also share the HID++ transport with the stream and is not automatically
faster than the next unsolicited 16.7 ms frame. Read-only inspection is safe; undocumented writes
or fuzzing are not recommended. The practical route is to query known actuation settings where
possible and learn the raw-at-click distribution statistically.

## Generic Windows pen backends

### Synthetic pointer

Microsoft's
[`InjectSyntheticPointerInput`](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-injectsyntheticpointerinput)
documentation says `count` is the number of contacts and must be 1 for `PT_PEN`. There is no array
of historical pen points in one call. The
[`POINTER_INFO`](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-pointer_info)
structure supports either `dwTime` or `PerformanceCount`; values must be valid, monotonic, and not
both set. Calls too close in time can return `ERROR_NOT_READY`. The current implementation uses
zero for both fields, so Windows assigns current delivery time.

Applications receive move updates through
[`WM_POINTERUPDATE`](https://learn.microsoft.com/en-us/windows/win32/inputmsg/wm-pointerupdate),
which can be coalesced. A receiver can request the retained samples through
[`GetPointerInfoHistory`](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getpointerinfohistory),
but only while handling the current pointer message. Therefore rapid calls may retain ordering in
Windows yet still look like one update to an application that does not consume history.

The isolated `scripts/probe_pointer_delivery.py` harness records pen messages and
`GetPointerPenInfoHistory`. In this non-interactive command environment, injection calls succeeded
but the hidden/automated receiver did not become the target window, so no coalescing conclusion was
drawn and no result file is presented as evidence. Run it interactively before relying on catch-up:

```powershell
.\.venv\Scripts\python.exe scripts\probe_pointer_delivery.py `
  --duration 8 --synthetic --interval-ms 1 --points 24 `
  --output work\pointer_probe\synthetic-1ms.json
```

Repeat at 0.25, 4, and 8 ms. A valid run must show the probe window under the injected path and
nonzero `received_messages`.

### VMulti

The current report carries the fields a normal Windows Ink pen needs, and Windows 10 build 19045
recognizes it as a pen. However, Microsoft's
[`Required HID top-level collections`](https://learn.microsoft.com/en-us/windows-hardware/design/component-guidelines/required-hid-top-level-collections)
guidance requires X/Y physical ranges, unit, unit exponent, and sufficient resolution. The current
descriptor supplies logical X/Y ranges but omits that physical metadata. The
[`pen sample descriptors`](https://learn.microsoft.com/en-us/windows-hardware/design/component-guidelines/pen-sample-report-descriptors)
and [`Windows pen states`](https://learn.microsoft.com/en-us/windows-hardware/design/component-guidelines/windows-pen-states)
should be the basis for the next driver revision.

The manual queue implementation is also not a buffered input queue: a vendor write retrieves and
completes one pending HID read. A burst sent faster than Windows posts reads can fail or lose
reports. Before using VMulti for buffered onset replay, run the receiver externally and compare
sent sequence numbers/coordinates to delivered points:

```powershell
.\.venv\Scripts\python.exe scripts\probe_pointer_delivery.py `
  --duration 20 --output work\pointer_probe\vmulti.json
```

Then start VMulti output normally and draw across the visible probe window. No driver is installed,
replaced, or signed by these investigation scripts.

## Application compatibility

| Application | Documented input path | Basic generic pressure | Rapid replay status | Plugin still useful for |
|---|---|---|---|---|
| Krita 5.3.3 | Windows Ink via Windows 8+ Pointer Input; WinTab is an alternative | Measured in the existing project with both generic backends | Not isolated from plugin/runtime effects | Perfect-Ink final replay, preset-aware smoothing/tails, native paint lifecycle |
| Adobe Photoshop on Windows | Windows Ink default; legacy WinTab optional | Likely: Adobe documents Pen Pressure for brush Shape Dynamics | Not tested; Photoshop unavailable | Only app-native correction/re-render if generic delivery is insufficient |
| Neutral WM_POINTER receiver | Windows pointer messages/history | Harness implemented | Manual visible-window run required | Ground truth only |

Krita documents its Windows Ink option in
[`Tablet Settings`](https://docs.krita.org/en/reference_manual/preferences/tablet_settings.html)
and explains that the operating system/driver supplies tablet input in its
[`Drawing Tablets`](https://docs.krita.org/en/user_manual/drawing_tablets.html) guide. Adobe's
[`tablet support FAQ`](https://helpx.adobe.com/photoshop/kb/tablet-support-faq-photoshop.html)
documents Windows Ink and Pen Pressure.

### Photoshop manual test

Photoshop was not installed and was not automated. For each backend, record Photoshop version,
Windows version, backend, brush preset, and a screen/video capture:

1. Select Photoshop's current Windows Ink path and enable **Shape Dynamics → Control: Pen Pressure**.
2. Draw a stationary light/heavy tap, slow pressure ramp, short flick, and fast stroke.
3. Repeat with the synthetic backend and VMulti separately.
4. Compare with the neutral receiver JSON captured simultaneously where possible.

Interpret outcomes distinctly:

- No cursor/pen events: pen not recognized.
- Pen events but one brush width: recognized, pressure missing or preset not configured.
- Pressure works but the opening path jumps: the catch-up batch was coalesced/ignored.
- Continuous path and pressure matching the receiver: generic behavior is sufficient.

Native mouse clicks still need suppression while the virtual tip is active, with the existing
fail-open and force-stop behavior. Cursor movement need not be delayed merely because pen-down is
delayed, but the buffered path must keep one coordinate anchor to avoid a release/re-entry jump.

## Krita plugin compatibility and responsibility

The tested local environment is Windows 10 Home 10.0.19045, Krita **5.3.3** at git commit
`858d352e52e68831693067763b9cdaf8bb9a05ce`, Qt **5.15.7**. No other portable Krita builds were
available for a nondestructive loader matrix.

The plugin depends directly on classes and methods including `KisToolFreehand`, `KisToolPaint`,
`KisToolFreehandHelper` (`paintAt`, `paintLine`, `paintBezier`, painter creation),
`KisSmoothingOptions`, `KisCanvas2`, `KisCoordinatesConverter`,
`KisFigurePaintingToolHelper`, `KoPointerEvent`, `KoToolBase`, and paint-op preset accessors.
These are C++ symbols whose names, signatures, object layouts, and ownership can change. The local
Krita CMake file explicitly states that Krita does not guarantee C++ binary compatibility between
releases. `X-Krita-Version=28` metadata does not create an ABI contract.

Support policy:

- **Binary across 5.3.x:** never promise it. A DLL may happen to load when every imported symbol
  remains identical, but exact-patch builds are the supportable unit.
- **Source across 5.3.x:** likely high. Maintain one source tree and compile/test it against each
  supported patch in CI, adding small conditionals only when needed.
- **Krita 6 / Qt 6:** separate binary mandatory. Qt 6 changes tablet/pointer construction (for
  example, `QTabletEvent` uses a `QPointingDevice*`) and Krita internals may also differ.
- **Python plugin:** more version-stable but cannot subclass/intercept the native
  `KisToolFreehand` stroke lifecycle exposed only through C++. It cannot replace this plugin.

Import inspection command:

```powershell
& 'E:\kd\tools\llvm-mingw-20251118-ucrt-x86_64\bin\llvm-readobj.exe' `
  --coff-imports dist\krita\5.3.3\kritatoolsmousepressure.dll
```

Responsibilities that belong in the generic backend:

- hardware discovery, pressure stream maintenance, and 10-bit decoding;
- native click timing, Raw Input movement, contact/tip state, mapping, and safe suppression;
- coordinates, pressure, tilt, in-range/tip reports, cleanup, and fail-open behavior.

Krita-specific enhancements:

- Perfect-Freehand/path-assist final rendering;
- brush-preset-aware pressure smoothing or tail repair;
- repaint/undo/replay through `KisToolFreehandHelper` and Krita's native paint engine.

Basic pressure emulation should not require a Krita plugin. Any correction that replaces already
rendered raster ink necessarily needs application semantics and cannot be made universally generic.

## Safe timing capture and next experiment

The added timing observer is inert unless explicitly attached. The standalone capture enables only
the already-known pressure lease, never suppresses native clicks, never injects a pen, retains the
force-stop hotkey, times out automatically, and always performs cleanup.

Run this with the physical mouse connected:

```powershell
.\.venv\Scripts\python.exe scripts\capture_input_timing.py `
  --duration 180 --meaningful-raw 420 `
  --output work\input_timing\100-varied-presses.json
```

Collect at least 100 presses covering stationary slow/fast presses, slow starts, flicks, press-then-
move, and move-and-press. If actuation settings are changed, record each setting in a separate file.
The capture records the original hook/Raw Input times, MouseButtonSpy, every relevant feature
`0x0C` payload, decoded pressures, and derives aligned summary distributions on one monotonic clock.

For the output half of the pipeline, enable **Debug mode** and run the normal UI:

```powershell
.\.venv\Scripts\mp-dev-ui.exe
```

Opt-in stroke traces now include the original hook and Raw Input timestamps (including up to
250 ms of pre-stroke native events) alongside their existing fresh-pressure, mapped-pressure,
contact, path, injection, and pen-up events. This changes diagnostics only; the observer is absent
when Debug mode is off. Use `Ctrl+Shift+F12` to force-stop normal output if required.

The capture has now been analyzed with:

```powershell
.\.venv\Scripts\python.exe scripts\analyze_input_timing_capture.py `
  work\input_timing\input-timing.json --raw-min 379 --raw-max 652 `
  --floor-percent 15 `
  --output docs\investigation\results\input_timing_capture_summary.json
```

The smallest next experiment is not a new predictor. It is an end-to-end assertion that the first
generic pen-down carries at least the configured floor:

1. Draw a varied set with normal Debug mode enabled.
2. Compare native `raw_left_down`, first `update`, and first successful `inject(tag=contact)`.
3. Verify pressure is at least 15% on the very first injected down, with no earlier zero-pressure
   down and no coordinate correction.
4. If the trace is correct but ink is thin, use the neutral receiver to determine whether Windows
   or the application discarded/coalesced the opening frame.
5. Only if the first injection is wrong should the production contact path change.

Do not first implement generic buffering or body-pressure prediction. Buffering has measured path
latency, while prediction has unacceptable worst-case pressure error.

### End-to-end Debug trace result

The follow-up in-app run produced 144 traces between 11:39 and 11:40; 143 contained aligned Raw
Input down, pressure update, and successful contact injection events. Every one of those 143
strokes had:

- no earlier pen injection before contact;
- a valid pen-down flag on the first contact event;
- first injected pressure exactly `154/1024` (the configured 15% floor);
- no first-contact pressure below the floor.

The pressure floor is therefore not being lost between mapping and the injection call. The trace's
first `update.actual_pressure` is often zero because that diagnostic is recorded before the contact
floor is selected for injection; the actual `inject.pressure` field is authoritative.

The newly isolated latency is the wake-up path:

| Raw Input button-down to first successful pen-down | Value |
|---|---:|
| Median | 2.17 ms |
| p90 | 10.61 ms |
| p99 | 15.84 ms |
| Maximum | 16.56 ms |

Once the emitter update begins, update-to-injection is only 0.10 ms median and 0.23 ms p90. The
long tail occurs when a Raw Input button-down has no movement (`dx=dy=0`): button-down updates
state but does not signal the event-driven movement queue, so contact waits for the next pressure
processor tick. A button packet containing movement wakes immediately. This explains why the
start hiccup is subtle and intermittent.

The low-level hook and Raw Input button events were within 0.06 ms median. A few hook callbacks
were processed after the first injection; their post-injection desktop coordinates must not be
used to diagnose physical path displacement. Device-scoped Raw Input and its independent logical
position remain the authoritative onset path.

The smallest justified runtime change is now available as the per-button **Immediate stroke start
(experimental)** advanced toggle. When enabled, an accepted zero-motion Raw Input button-down
wakes the existing event-driven emission queue. It reuses the latest pressure sample and existing
15% floor, adds no buffering or prediction, and leaves coordinate selection unchanged. The toggle
defaults off so the original path remains an A/B control. Expected outcome: remove the measured
0–16.6 ms scheduling lottery while preserving the verified first-contact pressure.

The first A/B run exposed a second queue-order race: on Raw-Input-first strokes, output could begin
before the low-level hook supplied the authoritative desktop coordinate. One recorded stroke began
148 px from the hook coordinate. The experimental path now gates only that opening report for up to
4 ms (the observed hook tail was 3.1 ms), consumes movement carried by the button-down packet as
part of the click anchor, and retains any subsequent validated deltas. Hook-first strokes still wake
immediately; a missing hook falls back to the current OS cursor after the bounded wait.

## Verification performed

```text
python scripts/analyze_input_backend_evidence.py --trace-limit 500
.venv\Scripts\python.exe -m py_compile scripts/analyze_input_backend_evidence.py scripts/capture_input_timing.py scripts/probe_pointer_delivery.py
.venv\Scripts\python.exe -m pytest -q
```

Result after adding the opt-in anchor-gated wake path: **222 tests and 2 subtests passed**. The proof
scripts are isolated, and no production contact, mapping, injection, or wake default was changed.

Raw and reusable investigation artifacts:

- `docs/investigation/results/input_evidence_summary.json`
- `docs/investigation/results/input_timing_capture_summary.json`
- `docs/investigation/results/input_timing_capture_summary.csv`
- `scripts/analyze_input_backend_evidence.py`
- `scripts/analyze_input_timing_capture.py`
- `scripts/capture_input_timing.py`
- `scripts/probe_pointer_delivery.py`
