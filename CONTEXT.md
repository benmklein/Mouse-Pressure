# Superstrike Pressure Bridge

## What This Project Is

A tool that reads the analog pressure data from a Logitech G Pro X2 Superstrike
mouse's HITS (Haptic Inductive Trigger System) buttons and converts it into
virtual drawing tablet pressure — enabling pressure-sensitive painting in apps
like Krita using a gaming mouse instead of a pen tablet.

## Why This Exists

The Superstrike (launched Feb 2026) has electromagnetic induction-based analog
mouse buttons with 0.6mm travel and ~60 micrometer precision. Logitech's G Hub
software can visualize the continuous pressure value in real time, which means
the analog data IS being transmitted over USB. But no drawing application can
read it as pressure input. This project bridges that gap.

Nobody has done this before. The mouse is 5 weeks old.

## Hardware

- **Mouse**: Logitech G Pro X2 Superstrike
  - HITS analog buttons on left/right click only
  - 0.6mm total button travel distance
  - 10 actuation levels configurable in G Hub
  - Inductive sensing (not mechanical switches)
  - USB HID device, also supports Lightspeed wireless via USB dongle
  - Logitech Vendor ID: 0x046D (wired) — wireless dongle may differ
  - HERO 2 sensor, up to 44,000 DPI, 8kHz polling
- **Software**: Logitech G Hub required for HITS configuration
  - G Hub shows real-time pressure visualization
  - Settings: actuation (10 levels), rapid trigger (5 levels), haptics (6 levels)

## Project Goals

### Phase 1: Sniff & Decode
- Capture raw USB HID reports from the Superstrike
- Identify which bytes contain the analog pressure values for L/R buttons
- Log and visualize the pressure data to confirm we're reading it correctly
- Document the HID report format

### Phase 2: Bridge to Drawing Apps
- Emit virtual Wintab or Windows Ink tablet pressure events
- Map Superstrike's 0.6mm analog range to 0-1023 (or 0-8191) pressure levels
- Allow configurable pressure curves (linear, ease-in, ease-out, S-curve)
- Test with Krita's AI Diffusion plugin workflow

### Phase 3: Web Bridge (Optional)
- Expose pressure data via WebHID or a local WebSocket server
- Enable pressure-sensitive input in PixiJS browser applications
- This is for a game development workflow (adult rhythm-tracking game)

### Phase 4: UI (Optional)
- Simple GUI for pressure visualization, curve adjustment, calibration
- System tray app that runs in background
- Could use PyQt6, Dear ImGui (pyimgui), or a web UI

## Technical Approach

### Reading HID Data

```python
# Option A: hidapi (recommended starting point)
import hid

# List all HID devices, find the Superstrike
for device in hid.enumerate():
    if device['vendor_id'] == 0x046D:
        print(device)

# Open and read reports
device = hid.device()
device.open(0x046D, PRODUCT_ID)  # Need to find product ID
device.set_nonblocking(True)

while True:
    data = device.read(64)
    if data:
        # Parse analog values from the report
        pass
```

```python
# Option B: pyusb (lower level, more control)
import usb.core
import usb.util

dev = usb.core.find(idVendor=0x046D, idProduct=PRODUCT_ID)
```

```python
# Option C: For web bridge later
# WebHID API in browser can read HID devices directly
# navigator.hid.requestDevice({filters: [{vendorId: 0x046D}]})
```

### Emitting Virtual Tablet Events (Windows)

- **vhid / vmulti**: Virtual HID driver that can emit tablet events
- **pyvda / wintab**: Python wintab bindings (limited)  
- **SendInput with POINTER_INFO**: Windows Pointer Input API supports pressure
- **OpenTabletDriver**: Open source tablet driver with plugin system — could
  potentially write a plugin that reads from our bridge

### Emitting Virtual Tablet Events (Linux)

- **python-evdev**: Create a virtual input device with uinput
- **evdev.UInput**: Can create a virtual tablet with pressure axis
- This is significantly easier than Windows

### Emitting Virtual Tablet Events (macOS)

- Not a priority but possible via IOKit or CGEvent

## Key Unknowns / Research Needed

1. **Product ID**: What USB product ID does the Superstrike report? Need to
   enumerate HID devices with it plugged in. May have multiple interfaces
   (one for standard mouse, one for vendor-specific data).

2. **Report format**: How are the analog values encoded in the HID reports?
   They might be in the standard mouse report (unlikely) or in a vendor-specific
   report on a separate HID interface. G Hub reads them somehow.

3. **Wireless vs Wired**: The Lightspeed dongle may present differently than
   direct USB. Need to test both.

4. **G Hub interference**: Does G Hub need to be running? Does it claim
   exclusive access to the HID interface? We may need to coexist with it
   (since users need it for HITS configuration) or find a way to read the
   same data.

5. **Report rate**: At 8kHz polling, we're getting up to 8000 reports/second.
   We don't need all of them for drawing — downsampling to ~100-200Hz is fine
   for pressure input.

6. **Pressure range**: What's the actual numeric range of the analog values?
   Could be 0-255, 0-1023, 0-4095, or something else. The 60 micrometer
   precision over 0.6mm travel suggests ~10 distinct positions minimum,
   but the sensor likely reports much more granularly.

## Dependencies

- `hidapi` — cross-platform HID access (wraps native HID APIs)
- `pyusb` — alternative/supplement for USB device discovery
- `pyqt6` or `pyimgui` — for Phase 4 UI (optional)
- `websockets` — for Phase 3 web bridge (optional)
- `numpy` — for pressure curve math
- `matplotlib` — for early visualization/debugging

## Dev Environment

- Python 3.12+
- uv for package management
- Platform: Windows primary (most drawing apps), Linux secondary

## Useful References

- USB HID specification: https://www.usb.org/hid
- Logitech HID++ protocol docs (community reverse-engineered):
  https://github.com/libratbag/libratbag (Linux mouse config tool)
- hidapi Python bindings: https://github.com/trezor/cython-hidapi
- OpenTabletDriver: https://github.com/OpenTabletDriver/OpenTabletDriver
- Wireshark USB capture: https://wiki.wireshark.org/CaptureSetup/USB
- python-evdev (Linux virtual input): https://python-evdev.readthedocs.io

## Who This Is For

This is a personal project by a game developer / former VFX artist / former
cam model who is very good with a mouse (FPS/aim trainer background) but has
never used a pen tablet. The goal is to enable a compositing-style digital
art workflow (layer blending, opacity masking, soft brush work) using familiar
mouse input with analog pressure from the Superstrike's HITS buttons.

If this works, it could also be useful for:
- Any digital artist who prefers mouse over tablet
- Accessibility (people who can't use pen tablets)
- The broader Superstrike/analog mouse community
- Integration with the developer's own PixiJS-based game
