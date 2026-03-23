from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from superstrike_pressure.bridge.config import LaunchConfig  # noqa: E402
from superstrike_pressure.web import main as web_main  # noqa: E402


class WebMainTests(unittest.IsolatedAsyncioTestCase):
    def test_build_launch_config_uses_env_fallback(self) -> None:
        args = web_main._parse_args([])  # noqa: SLF001
        old_val = os.environ.get("SUPERSTRIKE_CONFIG_DIR")
        os.environ["SUPERSTRIKE_CONFIG_DIR"] = "C:/tmp/superstrike-test"
        try:
            launch = web_main._build_launch_config(args)  # noqa: SLF001
        finally:
            if old_val is None:
                del os.environ["SUPERSTRIKE_CONFIG_DIR"]
            else:
                os.environ["SUPERSTRIKE_CONFIG_DIR"] = old_val
        self.assertEqual(launch.config_dir, "C:/tmp/superstrike-test")

    def test_ready_and_error_payload_shape(self) -> None:
        ready = web_main._ready_payload(port=27842)  # noqa: SLF001
        self.assertEqual(ready["event"], "ws_ready")
        self.assertEqual(ready["host"], "127.0.0.1")
        self.assertEqual(ready["port"], 27842)
        self.assertIn("pid", ready)
        self.assertIn("version", ready)

        err = web_main._error_payload("bind failed")  # noqa: SLF001
        self.assertEqual(err, {"event": "ws_error", "message": "bind failed"})

    async def test_run_server_prints_error_payload_on_bind_failure(self) -> None:
        class _FakeConfigStore:
            def __init__(self, config_dir=None) -> None:
                _ = config_dir

        class _FakeProfileStore:
            def __init__(self, config_dir=None) -> None:
                _ = config_dir

        class _FakeRuntimeService:
            def __init__(self, launch_config=None, config_store=None, log_bus=None) -> None:
                _ = launch_config
                _ = config_store
                _ = log_bus
                self.stream_active = False

            async def stop_stream(self) -> None:
                return

        class _FakeBridgeServer:
            def __init__(self, runtime_service=None, profile_store=None, config_store=None, log_bus=None, port=0):
                _ = runtime_service
                _ = profile_store
                _ = config_store
                _ = log_bus
                _ = port

            async def start(self) -> int:
                raise RuntimeError("bind failed: address in use")

            async def stop(self) -> None:
                return

        old_config_store = web_main.ConfigStore
        old_profile_store = web_main.ProfileStore
        old_runtime_service = web_main.RuntimeService
        old_bridge_server = web_main.BridgeServer
        web_main.ConfigStore = _FakeConfigStore
        web_main.ProfileStore = _FakeProfileStore
        web_main.RuntimeService = _FakeRuntimeService
        web_main.BridgeServer = _FakeBridgeServer
        try:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = await web_main._run_server(LaunchConfig(), port=27842)  # noqa: SLF001
        finally:
            web_main.ConfigStore = old_config_store
            web_main.ProfileStore = old_profile_store
            web_main.RuntimeService = old_runtime_service
            web_main.BridgeServer = old_bridge_server

        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue().strip())
        self.assertEqual(payload["event"], "ws_error")
        self.assertIn("bind failed", payload["message"])


if __name__ == "__main__":
    unittest.main()
