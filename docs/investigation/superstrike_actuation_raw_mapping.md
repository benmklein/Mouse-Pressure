# Superstrike actuation level to raw-pressure mapping

## Answer

No known protocol source maps actuation levels 1 through 10 to the 10-bit raw
pressure codes seen around 300 through 700. Logitech defines actuation as
keyplate travel distance, with lower levels shallower and more sensitive. It
does not publish millimeters, percentages, ADC codes, or an equal-spacing
formula. See the official [setup guide, pages 9 and 11](https://www.logitech.com/content/dam/support/g-mice/pro-x-superstrike/user-manual/pro-x2-superstrike-setup-guide-amr.pdf).
The settings API and ADC stream use different representations:

- Actuation is a logical depth from 1, shallow, through 10, deep. On the wire it
  is `logical << 2`, so levels 1 through 10 are `0x04, 0x08, ... 0x28`.
- Raw pressure is a 10-bit sensor code decoded from the mode-3 address-`0x10`
  stream as `(high_byte << 2) | (low_byte >> 6)` for each button.

So `level 1 = raw 350`, `level 2 = raw 400`, and similar tables are
unsupported guesses. The relationship may be nonlinear and may differ by
button, unit, temperature, calibration, and press direction. Measure it.

Confidence is high for the wire encoding and low for any numeric ADC mapping
until a level sweep is captured.

## Measured mapping on the development mouse

A guided capture on 2026-08-21 measured five slow presses at every level for
each button. Raw Input button edges and the adjacent 60 Hz pressure samples
used the same monotonic clock; the values below are linearly interpolated at
the edge. All 100 trials had increasing pressure around the edge and sample
gaps below 20.5 ms.

| Actuation level | Left median raw | Right median raw |
|---:|---:|---:|
| 1 | 334.9 | 344.3 |
| 2 | 335.5 | 349.2 |
| 3 | 347.8 | 359.4 |
| 4 | 359.6 | 368.4 |
| 5 | 376.6 | 382.5 |
| 6 | 395.1 | 399.6 |
| 7 | 416.0 | 421.0 |
| 8 | 449.0 | 446.2 |
| 9 | 478.2 | 463.0 |
| 10 | 517.3 | 488.4 |

The result is monotonic but neither linear nor identical between buttons.
Left levels 1 and 2 overlap at this sample size. This is enough to reject a
fixed formula such as 50 raw units per level and to justify exposing the
firmware's discrete 1 through 10 actuation setting when tactile alignment
matters. It is not a universal lookup table: production code should read the
logical hardware level, and any raw threshold estimate should be calibrated
per button and device. More repetitions are required for percentile bounds.

Capture: [`actuation_raw_mapping_capture.json`](actuation_raw_mapping_capture.json)

## Known `0x1B0C` encoding

Resolve feature ID `0x1B0C` through HID++ `IRoot.getFeature`; do not assume its
runtime feature index is always `0x0C`. HID++ long reports have report ID,
device index, feature index, function/software-ID byte, then parameters. See
Logitech's [HID++ packet structure](https://github.com/Logitech/cpg-docs/blob/master/hidpp20/README.rst#transport-layer) and the independent
[Superstrike capture notes](https://github.com/kazehana99k/Logitech-PRO-X2-SUPERSTRIKE-Linux-haptics-actuation-control-/blob/main/PROTOCOL.md#2-core-analoghits-feature--0x1b0c).

The device exposes these configuration functions:

| Function | Request | Response or effect |
|---|---|---|
| 0, capabilities | none | `[flags, button_count, max_act<<2, max_rt<<2, max_haptics<<2, ...]` |
| 2, get config | `[button]` | `[button, act<<2, rt<<2\|flag, haptics<<2]` |
| 1, set config | full four-byte record | Applies live |

For the PRO X2 Superstrike, the reported maxima decode to actuation 10, rapid
trigger 5, and haptics 5. Buttons 0 and 1 are left and right. The firmware
reports a third button, but current tools expose only the two user-accessible
main buttons. The full implementation is visible in
[`analog.go`](https://github.com/mclol0/linux-superstrike/blob/42588e73b7dc9875e2b6094a7f25081c73b14d0d/internal/hidpp/analog.go).

The exact actuation bytes are:

| Logical level | Wire byte |
|---:|---:|
| 1 | `0x04` |
| 2 | `0x08` |
| 3 | `0x0C` |
| 4 | `0x10` |
| 5 | `0x14` |
| 6 | `0x18` |
| 7 | `0x1C` |
| 8 | `0x20` |
| 9 | `0x24` |
| 10 | `0x28` |

Solaar initially treated the wire bytes as logical values. Values 1 through 3
then set reserved low bits and the mouse rejected them. The merged fix shifts
all three settings left by two on write and right by two on read. It also
preserves rapid-trigger byte bit 0, a firmware-managed sensitivity flag. The
fix was confirmed on hardware. See [Solaar PR #3207](https://github.com/pwr-Solaar/Solaar/pull/3207) and
[issue #3202](https://github.com/pwr-Solaar/Solaar/issues/3202).

Logitech says the haptic impulse occurs at the exact actuation crossing. See
the official [haptics support article](https://support.logi.com/hc/en-gb/articles/37935435691159-How-do-I-adjust-the-click-haptics-intensity-on-my-PRO-X2-SUPERSTRIKE-mouse).
This supports using the native digital down as the pen-contact trigger. It does
not provide a raw ADC threshold for any level.

Function 1 replaces the complete per-button record. A writer that changes only
actuation must first read function 2, replace byte 1, and preserve the rapid
trigger byte and haptic byte exactly. In particular, reconstructing the rapid
trigger byte from its displayed level can lose bit 0 and cause
`INVALID_ARGUMENT`.

## What the raw values mean

This repository's mode-3 decoder reads address `0x10` as two 16-bit big-endian
channels and shifts each right by 6. That produces independent 10-bit left and
right codes. See [`extract_mode3_lr_pressure_raw`](../../src/mouse_pressure/sniff/hidpp_pressure.py)
and its [resolution test](../../tests/test_hidpp_pressure.py).

Existing captures establish only the raw stream's observed behavior:

- idle values sit around 300;
- pressed values observed so far extend beyond 600;
- address `0x10` supplies both channels near 60 Hz;
- all four low two-bit codes occur, confirming real 10-bit amplitude resolution;
- the digital mouse-down edge is effectively simultaneous with MouseButtonSpy,
  while the next raw frame can arrive almost 17 ms later.

See the repository's [input evidence summary](results/input_evidence_summary.json)
and [input backend investigation](input_backend_feasibility.md#confirmed-by-repository-data-or-source).

None of function 0, function 1, or function 2 contains an ADC threshold. The
actuation byte is a level, not a sensor code. Known function-0 and function-2
responses are capability and configuration records, while the 10-bit ADC values
arrive through the separately enabled monitoring stream. No upstream source
publishes a conversion formula.

The existing 112-press capture measured an interpolated click-edge median of
`378.89`, p90 `397.90`, and maximum `435.58` at one fixed, unrecorded hardware
actuation level. Its configured software `raw_min` was `379`. That close match
validates the interpolation for that run, but it does not identify the hardware
level or establish a ten-level mapping. See the [capture analysis](input_backend_feasibility.md#new-112-press-physical-capture).

## Safe read and write lifecycle

For an empirical sweep, use this order:

1. Stop G HUB or any other process controlling HID++. Concurrent access can
   return `ERR_BUSY`; the Windows capture notes observed this directly.
2. Open the supported HID++ command collection and resolve feature `0x1B0C`
   through `IRoot.getFeature`.
3. Read capabilities, then read and retain the full function-2 record for both
   buttons. Save the exact bytes, not only decoded levels.
4. Arm restoration before the first write. The restore payload must include
   actuation, rapid-trigger byte including its flag, and haptics for both
   buttons. An `atexit` handler alone does not cover process termination or
   power loss.
5. Pause the raw-pressure monitoring lease before changing configuration. Use
   the repository's known config unlock, full-record function-1 write, and
   commit/lock sequence. Put commit/lock in `finally`.
6. Read function 2 back and require an exact match. A HID++ echo proves packet
   acceptance, not physical behavior.
7. Restore the prior monitoring flags, reacquire the short raw-ADC lease, and
   renew it while capturing.
8. After each level or on any failure, pause monitoring and restore the exact
   original function-2 records. Verify them before releasing crash recovery.

The current production lease already pauses pressure streaming around settings
changes and restores original device state on stop or failed startup. See
[`TemporaryDeviceSettingsLease`](../../src/mouse_pressure/runtime/device_settings_lease.py).
An actuation experiment must extend its snapshot concept to the full button
records. The current persisted snapshot contains only DPI and haptic levels, so
it is not sufficient crash recovery for actuation writes.

Direct function-1 writes apply live according to two independent Superstrike
implementations. This repository's capture-derived write path also wraps config
writes. Keep that established local sequence for the experiment rather than
introducing a second write lifecycle. Do not fuzz undocumented functions or
out-of-range bytes.

## Experiment to build the lookup table

Run separate sweeps for left and right. Do not combine them.

1. Fix rapid trigger and haptic strength. Record their exact raw config bytes,
   firmware version, connection mode, and the resting raw value.
2. For one actuation level, enable the mode-3 raw stream and capture every
   address-`0x10` sample plus device-scoped Raw Input down/up events on the same
   monotonic clock.
3. Make at least 50 presses. Prefer 100. Include 25 slow ramps, 25 ordinary
   clicks, 25 fast clicks, and 25 releases followed by immediate represses.
   Slow ramps reduce interpolation error; the other groups reveal dynamic and
   rapid-trigger effects.
4. Repeat for all levels 1 through 10. Randomize level order or sweep up then
   down to expose drift and hysteresis.
5. For each down edge, estimate raw pressure at the edge from the samples
   immediately before and after it. Linear interpolation is acceptable for
   slow monotonic ramps. Also retain both bounding samples, their ages, slope,
   and the first post-edge sample. Reject trials with missing bounds, a gap over
   25 ms, non-monotonic pressure near the edge, or another button held.
   Timestamp Raw Input independently when it arrives. Do not poll button state
   only when a 60 Hz ADC frame arrives; that loses the edge time and makes the
   result depend on press speed and stream phase.
6. Report per level and per button: median interpolated raw, median absolute
   deviation, 5th and 95th percentiles, preceding/following sample age, and
   press-speed group. Fit an isotonic lookup only if medians are monotonic.
7. Repeat levels 1, 5, and 10 after the full sweep. A changed median indicates
   drift, warming, or mechanical settling.

The resulting table should look like this, with measured distributions rather
than single invented thresholds:

| Button | Level | Median raw at down | P05 | P95 | Trials | Slow/normal/fast bias |
|---|---:|---:|---:|---:|---:|---:|
| Left | 1 | pending | pending | pending | 0 | pending |
| Left | 2 | pending | pending | pending | 0 | pending |
| ... | ... | ... | ... | ... | ... | ... |
| Right | 10 | pending | pending | pending | 0 | pending |

### Verify haptic alignment

Logitech specifies that the haptic fires at the actuation crossing. Raw Input
down still measures firmware activation, not the physical impulse. To verify
the whole path rather than rely on the specification, attach a piezo disc or
accelerometer to the button shell and record it with a timestamped acquisition
channel. A microphone is a weaker fallback because mechanical bottom-out also
makes sound.

For each trial, compare three times:

- interpolated raw threshold crossing;
- device-scoped Raw Input down;
- haptic impulse onset or peak.

Report haptic-minus-down latency and haptic-minus-threshold latency by level.
If the haptic and digital down stay aligned while raw-at-down changes with the
configured level, pen contact triggered by native down will align with the felt
click. If they do not, software cannot guarantee tactile alignment from the
level setting alone.

## Decision rule

Use native actuation levels, not a free 0 through 100 percent threshold, when
exact haptic alignment matters. After measurement, a requested raw threshold
may be rounded to the nearest measured level, but label that as quantized and
device-specific. A continuous software threshold between hardware levels will
not move the firmware haptic to that threshold.
