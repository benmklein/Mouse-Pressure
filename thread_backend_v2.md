# Codex Thread: Python Backend — Implementation Plan v2
# Superstrike Bridge WS Layer + Pre-WS Cleanup

Date: 2026-03-22
Supersedes: thread_backend.md

This document is the authoritative implementation guide for the backend sprint.
All five open decisions from the audit are resolved here. Do not re-open them.
Follow the build order exactly — pre-WS cleanup must be complete before any WS
code is written.

---

## Resolved Decisions (Locked)

**1. Curve name mapping**
Protocol names map to internal math names as follows:
- `linear`  → `linear`
- `soft`    → `ease_in`
- `hard`    → `ease_out`
- `scurve`  → `s_curve`

Add `normalize_curve_name(name: str) -> str` in `curves.py`.
Accept protocol names at all entry points (CLI + WS).
Keep old names as deprecated aliases in CLI only — WS accepts protocol names only.

**2. Right channel — MVP policy**
Right channel is decoded, included in telemetry, and stored in config/profiles.
Right channel has NO effect on pen injection in this sprint.
In `synthetic_pen.py`, add a clearly marked TODO:
```python
# TODO: right channel injection — eraser mode / haptics / symmetry control
# Currently telemetry + config only. See thread_backend_v2.md decision 2.
```

**3. Canonical synthetic pen module**
`scripts/pressure_to_pen.py` logic is the canonical implementation.
New home: `src/superstrike_pressure/bridge/synthetic_pen.py`
Existing scripts become thin CLI wrappers over this module.
`tablet_emitter.py` synthetic logic is removed after parity check; VMulti path
in `tablet_emitter.py` remains untouched.

**4. Deadzone representation**
Protocol and config schema store deadzones as integers 0..20 (percent).
Internal curve engine uses normalized floats 0.0..1.0.
Conversion happens at the boundary in `models.py` validation, nowhere else.

**5. Launch-time lock list**
`mode`, `mode_arg`, `backend`, `source`, `pipe_name`, VMulti flags/paths, and
logging destinations are launch-time-only. They are NOT exposed via WS or
included in RuntimeConfig. They live in LaunchConfig (process args/env only).

---

## What Stays Out of Scope This Sprint

Do not touch these. They are acknowledged debt, not this sprint's work:
- VMulti path cleanup or rework
- Automated test suite
- Docs rewrite or stale doc cleanup
- Exploratory scripts (haptic_*, hidpp_probe, USB/pcap tooling)
- 120/240 Hz cursor interpolation
- Multi-client arbitration beyond basic broadcast
- Right channel pen injection

---

## Phase 1: Pre-WS Cleanup

Complete all of Phase 1 before writing any WS code. Each task here unblocks
the WS layer — doing them out of order will cause rework.

### Task 1.1 — Canonical synthetic pen module

Create: `src/superstrike_pressure/bridge/synthetic_pen.py`

This module contains:
- Win32 `InjectSyntheticPointerInput` injection logic
- Contact state machine (idle / hovering / contact)
- All gating parameters as a dataclass: `SyntheticPenConfig`
- Public API:
  ```python
  class SyntheticPenEmitter:
      def __init__(self, config: SyntheticPenConfig): ...
      def update(self, left_mapped: int, right_mapped: int) -> None: ...
      def release(self) -> None: ...
  ```

Source: port the logic from `scripts/pressure_to_pen.py` — this is the
canonical implementation. Do not port logic from `tablet_emitter.py`
synthetic path.

Parameters to expose in `SyntheticPenConfig`:
- `contact_threshold: int` (default 10, mapped range 0..1023)
- `release_threshold: int` (default 6, mapped range 0..1023)
- `contact_source: str` (default "lmb_and_pressure")
- `pressure_mode: str` (default "absolute")
- `rise_per_frame: int` (default 256, clamped 0..1024)
- `fall_per_frame: int` (default 512, clamped 0..1024)
- `min_contact_pressure: int` (default 0, clamped 0..1024)
- `suppress_lmb: bool` (default False)
- `no_click_through: bool` (default False)
- `click_max_ms: int` (default 220)
- `click_move_px: int` (default 6)
- `click_pressure_max: int` (default 12)

After creating this module, update `scripts/pressure_to_pen.py` to be a thin
wrapper: parse CLI args, build `SyntheticPenConfig`, instantiate
`SyntheticPenEmitter`, run loop. No logic should remain in the script itself.

Remove duplicated synthetic state machine from `tablet_emitter.py` after
confirming the wrapper still produces identical behavior via manual smoke test.

### Task 1.2 — Curve name normalization

