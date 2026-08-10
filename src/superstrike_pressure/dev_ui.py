"""Desktop UI entry point and toolkit-independent bridge helpers."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Callable

from superstrike_pressure.bridge.curves import (
    PressureConfig,
    map_pressure,
    normalize_curve_name,
)
from superstrike_pressure.ui.stroke_analysis import stroke_analysis_data
from superstrike_pressure.web.calibration import run_calibration
from superstrike_pressure.web.config_store import ConfigStore
from superstrike_pressure.web.models import CURVE_STRENGTH_MAX, CURVE_STRENGTH_MIN
from superstrike_pressure.web.runtime_service import RuntimeService


@dataclass(frozen=True)
class DevSettings:
    raw_min: int
    raw_max: int
    deadzone: int
    curve: str
    curve_strength: float
    contact_preset: str
    suppress_lmb: bool
    release_teardown: bool
    onset_buffer: bool = False
    true_low_latency: bool = False
    stationary_pressure_updates: bool = False
    pressure_floor: int = 12
    path_stabilization: int = 0
    pressure_influence: int = 85
    injection_hz: float = 240.0

    def as_runtime_patch(self) -> dict[str, Any]:
        channel = {
            "raw_min": self.raw_min,
            "raw_max": self.raw_max,
            "deadzone_low": self.deadzone,
            "deadzone_high": self.deadzone,
            "curve": self.curve,
            "curve_strength": self.curve_strength,
            "contact_preset": self.contact_preset,
            "pressure_floor": self.pressure_floor,
            "path_stabilization": self.path_stabilization,
            "pressure_influence": self.pressure_influence,
            "onset_buffer": self.onset_buffer,
            "true_low_latency": self.true_low_latency,
            "stationary_pressure_updates": self.stationary_pressure_updates,
        }
        return {
            "linked": True,
            "suppress_lmb": self.suppress_lmb,
            "release_teardown": self.release_teardown,
            "left": channel,
        }


def parse_dev_settings(
    *,
    raw_min: str,
    raw_max: str,
    deadzone: str,
    curve: str,
    curve_strength: str,
    contact_preset: str,
    suppress_lmb: bool,
    release_teardown: bool,
    onset_buffer: bool = False,
    true_low_latency: bool = False,
    stationary_pressure_updates: bool = False,
    pressure_floor: str = "12",
    path_stabilization: str = "0",
    pressure_influence: str = "85",
    injection_hz: str = "240",
) -> DevSettings:
    """Convert control values into typed settings before backend validation."""
    try:
        parsed = DevSettings(
            raw_min=int(raw_min),
            raw_max=int(raw_max),
            deadzone=int(deadzone),
            curve=curve,
            curve_strength=float(curve_strength),
            contact_preset=contact_preset,
            suppress_lmb=bool(suppress_lmb),
            release_teardown=bool(release_teardown),
            onset_buffer=bool(onset_buffer),
            true_low_latency=bool(true_low_latency),
            stationary_pressure_updates=bool(stationary_pressure_updates),
            pressure_floor=int(pressure_floor),
            path_stabilization=int(path_stabilization),
            pressure_influence=int(pressure_influence),
            injection_hz=float(injection_hz),
        )
    except ValueError as exc:
        raise ValueError("Raw range, deadzone, and curve strength must be numeric.") from exc

    if parsed.raw_min >= parsed.raw_max:
        raise ValueError("Raw minimum must be lower than raw maximum.")
    if not CURVE_STRENGTH_MIN <= parsed.curve_strength <= CURVE_STRENGTH_MAX:
        raise ValueError(
            f"Curve strength must be between "
            f"{CURVE_STRENGTH_MIN:g} and {CURVE_STRENGTH_MAX:g}."
        )
    if not 0 <= parsed.pressure_floor <= 100:
        raise ValueError("Pressure floor must be between 0 and 100 percent.")
    if not 0 <= parsed.path_stabilization <= 100:
        raise ValueError("Path stabilization must be between 0 and 100 percent.")
    if not 0 <= parsed.pressure_influence <= 100:
        raise ValueError("Pressure influence must be between 0 and 100 percent.")
    if not 30.0 <= parsed.injection_hz <= 500.0:
        raise ValueError("Pen injection rate must be between 30 and 500 Hz.")
    return parsed


def sensitivity_mapping_points(
    settings: DevSettings,
    *,
    samples: int = 65,
    apply_pressure_shaping: bool = True,
) -> list[tuple[int, int]]:
    """Return raw ADC to effective pen-pressure points for the live graph."""
    count = max(2, int(samples))
    points: list[tuple[int, int]] = []
    for index in range(count):
        raw = round(
            settings.raw_min
            + (settings.raw_max - settings.raw_min) * index / (count - 1)
        )
        points.append(
            (
                raw,
                (
                    effective_pressure_for_raw(settings, raw)
                    if apply_pressure_shaping
                    else curve_pressure_for_raw(settings, raw)
                ),
            )
        )
    return points


def curve_pressure_for_raw(settings: DevSettings, raw: int) -> int:
    """Map one raw ADC sample through calibration, deadzone, and curve."""
    pressure_config = PressureConfig(
        raw_min=settings.raw_min,
        raw_max=settings.raw_max,
        out_min=0,
        out_max=1023,
        deadzone_low=settings.deadzone / 100.0,
        deadzone_high=1.0 - settings.deadzone / 100.0,
        curve=normalize_curve_name(settings.curve),
        curve_strength=settings.curve_strength,
    )
    return max(0, min(1024, int(map_pressure(raw, pressure_config))))


def effective_pressure_for_raw(settings: DevSettings, raw: int) -> int:
    """Map one raw ADC sample through the settings that affect brush size."""
    floor = round(settings.pressure_floor * 1024 / 100)
    mapped = curve_pressure_for_raw(settings, raw)
    if mapped > 0 and settings.pressure_influence < 100:
        mapped = round(512 + (mapped - 512) * settings.pressure_influence / 100.0)
    if mapped > 0 and floor > 0:
        mapped = max(mapped, floor)
    return max(0, min(1024, int(mapped)))


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
            name="superstrike-dev-runtime",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=2.0):
            raise RuntimeError("Could not start the bridge runtime loop")

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
        print("ERROR: the Superstrike control panel is Windows-only.")
        return 1
    try:
        from superstrike_pressure.pyside_ui import main as qt_main
    except ImportError as exc:
        print(f"ERROR: PySide6 is required for the desktop UI: {exc}")
        return 1
    return int(qt_main())


if __name__ == "__main__":
    raise SystemExit(main())
