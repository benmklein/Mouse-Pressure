"""WebSocket-mode entry point for Mouse Pressure."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from typing import Sequence

from mouse_pressure import __version__
from mouse_pressure.bridge.config import LaunchConfig
from mouse_pressure.web.config_store import ConfigStore
from mouse_pressure.web.log_bus import GLOBAL_LOG_BUS
from mouse_pressure.web.profile_store import ProfileStore
from mouse_pressure.web.runtime_service import RuntimeService
from mouse_pressure.web.server import BridgeServer


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Mouse Pressure WebSocket server")
    parser.add_argument("--mode", type=int, default=LaunchConfig.mode)
    parser.add_argument("--mode-arg", type=int, default=LaunchConfig.mode_arg)
    parser.add_argument("--backend", default=LaunchConfig.backend)
    parser.add_argument("--hz", type=float, default=LaunchConfig.hz)
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--config-dir", default=None)
    parser.add_argument("--trace-dir", default=None)
    parser.add_argument("--port", type=int, default=27842)
    return parser.parse_args(argv)


def _build_launch_config(args: argparse.Namespace) -> LaunchConfig:
    env_config_dir = os.environ.get("MOUSE_PRESSURE_CONFIG_DIR")
    config_dir = args.config_dir if args.config_dir is not None else env_config_dir
    return LaunchConfig(
        mode=args.mode,
        mode_arg=args.mode_arg,
        backend=args.backend,
        hz=args.hz,
        log_file=args.log_file,
        config_dir=config_dir,
        trace_dir=args.trace_dir,
    )


def _ready_payload(*, port: int) -> dict:
    return {
        "event": "ws_ready",
        "host": "127.0.0.1",
        "port": int(port),
        "pid": os.getpid(),
        "version": __version__,
    }


def _error_payload(message: str) -> dict:
    return {"event": "ws_error", "message": str(message)}


async def _run_server(launch_config: LaunchConfig, port: int) -> int:
    config_store = ConfigStore(config_dir=launch_config.config_dir)
    profile_store = ProfileStore(config_dir=launch_config.config_dir)
    runtime_service = RuntimeService(launch_config=launch_config, config_store=config_store, log_bus=GLOBAL_LOG_BUS)
    server = BridgeServer(
        runtime_service=runtime_service,
        profile_store=profile_store,
        config_store=config_store,
        log_bus=GLOBAL_LOG_BUS,
        port=port,
    )

    try:
        actual_port = await server.start()
    except Exception as exc:
        print(json.dumps(_error_payload(str(exc))), flush=True)
        return 1

    print(json.dumps(_ready_payload(port=actual_port)), flush=True)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_handlers: list[signal.Signals] = []

    def _request_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
            installed_handlers.append(sig)
        except (NotImplementedError, RuntimeError):
            # Windows + some embedded loops may not support add_signal_handler.
            continue

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        for sig in installed_handlers:
            try:
                loop.remove_signal_handler(sig)
            except Exception:
                continue
        if runtime_service.stream_active:
            try:
                await runtime_service.stop_stream()
            except Exception:
                pass
        await server.stop()

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    launch_config = _build_launch_config(args)
    try:
        return asyncio.run(_run_server(launch_config=launch_config, port=args.port))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
