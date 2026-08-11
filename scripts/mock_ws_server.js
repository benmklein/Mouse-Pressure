#!/usr/bin/env node
/**
 * scripts/mock_ws_server.js
 * Fake Mouse Pressure backend for UI development.
 * Implements the docs/web/protocol.md contract with realistic fake data.
 * The real Python backend is a drop-in replacement on the same protocol.
 *
 * Usage: node scripts/mock_ws_server.js [--port 27842]
 *
 * Simulates:
 *   - Startup stdout handshake (printed immediately)
 *   - All commands with realistic ack responses
 *   - 60Hz telemetry stream when stream is active
 *   - Heartbeat every 2s
 *   - Calibration sequence with phases
 *   - Log events
 */

const { WebSocketServer } = require('ws');
const { randomUUID } = require('crypto');

const PORT = parseInt(process.argv[process.argv.indexOf('--port') + 1] || '27842');

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let streamActive = false;
let telemetryInterval = null;

let config = {
  schema_version: 1,
  linked: true,
  left: {
    raw_min: 320,
    raw_max: 740,
    deadzone_low: 0,
    deadzone_high: 0,
    curve: 'linear',
    curve_strength: 1.0,
    contact_preset: 'medium',
  },
  right: {
    raw_min: 320,
    raw_max: 740,
    deadzone_low: 0,
    deadzone_high: 0,
    curve: 'linear',
    curve_strength: 1.0,
    contact_preset: 'medium',
  },
  app_profiles: {
    'krita.exe': 'krita',
    'Photoshop.exe': 'photoshop',
  },
};

const profiles = {
  default: JSON.parse(JSON.stringify(config)),
  krita: {
    ...JSON.parse(JSON.stringify(config)),
    left: { ...config.left, curve: 'soft', contact_preset: 'light' },
    right: { ...config.right, curve: 'soft', contact_preset: 'light' },
  },
};

const logBuffer = [];

function addLog(level, msg) {
  const entry = { level, ts: Date.now(), msg };
  logBuffer.push(entry);
  if (logBuffer.length > 500) logBuffer.shift();
  return entry;
}

addLog('INFO', 'Mock bridge started');
addLog('INFO', 'Supported analog mouse found (mock)');

// ---------------------------------------------------------------------------
// Telemetry simulation
// ---------------------------------------------------------------------------

let _t = 0;

function makeTelemetryPayload() {
  _t += 1 / 60;
  const leftRaw = streamActive
    ? Math.round(320 + 240 * Math.max(0, Math.sin(_t * 0.8) * Math.sin(_t * 0.3)))
    : 0;
  const rightRaw = streamActive
    ? Math.round(320 + 180 * Math.max(0, Math.sin(_t * 0.6 + 1.2) * Math.sin(_t * 0.4)))
    : 0;
  const toNorm = (raw, mn, mx) => Math.max(0, Math.min(1, (raw - mn) / (mx - mn)));
  const toMapped = (norm) => Math.round(norm * 1023);
  const ln = toNorm(leftRaw, config.left.raw_min, config.left.raw_max);
  const rn = toNorm(rightRaw, config.right.raw_min, config.right.raw_max);
  return {
    left_raw: leftRaw,
    right_raw: rightRaw,
    left_norm: Math.round(ln * 1000) / 1000,
    right_norm: Math.round(rn * 1000) / 1000,
    left_mapped: toMapped(ln),
    right_mapped: toMapped(rn),
    hz: 59.992,
  };
}

// ---------------------------------------------------------------------------
// WebSocket server
// ---------------------------------------------------------------------------

const wss = new WebSocketServer({ port: PORT, host: '127.0.0.1' });

wss.on('listening', () => {
  // Startup handshake — printed to stdout for Tauri to read
  const readyLine = JSON.stringify({
    event: 'ws_ready',
    host: '127.0.0.1',
    port: PORT,
    pid: process.pid,
    version: '0.0.1-mock',
  });
  process.stdout.write(readyLine + '\n');
  console.error(`[mock] WS server listening on ws://127.0.0.1:${PORT}`);
});

