"""
Phase 1: Discover and sniff the supported mouse's HID reports.

VIBE CODE INSTRUCTIONS:
-----------------------
This is the most important file in Phase 1. The goal is to:

1. Enumerate all HID devices and find the supported mouse
2. Open each of its HID interfaces and read reports
3. Print hex dumps of reports while user varies click pressure
4. Identify which bytes change when pressure changes

The supported mouse exposes multiple HID interfaces:
  - Standard mouse interface (movement, standard button clicks)
  - Vendor-specific interface (where analog data probably lives)
  
G Hub reads the analog data somehow, so it's definitely in the USB stream.
We just need to find which interface and which bytes.

APPROACH:
  - Start by enumerating everything from vendor 0x046D
  - Try opening each interface and reading from it
  - Have the user slowly press and release the left button
  - Log all reports and look for bytes that correlate with pressure
  - The wireless dongle may have different product IDs than wired

GOTCHAS:
  - On Windows, you may need to run as administrator
  - G Hub may claim exclusive access to some interfaces
  - The mouse may use Logitech's HID++ protocol for vendor data
  - hidapi on Windows uses the Windows HID API, not libusb
  - Some interfaces may require specific report IDs to read
"""

import sys
import time

LOGITECH_VENDOR_ID = 0x046D


def enumerate_logitech_devices():
    """List all Logitech HID devices. Run this first."""
    import hid
    
    print("=" * 60)
    print("Scanning for Logitech HID devices...")
    print("=" * 60)
    
    devices = [d for d in hid.enumerate() if d['vendor_id'] == LOGITECH_VENDOR_ID]
    
    if not devices:
        print("\nNo Logitech devices found!")
        print("Make sure a supported mouse is connected (wired or via dongle).")
        return []
    
    print(f"\nFound {len(devices)} Logitech HID interface(s):\n")
    
    for i, dev in enumerate(devices):
        print(f"  [{i}] Product: {dev['product_string']}")
        print(f"      Vendor ID:  0x{dev['vendor_id']:04X}")
        print(f"      Product ID: 0x{dev['product_id']:04X}")
        print(f"      Interface:  {dev['interface_number']}")
        print(f"      Usage Page: 0x{dev['usage_page']:04X}")
        print(f"      Usage:      0x{dev['usage']:04X}")
        print(f"      Path:       {dev['path']}")
        print()
    
    return devices


def sniff_interface(device_path: bytes, duration: float = 10.0):
    """Read raw HID reports from a specific interface.
    
    Args:
        device_path: The HID device path to open
        duration: How long to capture (seconds)
    """
    import hid
    
    print(f"Opening device: {device_path}")
    print(f"Capturing for {duration}s — vary your click pressure slowly!")
    print("Press Ctrl+C to stop early.\n")
    
    dev = hid.device()
    try:
        dev.open_path(device_path)
        dev.set_nonblocking(True)
    except OSError as e:
        print(f"Could not open interface: {e}")
        return
    
    start = time.time()
    report_count = 0
    last_data = None
    
    try:
        while time.time() - start < duration:
            try:
                data = dev.read(64)
            except OSError as e:
                print(f"\nRead failed on this interface: {e}")
                break
            if data:
                report_count += 1
                # Only print when data changes (avoid flooding terminal)
                if data != last_data:
                    elapsed = time.time() - start
                    hex_str = " ".join(f"{b:02X}" for b in data)
                    print(f"  [{elapsed:6.2f}s] ({len(data):2d}B) {hex_str}")
                    last_data = data
            else:
                time.sleep(0.001)  # 1ms sleep when no data
    except KeyboardInterrupt:
        pass
    finally:
        dev.close()
    
    print(f"\nCaptured {report_count} reports in {time.time() - start:.1f}s")


def run_discovery():
    """Main discovery flow."""
    devices = enumerate_logitech_devices()
    
    if not devices:
        sys.exit(1)
    
    print("-" * 60)
    print("Next step: sniff each interface to find the analog data.")
    print("Pick an interface number from above, then slowly press")
    print("and release the left mouse button while we capture.")
    print("-" * 60)
    
    try:
        choice = input("\nInterface index to sniff (or 'all' or 'q'): ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    
    if choice.lower() == 'q':
        return
    
    if choice.lower() == 'all':
        for dev in devices:
            print(f"\n{'=' * 60}")
            print(f"Sniffing: {dev['product_string']} (interface {dev['interface_number']})")
            print(f"{'=' * 60}")
            sniff_interface(dev['path'], duration=5.0)
    else:
        try:
            idx = int(choice)
        except ValueError:
            print(f"Invalid selection: {choice}")
            return
        if 0 <= idx < len(devices):
            sniff_interface(devices[idx]['path'], duration=15.0)
        else:
            print(f"Invalid index: {idx}")


if __name__ == "__main__":
    run_discovery()
