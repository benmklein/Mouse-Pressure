# Superstrike HID Report Format

## Status: PRESSURE STREAM IDENTIFIED (wireless via Lightspeed dongle)

The Lightspeed path uses HID++ reports on receiver PID `0xC54D`. From the G Hub
USB capture, analog pressure is carried in HID++ notifications on feature index
`0x0C` (feature ID `0x1B0C`).

Update (2026-03-21, live probing):
- Mode `b4=0x01` on function/address `0x3C` gives quantized pressure `0..10` on
  `11 01 0C 00 PP ...`.
- Modes `b4=0x02` and `b4=0x03` unlock additional high-rate streams on
  `11 01 0C 10 ...`, `11 01 0C 20 ...`, and `11 01 0C 30 ...` with wider byte
  variation (candidate higher-resolution/raw channels).

## Device Identification

| Field | Wired | Wireless (Dongle) |
|---|---|---|
| Vendor ID | 0x046D | 0x046D |
| Product ID | Unknown (not tested yet) | `0xC54D` (USB Receiver) |
| Interface # (standard mouse) | Unknown | `MI_00`, Usage Page `0x0001`, Usage `0x0002` |
| Interface # (vendor/analog) | Unknown | `MI_02`, Usage Page `0xFF00`, Usage `0x0001` and `0x0002` |
| HID++ command-capable path | Unknown | `MI_02 Col02` (`usage=0x0002`) |

## Report Format

### Standard Mouse Report (expected — typical Logitech)

```
Byte 0: Report ID (maybe)
Byte 1: Button state (bit flags)
Byte 2-3: X movement (int16)
Byte 4-5: Y movement (int16)
Byte 6: Scroll wheel (int8)
```

### Vendor-Specific Reports (pressure + button event stream)

```
Observed on receiver interface #2 (endpoint 1.2.3 -> host):

Pressure notifications:
11 01 0C 00 PP 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00

Button event notifications:
11 01 0F 00 00 SS 00 00 00 00 00 00 00 00 00 00 00 00 00 00

Where:
- PP = pressure level byte, observed range 0x00..0x0A
- SS = digital state byte (observed 0x00/0x01)

Host write seen during G Hub session (SET_REPORT to 1.2.0):
10 01 0C 3C 01 3C 00

Immediate device response on interrupt stream:
11 01 0C 3C 01 3C 00 00 00 00 00 00 00 00 00 00 00 00 00 00

Interpretation:
Byte 0: report ID (`0x10` short for host write, `0x11` long for notifications)
Byte 1: device index = 0x01
Byte 2: feature index (`0x0C` for pressure, `0x0F` for MouseButtonSpy events)
Byte 3: function+swid (`0x00` for notifications, `0x3C` seen in command/echo pair)
Byte 4: pressure value (for feature index 0x0C notifications)

Feature index mapping discovered by ROOT/FEATURE_SET enumeration:
index 0x0C => feature ID 0x1B0C (unknown/vendor-specific)
index 0x0F => feature ID 0x8110 (MouseButtonSpy)

Current state:
- Continuous pressure ramps are visible in feature index 0x0C notifications
- MouseButtonSpy (0x8110) remains a separate digital button-event stream
```

## Full Feature Table (Read-only, 2026-03-21)

This table was generated with read-only HID++ queries only:
- `ROOT.GET_FEATURE(0x0001)`
- `FEATURE_SET.GET_COUNT`
- `FEATURE_SET.GET_FEATURE_ID(index)` for all indices `0x00..0x23`

No feature enable/subscribe commands were sent for this pass.