wss.on('connection', (ws) => {
  console.error('[mock] Client connected');

  // Send initial heartbeat immediately
  send(ws, {
    type: 'event',
    request_id: null,
    payload: heartbeatPayload(),
  });

  // Heartbeat timer
  const hbTimer = setInterval(() => {
    if (ws.readyState === ws.OPEN) {
      send(ws, { type: 'event', request_id: null, payload: heartbeatPayload() });
    }
  }, 2000);

  // Telemetry — shared interval, broadcast to all clients
  if (!telemetryInterval) {
    telemetryInterval = setInterval(() => {
      if (!streamActive) return;
      const payload = makeTelemetryPayload();
      wss.clients.forEach((client) => {
        if (client.readyState === client.OPEN) {
          send(client, { type: 'telemetry', request_id: null, payload });
        }
      });
    }, 1000 / 60);
  }

  ws.on('message', (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch {
      console.error('[mock] Invalid JSON received');
      return;
    }
    handleCommand(ws, msg);
  });

  ws.on('close', () => {
    console.error('[mock] Client disconnected');
    clearInterval(hbTimer);
    if (wss.clients.size === 0 && telemetryInterval) {
      clearInterval(telemetryInterval);
      telemetryInterval = null;
    }
  });
});

// ---------------------------------------------------------------------------
// Command handlers
// ---------------------------------------------------------------------------

function handleCommand(ws, msg) {
  const { cmd, request_id, payload = {} } = msg;
  console.error(`[mock] cmd=${cmd} rid=${request_id}`);

  switch (cmd) {
    case 'stream.start':
      return handleStreamStart(ws, request_id);
    case 'stream.stop':
      return handleStreamStop(ws, request_id);
    case 'config.get':
      return ack(ws, request_id, { config });
    case 'config.patch':
      return handleConfigPatch(ws, request_id, payload);
    case 'calibrate.start':
      return handleCalibrate(ws, request_id, payload);
    case 'profiles.list':
      return ack(ws, request_id, {
        profiles: Object.keys(profiles).map((name) => ({
          name,
          modified_at: Math.floor(Date.now() / 1000) - 3600,
        })),
      });
    case 'profiles.save':
      return handleProfileSave(ws, request_id, payload);
    case 'profiles.load':
      return handleProfileLoad(ws, request_id, payload);
    case 'profiles.delete':
      return handleProfileDelete(ws, request_id, payload);
    case 'profiles.export':
      return handleProfileExport(ws, request_id, payload);
    case 'profiles.import':
      return handleProfileImport(ws, request_id, payload);
    case 'log.get_recent': {
      const limit = payload.limit ?? 100;
      return ack(ws, request_id, { entries: logBuffer.slice(-limit) });
    }
    default:
      return error(ws, request_id, 'internal_error', `Unknown command: ${cmd}`);
  }
}

function handleStreamStart(ws, request_id) {
  if (streamActive) {
    return error(ws, request_id, 'stream_already_active', 'Stream is already running');
  }
  streamActive = true;
  const entry = addLog('INFO', 'Stream started');
  broadcastLogEvent(entry);
  ack(ws, request_id, {});
}

function handleStreamStop(ws, request_id) {
  if (!streamActive) {
    return error(ws, request_id, 'stream_not_active', 'Stream is not running');
  }
  streamActive = false;
  const entry = addLog('INFO', 'Stream stopped');
  broadcastLogEvent(entry);
  ack(ws, request_id, {});
}

function handleConfigPatch(ws, request_id, patch) {
  // Validate
  const testLeft = { ...config.left, ...(patch.left || {}) };
  const testRight = { ...config.right, ...(patch.right || {}) };
  if (testLeft.raw_min >= testLeft.raw_max) {
    return error(ws, request_id, 'invalid_config', 'raw_min must be less than raw_max (left)');
  }
  if (testRight.raw_min >= testRight.raw_max) {
    return error(ws, request_id, 'invalid_config', 'raw_min must be less than raw_max (right)');
  }

  // Apply
  if (patch.linked !== undefined) config.linked = patch.linked;
  if (patch.left) config.left = { ...config.left, ...patch.left };
  if (patch.right) {
    config.right = { ...config.right, ...patch.right };
  }
  if (config.linked && patch.left) {
    config.right = { ...config.left };
  }
  if (patch.app_profiles) config.app_profiles = { ...config.app_profiles, ...patch.app_profiles };

  broadcastConfigChanged();
  ack(ws, request_id, { config });
}

