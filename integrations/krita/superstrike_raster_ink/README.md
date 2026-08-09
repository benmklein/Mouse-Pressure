# Superstrike Raster Ink Tool

This is a native Krita tool prototype. It keeps Krita's active raster brush,
pressure sensors, layers, blending, and undo pipeline while replacing the
incoming centerline with two deliberately separate passes:

1. The raw Krita path supplies the live preview by default, adding no path
   latency. An optional causal velocity-adaptive One Euro filter can smooth the
   live preview when some trailing is acceptable.
2. An optional symmetric corner-aware pass cancels that preview at release,
   resamples it at one-pixel screen-space intervals, and replays a refined
   centerline through the same Krita brush engine. This keeps the result
   consistent across canvas zoom levels and uneven input-event spacing.

The release replay interpolates pressure, tilt, rotation, and timestamps from
the original `KoPointerEvent` stream. It emits one tablet event per screen-space
pixel, so repeated pressure reports at an unchanged coordinate cannot build a
blob. Each endpoint's local pressure profile is inspected for a low-pressure
run followed by a sustained body regime; only a detected run receives the
compact concave tail envelope. Detection is independent of total stroke length.

## Build and installation

Stage 2 has been compiled against the exact source commit used by Krita 5.3.3,
`858d352e52e68831693067763b9cdaf8bb9a05ce`, with Krita's supported Qt 5.15.7
and LLVM-MinGW toolchain. The standard Windows installer is a runtime rather
than a C++ SDK, so the reusable build environment lives under `E:\kd`.

Rebuild and install without administrator access:

```powershell
.\scripts\rebuild_krita_raster_ink.ps1
.\scripts\install_krita_raster_ink.ps1
```

The compiled DLL is copied to `dist/krita/5.3.3`. Installation uses
`%LOCALAPPDATA%\SuperstrikeKritaPlugins` and sets the per-user
`KRITA_PLUGIN_PATH`. A junction keeps all of Krita's built-in native plugins in
the same search tree. Restart Krita after installing.

To remove the per-user installation:

```powershell
.\scripts\install_krita_raster_ink.ps1 -Uninstall
```

## Defaults

- Live smoothing: disabled
- Live smoothing cutoff (when enabled): 18 Hz
- Speed response: 0.08
- Final refinement: enabled
- Final smoothing: 0.42
- Final passes: 2
- Adaptive endpoint tails: enabled
- Maximum detected tail: 72 screen px

Higher cutoff follows the cursor more closely. Higher speed response removes
more filtering as the stroke accelerates. Final refinement never changes the
first or last sample, protects gesture-sized sharp corners, and runs only after
release, so it does not add live path latency.

The matching standalone reference implementation is
`src/superstrike_pressure/ink/raster_ink.py`; its tests let us tune behavior
against captured strokes before paying the cost of another Krita build.