In `curves.py`, add:
```python
_CURVE_ALIASES = {
    "soft": "ease_in",
    "hard": "ease_out",
    "scurve": "s_curve",
    # legacy names kept as aliases
    "ease_in": "ease_in",
    "ease_out": "ease_out",
    "s_curve": "s_curve",
    "linear": "linear",
}

def normalize_curve_name(name: str) -> str:
    normalized = _CURVE_ALIASES.get(name.lower())
    if normalized is None:
        raise ValueError(f"Unknown curve name: {name!r}")
    return normalized
```

Update all CLI `choices=` lists to accept protocol names as primary.
Keep old names working. WS layer will only ever send protocol names.

### Task 1.3 — Config split: RuntimeConfig vs LaunchConfig

Create: `src/superstrike_pressure/bridge/config.py`

```python
@dataclass
class ChannelConfig:
    raw_min: int = 80
    raw_max: int = 185
    deadzone_low: int = 0        # percent 0..20
    deadzone_high: int = 0       # percent 0..20
    curve: str = "linear"        # protocol name
    curve_strength: float = 1.0  # 0.5..2.0
    contact_preset: str = "medium"  # light|medium|firm

@dataclass
class RuntimeConfig:
    schema_version: int = 1
    linked: bool = True
    left: ChannelConfig = field(default_factory=ChannelConfig)
    right: ChannelConfig = field(default_factory=ChannelConfig)
    app_profiles: dict[str, str] = field(default_factory=dict)

@dataclass
class LaunchConfig:
    mode: int = 3
    mode_arg: int = 0
    backend: str = "synthetic"
    hz: float = 60.0
    log_file: str | None = None
    config_dir: str | None = None  # from SUPERSTRIKE_CONFIG_DIR env var
```

Contact preset → threshold mapping (hardcoded, not user-configurable):
```python
CONTACT_PRESETS = {
    "light":  {"contact_threshold": 6,  "release_threshold": 4},
    "medium": {"contact_threshold": 10, "release_threshold": 6},
    "firm":   {"contact_threshold": 18, "release_threshold": 12},
}
```

### Task 1.4 — Validation layer

In `src/superstrike_pressure/web/models.py`, add a single validation function
used by both CLI parsing and WS command handling:

```python
def validate_channel_config(ch: dict) -> list[str]:
    """Returns list of error strings. Empty = valid."""
```

Validation rules (enforce all):
- `raw_min < raw_max` (strict less than)
- `raw_min` in 50..150, `raw_max` in 120..220
- `deadzone_low` in 0..20, `deadzone_high` in 0..20
- `deadzone_low <= deadzone_high`
- `curve` must be one of: linear, soft, hard, scurve (protocol names only)
  (normalize via `normalize_curve_name` before storing)
- `curve_strength` in 0.5..2.0
- `contact_preset` one of: light, medium, firm

Profile name validation:
- 1..64 characters
- Alphanumeric, spaces, hyphens, underscores only
- Not empty after strip

Process name validation (app_profiles keys):
- Must end in .exe
- No path separators
- 1..128 characters

Deadzone unit conversion (call at boundary only, in models.py):
```python
def deadzone_pct_to_float(pct: int) -> float:
    return pct / 100.0
```

---

## Phase 2: WS Layer Implementation

Build in this exact order. Each task depends on the previous.

### Task 2.1 — Config and profile persistence

Files:
- `src/superstrike_pressure/web/config_store.py`
- `src/superstrike_pressure/web/profile_store.py`

Config store:
- Reads/writes `<config_dir>/config.json`
- `load() -> RuntimeConfig` — returns defaults if file missing
- `save(config: RuntimeConfig) -> None` — atomic write (write temp, rename)
- `config_dir` resolved from: `SUPERSTRIKE_CONFIG_DIR` env var → fallback
  `~/.superstrike/`

Profile store:
- Reads/writes `<config_dir>/profiles/<name>.json`
- `list() -> list[dict]` — returns `[{name, modified_at}, ...]`
- `save(name, config: RuntimeConfig) -> None`
- `load(name) -> RuntimeConfig` — raises `ProfileNotFoundError` if missing
- `delete(name) -> None` — raises `ProfileNotFoundError` if missing
- `export_json(name) -> str` — returns pretty-printed JSON string
- `import_json(json_str) -> str` — validates schema_version, saves, returns name

Both stores: validate `schema_version == 1` on read. Raise `SchemaMismatchError`
if not. Do not silently migrate.

### Task 2.2 — Log bus

File: `src/superstrike_pressure/web/log_bus.py`