| Index | Feature ID | Type | Known in libratbag list | Name (if known) |
|---|---|---:|---|---|
| `0x00` | `0x0000` | `0x00` | Yes | Root |
| `0x01` | `0x0001` | `0x00` | Yes | FeatureSet |
| `0x02` | `0x0003` | `0x00` | Yes | FirmwareInfo |
| `0x03` | `0x0005` | `0x00` | Yes | DeviceNameType |
| `0x04` | `0x1D4B` | `0x00` | Yes | WirelessStatus |
| `0x05` | `0x0020` | `0x00` | Yes | ConfigChange |
| `0x06` | `0x1004` | `0x00` | No |  |
| `0x07` | `0x2250` | `0x00` | No |  |
| `0x08` | `0x2251` | `0x00` | No |  |
| `0x09` | `0x2202` | `0x00` | No |  |
| `0x0A` | `0x8090` | `0x00` | Yes | ModeStatus |
| `0x0B` | `0x80E0` | `0x00` | No |  |
| `0x0C` | `0x1B0C` | `0x00` | No |  |
| `0x0D` | `0x8061` | `0x00` | No |  |
| `0x0E` | `0x8100` | `0x00` | Yes | OnboardProfiles |
| `0x0F` | `0x8110` | `0x00` | Yes | MouseButtonSpy |
| `0x10` | `0x1500` | `0x00` | No |  |
| `0x11` | `0x1801` | `0x70` | No |  |
| `0x12` | `0x1802` | `0x70` | Yes | DeviceReset |
| `0x13` | `0x1803` | `0x70` | No |  |
| `0x14` | `0x1806` | `0x70` | Yes | ConfigurableDeviceProperties |
| `0x15` | `0x1817` | `0x70` | No |  |
| `0x16` | `0x1805` | `0x60` | Yes | OOBState |
| `0x17` | `0x1830` | `0x70` | No |  |
| `0x18` | `0x1877` | `0x70` | No |  |
| `0x19` | `0x9403` | `0x70` | No |  |
| `0x1A` | `0x1861` | `0x70` | No |  |
| `0x1B` | `0x1890` | `0x68` | No |  |
| `0x1C` | `0x18A1` | `0x70` | No |  |
| `0x1D` | `0x1E00` | `0x40` | Yes | EnableHiddenFeatures |
| `0x1E` | `0x1E02` | `0x60` | No |  |
| `0x1F` | `0x1E22` | `0x70` | No |  |
| `0x20` | `0x1E30` | `0x70` | No |  |
| `0x21` | `0x1602` | `0x00` | No |  |
| `0x22` | `0x1EB0` | `0x70` | No |  |
| `0x23` | `0x18B1` | `0x70` | No |  |

## Unknown Feature Candidates (not in libratbag known list)

- `index 0x06` -> `0x1004` (type `0x00`)
- `index 0x07` -> `0x2250` (type `0x00`)
- `index 0x08` -> `0x2251` (type `0x00`)
- `index 0x09` -> `0x2202` (type `0x00`)
- `index 0x0B` -> `0x80E0` (type `0x00`)
- `index 0x0C` -> `0x1B0C` (type `0x00`)
- `index 0x0D` -> `0x8061` (type `0x00`)
- `index 0x10` -> `0x1500` (type `0x00`)
- `index 0x11` -> `0x1801` (type `0x70`)
- `index 0x13` -> `0x1803` (type `0x70`)
- `index 0x15` -> `0x1817` (type `0x70`)
- `index 0x17` -> `0x1830` (type `0x70`)
- `index 0x18` -> `0x1877` (type `0x70`)
- `index 0x19` -> `0x9403` (type `0x70`)
- `index 0x1A` -> `0x1861` (type `0x70`)
- `index 0x1B` -> `0x1890` (type `0x68`)
- `index 0x1C` -> `0x18A1` (type `0x70`)
- `index 0x1E` -> `0x1E02` (type `0x60`)
- `index 0x1F` -> `0x1E22` (type `0x70`)
- `index 0x20` -> `0x1E30` (type `0x70`)
- `index 0x21` -> `0x1602` (type `0x00`)
- `index 0x22` -> `0x1EB0` (type `0x70`)
- `index 0x23` -> `0x18B1` (type `0x70`)

Confirmed pressure carrier in this capture: `index 0x0C` -> feature ID `0x1B0C`.
Other unknown features remain candidates for additional HITS metadata.

## G Hub Capture Analysis (pcapng + payload export)

Capture files analyzed:
- `docs/ghub_pressure_capture.pcapng`
- `docs/ghub_payloads.csv` (summary export; no payload bytes in `usb.capdata`)
- `docs/ghub_payloads_ext.csv` (payload-capable export with `usbhid.data`)

### 1) SET_REPORT requests (host writes)

All SET_REPORT writes in this capture:

| Count | Destination | Notes |
|---|---|---|
| `493` | `1.7.0` | Unrelated device (`VID:PID 1038:1610`), repeating 650-byte payload |
| `1` | `1.2.0` (`VID:PID 046D:C54D`) | Relevant Logitech receiver command |

