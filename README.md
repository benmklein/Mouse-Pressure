# Superstrike Pressure Bridge

Turn your Logitech G Pro X2 Superstrike's analog HITS buttons into drawing
tablet pressure input. Paint with pressure sensitivity using your gaming mouse.

## Quickstart

```bash
# Install uv if you haven't
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and setup
git clone <repo>
cd superstrike-pressure
uv sync

# Phase 1: Discover your mouse
uv run ss-sniff

# Optional: HID++ feature probing (with automatic MouseButtonSpy cleanup)
uv run python scripts/hidpp_probe.py --log-file docs/hidpp_probe_safe_log.txt

# Phase 2: Run the pressure bridge
uv run ss-bridge

# Debug: Visualize pressure data
uv run ss-visualize
```

## Project Status

🔴 Phase 1: Sniff & Decode — not started
🔴 Phase 2: Bridge to Drawing Apps — not started
🔴 Phase 3: Web Bridge — not started
🔴 Phase 4: UI — not started

## How It Works

The Superstrike's HITS buttons send analog pressure data over USB HID.
This tool reads that data and re-emits it as virtual tablet pressure events,
making any drawing application think you have a pressure-sensitive tablet.

See [CONTEXT.md](CONTEXT.md) for full technical details and research notes.

## Synthetic Pen Release Notes

When using Windows synthetic pen injection with Krita, a plain release frame
(`UP | INRANGE`) can leave a short perceived tail/lag at stroke end. The bridge
supports an optional teardown sequence that fully closes the in-range session:

1. `UP | INRANGE`
2. `UPDATE | INRANGE` (hover)
3. `UPDATE` (out-of-range/end-hover)

Use `--release-teardown` with `scripts/pressure_to_pen.py` while testing or if
you observe end-of-stroke lag.

Regression tests:

```bash
uv run python -m unittest tests.test_synthetic_pen_release
```

Detailed note: [docs/release_teardown.md](docs/release_teardown.md)
