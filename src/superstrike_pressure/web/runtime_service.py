"""Runtime coordinator for streaming, mapping, and live config updates."""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from dataclasses import replace
from typing import Callable, TypeAlias

from superstrike_pressure.bridge.config import CONTACT_PRESETS, ChannelConfig, LaunchConfig, RuntimeConfig
from superstrike_pressure.bridge.curves import PressureConfig, map_normalized_pressure, normalize_curve_name
from superstrike_pressure.bridge.synthetic_pen import SyntheticPenConfig, SyntheticPenEmitter
from superstrike_pressure.sniff.hidpp_pressure import (
    PressureHidppSession,
    extract_mode3_lr_pressure_raw,
    normalize_raw_pressure,
    parse_feature_0c_frame,
)
from superstrike_pressure.web.config_store import ConfigStore, runtime_config_from_dict, runtime_config_to_dict
from superstrike_pressure.web.log_bus import GLOBAL_LOG_BUS, LogBus
from superstrike_pressure.web.models import (
    StreamAlreadyActiveError,
    StreamNotActiveError,
    ValidationError,
    deadzone_pct_to_float,
    validate_process_name,
)

_RawSample: TypeAlias = tuple[float, int, int]
_DeviceCommand: TypeAlias = tuple[
    dict[str, int],
    asyncio.AbstractEventLoop,
    asyncio.Future[dict[str, int]],
]


