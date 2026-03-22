# Codex Thread: Python Backend (WebSocket Layer)

## What you are building

You are adding a WebSocket server layer to an existing working Python application
called the Superstrike Bridge. The bridge already:
- Reads analog pressure data from a Logitech G Pro X2 Superstrike via HID++ (60 Hz)
- Injects it as Windows synthetic pen pressure via InjectSyntheticPointerInput
- Accepts CLI arguments: --hz, --curve, --raw-min, --raw-max, --deadzone-low

You are NOT modifying the core HID++ reading or injection logic. You are wrapping
it with a WebSocket server so a Tauri UI can connect and control it.

## Your deliverables

1. `bridge/ws_server.py` — asyncio WebSocket server implementing the full protocol
2. `bridge/ws_models.py` — typed dataclasses for ConfigObject, all message envelopes
3. `bridge/ws_test_client.py` — standalone test script (not shipped, for your use only)
4. Updated main entry point that starts the WS server alongside the existing loop

## Protocol contract

Follow `protocol.md` exactly. It is the shared contract with the UI thread.
Field names, message shapes, and error codes must match precisely.
Do not invent new fields or rename existing ones.

## Key requirements

### Startup handshake (CRITICAL)
On startup, after the WS server is bound, print exactly one line to stdout:
```
{"event": "ws_ready", "host": "127.0.0.1", "port": 27842, "pid": <pid>, "version": "<version>"}
```
Flush stdout immediately after. Tauri reads this to discover the port.
If binding fails, print:
```
{"event": "ws_error", "message": "<reason>"}
```
Then exit with code 1.

Port logic: try 27842 first. If unavailable, try 27843–27849. Use whatever port
binds successfully and report it in the readiness line.

### Config ownership
Python is the sole owner of config and profile file I/O.
- Config lives at: path from env var SUPERSTRIKE_CONFIG_DIR / config.json
- Profiles live at: SUPERSTRIKE_CONFIG_DIR / profiles / <name>.json
- SUPERSTRIKE_CONFIG_DIR is set by Tauri at launch. Fall back to a sensible
  default (e.g. user home / .superstrike) if not set.

### Single source of truth
Runtime config lives in a Python state object. The UI is a live editor.
When config.patch arrives, validate, apply live to the running bridge, persist
to config.json, then echo the full effective config back in the ack.

### Linked channels
When config.linked is true, always mirror left config to right before applying.
Store only one copy in memory; serialize both sides to JSON.

### Telemetry
Emit telemetry messages to all connected clients at 60 Hz while stream is active.
Use the exact field names from protocol.md: left_raw, right_raw, left_norm,
right_norm, left_mapped, right_mapped, hz.

### Heartbeat
Emit a heartbeat event every 2 seconds to all connected clients regardless of
stream state.

### Calibration
calibrate.start is an async multi-phase flow:
1. Phase "idle" — sample for 1.5s, find minimum raw value
2. Phase "light" — sample for 1.5s, find a stable low value
3. Phase "heavy" — sample for 1.5s, find maximum raw value
Emit calibrate.progress events during each phase.
On completion, apply detected raw_min/raw_max to config and emit final ack.

### Schema version
Always write schema_version: 1 to profile JSON.
On profiles.import, reject any file where schema_version != 1 with error code
schema_mismatch.

### Error handling
Every command must return either ack or error. Never leave a request_id
unanswered. Use the exact error codes from protocol.md.

## What NOT to do

- Do not modify the HID++ reading or InjectSyntheticPointerInput logic
- Do not couple anything to Tauri or the frontend codebase
- Do not use Flask, FastAPI, or any HTTP framework — plain asyncio websockets only
- Do not store config in memory only — always persist on change

## Testing your work

Use ws_test_client.py to verify each command manually. Test sequence:
1. Connect, receive first heartbeat
2. Send stream.start, verify telemetry begins at ~60 Hz
3. Send config.patch with a valid change, verify echo
4. Send config.patch with raw_min >= raw_max, verify error response
5. Send calibrate.start, verify phase events and final ack
6. Send profiles.save then profiles.list then profiles.load
7. Send profiles.import with schema_version: 2, verify schema_mismatch error
8. Send stream.stop, verify telemetry stops
