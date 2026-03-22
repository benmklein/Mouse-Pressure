# Superstrike Bridge — WebSocket Protocol Spec
Version: 1
Date: 2026-03-22

This file is the single source of truth for the WebSocket contract between the
Python bridge (backend) and the Tauri/React UI (frontend). Both implementation
threads must follow this exactly. Field names are the contract — no synonyms,
no camelCase variants, no abbreviations beyond what is listed here.

If either thread needs to deviate, update this file and manually sync the
change to the other thread before continuing.

---

## Transport

- Protocol: WebSocket (ws://, no TLS for localhost)
- Host: 127.0.0.1
- Preferred port: 27842 (fallback: next available, communicated via stdout handshake)
- All messages: UTF-8 JSON, one message per frame

---

## Startup Handshake (stdout, not WebSocket)

Python prints exactly one line to stdout when the WS server is ready:

```json
{"event": "ws_ready", "host": "127.0.0.1", "port": 27842, "pid": 12345, "version": "1.0.0"}
```

On failure:

```json
{"event": "ws_error", "message": "bind failed: address in use"}
```

Tauri reads stdout, waits for `event == "ws_ready"`, extracts `port`, then
tells React to connect. Timeout: 10 seconds.

---

## Message Envelopes

### Command (UI → Backend)

```json
{
  "cmd": "<command_name>",
  "request_id": "<uuidv4>",
  "payload": {}
}
```

### Response (Backend → UI)

```json
{
  "type": "ack" | "error" | "event" | "telemetry",
  "request_id": "<uuidv4 | null>",
  "payload": {}
}
```

- `request_id` is echoed on `ack` and `error` responses.
- `request_id` is `null` on unsolicited `event` and `telemetry` messages.
- `error` payload always contains `{ "code": "<string>", "message": "<string>" }`.

---

## Commands (UI → Backend)

### stream.start
Start the HID++ pressure stream.
```json
payload: {}
```
Response: `ack {}` or `error`

### stream.stop
Stop the HID++ pressure stream cleanly.
```json
payload: {}
```
Response: `ack {}` or `error`

### config.get
Get the full current config.
```json
payload: {}
```
Response: `ack { "config": <ConfigObject> }`

### config.patch
Apply a partial config update. Backend validates and applies live.
```json
payload: {
  "linked": true,
  "left": { "raw_min": 80, "curve": "linear" }
}
```
Response: `ack { "config": <ConfigObject> }` (full effective config echoed back)
or `error { "code": "invalid_config", "message": "..." }`

### calibrate.start
Begin guided calibration for one or both channels.
```json
payload: {
  "channel": "left" | "right" | "both"
}
```
Response: sequence of `calibrate.progress` events (see Events), then final
`ack { "result": { "left": { "raw_min": 79, "raw_max": 157 }, "right": { "raw_min": 78, "raw_max": 162 } } }`

### profiles.list
List all saved profiles.
```json
payload: {}
```
Response: `ack { "profiles": [ { "name": "krita", "modified_at": 1742000000 }, ... ] }`

### profiles.save
Save current config as a named profile (creates or overwrites).
```json
payload: {
  "name": "krita",
  "config": <ConfigObject>
}
```
Response: `ack {}` or `error`

### profiles.load
Load a named profile and apply it as the active config.
```json
payload: { "name": "krita" }
```
Response: `ack { "config": <ConfigObject> }` or `error { "code": "not_found" }`

### profiles.delete
Delete a named profile.
```json
payload: { "name": "krita" }
```
Response: `ack {}` or `error { "code": "not_found" }`

### profiles.export
Export a named profile as a JSON string for file download.
```json
payload: { "name": "krita" }
```
Response: `ack { "json": "<escaped json string>" }`

### profiles.import
Import a profile from a JSON string. Validates schema_version before applying.
```json
payload: { "json": "<escaped json string>" }
```
Response: `ack { "name": "<imported profile name>" }` or
`error { "code": "schema_mismatch", "message": "..." }`

### log.get_recent
Fetch recent log entries (for page load / catch-up).
```json
payload: { "limit": 100 }
```
Response: `ack { "entries": [ <LogEntry>, ... ] }`

---

## Unsolicited Events (Backend → UI)

These are sent proactively by the backend. `request_id` is always `null`.

### telemetry
Sent at 60 Hz while stream is active. UI must throttle rendering to 30 Hz.
```json
{
  "type": "telemetry",
  "request_id": null,
  "payload": {
    "left_raw": 95,
    "right_raw": 0,
    "left_norm": 0.19,
    "right_norm": 0.0,
    "left_mapped": 142,
    "right_mapped": 0,
    "hz": 59.992
  }
}
```
All values: integers except `left_norm`, `right_norm` (float 0.0–1.0), `hz` (float).

### heartbeat
Sent every 2 seconds regardless of stream state.
```json
{
  "type": "event",
  "request_id": null,
  "payload": {
    "event": "heartbeat",
    "status": "running" | "error",
    "device_found": true,
    "stream_active": true,
    "version": "1.0.0"
  }
}
```

### calibrate.progress
Sent during calibration sequence.
```json
{
  "type": "event",
  "request_id": null,
  "payload": {
    "event": "calibrate.progress",
    "channel": "left" | "right",
    "phase": "idle" | "light" | "heavy" | "done",
    "value": 95
  }
}
```

### config.changed
Sent when config changes from any source (e.g. another client, file reload).
```json
{
  "type": "event",
  "request_id": null,
  "payload": {
    "event": "config.changed",
    "config": <ConfigObject>
  }
}
```

### log.event
Sent immediately when a loggable event occurs.
```json
{
  "type": "event",
  "request_id": null,
  "payload": {
    "event": "log.event",
    "level": "INFO" | "WARN" | "ERROR",
    "ts": 1742000000000,
    "msg": "Stream started successfully"
  }
}
```

---

## ConfigObject Schema

```json
{
  "schema_version": 1,
  "linked": true,
  "left": {
    "raw_min": 80,
    "raw_max": 185,
    "deadzone_low": 0,
    "deadzone_high": 0,
    "curve": "linear" | "soft" | "hard" | "scurve",
    "curve_strength": 1.0,
    "contact_preset": "light" | "medium" | "firm"
  },
  "right": {
    "raw_min": 80,
    "raw_max": 185,
    "deadzone_low": 0,
    "deadzone_high": 0,
    "curve": "linear" | "soft" | "hard" | "scurve",
    "curve_strength": 1.0,
    "contact_preset": "light" | "medium" | "firm"
  },
  "app_profiles": {
    "krita.exe": "krita",
    "Photoshop.exe": "photoshop"
  }
}
```

Rules:
- When `linked: true`, backend mirrors `left` config to `right`. UI shows only left controls.
- `raw_min` must be strictly less than `raw_max`. Backend rejects otherwise.
- `deadzone_low` and `deadzone_high` are percentages: 0–20 (integer).
- `curve_strength` is float 0.5–2.0.
- `schema_version` is always `1` for this iteration. Increment if breaking changes occur.

---

## Error Codes

| Code | Meaning |
|---|---|
| `invalid_config` | Config patch failed validation (raw_min >= raw_max, out of range, etc.) |
| `not_found` | Named profile does not exist |
| `schema_mismatch` | Imported profile has incompatible schema_version |
| `device_not_found` | HID++ device not detected |
| `stream_already_active` | stream.start called while already running |
| `stream_not_active` | stream.stop called while not running |
| `internal_error` | Unexpected backend error (always include message) |

---

## Constraints Both Threads Must Respect

1. Field names are exact — no camelCase, no abbreviations not listed above.
2. Backend is sole owner of config/profile file I/O.
3. Frontend never writes to disk directly.
4. All integers displayed in UI must be rounded — no float artifacts.
5. `schema_version` must be checked on `profiles.import` before applying.
6. Telemetry is ingested at 60 Hz, rendered at max 30 Hz (UI throttles).
7. The mock server (for UI thread) must implement every command above.
