"""
Quick USB sniffing helper.

Alternative to the hidapi approach — use this if hidapi can't access
the vendor-specific interface (which G Hub may claim exclusively).

WIRESHARK APPROACH (most reliable for initial discovery):
---------------------------------------------------------
1. Install Wireshark + USBPcap (included in Wireshark installer on Windows)
2. Start capturing on the USBPcap interface
3. Filter by: usb.idVendor == 0x046d
4. Slowly press and release the left Superstrike button
5. Look for HID reports that contain changing byte values
6. The analog pressure value will show up as a byte (or two bytes)
   that smoothly increases as you press harder

TIPS:
- Compare reports when button is NOT pressed vs lightly pressed vs hard pressed
- Look for vendor-specific usage pages (0xFF00-0xFFFF)
- Logitech HID++ typically uses report ID 0x10 (short) or 0x11 (long)
- The analog value might be in a HID++ feature report, not a standard input report

PYUSB APPROACH (if hidapi doesn't work):
-----------------------------------------
Run this script to list USB configurations and endpoints.
"""

def list_usb_details():
    """List detailed USB config for Logitech devices using pyusb."""
    try:
        import usb.core
        import usb.util
    except ImportError:
        print("pyusb not installed. Run: uv pip install pyusb")
        print("On Windows, you also need a libusb backend (e.g., zadig)")
        return
    
    devices = list(usb.core.find(find_all=True, idVendor=0x046D))
    
    if not devices:
        print("No Logitech USB devices found.")
        return
    
    for dev in devices:
        print(f"\n{'=' * 60}")
        print(f"Device: {dev.product or 'Unknown'}")
        print(f"  Vendor:  0x{dev.idVendor:04X}")
        print(f"  Product: 0x{dev.idProduct:04X}")
        print(f"  Bus:     {dev.bus}")
        print(f"  Address: {dev.address}")
        
        try:
            for cfg in dev:
                print(f"\n  Configuration {cfg.bConfigurationValue}:")
                for intf in cfg:
                    print(f"    Interface {intf.bInterfaceNumber}:")
                    print(f"      Class:    0x{intf.bInterfaceClass:02X}")
                    print(f"      Subclass: 0x{intf.bInterfaceSubClass:02X}")
                    print(f"      Protocol: 0x{intf.bInterfaceProtocol:02X}")
                    for ep in intf:
                        direction = "IN" if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN else "OUT"
                        print(f"      Endpoint 0x{ep.bEndpointAddress:02X} ({direction}):")
                        print(f"        Max packet: {ep.wMaxPacketSize}")
                        print(f"        Interval:   {ep.bInterval}")
        except Exception as e:
            print(f"    (Could not read config: {e})")
            print(f"    This is normal if G Hub has the device open.")


if __name__ == "__main__":
    list_usb_details()
