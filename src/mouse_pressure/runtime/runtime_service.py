"""Runtime coordinator for streaming, mapping, and live config updates."""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from typing import Callable, TypeAlias

from mouse_pressure.bridge.config import ChannelConfig, LaunchConfig, RuntimeConfig
from mouse_pressure.bridge.curves import (
    map_normalized_pressure,
    normalize_curve_name,
    pressure_config_for_channel,
)
from mouse_pressure.bridge.pen_output import PenOutput
from mouse_pressure.bridge.synthetic_pen import SyntheticPenConfig, SyntheticPenEmitter
from mouse_pressure.runtime.config_store import (
    ConfigStore,
    runtime_config_from_dict,
    runtime_config_to_dict,
)
from mouse_pressure.runtime.device_settings import (
    SessionDeviceSettings,
    apply_device_settings,
    device_settings_differ,
    read_device_settings,
    validate_device_settings,
)
from mouse_pressure.runtime.device_settings_lease import TemporaryDeviceSettingsLease
from mouse_pressure.runtime.log_bus import GLOBAL_LOG_BUS, LogBus
from mouse_pressure.runtime.models import (
    StreamAlreadyActiveError,
    StreamNotActiveError,
    ValidationError,
)
from mouse_pressure.sandbox_telemetry import SandboxTelemetryWriter
from mouse_pressure.sniff.hidpp_pressure import (
    PressureHidppSession,
    extract_mode3_lr_pressure_raw,
    normalize_raw_pressure,
    parse_feature_0c_frame,
)

_RawSample: TypeAlias = tuple[float, int, int]
_DeviceCommand: TypeAlias = tuple[
    SessionDeviceSettings,
    asyncio.AbstractEventLoop,
    asyncio.Future[SessionDeviceSettings],
]


