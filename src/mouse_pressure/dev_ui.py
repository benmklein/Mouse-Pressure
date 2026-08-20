"""Desktop UI entry point and toolkit-independent driver helpers."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Callable

from mouse_pressure.runtime.calibration import run_calibration
from mouse_pressure.runtime.config_store import ConfigStore
from mouse_pressure.runtime.runtime_service import RuntimeService
from mouse_pressure.ui.stroke_analysis import (
    stroke_analysis_data as stroke_analysis_data,
)


class BridgeController:
    """Own a persistent asyncio loop for RuntimeService lifecycle calls."""

    def __init__(self, service: RuntimeService) -> None:
        self.service = service
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._close_lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run_loop,
            name="mouse-pressure-runtime",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=2.0):
            raise RuntimeError("Could not start the pressure driver runtime")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def start(self, *, device_settings: dict[str, int] | None = None) -> Future[None]:
        operation = (
            self.service.start_stream()
            if device_settings is None
            else self.service.start_stream(device_settings=device_settings)
        )
        return asyncio.run_coroutine_threadsafe(operation, self._loop)

    def stop(self) -> Future[None]:
        return asyncio.run_coroutine_threadsafe(self.service.stop_stream(), self._loop)

    def detect_device_settings(self) -> Future[dict[str, int]]:
        return asyncio.run_coroutine_threadsafe(
            self.service.detect_device_settings(), self._loop
        )

    def apply_device_settings(
        self,
        *,
        dpi: int,
        haptic_left: int,
        haptic_right: int,
    ) -> Future[dict[str, int]]:
        return asyncio.run_coroutine_threadsafe(
            self.service.apply_device_settings(
                dpi=dpi,
                haptic_left=haptic_left,
                haptic_right=haptic_right,
            ),
            self._loop,
        )

    def calibrate(
        self,
        channel: str,
        *,
        config_store: ConfigStore,
        progress_cb: Callable[[dict[str, Any]], None],
    ) -> Future[dict[str, dict[str, int]]]:
        return asyncio.run_coroutine_threadsafe(
            run_calibration(channel, self.service, progress_cb, config_store),
            self._loop,
        )

    def close(self, timeout: float = 4.0) -> bool:
        """Stop the runtime loop without allowing cleanup to hang the UI.

        Returns ``True`` when the service and loop stopped within the timeout.
        Repeated calls are safe and return immediately.
        """
        with self._close_lock:
            if self._closed:
                return not self._thread.is_alive()
            self._closed = True
        if not self._thread.is_alive():
            return True

        deadline = time.monotonic() + max(0.0, float(timeout))
        graceful = True

        async def stop_if_needed() -> None:
            if self.service.stream_active:
                await self.service.stop_stream()

        future: Future[None] | None = None
        try:
            future = asyncio.run_coroutine_threadsafe(stop_if_needed(), self._loop)
            future.result(timeout=max(0.0, deadline - time.monotonic()))
        except FutureTimeoutError:
            graceful = False
            if future is not None:
                future.cancel()
        except Exception:
            # Cleanup errors must not strand the desktop window on "Closing".
            graceful = False
        finally:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                pass
            self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return graceful and not self._thread.is_alive()


def main() -> int:
    """Launch the modern PySide6 control panel."""
    if sys.platform != "win32":
        print("ERROR: Mouse Pressure is Windows-only.")
        return 1
    try:
        from mouse_pressure.pyside_ui import main as qt_main
    except ImportError as exc:
        print(f"ERROR: PySide6 is required for the desktop UI: {exc}")
        return 1
    return int(qt_main())


if __name__ == "__main__":
    raise SystemExit(main())
