# Krita raster ink plan

The raster ink experiment belongs inside Krita rather than in the system-wide
pen bridge. The bridge should deliver the lowest-latency physical position and
pressure it can; the Krita tool can then revise the raster stroke because it
owns the brush engine, layer transaction, and undo history.

## Stage 1: algorithm and native tool source

Implemented in `integrations/krita/superstrike_raster_ink`.

- The live path is unmodified by default, so the tool adds no path filtering
  latency while the button is held.
- Optional live smoothing uses a velocity-adaptive One Euro filter.
- On release, the temporary stroke is canceled and a symmetric, corner-aware
  centerline is replayed through Krita's active brush engine.
- Final cleanup resamples the centerline at one-pixel screen-space intervals
  before filtering, so uneven mouse event spacing and canvas zoom do not change
  its visible strength.
- Replayed samples retain their original pressure, tilt, rotation, and time.
- The final pass preserves endpoints and protects deliberate large corners.
- The standalone Python implementation and captured-trace renderer allow
  tuning without rebuilding Krita.

This approach intentionally differs from Krita's Dynamic Brush. Dynamic Brush
filters the live pointer through a virtual mass and drag model, so its smooth
result necessarily trails the physical pointer. Superstrike Raster Ink makes
the expensive non-causal cleanup after release instead.

## Stage 2: build and install

Completed for Krita 5.3.3. The tool was built from the exact installed source
commit (`858d352`) using Qt 5.15.7, LLVM-MinGW 21.1.6, CMake 3.31.10, and Ninja
1.13.1. The release DLL is under `dist/krita/5.3.3` and its SHA-256 is
`941D30829687EB7715A3BA50A2FEDFBF8E62A9B76660983CCF09236DC81E57C5`.

The per-user install lives at
`%LOCALAPPDATA%\SuperstrikeKritaPlugins\kritatoolsuperstrikeink.dll`. It avoids
the administrator permission required to write into Program Files. The
`builtin` junction in that directory exposes Krita's original plugin folder,
and the per-user `KRITA_PLUGIN_PATH` points Krita at the combined tree.

The DLL's embedded Qt/KPlugin metadata was compared with Dynamic Brush, its
factory was loaded against the installed Krita runtime, and a headless Krita
export succeeded with the combined plugin tree. Restart the already-running
Krita process before the UI smoke tests below.

Acceptance checks:

1. The tool appears next to Freehand and Dynamic Brush after restart.
2. With **Smooth live preview** off, its live cursor/path timing matches
   Freehand.
3. Releasing a stroke leaves one undo entry, not the preview plus the final.
4. Pixel, color-smudge, textured, eraser, and blended presets keep their native
   Krita behavior.
5. Pressure, tilt, assistants, canvas rotation, zoom, and mirrored canvas are
   preserved.

## Stage 3: tune with real strokes

The first matched Freehand/Superstrike comparison showed that the gesture was
preserved but short positional wobble survived the original three-sample
filter. The final pass now resamples by screen-space arc length, uses a wider
triangular window, and detects deliberate corners over a gesture-sized span.
This is release-time-only processing: live latency is unchanged while **Smooth
live preview** is disabled. The centerline pass itself leaves pressure
untouched; short-stroke pressure shaping is a separate, narrowly gated step.

Short flicks exposed a second effect: the sensor's otherwise smooth temporal
pressure ramp could occupy 20-30 screen pixels and render as a thin stem before
the full-width body. Final replay now emits exactly one spatially interpolated
tablet event per screen-space pixel, collapsing stationary pressure updates so
they cannot repeatedly paint a blob at one coordinate. Each endpoint is
examined independently: a smoothed local pressure profile must begin below 65%
of its robust body level and then cross and sustain the body regime. Only that
detected span receives the concave tail envelope. This works on long and short
strokes without changing stable-pressure endpoints. Tool Options exposes an
enable toggle and a maximum detection distance, defaulting to 72 screen pixels.

The next tuning check is another matched signature at the same zoom. Adjust
**Final smoothing** before adding passes: lower it toward `0.30` if intended
curves are rounded, or raise it toward `0.60` if short wobble remains.

If release-time replacement flickers or competes with Krita's asynchronous
renderer, move the preview and final replay into one dedicated stroke strategy
so Krita serializes cancellation and replacement internally.