async function handleCalibrate(ws, request_id, payload) {
  const channel = payload.channel || 'both';
  const channels = channel === 'both' ? ['left', 'right'] : [channel];
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));

  const phases = [
    { phase: 'idle', duration: 1500, label: 'Rest hand on mouse' },
    { phase: 'light', duration: 1500, label: 'Press lightly' },
    { phase: 'heavy', duration: 1500, label: 'Press hard' },
  ];

  const result = {};

  for (const ch of channels) {
    for (const { phase, duration } of phases) {
      // Simulate progress values
      const baseValue = phase === 'idle' ? 79 : phase === 'light' ? 100 : 155;
      for (let i = 0; i < 3; i++) {
        await delay(duration / 3);
        broadcastEvent({
          event: 'calibrate.progress',
          channel: ch,
          phase,
          value: baseValue + Math.round(Math.random() * 4 - 2),
        });
      }
    }

    broadcastEvent({ event: 'calibrate.progress', channel: ch, phase: 'done', value: 0 });
    result[ch] = { raw_min: 316, raw_max: ch === 'left' ? 628 : 648 };

    // Apply to config
    config[ch].raw_min = result[ch].raw_min;
    config[ch].raw_max = result[ch].raw_max;
  }

  const entry = addLog('INFO', `Calibration complete: ${JSON.stringify(result)}`);
  broadcastLogEvent(entry);
  broadcastConfigChanged();
  ack(ws, request_id, { result });
}

function handleProfileSave(ws, request_id, payload) {
  const { name, config: profileConfig } = payload;
  if (!name) return error(ws, request_id, 'invalid_config', 'Profile name required');
  profiles[name] = JSON.parse(JSON.stringify(profileConfig));
  addLog('INFO', `Profile saved: ${name}`);
  ack(ws, request_id, {});
}

function handleProfileLoad(ws, request_id, payload) {
  const { name } = payload;
  if (!profiles[name]) return error(ws, request_id, 'not_found', `Profile not found: ${name}`);
  config = JSON.parse(JSON.stringify(profiles[name]));
  broadcastConfigChanged();
  addLog('INFO', `Profile loaded: ${name}`);
  ack(ws, request_id, { config });
}

function handleProfileDelete(ws, request_id, payload) {
  const { name } = payload;
  if (!profiles[name]) return error(ws, request_id, 'not_found', `Profile not found: ${name}`);
  delete profiles[name];
  addLog('INFO', `Profile deleted: ${name}`);
  ack(ws, request_id, {});
}

function handleProfileExport(ws, request_id, payload) {
  const { name } = payload;
  if (!profiles[name]) return error(ws, request_id, 'not_found', `Profile not found: ${name}`);
  ack(ws, request_id, { json: JSON.stringify(profiles[name], null, 2) });
}

function handleProfileImport(ws, request_id, payload) {
  let parsed;
  try {
    parsed = JSON.parse(payload.json);
  } catch {
    return error(ws, request_id, 'schema_mismatch', 'Invalid JSON');
  }
  if (parsed.schema_version !== 1) {
    return error(ws, request_id, 'schema_mismatch', `Unsupported schema_version: ${parsed.schema_version}`);
  }
  const name = `imported_${Date.now()}`;
  profiles[name] = parsed;
  addLog('INFO', `Profile imported as: ${name}`);
  ack(ws, request_id, { name });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function send(ws, obj) {
  if (ws.readyState === ws.OPEN) {
    ws.send(JSON.stringify(obj));
  }
}

function ack(ws, request_id, payload) {
  send(ws, { type: 'ack', request_id, payload });
}

function error(ws, request_id, code, message) {
  send(ws, { type: 'error', request_id, payload: { code, message } });
}

function heartbeatPayload() {
  return {
    event: 'heartbeat',
    status: 'running',
    device_found: true,
    stream_active: streamActive,
    version: '0.0.1-mock',
  };
}

function broadcastEvent(eventPayload) {
  wss.clients.forEach((client) => {
    if (client.readyState === client.OPEN) {
      send(client, { type: 'event', request_id: null, payload: eventPayload });
    }
  });
}

function broadcastLogEvent(entry) {
  broadcastEvent({ event: 'log.event', ...entry });
}

function broadcastConfigChanged() {
  broadcastEvent({ event: 'config.changed', config });
}
