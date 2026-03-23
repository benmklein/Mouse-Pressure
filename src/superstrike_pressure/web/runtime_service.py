"""Runtime coordinator for streaming, mapping, and live config updates."""

from __future__ import annotations

import asyncio
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


class RuntimeService:
    def __init__(
        self,
        launch_config: LaunchConfig,
        config_store: ConfigStore,
        *,
        log_bus: LogBus | None = None,
        session_factory: Callable[[Callable[[str], None]], PressureHidppSession] = PressureHidppSession,
        emitter_factory: Callable[[SyntheticPenConfig, Callable[[str], None]], SyntheticPenEmitter] = SyntheticPenEmitter,
    ) -> None:
        self.launch_config = launch_config
        self.config_store = config_store
        self.log_bus = log_bus or GLOBAL_LOG_BUS
        self._session_factory = session_factory
        self._emitter_factory = emitter_factory

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
        self._processor_task: asyncio.Task[None] | None = None
        self._last_sample_t: float | None = None
        self._state_lock = threading.Lock()

    async def start_stream(self) -> None:
        if self._stream_active:
            raise StreamAlreadyActiveError("Stream is already active")

        self._loop = asyncio.get_running_loop()
        self._reader_stop.clear()
        self._sample_queue = asyncio.Queue(maxsize=1024)
        self._raw_sample_queue = asyncio.Queue(maxsize=256)
        self._last_sample_t = None

        session = self._session_factory(self._log)
        try:
            session.open()
            self._device_found = True
            session.enable_pressure_stream(mode=self.launch_config.mode, mode_arg=self.launch_config.mode_arg)

            emitter = self._emitter_factory(self._emitter_config_from_runtime(), self._log)
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
        self.log_bus.info("Stream started")

    async def stop_stream(self) -> None:
        if not self._stream_active:
            raise StreamNotActiveError("Stream is not active")

        self._stream_active = False
        self._reader_stop.set()

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

        if self._emitter is not None:
            try:
                self._emitter.release()
            finally:
                self._emitter.close()
            self._emitter = None

        if self._session is not None:
            self._session.close()
            self._session = None

        self._sample_queue = None
        self._raw_sample_queue = None
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
                presets = CONTACT_PRESETS[validated.left.contact_preset]
                self._emitter.config = replace(
                    self._emitter.config,
                    contact_threshold=presets["contact_threshold"],
                    release_threshold=presets["release_threshold"],
                )

        self.config_store.save(validated)
        return validated

    def get_config(self) -> RuntimeConfig:
        return self._config

    def set_telemetry_callback(self, cb: Callable[[dict], None] | None) -> None:
        self._telemetry_callback = cb

    async def wait_for_raw_sample(self, timeout_s: float = 1.0) -> tuple[int, int]:
        if not self._stream_active or self._raw_sample_queue is None:
            raise StreamNotActiveError("Stream is not active")
        return await asyncio.wait_for(self._raw_sample_queue.get(), timeout=timeout_s)

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
            deadzone_high=deadzone_pct_to_float(channel.deadzone_high),
            curve=normalize_curve_name(channel.curve),
            curve_strength=channel.curve_strength,
        )

    def _emitter_config_from_runtime(self) -> SyntheticPenConfig:
        thresholds = CONTACT_PRESETS[self._config.left.contact_preset]
        return SyntheticPenConfig(
            contact_threshold=thresholds["contact_threshold"],
            release_threshold=thresholds["release_threshold"],
        )

    def _reader_loop(self) -> None:
        session = self._session
        loop = self._loop
        if session is None or loop is None:
            return

        while not self._reader_stop.is_set():
            item = session.read_next(timeout_s=0.05)
            if item is None:
                continue

            ts, data = item
            frame = parse_feature_0c_frame(data, ts)
            if frame is None:
                continue

            left_raw, right_raw = extract_mode3_lr_pressure_raw(frame)
            if left_raw is None or right_raw is None:
                continue

            loop.call_soon_threadsafe(self._enqueue_sample, (ts, left_raw, right_raw))

        loop.call_soon_threadsafe(self._enqueue_sample, None)

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

    async def _process_samples(self) -> None:
        sample_queue = self._sample_queue
        if sample_queue is None:
            return

        while True:
            item = await sample_queue.get()
            if item is None:
                break
            ts, left_raw, right_raw = item
            self._emit_sample(ts=ts, left_raw=left_raw, right_raw=right_raw)

    def _emit_sample(self, *, ts: float, left_raw: int, right_raw: int) -> None:
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
                emitter.update(left_mapped=left_mapped, right_mapped=right_mapped)
            except Exception as exc:
                self.log_bus.error(f"Synthetic pen update failed: {exc}")

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
