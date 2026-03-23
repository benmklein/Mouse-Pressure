"""WebSocket server for Superstrike bridge protocol."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from superstrike_pressure import __version__
from superstrike_pressure.web.config_store import ConfigStore
from superstrike_pressure.web.log_bus import LogBus
from superstrike_pressure.web.profile_store import ProfileStore
from superstrike_pressure.web.runtime_service import RuntimeService
from superstrike_pressure.web.ws_protocol import WsProtocolRouter


def _import_websockets():
    try:
        import websockets
    except ModuleNotFoundError as exc:
        raise RuntimeError("websockets package is required for WS mode") from exc
    return websockets


class BridgeServer:
    def __init__(
        self,
        runtime_service: RuntimeService,
        profile_store: ProfileStore,
        config_store: ConfigStore,
        log_bus: LogBus,
        port: int = 27842,
    ) -> None:
        self.runtime_service = runtime_service
        self.profile_store = profile_store
        self.config_store = config_store
        self.log_bus = log_bus
        self.port = int(port)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set[Any] = set()
        self._server: Any = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._bound_port: int | None = None
        self._status = "running"
        self._running = False
        self._subscribed_logs = False
        self._ws_module: Any = None

        self._router = WsProtocolRouter(
            runtime_service=self.runtime_service,
            profile_store=self.profile_store,
            config_store=self.config_store,
            log_bus=self.log_bus,
            event_sender=self._broadcast_envelope,
        )

    async def start(self) -> int:
        if self._running:
            if self._bound_port is None:
                raise RuntimeError("BridgeServer started without bound port")
            return self._bound_port

        self._loop = asyncio.get_running_loop()
        self._ws_module = _import_websockets()
        self._bound_port = await self._bind_first_available_port()
        self._running = True
        self.runtime_service.set_telemetry_callback(self._on_telemetry)
        if not self._subscribed_logs:
            self.log_bus.subscribe(self._on_log_entry)
            self._subscribed_logs = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return self._bound_port

    async def stop(self) -> None:
        self._running = False

        self.runtime_service.set_telemetry_callback(None)

        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        clients = list(self._clients)
        self._clients.clear()
        for ws in clients:
            try:
                await ws.close()
            except Exception:
                continue

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _bind_first_available_port(self) -> int:
        candidate_ports = [self.port]
        if self.port == 27842:
            candidate_ports.extend(range(27843, 27850))

        last_error: Exception | None = None
        for candidate in candidate_ports:
            try:
                self._server = await self._ws_module.serve(self._handle_ws_client, "127.0.0.1", candidate)
                return candidate
            except OSError as exc:
                last_error = exc
                continue

        if last_error is not None:
            raise last_error
        raise RuntimeError("No candidate ports available")

    async def _handle_ws_client(self, websocket) -> None:
        self._clients.add(websocket)
        try:
            async for raw_message in websocket:
                await self._handle_client_message(websocket, raw_message)
        except Exception:
            self._status = "error"
        finally:
            self._clients.discard(websocket)

    async def _handle_client_message(self, websocket, raw_message: Any) -> None:
        try:
            msg = json.loads(raw_message)
        except Exception:
            await self._send_envelope(
                websocket,
                {
                    "type": "error",
                    "request_id": None,
                    "payload": {"code": "internal_error", "message": "Invalid JSON"},
                },
            )
            return

        if not isinstance(msg, dict):
            await self._send_envelope(
                websocket,
                {
                    "type": "error",
                    "request_id": None,
                    "payload": {"code": "internal_error", "message": "Invalid message envelope"},
                },
            )
            return

        cmd = msg.get("cmd")
        request_id = msg.get("request_id")
        payload = msg.get("payload")
        if not isinstance(cmd, str):
            await self._send_envelope(
                websocket,
                {
                    "type": "error",
                    "request_id": request_id if isinstance(request_id, str) else None,
                    "payload": {"code": "internal_error", "message": "Missing command name"},
                },
            )
            return

        await self._router.dispatch(
            cmd=cmd,
            request_id=request_id if isinstance(request_id, str) else None,
            payload=payload if isinstance(payload, dict) else {},
            send_response=lambda envelope: self._send_envelope(websocket, envelope),
        )

    async def _send_envelope(self, websocket, envelope: dict[str, Any]) -> None:
        try:
            await websocket.send(json.dumps(envelope))
        except Exception:
            self._clients.discard(websocket)

    async def _broadcast_envelope(self, envelope: dict[str, Any]) -> None:
        if not self._clients:
            return
        payload = json.dumps(envelope)
        stale_clients: list[Any] = []
        for ws in list(self._clients):
            try:
                await ws.send(payload)
            except Exception:
                stale_clients.append(ws)
        for ws in stale_clients:
            self._clients.discard(ws)

    def _on_telemetry(self, telemetry_payload: dict) -> None:
        if not self._running or self._loop is None:
            return
        envelope = {"type": "telemetry", "request_id": None, "payload": telemetry_payload}
        self._loop.call_soon_threadsafe(asyncio.create_task, self._broadcast_envelope(envelope))

    def _on_log_entry(self, entry) -> None:
        if not self._running or self._loop is None:
            return
        envelope = {
            "type": "event",
            "request_id": None,
            "payload": {
                "event": "log.event",
                "level": entry.level,
                "ts": entry.ts,
                "msg": entry.msg,
            },
        }
        self._loop.call_soon_threadsafe(asyncio.create_task, self._broadcast_envelope(envelope))

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await self._broadcast_envelope(self._heartbeat_envelope())
            await asyncio.sleep(2.0)

    def _heartbeat_envelope(self) -> dict[str, Any]:
        return {
            "type": "event",
            "request_id": None,
            "payload": {
                "event": "heartbeat",
                "status": self._status,
                "device_found": self.runtime_service.device_found,
                "stream_active": self.runtime_service.stream_active,
                "version": __version__,
            },
        }
