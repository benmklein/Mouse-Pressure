"""Native worker-backed Windows synthetic pen injector.

The pressure/contact state machine remains in Python. Only the final ordered
Windows pointer-report queue and ``InjectSyntheticPointerInput`` calls live in
the native relay, keeping the behavioral surface identical to the proven
synthetic backend while moving frame pacing off the Python event loop.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from mouse_pressure.bridge.native_relay_binding import (
    NativeInputCaptureHandle,
    NativeRelayInput,
    NativeSyntheticRelayHandle,
    load_native_relay,
)
from mouse_pressure.bridge.native_relay_binding import (
    find_native_relay as find_native_relay,
)
from mouse_pressure.bridge.synthetic_pen import _SyntheticPenInjector

NATIVE_FRAME_INTERVAL_US = 120


class NativeTransformedMouseCapture:
    """Dedicated native low-level hook feeding transformed desktop points.

    Raw Input remains responsible for device identity and release ordering.
    This collector only removes the Python callback from the geometry hot path;
    every returned coordinate has already passed through Windows pointer-speed,
    acceleration, DPI, and virtual-desktop transforms.
    """

    def __init__(
        self,
        log: Callable[[str], None],
        *,
        library_path: str | Path | None = None,
    ) -> None:
        self.log = log
        self._library_path = Path(library_path) if library_path is not None else None
        self._capture: NativeInputCaptureHandle | None = None

    def open(self) -> None:
        if self._capture:
            return
        library = load_native_relay(self._library_path)
        self._capture = library.create_input_capture()
        self.log(f"Native transformed-input capture active path={library.path}")

    def drain_moves(self, capacity: int = 4096) -> list[dict[str, int | bool | float]]:
        if self._capture is None or capacity <= 0:
            return []
        batch = self._capture.drain_moves(capacity)
        return [
            {
                "x": int(item.x),
                "y": int(item.y),
                "injected": bool(int(item.flags) & 0x00000001),
                "flags": int(item.flags),
                "message_time_ms": int(item.message_time_ms),
                "observed_at": (
                    float(item.observed_qpc) / float(item.qpc_frequency)
                    if item.qpc_frequency
                    else time.perf_counter()
                ),
            }
            for item in batch
        ]

    def stats(self) -> dict[str, int]:
        if self._capture is None:
            return {}
        stats = self._capture.stats()
        if stats is None:
            return {}
        return {
            "captured": int(stats.captured),
            "drained": int(stats.drained),
            "dropped": int(stats.dropped),
            "max_queue_depth": int(stats.max_queue_depth),
            "last_error": int(stats.last_error),
        }

    def close(self) -> None:
        capture = self._capture
        if capture is None:
            return
        stats = self.stats()
        capture.close()
        self._capture = None
        if stats:
            self.log(
                "Native transformed-input stats "
                f"captured={stats['captured']} drained={stats['drained']} "
                f"dropped={stats['dropped']} max_depth={stats['max_queue_depth']}"
            )


class NativeSyntheticPenInjector:
    """Drop-in injector using a high-priority native report worker."""

    manages_frame_spacing = True

    def __init__(
        self,
        log: Callable[[str], None],
        *,
        library_path: str | Path | None = None,
    ) -> None:
        self.log = log
        self._desktop = _SyntheticPenInjector(log=log)
        self._library_path = Path(library_path) if library_path is not None else None
        self._relay: NativeSyntheticRelayHandle | None = None
        self._next_token = 1
        self.last_submission_token: int | None = None
        self._delivery_lock = threading.Lock()
        self._pending_deliveries: dict[int, dict[str, int | bool]] = {}

    def open(self) -> None:
        if self._relay is not None:
            return
        library = load_native_relay(self._library_path)
        self._relay = library.create_synthetic_relay(NATIVE_FRAME_INTERVAL_US)
        self.log(
            f"SYNTH native relay open api={library.api_version} "
            f"frame_interval={NATIVE_FRAME_INTERVAL_US}us path={library.path}"
        )

    def close(self) -> None:
        relay = self._relay
        if relay is None:
            return
        drained = relay.wait_idle(3000)
        stats = relay.stats()
        if stats is not None:
            injected = max(1, int(stats.injected + stats.failed))
            self.log(
                "SYNTH native relay stats "
                f"submitted={stats.submitted} injected={stats.injected} "
                f"failed={stats.failed} queue_full={stats.queue_full} "
                f"max_depth={stats.max_queue_depth} "
                f"queue_avg={stats.total_queue_delay_us / injected:.1f}us "
                f"queue_max={stats.max_queue_delay_us}us "
                f"inject_avg={stats.total_inject_call_us / injected:.1f}us "
                f"inject_max={stats.max_inject_call_us}us drained={int(drained)}"
                f" completion_dropped={stats.completion_dropped}"
            )
        relay.close()
        self._relay = None

    def inject(
        self,
        *,
        flags: int,
        x: int,
        y: int,
        pressure_1024: int,
        tag: str,
        rotation: int | None = None,
        tilt_x: int | None = None,
        tilt_y: int | None = None,
    ) -> tuple[bool, int]:
        if self._relay is None:
            return False, 6  # ERROR_INVALID_HANDLE
        token = self._next_token
        self._next_token += 1
        self.last_submission_token = token
        ok, error = self._relay.submit(
            flags=int(flags),
            x=int(x),
            y=int(y),
            pressure=max(0, min(1024, int(pressure_1024))),
            rotation=max(0, min(359, int(rotation or 0))),
            tilt_x=max(-90, min(90, int(tilt_x or 0))),
            tilt_y=max(-90, min(90, int(tilt_y or 0))),
            pen_mask=(
                (2 if rotation is not None else 0)
                | (4 if tilt_x is not None else 0)
                | (8 if tilt_y is not None else 0)
            ),
            token=token,
        )
        if not ok:
            self.log(
                f"INJECT {tag} native submit failed err={error} flags=0x{flags:08X} "
                f"x={x} y={y} pressure={pressure_1024}"
            )
        return ok, error

    def inject_batch(
        self,
        reports: list[dict[str, Any]],
    ) -> tuple[bool, int, list[int]]:
        """Submit one Raw Input-derived path to the native scheduler."""
        if self._relay is None:
            return False, 6, []  # ERROR_INVALID_HANDLE
        if not reports:
            return True, 0, []
        tokens = list(range(self._next_token, self._next_token + len(reports)))
        self._next_token += len(reports)
        self.last_submission_token = tokens[-1]
        native_reports = (NativeRelayInput * len(reports))()
        for index, (report, token) in enumerate(zip(reports, tokens, strict=True)):
            rotation = report.get("rotation")
            tilt_x = report.get("tilt_x")
            tilt_y = report.get("tilt_y")
            native_reports[index] = NativeRelayInput(
                int(report["flags"]),
                int(report["x"]),
                int(report["y"]),
                max(0, min(1024, int(report["pressure_1024"]))),
                max(0, min(359, int(rotation or 0))),
                max(-90, min(90, int(tilt_x or 0))),
                max(-90, min(90, int(tilt_y or 0))),
                (2 if rotation is not None else 0)
                | (4 if tilt_x is not None else 0)
                | (8 if tilt_y is not None else 0),
                token,
            )
        ok, error = self._relay.submit_batch(native_reports, len(reports))
        if not ok:
            self.log(
                f"INJECT native batch submit failed err={error} "
                f"reports={len(reports)}"
            )
        return ok, error, tokens

    def wait_idle(self, timeout_ms: int = 10) -> bool:
        if self._relay is None:
            return False
        return self._relay.wait_idle(timeout_ms)

    def drain_delivery_events(self, capacity: int = 4096) -> list[dict[str, int | bool]]:
        """Return completed native reports without blocking the input path."""
        if self._relay is None or capacity <= 0:
            return []
        batch = self._relay.drain_completions(capacity)
        drained = [
            {
                "token": int(item.token),
                "submitted_qpc": int(item.submitted_qpc),
                "inject_begin_qpc": int(item.inject_begin_qpc),
                "completed_qpc": int(item.completed_qpc),
                "qpc_frequency": int(item.qpc_frequency),
                "flags": int(item.flags),
                "x": int(item.x),
                "y": int(item.y),
                "pressure": int(item.pressure),
                "success": bool(item.success),
                "error": int(item.error),
                "queue_delay_us": int(item.queue_delay_us),
                "inject_call_us": int(item.inject_call_us),
            }
            for item in batch
        ]
        with self._delivery_lock:
            pending = list(self._pending_deliveries.values())
            self._pending_deliveries.clear()
        return pending + drained

    def collect_delivery_events(
        self,
        tokens: set[int] | frozenset[int],
        timeout_ms: int = 25,
    ) -> list[dict[str, int | bool]]:
        """Collect selected completions on a diagnostics thread."""
        wanted = {int(token) for token in tokens}
        if not wanted:
            return []
        deadline = time.perf_counter() + max(0, int(timeout_ms)) / 1000.0
        selected: dict[int, dict[str, int | bool]] = {}
        while wanted - selected.keys():
            events = self.drain_delivery_events()
            with self._delivery_lock:
                for event in events:
                    token = int(event["token"])
                    if token in wanted:
                        selected[token] = event
                    else:
                        self._pending_deliveries[token] = event
            if not (wanted - selected.keys()) or time.perf_counter() >= deadline:
                break
            time.sleep(0.0005)
        return [selected[token] for token in sorted(selected)]

    def get_cursor_pos(self) -> tuple[int, int]:
        return self._desktop.get_cursor_pos()

    def is_lmb_down(self) -> bool:
        return self._desktop.is_lmb_down()

    def is_rmb_down(self) -> bool:
        return self._desktop.is_rmb_down()

    def emit_left_click(self) -> None:
        self._desktop.emit_left_click()

    def emit_right_click(self) -> None:
        self._desktop.emit_right_click()
