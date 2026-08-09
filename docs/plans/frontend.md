# Codex Thread: Tauri + React Frontend (UI Layer)

## What you are building

A desktop configuration UI for the Superstrike Bridge — a tool that turns a
gaming mouse into a pressure-sensitive drawing tablet. The UI opens from a
system tray icon, communicates with a Python backend over a local WebSocket,
and lets users configure pressure curves, calibrate, and manage profiles.

The Python backend is being built in a parallel thread. You will develop against
a mock server (`scripts/mock_ws_server.js`) that implements the full protocol. When both
threads are done, pointing the UI at the real backend is the only integration step.

## Stack (locked, do not change)

- Tauri 2
- React 18 + TypeScript
- shadcn/ui (copy-into-project model) + Tailwind CSS
- uPlot for the real-time pressure graph
- WebSocket (no socket.io, no libraries — native browser WebSocket API)

## Files provided

- `../web/protocol.md` — the WebSocket contract. Follow it exactly.
- `scripts/mock_ws_server.js` — fake backend. Run from the repository root with:
  `node scripts/mock_ws_server.js`
  It prints a readiness line to stdout and opens ws://127.0.0.1:27842.

## Architecture

### Process model (Tauri Rust side)
- On app startup: spawn the Python bridge as a Tauri sidecar (for real backend)
  OR connect to mock_ws_server during development
- Read stdout from the child process, wait for the readiness line:
  `{"event": "ws_ready", "host": "...", "port": 27842, ...}`
- Extract port, store in app state, emit to frontend via Tauri event
- Window close-requested: hide window (do NOT quit or kill bridge)
- Tray menu items: Open, Restart Bridge, Quit
- Crash/exit: auto-restart with backoff (0.5s, 1s, 2s, 5s, max 10s)
  After 5 rapid crashes, stop and surface error in tray + UI

### WebSocket (React side)
- Connect to ws://127.0.0.1:<port from Tauri event>
- Reconnect with exponential backoff on disconnect
- All commands use the envelope from `../web/protocol.md` with a uuidv4 request_id
- Maintain a pending request map: { request_id -> { resolve, reject, timeout } }
- Match ack/error responses to pending requests by request_id
- Unsolicited events (heartbeat, telemetry, log.event, config.changed) go
  to an event bus / context

### State management
- All runtime state lives in the Python backend. React is a live editor.
- On connect: send config.get, populate local UI state from response
- On config.changed event: update local UI state (another source changed it)
- Local UI state is ONLY for rendering. On every slider release / select change,
  send config.patch immediately and update from the ack response.
- Do NOT send config.patch on every slider drag tick — only on commit (mouseup)

## UI Structure

### Window
- 800x560px, not resizable (MVP)
- Title bar: "Superstrike Bridge" | connection status pill | Hz readout
- Left sidebar navigation (130px wide)
- Main content area (remaining width)
- Footer bar: profile strip (left) | Calibrate + Save buttons (right)

### Sidebar nav items
1. Pressure (default)
2. Haptics (stub — show "coming soon" placeholder)
3. Profiles
4. Diagnostics
5. About

### Page: Pressure

Section 1 — Live Signal:
- Channel tabs: Left / Right / Both
- Linked/Unlinked badge toggle (right-aligned in same row)
- uPlot graph, scrolling, 30 Hz render (throttle telemetry in a ring buffer)
- Below graph: three stat chips — current raw | normalized | mapped

Section 2 — Mapping:
- Raw Min slider (integer, 50–150)
- Raw Max slider (integer, 120–220)
- Dead Zone slider (0–20, labeled as %)
- Curve selector: four cards with SVG previews — Linear / Soft / Hard / S-Curve
- When linked: one set of controls. When unlinked: Left column | Right column

Section 3 — Contact Behavior:
- Three buttons: Light Touch / Medium / Firm
- Maps to contact_preset field in config
- No raw values shown

Section 4 — Session Stats:
- Three metric chips: Avg Pressure | Peak L/R | Update Rate (Hz)
- Populated from telemetry, reset on stream start

### Page: Profiles
- List of profiles (name, last modified)
- Active profile highlighted
- Per-profile actions: Load, Rename, Duplicate, Delete, Export JSON
- New Profile button (prompts for name, saves current config)
- Import JSON button (sends profiles.import, handles schema_mismatch error)
- App assignment table: process name → profile name (editable, two columns)

### Page: Diagnostics
- Session log: scrolling list of timestamped events from log.event stream
- Filter by level: ALL / INFO / WARN / ERROR
- Save Log button (exports to .txt via Tauri dialog)
- Signal Lab section (collapsed by default): shows raw byte values from telemetry

### Page: About
- App version, bridge version (from heartbeat payload)
- Link to GitHub (placeholder)
- Credits

### First-run onboarding
- Shown when no config exists (backend sends a flag in first heartbeat or config.get)
- Blocking overlay (not a separate route)
- Three steps: Welcome → Calibrate → Done
- Calibrate step sends calibrate.start { channel: "both" } and shows phase progress
- Done step dismisses overlay, default profile is active

## uPlot integration

- Create a small wrapper component: `<PressureGraph data={...} />`
- Create uPlot instance once on mount (useEffect, empty deps), destroy on unmount
- Feed updates via `uplot.setData(newData)` — do NOT recreate on each frame
- Recreate only when channel mode changes (Left / Right / Both)
- Ring buffer size: 300 samples (5 seconds at 60 Hz)
- Left channel: blue (#378ADD). Right channel: orange (#EF9F27).
- Both channels: render two series on the same plot

## shadcn/ui components to use

- Slider — for raw min/max/deadzone controls
- Tabs — for channel selector (Left/Right/Both)
- Badge — for Linked/Unlinked toggle, status pill
- Card — for curve selector options and stat chips
- Button — all actions
- ScrollArea — for profile list and session log
- Separator — between sections
- Dialog — for profile name input, import, rename

## Design rules

- Light mode primary, dark mode must work — use CSS variables, never hardcode colors
- No gradients, no drop shadows on cards
- All slider readouts: integers only, no float artifacts
- Left channel consistently blue, right channel consistently orange throughout
- Status pill: green = connected, red = disconnected, amber = reconnecting
- Font: system font stack via Tailwind default

## What NOT to do

- Do not write to disk from React — all persistence goes through WebSocket commands
- Do not send config.patch on slider drag (only on release)
- Do not use socket.io or any WebSocket library — native WebSocket API only
- Do not import uPlot via React wrapper packages — use vanilla uPlot directly
- Do not store sensitive state in localStorage

## Development workflow

1. Run `node scripts/mock_ws_server.js` in one terminal
2. Run `npm run tauri dev` in another
3. The mock server handles all commands and emits realistic fake telemetry
4. The real backend is a drop-in replacement — no UI code changes needed at merge

## Build order (follow this sequence)

1. Tauri shell: tray icon, window hide-on-close, sidecar stdout reading
2. WebSocket connection layer: connect, reconnect, request/response map
3. Connection status display in title bar (uses heartbeat)
4. Live pressure graph (uPlot wrapper, fake data from telemetry)
5. Mapping controls (sliders + curve selector) wired to config.patch
6. Linked/Unlinked toggle
7. Calibration flow (multi-step, uses calibrate.progress events)
8. Profiles page
9. Diagnostics page (session log)
10. First-run onboarding overlay
11. About page, polish, packaging