Relevant Logitech SET_REPORT transaction:

| Frame | Time (s) | Direction | bRequest | wValue | wIndex | Payload bytes |
|---|---:|---|---|---|---|---|
| `2249` | `6.480798` | `host -> 1.2.0` | `0x09` | `0x0210` | `2` | `10 01 0C 3C 01 3C 00` |
| `2250` | `6.480883` | `1.2.0 -> host` | - | - | - | (status/ACK, no data) |

### 2) URB_INTERRUPT response streams

Device-to-host interrupt streams observed:

| Source -> Destination | HID bytes | Count | Notes |
|---|---:|---:|---|
| `1.2.1 -> host` | `13` | `1706` | Standard mouse movement/button path |
| `1.2.3 -> host` | `20` | `181` | HID++ vendor stream (pressure + button events) |

Header distribution on `1.2.3 -> host`:

| Header (bytes 0-3) | Count | Meaning |
|---|---:|---|
| `11 01 0C 00` | `150` | Pressure notifications |
| `11 01 0F 00` | `30` | MouseButtonSpy digital events |
| `11 01 0C 3C` | `1` | Echo/response matching host command payload |

Pressure-byte behavior inside `11 01 0C 00 ...` frames:
- Byte 4 (`PP`) smoothly ramps with slow press/release, observed `0x00..0x0A`.
- Byte 5 is almost always `0x00` (one early `0x01`).
- Bytes 6..19 are zero in this capture.

Representative pressure ramps:
- `frame 2409 -> 2459` (`9.447s -> 10.133s`): `0,1,2,3,4,5,6,7,8,9,10`
- `frame 2865 -> 2959` (`17.383s -> 19.377s`): `10,9,8,7,6,5,4,3,2,1`

### 3) Cross-reference to HID++ feature table

From prior local HID++ probing:
- Feature index `0x0C` maps to feature ID `0x1B0C` (unknown in libratbag list).
- Feature index `0x0F` maps to feature ID `0x8110` (MouseButtonSpy).

Conclusion:
- Analog HITS pressure is carried by feature index `0x0C` notifications.
- The pressure field is byte 4 of HID++ long report `11 01 0C 00 ...`.
- The single observed host write to the receiver was `10 01 0C 3C 01 3C 00`.

### 4) Request/response format summary (current best decode)

Control write (host):
- Transport: USB HID `SET_REPORT` (`bRequest=0x09`)
- Setup: `wValue=0x0210`, `wIndex=2`
- Payload: `10 01 0C 3C 01 3C 00`

Interrupt response/notifications (device):
- Pressure notification: `11 01 0C 00 PP 00 ...`
- Digital button event: `11 01 0F 00 00 SS ...`
- Command echo response: `11 01 0C 3C 01 3C 00 ...`

## Feature 0x0C Function Probe (Read-only)

Using long HID++ requests with `swid=0x08` and zero payload:
- `func0` (`addr=0x08`) -> `11 01 0C 08 00 03 28 14 14 01 ...`
- `func1` (`addr=0x18`) -> error frame `11 01 FF 0C 18 02 ...`
- `func2` (`addr=0x28`) -> `11 01 0C 28 00 04 05 14 ...`
- `func3` (`addr=0x38`) -> zero payload response
- `func4` (`addr=0x48`) -> zero payload response

This suggests function 0 and function 2 expose static configuration/capability
fields (not yet decoded), while function 1 rejects this request shape.

## 0x3C Mode Sweep (Write + Observe)

Command form tested:
- `11 01 0C 3C <mode> 3C <arg> 00...`

Observed behavior:
- `mode=0x00`: only echo response (`addr=0x3C`), no pressure stream
- `mode=0x01`: quantized pressure stream (`addr=0x00`, byte4=`0..10`)
- `mode=0x02` or `0x03`: high-rate streams on addresses `0x10/0x20/0x30`
  in addition to the `0x3C` echo

Detailed logs:
- `docs/hidpp_0c_probe_results.txt`

## Mode Comparison (from user captures)

Parsed logs:
- `docs/pressure_mode2_log.txt`
- `docs/pressure_mode3_log.txt`

Observed cadence:
- Mode `0x02`: `1564` feature-`0x0C` packets over `8.675s` -> `~180.3 Hz` total
  (`~60.2 Hz` each for addr `0x10`, `0x20`, `0x30`)