class RuntimeService:
    def __init__(
        self,
        launch_config: LaunchConfig,
        config_store: ConfigStore,
        *,
        log_bus: LogBus | None = None,
        session_factory: Callable[[Callable[[str], None]], PressureHidppSession] = PressureHidppSession,
        emitter_factory: Callable[[SyntheticPenConfig, Callable[[str], None]], SyntheticPenEmitter] = SyntheticPenEmitter,
        stream_stall_timeout_s: float = 4.0,
        stream_recovery_after_s: float = 0.5,
        stream_keepalive_interval_s: float = 2.0,
        max_stream_recovery_attempts: int = 3,
    ) -> None:
        self.launch_config = launch_config
        self.config_store = config_store
        self.log_bus = log_bus or GLOBAL_LOG_BUS
        self._session_factory = session_factory
        self._emitter_factory = emitter_factory
        self._stream_stall_timeout_s = max(0.25, float(stream_stall_timeout_s))
        self._stream_recovery_after_s = max(0.1, float(stream_recovery_after_s))
        self._stream_keepalive_interval_s = max(1.0, float(stream_keepalive_interval_s))
        self._max_stream_recovery_attempts = max(1, int(max_stream_recovery_attempts))

        self._config = self.config_store.load()
        self._left_curve_config = self._curve_config_for(self._config.left)
        self._right_curve_config = self._curve_config_for(self._config.right)

        self._telemetry_callback: Callable[[dict], None] | None = None

        self._stream_active = False
        self._device_found = False

        self._session: PressureHidppSession | None = None
        self._emitter: SyntheticPenEmitter | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader_thread: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._sample_queue: asyncio.Queue[_RawSample | None] | None = None
        self._raw_sample_queue: asyncio.Queue[tuple[int, int]] | None = None
        self._movement_queue: asyncio.Queue[bool | None] | None = None
        self._processor_task: asyncio.Task[None] | None = None
        self._movement_task: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._latest_emission_sample: _RawSample | None = None
        self._last_sample_t: float | None = None
        self._last_inject_monotonic: float | None = None
        self._inject_hz = 0.0
        self._last_sample_monotonic: float | None = None
        self._failure_callback: Callable[[str], None] | None = None
        self._state_lock = threading.Lock()
        self._device_commands: queue.Queue[_DeviceCommand] = queue.Queue()

    async def start_stream(self) -> None:
        if self._stream_active:
            raise StreamAlreadyActiveError("Stream is already active")

        self._loop = asyncio.get_running_loop()
        self._reader_stop.clear()
        # Pressure HID reports can arrive much faster than the synthetic pointer
        # rate. Keep only the newest report so old pen frames can never build up
        # behind the cursor and replay after the physical button is released.
        self._sample_queue = asyncio.Queue(maxsize=1)
        self._raw_sample_queue = asyncio.Queue(maxsize=256)
        self._movement_queue = asyncio.Queue(maxsize=512)
        self._latest_emission_sample = None
        self._last_sample_t = None
        self._last_inject_monotonic = None
        self._inject_hz = 0.0
        self._last_sample_monotonic = time.perf_counter()

        session = self._session_factory(self._log)
        try:
            session.open()
            self._device_found = True
            session.enable_pressure_stream(mode=self.launch_config.mode, mode_arg=self.launch_config.mode_arg)

            emitter = self._emitter_factory(self._emitter_config_from_runtime(), self._log)
            movement_callback = getattr(emitter, "set_movement_callback", None)
            if callable(movement_callback):
                movement_callback(self._signal_movement)
            emitter.open()
        except Exception:
            self._device_found = False
            try:
                session.close()
            except Exception:
                pass
            raise

        self._session = session
        self._emitter = emitter
        self._stream_active = True
        self._reader_thread = threading.Thread(target=self._reader_loop, name="superstrike-reader", daemon=True)
        self._reader_thread.start()
        self._processor_task = asyncio.create_task(self._process_samples())
        self._movement_task = asyncio.create_task(self._process_movement())
        self._health_task = asyncio.create_task(self._monitor_stream_health())
        self.log_bus.info(
            f"Stream started (pressure ~60 Hz, raw mouse event-driven, "
            f"fallback pen tick {self.launch_config.hz:.0f} Hz)"
        )

    async def stop_stream(self) -> None:
        if not self._stream_active:
            raise StreamNotActiveError("Stream is not active")

        self._stream_active = False
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
            try:
                movement_callback = getattr(self._emitter, "set_movement_callback", None)
                if callable(movement_callback):
                    movement_callback(None)
                self._emitter.release()
            finally:
                self._emitter.close()
            self._emitter = None

        if self._session is not None:
            self._session.close()
            self._session = None

        self._sample_queue = None
        self._raw_sample_queue = None
        self._movement_queue = None
        self._latest_emission_sample = None
        self.log_bus.info("Stream stopped")

    def apply_config(self, patch: dict) -> RuntimeConfig:
        if not isinstance(patch, dict):
            raise ValidationError("config patch must be an object")

        merged = runtime_config_to_dict(self._config)
        self._merge_patch_dict(merged, patch)
        validated = runtime_config_from_dict(merged)

        with self._state_lock:
            self._config = validated
            self._left_curve_config = self._curve_config_for(validated.left)
            self._right_curve_config = self._curve_config_for(validated.right)
            if self._emitter is not None:
                left_presets = CONTACT_PRESETS[validated.left.contact_preset]
                right_presets = CONTACT_PRESETS[validated.right.contact_preset]
                self._emitter.config = replace(
                    self._emitter.config,
                    contact_threshold=left_presets["contact_threshold"],
                    release_threshold=left_presets["release_threshold"],
                    min_contact_pressure=round(
                        validated.left.pressure_floor * 1024 / 100
                    ),
                    path_stabilization=validated.left.path_stabilization,
                    pressure_influence=validated.left.pressure_influence,
                    onset_buffer=validated.left.onset_buffer,
                    right_contact_threshold=right_presets["contact_threshold"],
                    right_release_threshold=right_presets["release_threshold"],
                    right_min_contact_pressure=round(
                        validated.right.pressure_floor * 1024 / 100
                    ),
                    right_path_stabilization=validated.right.path_stabilization,
                    right_pressure_influence=validated.right.pressure_influence,
                    right_onset_buffer=validated.right.onset_buffer,
                    release_teardown=validated.release_teardown,
                    trace_raw_min=validated.left.raw_min,
                    trace_raw_max=validated.left.raw_max,
                    trace_curve=normalize_curve_name(validated.left.curve),
                    trace_curve_strength=validated.left.curve_strength,
                    right_trace_raw_min=validated.right.raw_min,
                    right_trace_raw_max=validated.right.raw_max,
                    right_trace_curve=normalize_curve_name(validated.right.curve),
                    right_trace_curve_strength=validated.right.curve_strength,
                )

        self.config_store.save(validated)
        return validated

    def get_config(self) -> RuntimeConfig:
        return self._config

    def set_telemetry_callback(self, cb: Callable[[dict], None] | None) -> None:
        self._telemetry_callback = cb

    def set_failure_callback(self, cb: Callable[[str], None] | None) -> None:
        self._failure_callback = cb

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
    ) -> dict[str, int]:
        """Apply mouse hardware settings on the HID reader thread."""
        if not self._stream_active or self._session is None:
            raise StreamNotActiveError("Start the bridge before applying device settings")
        if not 100 <= dpi <= 32000 or dpi % 50 != 0:
            raise ValidationError("DPI must be 100..32000 in 50-DPI increments")
        if not 0 <= haptic_left <= 5 or not 0 <= haptic_right <= 5:
            raise ValidationError("Haptic levels must be in 0..5")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, int]] = loop.create_future()
        self._device_commands.put(
            (
                {
                    "dpi": int(dpi),
                    "haptic_left": int(haptic_left),
                    "haptic_right": int(haptic_right),
                },
                loop,
                future,
            )
        )
        return await asyncio.wait_for(future, timeout=3.0)

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

        for boolean_name in ("suppress_lmb", "suppress_rmb", "release_teardown"):
            if boolean_name in patch:
                value = patch[boolean_name]
                if not isinstance(value, bool):
                    raise ValidationError(f"{boolean_name} must be a boolean")
                merged[boolean_name] = value

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

        if "app_profiles" in patch:
            profile_patch = patch["app_profiles"]
            if not isinstance(profile_patch, dict):
                raise ValidationError("app_profiles patch must be an object")
            merged_profiles = dict(merged.get("app_profiles") or {})
            for proc_name, profile_name in profile_patch.items():
                if not isinstance(proc_name, str):
                    raise ValidationError("app_profiles keys must be strings")
                proc_errors = validate_process_name(proc_name)
                if proc_errors:
                    raise ValidationError(proc_errors[0])
                if not isinstance(profile_name, str):
                    raise ValidationError("app_profiles values must be strings")
                merged_profiles[proc_name] = profile_name
            merged["app_profiles"] = merged_profiles

        if merged.get("linked", False):
            merged["right"] = dict(merged["left"])

    def _curve_config_for(self, channel: ChannelConfig) -> PressureConfig:
        return PressureConfig(
            raw_min=channel.raw_min,
            raw_max=channel.raw_max,
            out_min=0,
            out_max=1023,
            deadzone_low=deadzone_pct_to_float(channel.deadzone_low),
            deadzone_high=1.0 - deadzone_pct_to_float(channel.deadzone_high),
            curve=normalize_curve_name(channel.curve),
            curve_strength=channel.curve_strength,
        )

    def _emitter_config_from_runtime(self) -> SyntheticPenConfig:
        left_thresholds = CONTACT_PRESETS[self._config.left.contact_preset]
        right_thresholds = CONTACT_PRESETS[self._config.right.contact_preset]
        return SyntheticPenConfig(
            contact_threshold=left_thresholds["contact_threshold"],
            release_threshold=left_thresholds["release_threshold"],
            min_contact_pressure=round(
                self._config.left.pressure_floor * 1024 / 100
            ),
            path_stabilization=self._config.left.path_stabilization,
            pressure_influence=self._config.left.pressure_influence,
            onset_buffer=self._config.left.onset_buffer,
            right_contact_threshold=right_thresholds["contact_threshold"],
            right_release_threshold=right_thresholds["release_threshold"],
            right_min_contact_pressure=round(
                self._config.right.pressure_floor * 1024 / 100
            ),
            right_path_stabilization=self._config.right.path_stabilization,
            right_pressure_influence=self._config.right.pressure_influence,
            right_onset_buffer=self._config.right.onset_buffer,
            pressure_interp_steps=max(1, int(round(self.launch_config.hz / 60.0))),
            suppress_lmb=self._config.suppress_lmb,
            suppress_rmb=self._config.suppress_rmb,
            release_teardown=self._config.release_teardown,
            trace_dir=self.launch_config.trace_dir,
            trace_raw_min=self._config.left.raw_min,
            trace_raw_max=self._config.left.raw_max,
            trace_curve=normalize_curve_name(self._config.left.curve),
            trace_curve_strength=self._config.left.curve_strength,
            right_trace_raw_min=self._config.right.raw_min,
            right_trace_raw_max=self._config.right.raw_max,
            right_trace_curve=normalize_curve_name(self._config.right.curve),
            right_trace_curve_strength=self._config.right.curve_strength,
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
                        loop.call_soon_threadsafe(self._enqueue_sample, (ts, left_raw, right_raw))

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
                next_keepalive_at = time.perf_counter() + self._stream_keepalive_interval_s

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
                next_keepalive_at = time.perf_counter() + self._stream_keepalive_interval_s

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
            try:
                dpi = session.set_dpi(settings["dpi"])
                left, right = session.set_haptic_levels(
                    left=settings["haptic_left"],
                    right=settings["haptic_right"],
                )
                result = {
                    "dpi": dpi,
                    "haptic_left": left,
                    "haptic_right": right,
                }
            except Exception as exc:
                loop.call_soon_threadsafe(self._finish_device_command, future, None, exc)
            else:
                self.log_bus.info(
                    f"Device settings applied: DPI {dpi}, haptics L{left}/R{right}"
                )
                loop.call_soon_threadsafe(self._finish_device_command, future, result, None)
            finally:
                self._last_sample_monotonic = time.perf_counter()

    @staticmethod
    def _finish_device_command(
        future: asyncio.Future[dict[str, int]],
        result: dict[str, int] | None,
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
        while True:
            # Pressure arrives at about 60 Hz. Once the first report arrives,
            # independently tick the synthetic pen at the configured injection
            # rate so fast cursor motion is represented by more spatial points.
            if latest is None:
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
            pressure_changed = False
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

            message = (
                f"Pressure stream stalled for {silence_s:.1f}s; "
                "native clicks were restored and the bridge was stopped."
            )
            emitter = self._emitter
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
        right_norm = normalize_raw_pressure(right_raw, right_cfg.raw_min, right_cfg.raw_max)
        left_mapped = map_normalized_pressure(left_norm, left_cfg)
        right_mapped = map_normalized_pressure(right_norm, right_cfg)

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

        injected_at = time.perf_counter()
        if self._last_inject_monotonic is not None and injected_at > self._last_inject_monotonic:
            instant_inject_hz = 1.0 / (injected_at - self._last_inject_monotonic)
            if self._inject_hz <= 0.0:
                self._inject_hz = instant_inject_hz
            else:
                self._inject_hz = (self._inject_hz * 0.9) + (instant_inject_hz * 0.1)
        self._last_inject_monotonic = injected_at

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
