"""Guided channel calibration workflow."""

from __future__ import annotations

import asyncio
from typing import Callable

from superstrike_pressure.web.config_store import ConfigStore
from superstrike_pressure.web.models import ValidationError
from superstrike_pressure.web.runtime_service import RuntimeService

PHASES: tuple[str, ...] = ("idle", "light", "heavy")
PHASE_DURATION_S = 1.5
PROGRESS_INTERVAL_S = 0.25


def _channel_value(channel: str, left_raw: int, right_raw: int) -> int:
    return left_raw if channel == "left" else right_raw


async def _collect_phase_values(
    *,
    channel: str,
    phase: str,
    runtime_service: RuntimeService,
    progress_cb: Callable[[dict], None],
) -> tuple[list[int], int]:
    values: list[int] = []
    last_value = 0
    loop = asyncio.get_running_loop()
    start = loop.time()
    end = start + PHASE_DURATION_S
    next_progress = start

    progress_cb({"event": "calibrate.progress", "channel": channel, "phase": phase, "value": last_value})

    while True:
        now = loop.time()
        if now >= end:
            break

        timeout_s = min(PROGRESS_INTERVAL_S, end - now)
        try:
            left_raw, right_raw = await runtime_service.wait_for_raw_sample(timeout_s=timeout_s)
            value = _channel_value(channel, left_raw, right_raw)
            values.append(value)
            last_value = value
        except TimeoutError:
            pass

        now = loop.time()
        if now >= next_progress:
            progress_cb(
                {
                    "event": "calibrate.progress",
                    "channel": channel,
                    "phase": phase,
                    "value": last_value,
                }
            )
            next_progress = now + PROGRESS_INTERVAL_S

    return values, last_value


async def run_calibration(
    channel: str,
    runtime_service: RuntimeService,
    progress_cb: Callable[[dict], None],
    config_store: ConfigStore,
) -> dict:
    if channel not in {"left", "right", "both"}:
        raise ValidationError("channel must be one of: left, right, both")

    selected_channels = ("left", "right") if channel == "both" else (channel,)
    was_active = runtime_service.stream_active
    if not was_active:
        await runtime_service.start_stream()

    result: dict[str, dict[str, int]] = {}
    try:
        for ch_name in selected_channels:
            channel_values: list[int] = []
            last_value = 0
            for phase in PHASES:
                phase_values, phase_last = await _collect_phase_values(
                    channel=ch_name,
                    phase=phase,
                    runtime_service=runtime_service,
                    progress_cb=progress_cb,
                )
                channel_values.extend(phase_values)
                last_value = phase_last

            if channel_values:
                raw_min = min(channel_values)
                raw_max = max(channel_values)
            else:
                cfg = runtime_service.get_config().left if ch_name == "left" else runtime_service.get_config().right
                raw_min = cfg.raw_min
                raw_max = cfg.raw_max

            result[ch_name] = {"raw_min": int(raw_min), "raw_max": int(raw_max)}
            progress_cb(
                {
                    "event": "calibrate.progress",
                    "channel": ch_name,
                    "phase": "done",
                    "value": int(last_value),
                }
            )

        updated = runtime_service.apply_config(result)
        config_store.save(updated)
        return result
    finally:
        if not was_active and runtime_service.stream_active:
            await runtime_service.stop_stream()
