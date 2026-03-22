# Superstrike UI Open Questions Resolution

Date: 2026-03-22

This document resolves the open questions from `superstrike_ui_plan.docx` so implementation can start with fixed decisions.

## 1) Tauri 2 subprocess management (Python bridge)

Decision:
- Launch and supervise the Python bridge from the Tauri **Rust** side (app setup / tray lifecycle), not from React.
- Keep exactly one bridge child process alive while the app is running.
- Window hide/show must not affect bridge process lifetime.

Why:
- Process supervision survives frontend reloads and window state changes.
- Tauri sidecar APIs support spawn + stdout/stderr/close event handling directly.

Supervision policy:
- On startup: spawn bridge sidecar once.
- On crash/exit: auto-restart with backoff (e.g., 0.5s, 1s, 2s, 5s, max 10s).
- Crash-loop guard: if N crashes in short window, surface error in tray + UI and stop auto-restart until user clicks "Restart Bridge".
- Tray "Restart Bridge": terminate child gracefully, then spawn fresh.
- Tray "Quit": stop stream (best-effort), terminate child, then exit app.

## 2) WebSocket port conflict + startup handshake

Decision:
- Keep preferred port `27842`.
- If unavailable, Python probes next available port (or binds to 0 after trying preferred range).
- Python must print one structured readiness line to stdout as soon as WS server is bound.

Required readiness line (single line JSON):
```json
{"event":"ws_ready","host":"127.0.0.1","port":27842,"pid":12345,"version":"x.y.z"}
```

Failure line:
```json
{"event":"ws_error","message":"..."}
```

Handshake flow:
1. Tauri spawns bridge sidecar.
2. Tauri reads stdout stream and waits for `event=="ws_ready"` (timeout e.g. 10s).
3. Tauri stores selected port in app state and emits it to frontend.
4. React connects to `ws://127.0.0.1:<port>`.
5. If socket drops, React reconnects with backoff; if process dies, Tauri restart logic handles it.

Backend implementation detail:
- After `websockets.serve(...)`, read bound port via server socket (`getsockname`) and print+flush the readiness JSON line.

## 3) uPlot + React integration pattern

Decision:
- Use a **small local wrapper component** (not a heavy chart abstraction).
- Create uPlot instance once on mount, destroy on unmount.
- Feed data updates via `uplot.setData(...)` at throttled render rate (30 Hz).
- Recreate chart only when structural options change (series count/axes mode/theme), not on every frame.

Why:
- uPlot is imperative/vanilla JS; explicit wrapper keeps performance predictable and avoids accidental re-instantiation.
- Matches the requirement to ingest 60 Hz telemetry but render at 30 Hz.

## 4) Profile JSON storage location

Decision:
- Store profiles/config under app data directory (`AppData`) in a dedicated folder, e.g.:
  - `<AppData>/SuperstrikeBridge/profiles/*.json`
  - `<AppData>/SuperstrikeBridge/config.json`

Why:
- Clean install footprint and proper OS-scoped writable location.
- Avoids writing into install directory or sidecar binary location.

Interop note:
- Tauri passes resolved config dir to Python via environment variable at spawn:
  - `SUPERSTRIKE_CONFIG_DIR=<absolute path>`
- Python backend is sole owner of profile/config file IO.

## 5) shadcn Slider constraints

Decision:
- Use shadcn slider for integer stepping and bounded ranges.
- Enforce `raw_min < raw_max` in both frontend and backend.

Rules:
- Frontend prevents invalid drag crossover and shows inline validation.
- Backend remains authoritative and rejects invalid `config.patch`.
- UI displays rounded integers only; no raw float artifacts.

## Locked defaults for implementation

- Preferred WS port: `27842` (fallback allowed, discovered via stdout handshake).
- Process owner/supervisor: Tauri Rust.
- Runtime state authority: Python backend.
- Render throttle: 30 Hz UI for graph, 60 Hz telemetry ingestion.
- Config/profile storage: AppData directory managed by Python.
