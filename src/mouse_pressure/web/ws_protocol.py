"""WebSocket protocol command router."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable

from mouse_pressure.web.calibration import run_calibration
from mouse_pressure.web.config_store import ConfigStore, runtime_config_from_dict, runtime_config_to_dict
from mouse_pressure.web.log_bus import LogBus
from mouse_pressure.web.models import (
    ProfileNotFoundError,
    SchemaMismatchError,
    StreamAlreadyActiveError,
    StreamNotActiveError,
    ValidationError,
)
from mouse_pressure.web.profile_store import ProfileStore
from mouse_pressure.web.runtime_service import RuntimeService

SendEnvelope = Callable[[dict[str, Any]], Awaitable[None] | None]


async def _maybe_await(result: Awaitable[None] | None) -> None:
    if inspect.isawaitable(result):
        await result


class WsProtocolRouter:
    def __init__(
        self,
        runtime_service: RuntimeService,
        profile_store: ProfileStore,
        config_store: ConfigStore,
        log_bus: LogBus,
        *,
        event_sender: SendEnvelope | None = None,
    ) -> None:
        self.runtime_service = runtime_service
        self.profile_store = profile_store
        self.config_store = config_store
        self.log_bus = log_bus
        self._event_sender = event_sender

        self._handlers: dict[str, Callable[[dict, SendEnvelope], Awaitable[None]]] = {
            "stream.start": self._handle_stream_start,
            "stream.stop": self._handle_stream_stop,
            "config.get": self._handle_config_get,
            "config.patch": self._handle_config_patch,
            "calibrate.start": self._handle_calibrate_start,
            "profiles.list": self._handle_profiles_list,
            "profiles.save": self._handle_profiles_save,
            "profiles.load": self._handle_profiles_load,
            "profiles.delete": self._handle_profiles_delete,
            "profiles.export": self._handle_profiles_export,
            "profiles.import": self._handle_profiles_import,
            "log.get_recent": self._handle_log_get_recent,
        }

    async def dispatch(
        self,
        *,
        cmd: str,
        request_id: str | None,
        payload: dict | None,
        send_response: SendEnvelope,
    ) -> None:
        req_payload = payload if isinstance(payload, dict) else {}
        responded = False

        async def reply(envelope: dict[str, Any]) -> None:
            nonlocal responded
            message = dict(envelope)
            message["request_id"] = request_id
            if message.get("type") in {"ack", "error"}:
                if responded:
                    return
                responded = True
            await _maybe_await(send_response(message))

        handler = self._handlers.get(cmd)
        if handler is None:
            await reply(self._error("internal_error", f"Unknown command: {cmd}"))
            return

        try:
            await handler(req_payload, reply)
        except ValidationError as exc:
            await reply(self._error("invalid_config", str(exc)))
        except ProfileNotFoundError as exc:
            await reply(self._error("not_found", str(exc)))
        except SchemaMismatchError as exc:
            await reply(self._error("schema_mismatch", str(exc)))
        except StreamAlreadyActiveError as exc:
            await reply(self._error("stream_already_active", str(exc)))
        except StreamNotActiveError as exc:
            await reply(self._error("stream_not_active", str(exc)))
        except Exception as exc:
            if cmd == "stream.start" and not self.runtime_service.device_found:
                await reply(self._error("device_not_found", str(exc)))
            else:
                await reply(self._error("internal_error", str(exc)))

        if not responded:
            await reply(self._error("internal_error", f"Handler {cmd} did not send a response"))

    async def _emit_event(self, payload: dict[str, Any], *, event_type: str = "event") -> None:
        if self._event_sender is None:
            return
        envelope = {"type": event_type, "request_id": None, "payload": payload}
        await _maybe_await(self._event_sender(envelope))

    @staticmethod
    def _ack(payload: dict[str, Any]) -> dict[str, Any]:
        return {"type": "ack", "payload": payload}

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {"type": "error", "payload": {"code": code, "message": message}}

    async def _handle_stream_start(self, payload: dict, send_response: SendEnvelope) -> None:
        _ = payload
        await self.runtime_service.start_stream()
        await send_response(self._ack({}))

    async def _handle_stream_stop(self, payload: dict, send_response: SendEnvelope) -> None:
        _ = payload
        await self.runtime_service.stop_stream()
        await send_response(self._ack({}))

    async def _handle_config_get(self, payload: dict, send_response: SendEnvelope) -> None:
        _ = payload
        await send_response(self._ack({"config": runtime_config_to_dict(self.runtime_service.get_config())}))

    async def _handle_config_patch(self, payload: dict, send_response: SendEnvelope) -> None:
        config = self.runtime_service.apply_config(payload)
        config_dict = runtime_config_to_dict(config)
        await self._emit_event({"event": "config.changed", "config": config_dict})
        await send_response(self._ack({"config": config_dict}))

    async def _handle_calibrate_start(self, payload: dict, send_response: SendEnvelope) -> None:
        channel = payload.get("channel", "both")
        progress_tasks: list[asyncio.Task[None]] = []

        def progress_cb(event_payload: dict) -> None:
            progress_tasks.append(asyncio.create_task(self._emit_event(event_payload, event_type="event")))

        result = await run_calibration(
            channel=channel,
            runtime_service=self.runtime_service,
            progress_cb=progress_cb,
            config_store=self.config_store,
        )
        if progress_tasks:
            await asyncio.gather(*progress_tasks, return_exceptions=True)
        await self._emit_event(
            {
                "event": "config.changed",
                "config": runtime_config_to_dict(self.runtime_service.get_config()),
            }
        )
        await send_response(self._ack({"result": result}))

    async def _handle_profiles_list(self, payload: dict, send_response: SendEnvelope) -> None:
        _ = payload
        await send_response(self._ack({"profiles": self.profile_store.list()}))

    async def _handle_profiles_save(self, payload: dict, send_response: SendEnvelope) -> None:
        name = payload.get("name")
        raw_config = payload.get("config")
        if not isinstance(name, str):
            raise ValidationError("profile name must be provided")
        config = runtime_config_from_dict(raw_config)
        self.profile_store.save(name, config)
        await send_response(self._ack({}))

    async def _handle_profiles_load(self, payload: dict, send_response: SendEnvelope) -> None:
        name = payload.get("name")
        if not isinstance(name, str):
            raise ValidationError("profile name must be provided")
        loaded = self.profile_store.load(name)
        updated = self.runtime_service.apply_config(runtime_config_to_dict(loaded))
        config_dict = runtime_config_to_dict(updated)
        await self._emit_event({"event": "config.changed", "config": config_dict})
        await send_response(self._ack({"config": config_dict}))

    async def _handle_profiles_delete(self, payload: dict, send_response: SendEnvelope) -> None:
        name = payload.get("name")
        if not isinstance(name, str):
            raise ValidationError("profile name must be provided")
        self.profile_store.delete(name)
        await send_response(self._ack({}))

    async def _handle_profiles_export(self, payload: dict, send_response: SendEnvelope) -> None:
        name = payload.get("name")
        if not isinstance(name, str):
            raise ValidationError("profile name must be provided")
        exported = self.profile_store.export_json(name)
        await send_response(self._ack({"json": exported}))

    async def _handle_profiles_import(self, payload: dict, send_response: SendEnvelope) -> None:
        raw_json = payload.get("json")
        if not isinstance(raw_json, str):
            raise ValidationError("json must be a string")
        name = self.profile_store.import_json(raw_json)
        await send_response(self._ack({"name": name}))

    async def _handle_log_get_recent(self, payload: dict, send_response: SendEnvelope) -> None:
        limit = payload.get("limit", 100)
        if not isinstance(limit, int):
            raise ValidationError("limit must be an integer")
        entries = [entry.as_dict() for entry in self.log_bus.get_recent(limit=limit)]
        await send_response(self._ack({"entries": entries}))