```python
class LogBus:
    def __init__(self, maxlen: int = 500): ...
    def info(self, msg: str) -> LogEntry: ...
    def warn(self, msg: str) -> LogEntry: ...
    def error(self, msg: str) -> LogEntry: ...
    def get_recent(self, limit: int = 100) -> list[LogEntry]: ...
    def subscribe(self, callback: Callable[[LogEntry], None]) -> None: ...
```

`LogEntry`: `{ level: str, ts: int (unix ms), msg: str }`

Single global `LogBus` instance imported by all other web modules.
WS server subscribes and broadcasts `log.event` to all connected clients.

### Task 2.3 — Runtime service

File: `src/superstrike_pressure/web/runtime_service.py`

This is the central coordinator. It owns:
- The HID++ session (`hidpp_pressure.PressureHidppSession`)
- The `SyntheticPenEmitter` instance
- The active `RuntimeConfig`
- Telemetry broadcast callback

Public API:
```python
class RuntimeService:
    def __init__(self, launch_config: LaunchConfig, config_store: ConfigStore): ...
    
    async def start_stream(self) -> None: ...
    async def stop_stream(self) -> None: ...
    
    def apply_config(self, patch: dict) -> RuntimeConfig:
        # Validate, merge patch into current config, apply live to emitter,
        # persist via config_store, return effective config
    
    def get_config(self) -> RuntimeConfig: ...
    
    def set_telemetry_callback(self, cb: Callable[[dict], None]) -> None: ...
    # cb called at 60Hz with telemetry payload dict while stream active
    
    @property
    def stream_active(self) -> bool: ...
    
    @property
    def device_found(self) -> bool: ...
```

The stream loop runs in an asyncio thread executor (not blocking the event loop).
Reader thread: blocking `hid.read()` in a tight loop, decodes L/R raw values,
puts decoded samples into an `asyncio.Queue`.
Main async loop: drains queue, applies curve mapping, calls emitter, fires
telemetry callback.

When `apply_config` is called while stream is active:
- Update curve/deadzone parameters immediately (no restart needed)
- Update emitter contact thresholds immediately
- Do NOT restart the HID session

### Task 2.4 — Calibration service

File: `src/superstrike_pressure/web/calibration.py`

```python
async def run_calibration(
    channel: str,  # "left" | "right" | "both"
    runtime_service: RuntimeService,
    progress_cb: Callable[[dict], None],
    config_store: ConfigStore,
) -> dict:
```

Phases per channel: idle (1.5s) → light (1.5s) → heavy (1.5s) → done
For each phase, sample raw values from the live stream, compute min/max.
Call `progress_cb` with `calibrate.progress` event payload at each phase
transition and periodically within phases.

On completion:
- Apply detected `raw_min`/`raw_max` to config via `runtime_service.apply_config`
- Return result dict: `{ "left": { "raw_min": N, "raw_max": N }, ... }`

If stream is not active when calibration starts, start it temporarily,
run calibration, then return to previous state.

### Task 2.5 — WS protocol router

File: `src/superstrike_pressure/web/ws_protocol.py`

Maps incoming command names to handler coroutines. Each handler:
- Receives `(payload: dict, send_response: Callable)`
- Calls appropriate service method
- Calls `send_response` with ack or error envelope

Implement handlers for all commands in `protocol.md`:
`stream.start`, `stream.stop`, `config.get`, `config.patch`,
`calibrate.start`, `profiles.list`, `profiles.save`, `profiles.load`,
`profiles.delete`, `profiles.export`, `profiles.import`, `log.get_recent`

Error handling:
- Catch `ValidationError` → `error { code: "invalid_config" }`
- Catch `ProfileNotFoundError` → `error { code: "not_found" }`
- Catch `SchemaMismatchError` → `error { code: "schema_mismatch" }`
- Catch `StreamAlreadyActiveError` → `error { code: "stream_already_active" }`
- Catch `StreamNotActiveError` → `error { code: "stream_not_active" }`
- Catch all other exceptions → `error { code: "internal_error", message: str(e) }`
- Never let an exception escape a handler without sending an error response.
  Every request_id must receive exactly one response.

### Task 2.6 — WS server

File: `src/superstrike_pressure/web/server.py`

```python
class BridgeServer:
    def __init__(
        self,
        runtime_service: RuntimeService,
        profile_store: ProfileStore,
        config_store: ConfigStore,
        log_bus: LogBus,
        port: int = 27842,
    ): ...
    
    async def start(self) -> int:
        # Bind, return actual port
    
    async def stop(self) -> None: ...
```