class RuntimeService:
    def __init__(
        self,
        launch_config: LaunchConfig,
        config_store: ConfigStore,
        *,
        log_bus: LogBus | None = None,
        session_factory: Callable[
            [Callable[[str], None]], PressureHidppSession
        ] = PressureHidppSession,
        emitter_factory: Callable[
            [SyntheticPenConfig, Callable[[str], None]], SyntheticPenEmitter
        ] = SyntheticPenEmitter,
        stream_stall_timeout_s: float = 4.0,
        stream_recovery_after_s: float = 0.5,
        stream_keepalive_interval_s: float = 2.0,
        max_stream_recovery_attempts: int = 3,
    ) -> None:
        self.launch_config = launch_config
        self.config_store = config_store
        self.log_bus = log_bus or GLOBAL_LOG_BUS
        self._session_factory = session_factory
        self._crash_restore_enabled = session_factory is PressureHidppSession
        self._emitter_factory = emitter_factory
        self._stream_stall_timeout_s = max(0.25, float(stream_stall_timeout_s))
        self._stream_recovery_after_s = max(0.1, float(stream_recovery_after_s))
        self._stream_keepalive_interval_s = max(1.0, float(stream_keepalive_interval_s))
        self._max_stream_recovery_attempts = max(1, int(max_stream_recovery_attempts))

        self._config = self.config_store.load()
        self._left_curve_config = pressure_config_for_channel(self._config.left)
        self._right_curve_config = pressure_config_for_channel(
            self._effective_right_channel(self._config)
        )

        self._telemetry_callback: Callable[[dict], None] | None = None
        self._sandbox_telemetry = SandboxTelemetryWriter()

        self._stream_active = False
        self._device_found = False

        self._session: PressureHidppSession | None = None
        self._emitter: PenOutput | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader_thread: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._sample_queue: asyncio.Queue[_RawSample | None] | None = None
        self._raw_sample_queue: asyncio.Queue[tuple[int, int]] | None = None
        self._movement_queue: asyncio.Queue[bool | None] | None = None
        self._processor_task: asyncio.Task[None] | None = None
        self._movement_task: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._telemetry_ready_event: asyncio.Event | None = None
        self._latest_emission_sample: _RawSample | None = None
        self._last_sample_t: float | None = None
        self._last_inject_monotonic: float | None = None
        self._inject_hz = 0.0
        self._last_sample_monotonic: float | None = None
        self._failure_callback: Callable[[str], None] | None = None
        self._force_stop_callback: Callable[[str], None] | None = None
        self._force_stop_scheduled = False
        self._state_lock = threading.Lock()
        self._device_commands: queue.Queue[_DeviceCommand] = queue.Queue()
        self._device_settings_lease: TemporaryDeviceSettingsLease | None = None

    async def start_stream(
        self,
        *,
        device_settings: dict[str, int] | None = None,
    ) -> None:
        if self._stream_active:
            raise StreamAlreadyActiveError("Stream is already active")
        backend = str(self.launch_config.backend).strip().lower()
        if backend not in {"native_synthetic", "telemetry"}:
            raise RuntimeError(
                f"Unknown pen output backend {self.launch_config.backend!r}; "
                "expected 'native_synthetic' or internal 'telemetry'"
            )
        requested_device_settings = (
            validate_device_settings(device_settings)
            if device_settings is not None
            else None
        )

        self._loop = asyncio.get_running_loop()
        self._reader_stop.clear()
        # Pressure HID reports can arrive much faster than the synthetic pointer
        # rate. Keep only the newest report so old pen frames can never build up
        # behind the cursor and replay after the physical button is released.
        self._sample_queue = asyncio.Queue(maxsize=1)
        self._raw_sample_queue = asyncio.Queue(maxsize=256)
        self._movement_queue = (
            None if backend == "telemetry" else asyncio.Queue(maxsize=512)
        )
        self._telemetry_ready_event = (
            asyncio.Event() if backend == "telemetry" else None
        )
        self._latest_emission_sample = None
        self._last_sample_t = None
        self._last_inject_monotonic = None
        self._inject_hz = 0.0
        self._last_sample_monotonic = time.perf_counter()
        self._force_stop_scheduled = False

        session: PressureHidppSession | None = None
        for attempt in range(1, 4):
            session = self._session_factory(self._log)
            try:
                session.open()
                self._device_found = True
                session.enable_pressure_stream(
                    mode=self.launch_config.mode,
                    mode_arg=self.launch_config.mode_arg,
                )
                break
            except TimeoutError as exc:
                self._device_found = False
                try:
                    session.close()
                except Exception:
                    pass
                if attempt >= 3:
                    raise RuntimeError(
                        "The mouse is connected but its pressure command channel "
                        "is not responding. Close Logitech G HUB or unplug and "
                        "reconnect the mouse, then press Start again."
                    ) from exc
                self.log_bus.warn(
                    f"Pressure device did not answer during startup; retrying "
                    f"({attempt}/3)."
                )
                await asyncio.sleep(0.25)
            except Exception:
                self._device_found = False
                try:
                    session.close()
                except Exception:
                    pass
                raise

        if session is None:
            raise RuntimeError("Could not create the pressure device session")

        device_settings_lease: TemporaryDeviceSettingsLease | None = None
        if requested_device_settings is not None:
            device_settings_lease = TemporaryDeviceSettingsLease(
                config_dir=getattr(self.config_store, "config_dir", None),
                recovery_enabled=self._crash_restore_enabled,
                pressure_mode=self.launch_config.mode,
                pressure_mode_arg=self.launch_config.mode_arg,
            )
            try:
                applied = device_settings_lease.activate(
                    session,
                    requested_device_settings,
                )
                self.log_bus.info(
                    f"Session mouse settings applied: DPI {applied.dpi}, "
                    f"haptics L{applied.haptic_left}/R{applied.haptic_right}, "
                    f"actuation L{applied.actuation_left}/R{applied.actuation_right}"
                )
            except Exception:
                try:
                    session.close()
                except Exception:
                    pass
                raise

        emitter: PenOutput | None = None
        try:
            if backend != "telemetry":
                emitter = PenOutput(
                    self._emitter_config_from_runtime(),
                    self._log,
                    emitter_factory=self._emitter_factory,
                    movement_callback=self._signal_movement,
                    force_stop_callback=self._schedule_force_stop,
                )
                emitter.open()
        except Exception:
            self._device_found = False
            if device_settings_lease is not None:
                try:
                    restored = device_settings_lease.restore(session)
                    if restored is not None:
                        self._log_restored_device_settings(restored)
                except Exception as exc:
                    self.log_bus.error(
                        f"Could not restore the original mouse settings: {exc}"
                    )
            try:
                session.close()
            except Exception:
                pass
            raise

        self._session = session
        self._emitter = emitter
        self._device_settings_lease = device_settings_lease
        self._stream_active = True
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="mouse-pressure-reader", daemon=True
        )
        self._reader_thread.start()
        self._processor_task = asyncio.create_task(self._process_samples())
        self._movement_task = (
            asyncio.create_task(self._process_movement())
            if self._movement_queue is not None
            else None
        )
        self._health_task = asyncio.create_task(self._monitor_stream_health())
        if emitter is not None or backend == "telemetry":
            try:
                if emitter is not None:
                    await emitter.wait_until_ready(1.0)
                else:
                    ready_event = self._telemetry_ready_event
                    if ready_event is None:
                        raise RuntimeError(
                            "Pressure startup readiness was not initialized"
                        )
                    await asyncio.wait_for(ready_event.wait(), timeout=1.0)
            except Exception as exc:
                await self.stop_stream()
                if isinstance(exc, TimeoutError):
                    raise RuntimeError(
                        "The pressure stream did not produce a usable frame during "
                        "startup. Native clicks were left enabled; press Start to "
                        "try again."
                    ) from exc
                raise
            if emitter is not None:
                self.log_bus.info(
                    "Pressure input primed; mouse button suppression armed"
                )
        if backend == "telemetry":
            self.log_bus.info(
                "Telemetry-only pressure stream started; native mouse input unchanged."
            )
        else:
            self.log_bus.info(
                f"Stream started (pressure ~60 Hz, raw mouse event-driven, "
                f"fallback pen tick {self.launch_config.hz:.0f} Hz)"
            )

    async def stop_stream(self) -> None:
        if not self._stream_active:
            raise StreamNotActiveError("Stream is not active")

        self._stream_active = False
        self._sandbox_telemetry.set_inactive()
        self._reader_stop.set()

        health = self._health_task
        current_task = asyncio.current_task()
        if health is not None and health is not current_task:
            health.cancel()
            try:
                await health
            except asyncio.CancelledError:
                pass
        self._health_task = None

        reader = self._reader_thread
        if reader is not None:
            await asyncio.to_thread(reader.join, 1.5)
        self._reader_thread = None

        sample_queue = self._sample_queue
        if sample_queue is not None:
            self._enqueue_sample(None)

        processor = self._processor_task
        if processor is not None:
            try:
                await processor
            finally:
                self._processor_task = None

        movement_queue = self._movement_queue
        if movement_queue is not None:
            self._enqueue_movement(None)
        movement = self._movement_task
        if movement is not None:
            try:
                await movement
            finally:
                self._movement_task = None

        if self._emitter is not None:
            self._emitter.close()
            self._emitter = None

        if self._session is not None:
            lease = self._device_settings_lease
            if lease is not None:
                try:
                    restored = await asyncio.to_thread(
                        lease.restore,
                        self._session,
                        on_pause_error=lambda exc: self.log_bus.warn(
                            "Could not pause the pressure stream before restoring "
                            f"mouse settings: {exc}"
                        ),
                    )
                    if restored is not None:
                        self._log_restored_device_settings(restored)
                except Exception as exc:
                    self.log_bus.error(
                        f"Could not restore the original mouse settings: {exc}"
                    )
            self._session.close()
            self._session = None
        self._device_settings_lease = None

        self._sample_queue = None
        self._raw_sample_queue = None
        self._movement_queue = None
        self._telemetry_ready_event = None
        self._latest_emission_sample = None
        self.log_bus.info("Stream stopped")

    def apply_config(
        self, patch: dict, *, replace_existing: bool = False
    ) -> RuntimeConfig:
        if not isinstance(patch, dict):
            raise ValidationError("config patch must be an object")

        merged = runtime_config_to_dict(
            RuntimeConfig() if replace_existing else self._config
        )
        self._merge_patch_dict(merged, patch)
        validated = runtime_config_from_dict(merged)

        with self._state_lock:
            self._config = validated
            effective_right = self._effective_right_channel(validated)
            self._left_curve_config = pressure_config_for_channel(validated.left)
            self._right_curve_config = pressure_config_for_channel(effective_right)
            if self._emitter is not None:
                self._emitter.reconfigure(self._emitter_config_from_runtime())

        self.config_store.save(validated)
        return validated

    def restore_defaults(self, defaults: RuntimeConfig | None = None) -> RuntimeConfig:
        """Replace persisted runtime settings with the factory configuration."""
        config = defaults or RuntimeConfig()
        return self.apply_config(
            runtime_config_to_dict(config),
            replace_existing=True,
        )

    def get_config(self) -> RuntimeConfig:
        return self._config

    def set_telemetry_callback(self, cb: Callable[[dict], None] | None) -> None:
        self._telemetry_callback = cb

    def set_failure_callback(self, cb: Callable[[str], None] | None) -> None:
        self._failure_callback = cb

    def set_force_stop_callback(self, cb: Callable[[str], None] | None) -> None:
        self._force_stop_callback = cb

    def _schedule_force_stop(self, reason: str) -> None:
        """Move a global-hotkey request from the hook thread to asyncio."""
        loop = self._loop
        if loop is None:
            return

        def schedule() -> None:
            if not self._stream_active or self._force_stop_scheduled:
                return
            self._force_stop_scheduled = True
            asyncio.create_task(self._force_stop(reason))

        loop.call_soon_threadsafe(schedule)

    async def _force_stop(self, reason: str) -> None:
        try:
            self.log_bus.warn(
                f"{reason}; stopping the driver and restoring mouse settings"
            )
            if self._stream_active:
                await self.stop_stream()
        finally:
            self._force_stop_scheduled = False
            callback = self._force_stop_callback
            if callback is not None:
                callback(reason)

    async def wait_for_raw_sample(self, timeout_s: float = 1.0) -> tuple[int, int]:
        if not self._stream_active or self._raw_sample_queue is None:
            raise StreamNotActiveError("Stream is not active")
        return await asyncio.wait_for(self._raw_sample_queue.get(), timeout=timeout_s)

    async def apply_device_settings(
        self,
        *,
        dpi: int,
        haptic_left: int,
        haptic_right: int,
        actuation_left: int = 5,
        actuation_right: int = 5,
    ) -> dict[str, int]:
        """Apply mouse hardware settings on the HID reader thread."""
        if not self._stream_active or self._session is None:
            raise StreamNotActiveError(
                "Start the driver before applying device settings"
            )
        if not 100 <= dpi <= 32000 or dpi % 50 != 0:
            raise ValidationError("DPI must be 100..32000 in 50-DPI increments")
        if not 0 <= haptic_left <= 5 or not 0 <= haptic_right <= 5:
            raise ValidationError("Haptic levels must be in 0..5")
        if not 1 <= actuation_left <= 10 or not 1 <= actuation_right <= 10:
            raise ValidationError("Actuation levels must be in 1..10")

        requested = validate_device_settings(
            {
                "dpi": int(dpi),
                "haptic_left": int(haptic_left),
                "haptic_right": int(haptic_right),
                "actuation_left": int(actuation_left),
                "actuation_right": int(actuation_right),
            }
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[SessionDeviceSettings] = loop.create_future()
        self._device_commands.put(
            (
                requested,
                loop,
                future,
            )
        )
        applied = await asyncio.wait_for(future, timeout=3.0)
        return applied.to_dict()

    async def detect_device_settings(self) -> dict[str, int]:
        """Read DPI and haptics without starting the pressure driver."""
        if self._stream_active:
            raise StreamAlreadyActiveError(
                "Stop the driver before detecting the normal mouse settings"
            )

        def detect() -> SessionDeviceSettings:
            session = self._session_factory(self._log)
            try:
                session.open()
                return read_device_settings(session).session
            finally:
                session.close()

        detected = await asyncio.to_thread(detect)
        settings = detected.to_dict()
        self.log_bus.info(
            f"Mouse settings detected: DPI {settings['dpi']}, "
            f"haptics L{settings['haptic_left']}/R{settings['haptic_right']}, "
            f"actuation L{settings['actuation_left']}/R{settings['actuation_right']}"
        )
        return settings

    def _log_restored_device_settings(
        self,
        restored: SessionDeviceSettings,
    ) -> None:
        self.log_bus.info(
            f"Original mouse settings restored: DPI {restored.dpi}, "
            f"haptics L{restored.haptic_left}/R{restored.haptic_right}, "
            f"actuation L{restored.actuation_left}/R{restored.actuation_right}"
        )

    @property
    def stream_active(self) -> bool:
        return self._stream_active

    @property
    def device_found(self) -> bool:
        return self._device_found

    def _merge_patch_dict(self, merged: dict, patch: dict) -> None:
        if "linked" in patch:
            linked = patch["linked"]
            if not isinstance(linked, bool):
                raise ValidationError("linked must be a boolean")
            merged["linked"] = linked

        for boolean_name in (
            "left_enabled",
            "right_enabled",
            "suppress_lmb",
            "suppress_rmb",
            "debug_mode",
            "minimize_to_tray",
            "session_device_settings_follow_normal",
        ):
            if boolean_name in patch:
                value = patch[boolean_name]
                if not isinstance(value, bool):
                    raise ValidationError(f"{boolean_name} must be a boolean")
                merged[boolean_name] = value

        for hotkey_name in ("activation_hotkey", "deactivation_hotkey"):
            if hotkey_name in patch:
                hotkey = patch[hotkey_name]
                if not isinstance(hotkey, str):
                    raise ValidationError(f"{hotkey_name} must be a string")
                merged[hotkey_name] = hotkey

        for remap_name in ("remap_mode", "remap_hold_hotkey"):
            if remap_name in patch:
                value = patch[remap_name]
                if not isinstance(value, str):
                    raise ValidationError(f"{remap_name} must be a string")
                merged[remap_name] = value

        for integer_name in (
            "session_dpi",
            "session_haptic_left",
            "session_haptic_right",
        ):
            if integer_name in patch:
                value = patch[integer_name]
                if not isinstance(value, int):
                    raise ValidationError(f"{integer_name} must be an integer")
                merged[integer_name] = value

        if "left" in patch:
            left_patch = patch["left"]
            if not isinstance(left_patch, dict):
                raise ValidationError("left patch must be an object")
            merged["left"] = {**merged["left"], **left_patch}

        if "right" in patch:
            right_patch = patch["right"]
            if not isinstance(right_patch, dict):
                raise ValidationError("right patch must be an object")
            merged["right"] = {**merged["right"], **right_patch}

    @staticmethod
    def _effective_right_channel(config: RuntimeConfig) -> ChannelConfig:
        return config.left if config.linked else config.right

    def _emitter_config_from_runtime(self) -> SyntheticPenConfig:
        effective_right = self._effective_right_channel(self._config)
        left_target = (
            self._config.left.output_target if self._config.left_enabled else "disabled"
        )
        right_target = (
            effective_right.output_target if self._config.right_enabled else "disabled"
        )
        return SyntheticPenConfig(
            contact_threshold=10,
            release_threshold=6,
            min_contact_pressure=round(self._config.left.pressure_floor * 1024 / 100),
            path_stabilization=self._config.left.path_stabilization,
            pressure_influence=self._config.left.pressure_influence,
            onset_buffer=self._config.left.onset_buffer,
            true_low_latency=self._config.left.true_low_latency,
            # Preserve the user's Windows pointer transform. The native relay
            # correlates device-scoped Raw Input with transformed coordinates.
            allow_raw_direct_motion=False,
            stationary_pressure_updates=(self._config.left.stationary_pressure_updates),
            immediate_button_wake=True,
            right_contact_threshold=10,
            right_release_threshold=6,
            right_min_contact_pressure=round(
                effective_right.pressure_floor * 1024 / 100
            ),
            right_path_stabilization=effective_right.path_stabilization,
            right_pressure_influence=effective_right.pressure_influence,
            right_onset_buffer=effective_right.onset_buffer,
            right_true_low_latency=effective_right.true_low_latency,
            right_stationary_pressure_updates=(
                effective_right.stationary_pressure_updates
            ),
            right_immediate_button_wake=True,
            clean_stroke_endings=True,
            right_clean_stroke_endings=True,
            pressure_interp_steps=max(1, int(round(self.launch_config.hz / 60.0))),
            suppress_lmb=(
                self._config.left_enabled
                and (
                    self._config.suppress_lmb
                    or left_target
                    in {"mouse_sensitivity", "x_tilt", "y_tilt", "rotation"}
                )
            ),
            suppress_rmb=(
                self._config.right_enabled
                and (
                    (
                        self._config.suppress_lmb
                        if self._config.linked
                        else self._config.suppress_rmb
                    )
                    or right_target
                    in {"mouse_sensitivity", "x_tilt", "y_tilt", "rotation"}
                )
            ),
            left_output_target=left_target,
            right_output_target=right_target,
            remap_mode=self._config.remap_mode,
            remap_hold_hotkey=self._config.remap_hold_hotkey,
            sensitivity_light=self._config.left.sensitivity_light,
            sensitivity_firm=self._config.left.sensitivity_firm,
            right_sensitivity_light=effective_right.sensitivity_light,
            right_sensitivity_firm=effective_right.sensitivity_firm,
            x_tilt_light=self._config.left.x_tilt_light,
            x_tilt_firm=self._config.left.x_tilt_firm,
            right_x_tilt_light=effective_right.x_tilt_light,
            right_x_tilt_firm=effective_right.x_tilt_firm,
            y_tilt_light=self._config.left.y_tilt_light,
            y_tilt_firm=self._config.left.y_tilt_firm,
            right_y_tilt_light=effective_right.y_tilt_light,
            right_y_tilt_firm=effective_right.y_tilt_firm,
            rotation_light=self._config.left.rotation_light,
            rotation_firm=self._config.left.rotation_firm,
            right_rotation_light=effective_right.rotation_light,
            right_rotation_firm=effective_right.rotation_firm,
            deactivation_hotkey=self._config.deactivation_hotkey,
            debug_mode=self._config.debug_mode,
            output_backend=str(self.launch_config.backend).strip().lower(),
            trace_dir=self.launch_config.trace_dir,
            trace_raw_min=self._config.left.raw_min,
            trace_raw_max=self._config.left.raw_max,
            trace_curve=normalize_curve_name(self._config.left.curve),
            trace_curve_strength=self._config.left.curve_strength,
            right_trace_raw_min=effective_right.raw_min,
            right_trace_raw_max=effective_right.raw_max,
            right_trace_curve=normalize_curve_name(effective_right.curve),
            right_trace_curve_strength=effective_right.curve_strength,
        )

    def _reader_loop(self) -> None:
        session = self._session
        loop = self._loop
        if session is None or loop is None:
            return

        last_pressure_sample = time.perf_counter()
        next_recovery_at = last_pressure_sample + self._stream_recovery_after_s
        next_keepalive_at = last_pressure_sample + self._stream_keepalive_interval_s
        recovery_attempt = 0

        while not self._reader_stop.is_set():
            if self._service_device_commands(session):
                now = time.perf_counter()
                last_pressure_sample = now
                next_recovery_at = now + self._stream_recovery_after_s
                next_keepalive_at = now + self._stream_keepalive_interval_s

            item = session.read_next(timeout_s=0.05)
            if item is not None:
                ts, data = item
                frame = parse_feature_0c_frame(
                    data,
                    ts,
                    feature_index=getattr(session, "pressure_feature_index", 0x0C),
                    device_index=getattr(session, "device_index", None),
                )
                if frame is not None:
                    left_raw, right_raw = extract_mode3_lr_pressure_raw(frame)
                    if left_raw is not None and right_raw is not None:
                        now = time.perf_counter()
                        last_pressure_sample = now
                        next_recovery_at = now + self._stream_recovery_after_s
                        if recovery_attempt:
                            self.log_bus.info(
                                f"Pressure stream recovered after {recovery_attempt} re-enable attempt(s)"
                            )
                            recovery_attempt = 0
                        self._last_sample_monotonic = now
                        loop.call_soon_threadsafe(
                            self._enqueue_sample, (ts, left_raw, right_raw)
                        )

            now = time.perf_counter()
            if (
                not self._reader_stop.is_set()
                and recovery_attempt == 0
                and now >= next_keepalive_at
            ):
                try:
                    maintain = getattr(session, "maintain_pressure_stream", None)
                    if callable(maintain):
                        maintain()
                    else:
                        self._refresh_pressure_stream(session)
                except Exception as exc:
                    self.log_bus.warn(f"Pressure stream keepalive failed: {exc}")
                next_keepalive_at = (
                    time.perf_counter() + self._stream_keepalive_interval_s
                )

            if not self._reader_stop.is_set() and now >= next_recovery_at:
                recovery_attempt += 1
                silence_s = now - last_pressure_sample
                self.log_bus.warn(
                    f"Pressure stream silent for {silence_s:.1f}s; "
                    f"re-enable attempt {recovery_attempt}"
                )
                # Grant the recovery exchange time to complete before the health
                # monitor declares the stream dead. enable_pressure_stream owns
                # this HID handle on the reader thread, so reads and writes do
                # not race each other.
                if recovery_attempt <= self._max_stream_recovery_attempts:
                    self._last_sample_monotonic = now
                try:
                    self._refresh_pressure_stream(session)
                except Exception as exc:
                    self.log_bus.error(
                        f"Pressure stream re-enable attempt {recovery_attempt} failed: {exc}"
                    )
                next_recovery_at = time.perf_counter() + self._stream_recovery_after_s
                next_keepalive_at = (
                    time.perf_counter() + self._stream_keepalive_interval_s
                )

        loop.call_soon_threadsafe(self._enqueue_sample, None)

    def _service_device_commands(self, session: PressureHidppSession) -> bool:
        handled = False
        while True:
            try:
                settings, loop, future = self._device_commands.get_nowait()
            except queue.Empty:
                return handled

            handled = True
            self._last_sample_monotonic = time.perf_counter()
            stream_paused = False
            try:
                current = read_device_settings(
                    session,
                    discover_feature=False,
                )
                if device_settings_differ(current, settings):
                    lease = self._device_settings_lease
                    if lease is not None:
                        result = lease.apply_live(
                            session,
                            settings,
                            current_settings=current,
                        )
                    else:
                        session.disable_pressure_stream()
                        stream_paused = True
                        result = apply_device_settings(
                            session,
                            settings,
                            current_settings=current,
                        )
                        session.enable_pressure_stream(
                            mode=self.launch_config.mode,
                            mode_arg=self.launch_config.mode_arg,
                        )
                        stream_paused = False
                else:
                    result = apply_device_settings(
                        session,
                        settings,
                        current_settings=current,
                    )
            except Exception as exc:
                loop.call_soon_threadsafe(
                    self._finish_device_command, future, None, exc
                )
            else:
                self.log_bus.info(
                    f"Device settings applied: DPI {result.dpi}, "
                    f"haptics L{result.haptic_left}/R{result.haptic_right}, "
                    f"actuation L{result.actuation_left}/R{result.actuation_right}"
                )
                loop.call_soon_threadsafe(
                    self._finish_device_command, future, result, None
                )
            finally:
                if stream_paused:
                    try:
                        session.enable_pressure_stream(
                            mode=self.launch_config.mode,
                            mode_arg=self.launch_config.mode_arg,
                        )
                    except Exception as exc:
                        self.log_bus.error(
                            f"Could not resume pressure streaming after mouse "
                            f"settings failed: {exc}"
                        )
                self._last_sample_monotonic = time.perf_counter()

    @staticmethod
    def _finish_device_command(
        future: asyncio.Future[SessionDeviceSettings],
        result: SessionDeviceSettings | None,
        error: Exception | None,
    ) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        elif result is not None:
            future.set_result(result)

    def _refresh_pressure_stream(self, session: PressureHidppSession) -> None:
        refresh = getattr(session, "refresh_pressure_stream", None)
        if callable(refresh):
            refresh(mode=self.launch_config.mode, mode_arg=self.launch_config.mode_arg)
            return
        # Compatibility for injected test/dummy sessions and older adapters.
        session.enable_pressure_stream(
            mode=self.launch_config.mode,
            mode_arg=self.launch_config.mode_arg,
        )

    def _enqueue_sample(self, sample: _RawSample | None) -> None:
        if self._sample_queue is None:
            return
        try:
            self._sample_queue.put_nowait(sample)
        except asyncio.QueueFull:
            try:
                _ = self._sample_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._sample_queue.put_nowait(sample)
            except asyncio.QueueFull:
                return

    def _signal_movement(self) -> None:
        """Forward a Raw Input event from the Windows hook thread."""
        if not self._stream_active:
            return
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._enqueue_movement, True)
        except RuntimeError:
            return

    def _enqueue_movement(self, token: bool | None) -> None:
        movement_queue = self._movement_queue
        if movement_queue is None:
            return
        try:
            movement_queue.put_nowait(token)
        except asyncio.QueueFull:
            try:
                _ = movement_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                movement_queue.put_nowait(token)
            except asyncio.QueueFull:
                return

    async def _process_movement(self) -> None:
        """Inject one pen report for each ordered physical mouse event."""
        movement_queue = self._movement_queue
        if movement_queue is None:
            return
        while True:
            token = await movement_queue.get()
            if token is None:
                break
            latest = self._latest_emission_sample
            if latest is None:
                continue
            ts, left_raw, right_raw = latest
            self._emit_sample(
                ts=ts,
                left_raw=left_raw,
                right_raw=right_raw,
                publish_telemetry=False,
            )

    async def _process_samples(self) -> None:
        sample_queue = self._sample_queue
        if sample_queue is None:
            return

        period = 1.0 / max(1.0, float(self.launch_config.hz))
        next_emit = time.perf_counter()
        latest: _RawSample | None = None
        pressure_changed = False
        telemetry_only = self._emitter is None
        while True:
            # Pressure arrives at about 60 Hz. Once the first report arrives,
            # independently tick the synthetic pen at the configured injection
            # rate so fast cursor motion is represented by more spatial points.
            if latest is None or telemetry_only:
                item = await sample_queue.get()
            else:
                timeout_s = max(0.0, next_emit - time.perf_counter())
                try:
                    item = await asyncio.wait_for(sample_queue.get(), timeout=timeout_s)
                except TimeoutError:
                    item = ...

            if item is None:
                break
            if item is not ...:
                latest = item
                pressure_changed = True

            # Coalesce pressure updates, but never coalesce injection ticks.
            stop_after_emit = False
            while True:
                try:
                    newer = sample_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if newer is None:
                    stop_after_emit = True
                    break
                latest = newer
                pressure_changed = True

            if latest is None:
                if stop_after_emit:
                    break
                continue

            self._latest_emission_sample = latest

            wait_s = next_emit - time.perf_counter()
            if wait_s > 0:
                if stop_after_emit:
                    break
                continue

            ts, left_raw, right_raw = latest
            self._emit_sample(
                ts=ts,
                left_raw=left_raw,
                right_raw=right_raw,
                publish_telemetry=pressure_changed,
            )
            startup_ready = self._telemetry_ready_event
            if startup_ready is not None and not startup_ready.is_set():
                startup_ready.set()
            pressure_changed = False
            if telemetry_only:
                latest = None
            if stop_after_emit:
                break
            next_emit = time.perf_counter() + period

    async def _monitor_stream_health(self) -> None:
        while self._stream_active:
            await asyncio.sleep(0.25)
            last_sample = self._last_sample_monotonic
            if last_sample is None:
                continue
            silence_s = time.perf_counter() - last_sample
            if silence_s <= self._stream_stall_timeout_s:
                continue

            emitter = self._emitter
            if emitter is None:
                message = (
                    f"Pressure stream stalled for {silence_s:.1f}s; telemetry "
                    "stopped and native clicks were unchanged."
                )
            else:
                message = (
                    f"Pressure stream stalled for {silence_s:.1f}s; "
                    "native clicks were restored and the driver was stopped."
                )
            if emitter is not None:
                emitter.fail_open(message)
            self.log_bus.error(message)
            callback = self._failure_callback
            if callback is not None:
                callback(message)
            await self.stop_stream()
            return

    def _emit_sample(
        self,
        *,
        ts: float,
        left_raw: int,
        right_raw: int,
        publish_telemetry: bool = True,
    ) -> None:
        with self._state_lock:
            left_cfg = self._left_curve_config
            right_cfg = self._right_curve_config
            emitter = self._emitter

        left_norm = normalize_raw_pressure(left_raw, left_cfg.raw_min, left_cfg.raw_max)
        right_norm = normalize_raw_pressure(
            right_raw, right_cfg.raw_min, right_cfg.raw_max
        )
        left_mapped = map_normalized_pressure(left_norm, left_cfg)
        right_mapped = map_normalized_pressure(right_norm, right_cfg)
        if not self._config.left_enabled:
            left_mapped = 0
        if not self._config.right_enabled:
            right_mapped = 0

        if emitter is not None:
            try:
                emitter.update(
                    left_mapped=left_mapped,
                    right_mapped=right_mapped,
                    pressure_fresh=publish_telemetry,
                    left_raw=left_raw,
                    right_raw=right_raw,
                )
            except Exception as exc:
                self.log_bus.error(f"Synthetic pen update failed: {exc}")

        if emitter is not None:
            injected_at = time.perf_counter()
            if (
                self._last_inject_monotonic is not None
                and injected_at > self._last_inject_monotonic
            ):
                instant_inject_hz = 1.0 / (injected_at - self._last_inject_monotonic)
                if self._inject_hz <= 0.0:
                    self._inject_hz = instant_inject_hz
                else:
                    self._inject_hz = (self._inject_hz * 0.9) + (
                        instant_inject_hz * 0.1
                    )
            self._last_inject_monotonic = injected_at

        # Companion apps need a heartbeat even while pressure is held steady.
        # Publish on every injection/resample tick; the UI telemetry below
        # remains limited to fresh hardware reports for meaningful Hz values.
        self._sandbox_telemetry.publish(
            left_raw=left_raw,
            right_raw=right_raw,
            left_mapped=left_mapped,
            right_mapped=right_mapped,
            active=self._stream_active,
            device_found=self._device_found,
        )

        # Cursor/pen injection can be faster than pressure telemetry. Publish
        # telemetry only for a fresh pressure report so its displayed rate and
        # calibration sample stream remain meaningful.
        if not publish_telemetry:
            return

        hz = 0.0
        if self._last_sample_t is not None and ts > self._last_sample_t:
            hz = 1.0 / (ts - self._last_sample_t)
        self._last_sample_t = ts

        payload = {
            "left_raw": int(left_raw),
            "right_raw": int(right_raw),
            "left_norm": float(left_norm),
            "right_norm": float(right_norm),
            "left_mapped": int(left_mapped),
            "right_mapped": int(right_mapped),
            "hz": float(hz),
            "inject_hz": float(self._inject_hz),
        }
        if self._raw_sample_queue is not None:
            try:
                self._raw_sample_queue.put_nowait((left_raw, right_raw))
            except asyncio.QueueFull:
                try:
                    _ = self._raw_sample_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    self._raw_sample_queue.put_nowait((left_raw, right_raw))
                except asyncio.QueueFull:
                    pass

        callback = self._telemetry_callback
        if callback is not None:
            callback(payload)

    def _log(self, line: str) -> None:
        self.log_bus.info(line)
