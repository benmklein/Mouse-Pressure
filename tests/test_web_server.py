from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mouse_pressure.web.log_bus import LogBus  # noqa: E402
from mouse_pressure.web.server import BridgeServer  # noqa: E402


class _FakeRuntimeService:
    def __init__(self) -> None:
        self.device_found = True
        self.stream_active = False
        self.telemetry_cb = None

    async def start_stream(self) -> None:
        self.stream_active = True

    async def stop_stream(self) -> None:
        self.stream_active = False

    def set_telemetry_callback(self, cb) -> None:
        self.telemetry_cb = cb

    def apply_config(self, patch):
        _ = patch
        raise NotImplementedError

    def get_config(self):
        raise NotImplementedError


class _FakeProfileStore:
    def list(self):
        return []


class _FakeConfigStore:
    def save(self, config) -> None:
        _ = config


class _FakeWsServer:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return


class _FakeWsModule:
    def __init__(self, fail_ports: set[int] | None = None) -> None:
        self.fail_ports = fail_ports or set()
        self.calls: list[int] = []
        self.server: _FakeWsServer | None = None

    async def serve(self, handler, host: str, port: int):
        _ = host
        self.calls.append(port)
        if port in self.fail_ports:
            raise OSError("address already in use")
        self.server = _FakeWsServer(handler)
        return self.server


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


class BridgeServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_uses_fallback_port(self) -> None:
        runtime = _FakeRuntimeService()
        log_bus = LogBus()
        server = BridgeServer(
            runtime_service=runtime,
            profile_store=_FakeProfileStore(),
            config_store=_FakeConfigStore(),
            log_bus=log_bus,
        )

        import mouse_pressure.web.server as server_module

        fake_ws = _FakeWsModule(fail_ports={27842})
        old_import = server_module._import_websockets
        server_module._import_websockets = lambda: fake_ws
        try:
            bound_port = await server.start()
        finally:
            server_module._import_websockets = old_import

        self.assertEqual(bound_port, 27843)
        self.assertEqual(fake_ws.calls[:2], [27842, 27843])
        await server.stop()

    async def test_telemetry_and_log_events_are_broadcast(self) -> None:
        runtime = _FakeRuntimeService()
        log_bus = LogBus()
        server = BridgeServer(
            runtime_service=runtime,
            profile_store=_FakeProfileStore(),
            config_store=_FakeConfigStore(),
            log_bus=log_bus,
        )

        import mouse_pressure.web.server as server_module

        fake_ws = _FakeWsModule()
        old_import = server_module._import_websockets
        server_module._import_websockets = lambda: fake_ws
        try:
            await server.start()
            client = _FakeClient()
            server._clients.add(client)  # noqa: SLF001

            runtime.telemetry_cb({"left_raw": 99})
            log_bus.info("hello world")
            await asyncio.sleep(0.02)
        finally:
            server_module._import_websockets = old_import
            await server.stop()

        decoded = [json.loads(item) for item in client.sent]
        types = {item["type"] for item in decoded}
        self.assertIn("telemetry", types)
        self.assertIn("event", types)
        self.assertTrue(any(item["payload"].get("event") == "log.event" for item in decoded))

    async def test_invalid_json_message_returns_error(self) -> None:
        runtime = _FakeRuntimeService()
        server = BridgeServer(
            runtime_service=runtime,
            profile_store=_FakeProfileStore(),
            config_store=_FakeConfigStore(),
            log_bus=LogBus(),
        )
        client = _FakeClient()
        await server._handle_client_message(client, "not json")  # noqa: SLF001
        self.assertEqual(len(client.sent), 1)
        payload = json.loads(client.sent[0])
        self.assertEqual(payload["type"], "error")
        self.assertEqual(payload["payload"]["code"], "internal_error")


if __name__ == "__main__":
    unittest.main()