Responsibilities:
- Accept WebSocket connections (multiple clients supported, broadcast to all)
- Route incoming commands to `ws_protocol.py` handlers
- Broadcast telemetry at 60Hz (from runtime_service telemetry callback)
- Broadcast heartbeat every 2s
- Broadcast `log.event` from log_bus subscription
- Broadcast `config.changed` when config is patched

Heartbeat payload:
```json
{
  "event": "heartbeat",
  "status": "running" | "error",
  "device_found": true,
  "stream_active": true,
  "version": "<version string>"
}
```

Port selection: try 27842, then 27843–27849. Use first available.
Store actual bound port for use in startup handshake.

### Task 2.7 — Entry point + stdout handshake

File: `src/superstrike_pressure/web/main.py`

This is the new executable entry point for WS mode. It:

1. Reads `LaunchConfig` from CLI args + env vars
   - `SUPERSTRIKE_CONFIG_DIR` env var → `launch_config.config_dir`
2. Creates all service instances
3. Starts `BridgeServer`
4. On successful bind, prints readiness line and flushes:
   ```python
   import json, sys
   print(json.dumps({
       "event": "ws_ready",
       "host": "127.0.0.1",
       "port": actual_port,
       "pid": os.getpid(),
       "version": __version__,
   }), flush=True)
   ```
5. On bind failure, prints error line and exits 1:
   ```python
   print(json.dumps({
       "event": "ws_error",
       "message": str(e),
   }), flush=True)
   sys.exit(1)
   ```
6. Runs asyncio event loop until interrupted
7. On SIGINT/SIGTERM: stop stream cleanly, close WS connections, exit 0

Register new entry point in `pyproject.toml`:
```toml
[project.scripts]
ss-bridge-ws = "superstrike_pressure.web.main:main"
```

---

## Canonical Module Structure (Final)

```
src/superstrike_pressure/
  bridge/
    config.py           # RuntimeConfig, LaunchConfig, ChannelConfig, CONTACT_PRESETS
    synthetic_pen.py    # SyntheticPenEmitter + SyntheticPenConfig (canonical)
    curves.py           # curve math + normalize_curve_name()
    hid_runtime.py      # stream read/decode loop helpers (extract from existing)
  web/
    models.py           # validation functions, unit conversion, custom exceptions
    config_store.py     # config file IO
    profile_store.py    # profile CRUD/import/export
    log_bus.py          # log ring buffer + pub/sub
    calibration.py      # calibration flow + progress events
    runtime_service.py  # start/stop stream, apply config, telemetry fanout
    ws_protocol.py      # command handlers + envelope shaping
    server.py           # WS accept/broadcast/heartbeat
    main.py             # stdout handshake + asyncio entry point
  sniff/
    hidpp_pressure.py   # low-level HID++ session (unchanged)
    pressure_reader.py  # unchanged
scripts/
  pressure_to_pen.py    # thin wrapper (kept for manual dev use)
  ...                   # utility/experimental unchanged
```

---

## Protocol Compliance Checklist

Before marking any task complete, verify against `protocol.md`:

- [ ] All command names match exactly (no typos, no camelCase)
- [ ] All field names in telemetry payload match exactly
- [ ] All field names in ConfigObject match exactly
- [ ] All error codes are from the approved list in protocol.md
- [ ] `schema_version` checked on `profiles.import`
- [ ] `request_id` echoed on every ack and error response
- [ ] `request_id` is null on all unsolicited events and telemetry
- [ ] Heartbeat sent every 2s regardless of stream state
- [ ] Telemetry sent at 60Hz only while stream is active
- [ ] Stdout handshake line printed and flushed before any WS traffic

---

## Testing Each Task

No automated tests are required this sprint. Manual verification is sufficient.
Use `ws_test_client.py` (write a minimal one if it doesn't exist) to verify:

After Task 2.6 + 2.7:
1. Start `ss-bridge-ws`, confirm readiness line on stdout
2. Connect client, receive first heartbeat within 3s
3. `stream.start` → telemetry begins, confirm ~60Hz rate
4. `config.patch` with valid change → ack echoes full config
5. `config.patch` with `raw_min >= raw_max` → error `invalid_config`
6. `config.patch` with `curve: "soft"` → ack, confirm stored as `ease_in` internally
7. `calibrate.start { channel: "both" }` → phase events flow, final ack with result
8. `profiles.save` → `profiles.list` → `profiles.load` round trip
9. `profiles.import` with `schema_version: 2` → error `schema_mismatch`
10. `stream.stop` → telemetry stops, heartbeat continues
11. Kill process → exits cleanly, no zombie HID handles
