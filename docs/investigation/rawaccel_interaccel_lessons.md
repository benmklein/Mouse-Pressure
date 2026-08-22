# Raw Accel and InterAccel lessons

Reviewed against this repository at `ab4bb28e8b7422f9f92419f8321b5e00ed5671fe`.
External source revisions: Raw Accel
`53a721345617a1e29f3a16750cbdf807040cf44e`; InterAccel
`93164b64212a0a8494ebfea502a0d83052376e74`.

## Conclusion

Raw Accel does not contain a drop-in replacement for this project's pressure
output. Its decisive latency/ordering advantage comes from filtering physical
mouse packets in the kernel before Windows' Raw Input Thread. Reproducing that
advantage requires an installed, signed kernel driver, and Raw Accel still does
not create Windows pen-pressure reports.

There are two useful user-mode improvements to borrow: move more of the ordered
input/contact hot path into the existing native DLL, and make optional path
filtering depend on elapsed time rather than callback count. Most other ideas
are already present here or only matter if pressure-controlled mouse sensitivity
is added later.

## Ranked findings

### 1. Deepen the existing native relay, without adding a driver

**Potential value: high. Cost/risk: high.**

Raw Accel receives a batch of `MOUSE_INPUT_DATA` packets, transforms each packet,
then forwards the same ordered batch to the original class service callback
([driver callback](https://github.com/RawAccelOfficial/rawaccel/blob/53a721345617a1e29f3a16750cbdf807040cf44e/driver/driver.cpp#L48-L140)).
That single ordered hot path is the important architectural lesson.

This project already moved low-level-hook capture and final pen injection onto
high-priority native threads, but Python still owns contact decisions, pressure
interpolation, path preparation, and report sequencing
([native adapter](../../src/mouse_pressure/bridge/native_synthetic.py),
[stroke planner](../../src/mouse_pressure/bridge/synthetic_pen.py)). The next
meaningful latency/jitter experiment is therefore a native session that consumes
timestamped motion/button facts plus the latest pressure sample and emits the
ordered pen reports itself. Keep UI, configuration, device discovery, and HID++
maintenance in Python.

This can reduce Python wake-up and queue jitter. It cannot make the mouse and
60 Hz pressure sensor one physical stream, nor provide Raw Accel's pre-RIT
ordering guarantee.

### 2. Make path stabilization time-normalized

**Potential value: medium. Cost/risk: low to medium.**

Raw Accel adjusts smoothing coefficients using elapsed time—`1 - pow(coefficient,
time)`—so the filter response is stable across report-rate variation
([time-adjusted smoother](https://github.com/RawAccelOfficial/rawaccel/blob/53a721345617a1e29f3a16750cbdf807040cf44e/common/rawaccel.hpp#L79-L89)).
It also timestamps packet batches with a performance counter and divides the
elapsed interval across packets
([packet timing](https://github.com/RawAccelOfficial/rawaccel/blob/53a721345617a1e29f3a16750cbdf807040cf44e/driver/driver.cpp#L80-L94)).

The current path stabilizer's `alpha` depends on strength and pixel step, not
elapsed time ([current stabilizer](../../src/mouse_pressure/bridge/synthetic_pen.py)).
The native capture already records QPC-backed `observed_at`, but the stabilizer
ultimately receives coordinate tuples. Preserve timestamps through path
preparation and convert the chosen strength to a time-domain half-life. This
should make a nonzero stabilization value feel more consistent at 125, 500, and
1000 Hz and after short scheduling stalls. A/B it; default should remain zero.

### 3. Preserve fractional motion only if pressure controls sensitivity

**Potential value: conditional. Cost/risk: low.**

Both projects retain fractional X/Y remainders after scaling instead of losing
subpixel motion at every integer packet: Raw Accel keeps per-device carry
([carry handling](https://github.com/RawAccelOfficial/rawaccel/blob/53a721345617a1e29f3a16750cbdf807040cf44e/driver/driver.cpp#L107-L129));
InterAccel does the same before sending a transformed mouse packet
([InterAccel carry](https://github.com/KovaaK/InterAccel/blob/93164b64212a0a8494ebfea502a0d83052376e74/99.%20source/accel.cpp#L362-L376)).

The current pressure driver publishes absolute transformed desktop positions,
so this would not improve today's pen path. It becomes necessary if a future
feature scales relative mouse motion—such as harder pressure lowering pointer
sensitivity—otherwise low multipliers will quantize or discard movement.

### 4. Diagnostics and priority: already adopted

InterAccel raises its whole process to `HIGH_PRIORITY_CLASS`
([priority helper](https://github.com/KovaaK/InterAccel/blob/93164b64212a0a8494ebfea502a0d83052376e74/99.%20source/utils.cpp#L6-L9))
and warns that live console output can add latency
([README](https://github.com/KovaaK/InterAccel/blob/93164b64212a0a8494ebfea502a0d83052376e74/README.md#L17-L22)).
This project already uses `THREAD_PRIORITY_HIGHEST` for the two native hot-path
threads, native fixed-size queues/counters, and debug-off defaults. Raising the
entire UI/Python process would be broader and less safe; no change recommended.

## Kernel-only advantages

- One callback sees physical motion packets in their original order before the
  Raw Input Thread, avoiding low-level-hook/Raw-Input coordinate correlation.
- It edits existing mouse packets rather than suppressing one device stream and
  synthesizing another, so cursor semantics remain native across applications.
- Per-packet processing avoids ordinary user-mode scheduling on the mouse path.

Raw Accel explicitly describes itself as a signed system-space driver
([project README](https://github.com/RawAccelOfficial/rawaccel/blob/53a721345617a1e29f3a16750cbdf807040cf44e/ReadMe.md#L1-L13)).
InterAccel similarly depends on installing the Interception driver and rebooting
([InterAccel README](https://github.com/KovaaK/InterAccel/blob/93164b64212a0a8494ebfea502a0d83052376e74/README.md#L1-L20)).
Those benefits are therefore outside a no-kernel-driver release.

## Recommendation

Do not adopt Raw Accel or Interception as a dependency. First prototype only
finding 2. Consider finding 1 later if measured traces still show Python-side
wake/ordering tails large enough to justify moving the state machine across the
native boundary.
