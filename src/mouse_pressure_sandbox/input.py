"""Non-blocking access to processed Mouse Pressure telemetry."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from mouse_pressure.sandbox_telemetry import SandboxTelemetryReader


@dataclass(frozen=True)
class SensorSnapshot:
    connected: bool = False
    left_raw: int = 0
    right_raw: int = 0
    left_pressure: float = 0.0
    right_pressure: float = 0.0
    status: str = "Start Mouse Pressure to use the sensor"


class PressureSensorReader:
    """Poll the driver's local telemetry without ever opening the mouse HID."""

    def __init__(self, *, poll_hz: float = 240.0) -> None:
        self.poll_interval_s = 1.0 / max(30.0, float(poll_hz))
        self._snapshot = SensorSnapshot()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="mouse-pressure-sandbox-input",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def retry(self) -> None:
        self.stop()
        self._thread = None
        self.start()

    def snapshot(self) -> SensorSnapshot:
        with self._lock:
            return self._snapshot

    def _set_snapshot(self, snapshot: SensorSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def _run(self) -> None:
        reader = SandboxTelemetryReader()
        try:
            while not self._stop.is_set():
                sample = reader.read(max_age_s=0.5)
                if sample is not None and sample.active and sample.device_found:
                    snapshot = SensorSnapshot(
                        connected=True,
                        left_raw=sample.left_raw,
                        right_raw=sample.right_raw,
                        left_pressure=sample.left_pressure,
                        right_pressure=sample.right_pressure,
                        status="Receiving driver pressure",
                    )
                else:
                    snapshot = SensorSnapshot()
                self._set_snapshot(snapshot)
                self._stop.wait(self.poll_interval_s)
        finally:
            reader.close()
