from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mouse_pressure.bridge.config import RuntimeConfig  # noqa: E402
from mouse_pressure.web.log_bus import LogBus  # noqa: E402
from mouse_pressure.web.models import (  # noqa: E402
    ProfileNotFoundError,
    SchemaMismatchError,
    StreamAlreadyActiveError,
    StreamNotActiveError,
    ValidationError,
)
from mouse_pressure.web.ws_protocol import WsProtocolRouter  # noqa: E402


class _FakeRuntimeService:
    def __init__(self) -> None:
        self._config = RuntimeConfig()
        self.stream_active = False
        self.device_found = True
        self.start_calls = 0
        self.stop_calls = 0

    async def start_stream(self) -> None:
        if self.stream_active:
            raise StreamAlreadyActiveError("already active")
        self.stream_active = True
        self.start_calls += 1

    async def stop_stream(self) -> None:
        if not self.stream_active:
            raise StreamNotActiveError("inactive")
        self.stream_active = False
        self.stop_calls += 1

    def apply_config(self, patch: dict) -> RuntimeConfig:
        left_patch = patch.get("left") if isinstance(patch, dict) else None
        if isinstance(left_patch, dict):
            if left_patch.get("raw_min", self._config.left.raw_min) >= left_patch.get(
                "raw_max", self._config.left.raw_max
            ):
                raise ValidationError("raw_min must be strictly less than raw_max")
            for key, value in left_patch.items():
                setattr(self._config.left, key, value)
        if patch.get("linked") is not None:
            self._config.linked = bool(patch["linked"])
        return self._config

    def get_config(self) -> RuntimeConfig:
        return self._config


class _FakeProfileStore:
    def __init__(self) -> None:
        self._profiles: dict[str, RuntimeConfig] = {}

    def list(self):
        return [{"name": name, "modified_at": 1} for name in sorted(self._profiles)]

    def save(self, name: str, config: RuntimeConfig) -> None:
        self._profiles[name] = config

    def load(self, name: str) -> RuntimeConfig:
        if name not in self._profiles:
            raise ProfileNotFoundError(name)
        return self._profiles[name]

    def delete(self, name: str) -> None:
        if name not in self._profiles:
            raise ProfileNotFoundError(name)
        del self._profiles[name]

    def export_json(self, name: str) -> str:
        if name not in self._profiles:
            raise ProfileNotFoundError(name)
        return json.dumps({"schema_version": 1}, indent=2)

    def import_json(self, json_str: str) -> str:
        raw = json.loads(json_str)
        if raw.get("schema_version") != 1:
            raise SchemaMismatchError("bad schema")
        name = "imported"
        self._profiles[name] = RuntimeConfig()
        return name


class _FakeConfigStore:
    def save(self, config: RuntimeConfig) -> None:
        _ = config


