"""Guided channel calibration workflow."""

from __future__ import annotations

import asyncio
import math
from typing import Callable

from mouse_pressure.web.config_store import ConfigStore
from mouse_pressure.web.models import ValidationError
from mouse_pressure.web.runtime_service import RuntimeService

PHASES: tuple[str, ...] = ("idle", "light", "heavy")
PHASE_DURATION_S = 2.0
PROGRESS_INTERVAL_S = 0.25
CALIBRATION_SETTLE_S = 0.0
PHASE_COUNTDOWN_S = 3
MIN_CALIBRATION_SPAN = 8
PHASE_INSTRUCTIONS = {
    "prepare": "Release the button and get ready.",
    "idle": "Keep the button fully released.",
    "light": "Press as lightly as you want pressure output to begin.",
    "heavy": "Press as firmly as you want to represent 100%.",
    "done": "Calibration saved.",
}


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

    progress_cb(
        {
            "event": "calibrate.progress",
            "channel": channel,
            "phase": phase,
            "value": last_value,
            "instruction": PHASE_INSTRUCTIONS[phase],
        }
    )

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
                    "instruction": PHASE_INSTRUCTIONS[phase],
                }
            )
            next_progress = now + PROGRESS_INTERVAL_S

    return values, last_value


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(int(value) for value in values)
    index = min(len(ordered) - 1, max(0, math.ceil((len(ordered) - 1) * fraction)))
    return ordered[index]


async def _countdown_before_phase(
    *,
    channel: str,
    phase: str,
    progress_cb: Callable[[dict], None],
) -> None:
    for remaining in range(max(0, int(PHASE_COUNTDOWN_S)), 0, -1):
        progress_cb(
            {
                "event": "calibrate.progress",
                "channel": channel,
                "phase": "countdown",
                "next_phase": phase,
                "countdown": remaining,
                "value": 0,
                "instruction": PHASE_INSTRUCTIONS[phase],
            }
        )
        await asyncio.sleep(1.0)


def _calibrated_range(
    phase_values: dict[str, list[int]],
    *,
    fallback_min: int,
    fallback_max: int,
) -> tuple[int, int]:
    """Use robust phase extrema, falling back for legacy/non-paced samplers."""
    idle = phase_values.get("idle", [])
    light = phase_values.get("light", [])
    heavy = phase_values.get("heavy", [])
    all_values = [value for values in phase_values.values() for value in values]
    if light and heavy:
        # Use the held light press as the user's desired activation point, but
        # never place it inside the measured released/noise range.
        raw_min = _percentile(light, 0.25)
        if idle:
            raw_min = max(raw_min, _percentile(idle, 0.95) + 1)
        raw_max = _percentile(heavy, 0.95)
    elif idle and heavy:
        raw_min = _percentile(idle, 0.95)
        raw_max = _percentile(heavy, 0.95)
    elif all_values:
        raw_min = min(all_values)
        raw_max = max(all_values)
    else:
        raw_min, raw_max = int(fallback_min), int(fallback_max)
    if raw_max - raw_min < MIN_CALIBRATION_SPAN:
        raise ValidationError(
            "Calibration range was too small. Release fully, then use a lighter press for activation and a firmer press for 100%."
        )
    return int(raw_min), int(raw_max)


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
            current = runtime_service.get_config()
            current_channel = current.left if ch_name == "left" else current.right
            progress_cb(
                {
                    "event": "calibrate.progress",
                    "channel": ch_name,
                    "phase": "prepare",
                    "value": 0,
                    "instruction": PHASE_INSTRUCTIONS["prepare"],
                }
            )
            if CALIBRATION_SETTLE_S > 0:
                await asyncio.sleep(CALIBRATION_SETTLE_S)
            values_by_phase: dict[str, list[int]] = {}
            last_value = 0
            for phase in PHASES:
                await _countdown_before_phase(
                    channel=ch_name,
                    phase=phase,
                    progress_cb=progress_cb,
                )
                phase_values, phase_last = await _collect_phase_values(
                    channel=ch_name,
                    phase=phase,
                    runtime_service=runtime_service,
                    progress_cb=progress_cb,
                )
                values_by_phase[phase] = phase_values
                last_value = phase_last

            raw_min, raw_max = _calibrated_range(
                values_by_phase,
                fallback_min=current_channel.raw_min,
                fallback_max=current_channel.raw_max,
            )

            result[ch_name] = {"raw_min": int(raw_min), "raw_max": int(raw_max)}
            progress_cb(
                {
                    "event": "calibrate.progress",
                    "channel": ch_name,
                    "phase": "done",
                    "value": int(last_value),
                    "instruction": PHASE_INSTRUCTIONS["done"],
                }
            )

        updated = runtime_service.apply_config(result)
        config_store.save(updated)
        return result
    finally:
        if not was_active and runtime_service.stream_active:
            await runtime_service.stop_stream()
