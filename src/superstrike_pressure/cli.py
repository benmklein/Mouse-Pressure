"""CLI entry points for superstrike-pressure tools."""


def sniff():
    """Discover Superstrike HID device and dump raw reports.
    
    This is your Phase 1 starting point. Run this to:
    1. Find the Superstrike's vendor/product IDs
    2. Enumerate its HID interfaces  
    3. Capture raw HID reports while you press buttons at varying pressure
    4. Identify which bytes contain analog pressure data
    """
    from superstrike_pressure.sniff.discover import run_discovery
    run_discovery()


def bridge():
    """Run the pressure bridge (Phase 2).
    
    Reads Superstrike analog data and emits virtual tablet pressure events.
    """
    from superstrike_pressure.bridge.pressure_bridge import run_bridge
    run_bridge()


def visualize():
    """Launch pressure visualization for debugging/calibration.
    
    Shows a real-time plot of pressure values from the Superstrike.
    Useful for verifying you're reading the right bytes and for
    tuning pressure curves.
    """
    from superstrike_pressure.sniff.visualize import run_visualize
    run_visualize()


def pressure():
    """Run real-time pressure reader with min/max and payload byte stats."""
    from superstrike_pressure.sniff.pressure_reader import main as pressure_main
    return pressure_main()
