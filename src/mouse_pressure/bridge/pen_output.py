"""Compose and own one native synthetic pen output session."""

from __future__ import annotations

import asyncio
from typing import Callable, Protocol

from mouse_pressure.bridge.native_synthetic import (
    NativeSyntheticPenInjector,
    NativeTransformedMouseCapture,
)
from mouse_pressure.bridge.synthetic_pen import (
    SyntheticPenConfig,
    SyntheticPenEmitter,
    SyntheticPenSample,
)


class PenEmitter(Protocol):
    """Lifecycle contract required by the native pen-output composition root."""

    config: SyntheticPenConfig
    pen: object

    def set_native_input_capture(self, capture: object) -> None: ...

    def set_movement_callback(self, callback: Callable[[], None] | None) -> None: ...

    def set_force_stop_callback(
        self,
        callback: Callable[[str], None] | None,
    ) -> None: ...

    def open_unarmed(self) -> None: ...

    def arm_input(self) -> None: ...

    def update(
        self,
        left_mapped: int,
        right_mapped: int,
        *,
        pressure_fresh: bool = True,
        left_raw: int | None = None,
        right_raw: int | None = None,
    ) -> SyntheticPenSample | None: ...

    def set_debug_mode(self, enabled: bool) -> None: ...

    def sync_button_modes(self) -> None: ...

    def fail_open(self, reason: str) -> None: ...

    def release(self) -> None: ...

    def close(self) -> None: ...


EmitterFactory = Callable[
    [SyntheticPenConfig, Callable[[str], None]],
    PenEmitter,
]
InjectorFactory = Callable[[Callable[[str], None]], object]
CaptureFactory = Callable[[Callable[[str], None]], object]


class PenOutput:
    """Hide native adapter composition and output lifecycle ordering."""

    def __init__(
        self,
        config: SyntheticPenConfig,
        log: Callable[[str], None],
        *,
        emitter_factory: EmitterFactory = SyntheticPenEmitter,
        movement_callback: Callable[[], None] | None = None,
        force_stop_callback: Callable[[str], None] | None = None,
        injector_factory: InjectorFactory = (
            lambda logger: NativeSyntheticPenInjector(log=logger)
        ),
        capture_factory: CaptureFactory = (
            lambda logger: NativeTransformedMouseCapture(log=logger)
        ),
    ) -> None:
        emitter = emitter_factory(config, log)
        emitter.pen = injector_factory(log)
        emitter.set_native_input_capture(capture_factory(log))
        emitter.set_movement_callback(movement_callback)
        emitter.set_force_stop_callback(force_stop_callback)
        self._emitter = emitter
        self._closed = False
        self._opened = False
        self._armed = False
        self._ready_event = asyncio.Event()

    def open(self) -> None:
        """Open output while leaving click suppression disarmed."""
        if self._opened:
            return
        self._emitter.open_unarmed()
        self._opened = True

    @property
    def ready(self) -> bool:
        return self._ready_event.is_set()

    async def wait_until_ready(self, timeout_s: float) -> None:
        """Wait until one update succeeds and physical-click suppression is armed."""
        await asyncio.wait_for(
            self._ready_event.wait(),
            timeout=max(0.0, float(timeout_s)),
        )

    def update(
        self,
        left_mapped: int,
        right_mapped: int,
        *,
        pressure_fresh: bool = True,
        left_raw: int | None = None,
        right_raw: int | None = None,
    ) -> SyntheticPenSample | None:
        sample = self._emitter.update(
            left_mapped=left_mapped,
            right_mapped=right_mapped,
            pressure_fresh=pressure_fresh,
            left_raw=left_raw,
            right_raw=right_raw,
        )
        if not self._armed:
            self._emitter.arm_input()
            self._armed = True
            self._ready_event.set()
        return sample

    def reconfigure(self, config: SyntheticPenConfig) -> None:
        self._emitter.config = config
        self._emitter.set_debug_mode(config.debug_mode)
        self._emitter.sync_button_modes()

    def fail_open(self, reason: str) -> None:
        self._emitter.fail_open(reason)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._emitter.set_movement_callback(None)
            self._emitter.set_force_stop_callback(None)
            self._emitter.release()
        finally:
            self._emitter.close()