- Mode `0x03`: `754` feature-`0x0C` packets over `4.164s` -> `~181.1 Hz` total
  (`~60.3 Hz` each for addr `0x10`, `0x20`, `0x30`)

Smoothly varying payload bytes:
- Addr `0x10`: byte `4` (strong primary pressure candidate), bytes `5/7` coarse
  sub-steps (`0x00/0x40/0x80/0xC0`)
- Addr `0x20`: bytes `5` and `7`
- Addr `0x30`: bytes `5` and `7`

Cross-stream relationships (both logs):
- `addr0x30.byte5 == (addr0x20.byte5 + 0x40) mod 256`
- `addr0x30.byte7 == (addr0x20.byte7 + 0xC0) mod 256`

So `0x20` and `0x30` are mirrored/transformed representations, not independent
channels.

Primary pressure correlation check:
- In mode `0x03`, quantized legacy packets (`addr=0x00`, pressure `0..10`) are
  present in the same run.
- `addr0x10.byte4` has strong correlation with that quantized pressure
  (`r ~= 0.966`), while `addr0x20/0x30` bytes do not.

Conclusion from these captures:
- Best high-resolution pressure byte is `addr0x10.byte4`.
- Best decoded mode is `0x03` (widest observed primary range plus in-run
  quantized cross-check).

## Best-Mode Decode (Mode 0x03)

Enable command (HID++ feature `0x0C`, function/address `0x3C`):
- `11 01 0C 3C 03 3C 00 00 00 00 00 00 00 00 00 00 00 00 00 00`

Steady stream pattern (`~60 Hz` each):
1. `11 01 0C 10 A4 A5 A6 A7 00 ...`
   - `A4` (`byte4`) = primary high-resolution pressure
   - Observed range in log: `0x4E..0x9A` (`78..154`, span `76`)
2. `11 01 0C 20 4E B5 4E B7 00 ...`
   - Observed ranges (mode `0x03` log): `B5=0xC4..0xDE`, `B7=0x3F..0x6A`
3. `11 01 0C 30 81 C5 79 C7 00 ...`
   - Mirrored from stream #2:
   - `C5=(B5+0x40) mod 256`, `C7=(B7+0xC0) mod 256`

Occasional additional packets in mode `0x03`:
- `11 01 0C 00 Q 00 00 ...` where `Q` is quantized `0..10` legacy pressure
- Single control echo-like packet observed: `addr=0x3F`

Current interpretation:
- `addr0x10.byte4` should be used as the primary continuous pressure signal.
- `addr0x20/0x30` carry additional transformed telemetry that may include other
  axis/button state, but are not yet mapped to distinct left/right channels.

## Notes

- The Superstrike may use Logitech's HID++ protocol for vendor data
- HID++ uses feature-based communication — we may need to request
  specific features rather than passively reading reports
- See: https://github.com/libratbag/libratbag for Logitech HID++ docs
- G Hub's real-time pressure display confirms the data IS in the stream

## Research Trail

- 2026-03-21 — Enumerated Logitech HID devices on this host. Superstrike-over-dongle appears under receiver PID `0xC54D`.
- 2026-03-21 — `MI_02 Col02` accepts HID++ long writes and replies to ROOT/FEATURE_SET commands.
- 2026-03-21 — HID++ protocol version response: `major=4`, `minor=2`.
- 2026-03-21 — Feature table enumerated (`35` entries); `0x0C -> 0x1B0C` (unknown), `0x0F -> 0x8110` (MouseButtonSpy).
- 2026-03-21 — G Hub pcap decode identified pressure ramps in `11 01 0C 00 PP ...` with `PP=0..10`.

Raw logs:
- `docs/capture_log.txt` (60s passive capture, no dedup)
- `docs/hidpp_probe_log.txt` (initial ROOT/FEATURE_SET write probes)
- `docs/hidpp_feature_enum.txt` (full feature index enumeration)
- `docs/hidpp_8110_probe.txt` (function sweep for feature index `0x0F`)
- `docs/ghub_payloads_ext.csv` (tshark payload export with `usbhid.data`)

Safety:
- Use `scripts/hidpp_probe.py` for HID++ write probing.
- The script always runs a best-effort MouseButtonSpy disable sequence in
  `finally`/`atexit` cleanup (including interrupt handling).