class WsProtocolRouterTests(unittest.IsolatedAsyncioTestCase):
    async def _dispatch(self, router: WsProtocolRouter, *, cmd: str, payload: dict | None = None):
        responses: list[dict] = []
        await router.dispatch(
            cmd=cmd,
            request_id="rid-1",
            payload=payload,
            send_response=responses.append,
        )
        return responses

    async def test_config_get_returns_ack(self) -> None:
        events: list[dict] = []
        router = WsProtocolRouter(
            runtime_service=_FakeRuntimeService(),
            profile_store=_FakeProfileStore(),
            config_store=_FakeConfigStore(),
            log_bus=LogBus(),
            event_sender=events.append,
        )
        responses = await self._dispatch(router, cmd="config.get", payload={})
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["type"], "ack")
        self.assertEqual(responses[0]["request_id"], "rid-1")
        self.assertIn("config", responses[0]["payload"])
        self.assertEqual(events, [])

    async def test_config_patch_emits_config_changed(self) -> None:
        events: list[dict] = []
        router = WsProtocolRouter(
            runtime_service=_FakeRuntimeService(),
            profile_store=_FakeProfileStore(),
            config_store=_FakeConfigStore(),
            log_bus=LogBus(),
            event_sender=events.append,
        )
        responses = await self._dispatch(
            router,
            cmd="config.patch",
            payload={"left": {"raw_min": 80, "raw_max": 190}},
        )
        self.assertEqual(responses[0]["type"], "ack")
        self.assertEqual(events[0]["type"], "event")
        self.assertEqual(events[0]["payload"]["event"], "config.changed")
        self.assertIsNone(events[0]["request_id"])

    async def test_validation_error_maps_to_invalid_config(self) -> None:
        router = WsProtocolRouter(
            runtime_service=_FakeRuntimeService(),
            profile_store=_FakeProfileStore(),
            config_store=_FakeConfigStore(),
            log_bus=LogBus(),
        )
        responses = await self._dispatch(
            router,
            cmd="config.patch",
            payload={"left": {"raw_min": 200, "raw_max": 100}},
        )
        self.assertEqual(responses[0]["type"], "error")
        self.assertEqual(responses[0]["payload"]["code"], "invalid_config")

    async def test_not_found_maps_correctly(self) -> None:
        router = WsProtocolRouter(
            runtime_service=_FakeRuntimeService(),
            profile_store=_FakeProfileStore(),
            config_store=_FakeConfigStore(),
            log_bus=LogBus(),
        )
        responses = await self._dispatch(router, cmd="profiles.load", payload={"name": "missing"})
        self.assertEqual(responses[0]["type"], "error")
        self.assertEqual(responses[0]["payload"]["code"], "not_found")

    async def test_stream_state_errors_map_correctly(self) -> None:
        runtime = _FakeRuntimeService()
        router = WsProtocolRouter(
            runtime_service=runtime,
            profile_store=_FakeProfileStore(),
            config_store=_FakeConfigStore(),
            log_bus=LogBus(),
        )
        await self._dispatch(router, cmd="stream.start", payload={})
        responses = await self._dispatch(router, cmd="stream.start", payload={})
        self.assertEqual(responses[0]["payload"]["code"], "stream_already_active")
        await self._dispatch(router, cmd="stream.stop", payload={})
        responses = await self._dispatch(router, cmd="stream.stop", payload={})
        self.assertEqual(responses[0]["payload"]["code"], "stream_not_active")

    async def test_log_get_recent_uses_log_bus(self) -> None:
        bus = LogBus()
        bus.info("hello")
        router = WsProtocolRouter(
            runtime_service=_FakeRuntimeService(),
            profile_store=_FakeProfileStore(),
            config_store=_FakeConfigStore(),
            log_bus=bus,
        )
        responses = await self._dispatch(router, cmd="log.get_recent", payload={"limit": 1})
        self.assertEqual(responses[0]["type"], "ack")
        self.assertEqual(len(responses[0]["payload"]["entries"]), 1)
        self.assertEqual(responses[0]["payload"]["entries"][0]["msg"], "hello")

    async def test_profiles_import_schema_mismatch_maps(self) -> None:
        router = WsProtocolRouter(
            runtime_service=_FakeRuntimeService(),
            profile_store=_FakeProfileStore(),
            config_store=_FakeConfigStore(),
            log_bus=LogBus(),
        )
        responses = await self._dispatch(router, cmd="profiles.import", payload={"json": '{"schema_version":2}'})
        self.assertEqual(responses[0]["type"], "error")
        self.assertEqual(responses[0]["payload"]["code"], "schema_mismatch")

    async def test_unknown_command_returns_internal_error(self) -> None:
        router = WsProtocolRouter(
            runtime_service=_FakeRuntimeService(),
            profile_store=_FakeProfileStore(),
            config_store=_FakeConfigStore(),
            log_bus=LogBus(),
        )
        responses = await self._dispatch(router, cmd="unknown.cmd", payload={})
        self.assertEqual(responses[0]["type"], "error")
        self.assertEqual(responses[0]["payload"]["code"], "internal_error")

    async def test_calibrate_start_routes_result_and_progress_events(self) -> None:
        events: list[dict] = []
        runtime = _FakeRuntimeService()
        router = WsProtocolRouter(
            runtime_service=runtime,
            profile_store=_FakeProfileStore(),
            config_store=_FakeConfigStore(),
            log_bus=LogBus(),
            event_sender=events.append,
        )

        import mouse_pressure.web.ws_protocol as ws_protocol_module

        async def fake_run_calibration(channel, runtime_service, progress_cb, config_store):
            _ = runtime_service
            _ = config_store
            progress_cb({"event": "calibrate.progress", "channel": channel, "phase": "idle", "value": 80})
            await asyncio.sleep(0)
            return {"left": {"raw_min": 79, "raw_max": 160}}

        old_fn = ws_protocol_module.run_calibration
        ws_protocol_module.run_calibration = fake_run_calibration
        try:
            responses = await self._dispatch(router, cmd="calibrate.start", payload={"channel": "left"})
        finally:
            ws_protocol_module.run_calibration = old_fn

        self.assertEqual(responses[0]["type"], "ack")
        self.assertEqual(responses[0]["payload"]["result"]["left"]["raw_max"], 160)
        progress_events = [e for e in events if e["payload"].get("event") == "calibrate.progress"]
        self.assertGreaterEqual(len(progress_events), 1)


if __name__ == "__main__":
    unittest.main()
