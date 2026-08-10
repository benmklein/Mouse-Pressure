"""Low-latency preview and release-time refinement for raster ink strokes.

The live filter is a two-dimensional One Euro filter. Its cutoff rises with
pointer speed, so slow hand jitter is softened while fast gestures stay close
to the cursor. The final pass is symmetric and corner-aware; it can revise the
temporary preview after release without rounding deliberate sharp turns.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, replace
from typing import Iterable


@dataclass(frozen=True, slots=True)
class InkPoint:
    x: float
    y: float
    pressure: float = 1.0
    time_ms: float = 0.0


def _alpha(cutoff_hz: float, dt_s: float) -> float:
    cutoff = max(0.001, float(cutoff_hz))
    dt = max(0.000_001, float(dt_s))
    tau = 1.0 / (2.0 * math.pi * cutoff)
    return 1.0 / (1.0 + tau / dt)


def _lerp(a: float, b: float, amount: float) -> float:
    return a + (b - a) * amount


class LowLatencyInkFilter:
    """Velocity-adaptive causal position filter for the live preview."""

    def __init__(
        self,
        *,
        min_cutoff_hz: float = 18.0,
        speed_coefficient: float = 0.08,
        derivative_cutoff_hz: float = 1.0,
        default_hz: float = 240.0,
    ) -> None:
        self.min_cutoff_hz = max(0.001, float(min_cutoff_hz))
        self.speed_coefficient = max(0.0, float(speed_coefficient))
        self.derivative_cutoff_hz = max(0.001, float(derivative_cutoff_hz))
        self.default_dt_s = 1.0 / max(1.0, float(default_hz))
        self.reset()

    def reset(self) -> None:
        self._last_raw: InkPoint | None = None
        self._last_filtered: InkPoint | None = None
        self._filtered_dx = 0.0
        self._filtered_dy = 0.0

    def update(self, point: InkPoint) -> InkPoint:
        previous_raw = self._last_raw
        previous_filtered = self._last_filtered
        if previous_raw is None or previous_filtered is None:
            self._last_raw = point
            self._last_filtered = point
            return point

        dt_s = (float(point.time_ms) - float(previous_raw.time_ms)) / 1000.0
        if dt_s <= 0.0 or dt_s > 0.1:
            dt_s = self.default_dt_s

        raw_dx = (point.x - previous_raw.x) / dt_s
        raw_dy = (point.y - previous_raw.y) / dt_s
        derivative_alpha = _alpha(self.derivative_cutoff_hz, dt_s)
        self._filtered_dx = _lerp(self._filtered_dx, raw_dx, derivative_alpha)
        self._filtered_dy = _lerp(self._filtered_dy, raw_dy, derivative_alpha)
        speed = math.hypot(self._filtered_dx, self._filtered_dy)

        cutoff = self.min_cutoff_hz + self.speed_coefficient * speed
        position_alpha = _alpha(cutoff, dt_s)
        filtered = replace(
            point,
            x=_lerp(previous_filtered.x, point.x, position_alpha),
            y=_lerp(previous_filtered.y, point.y, position_alpha),
        )
        self._last_raw = point
        self._last_filtered = filtered
        return filtered

    def process(self, points: Iterable[InkPoint]) -> list[InkPoint]:
        self.reset()
        return [self.update(point) for point in points]


class StartupPressurePreviewFilter:
    """Ease only the live startup pressure while leaving position untouched."""

    def __init__(
        self,
        *,
        duration_ms: float = 65.0,
        cutoff_hz: float = 30.0,
        default_hz: float = 240.0,
    ) -> None:
        self.duration_ms = max(0.0, float(duration_ms))
        self.cutoff_hz = max(0.001, float(cutoff_hz))
        self.default_dt_s = 1.0 / max(1.0, float(default_hz))
        self.reset()

    def reset(self) -> None:
        self._started_at_ms: float | None = None
        self._last_time_ms: float | None = None
        self._pressure = 0.0

    def update(self, point: InkPoint) -> InkPoint:
        if self._started_at_ms is None or self._last_time_ms is None:
            self._started_at_ms = float(point.time_ms)
            self._last_time_ms = float(point.time_ms)
            self._pressure = float(point.pressure)
            return point

        elapsed_ms = float(point.time_ms) - self._started_at_ms
        dt_s = (float(point.time_ms) - self._last_time_ms) / 1000.0
        if dt_s <= 0.0 or dt_s > 0.1:
            dt_s = self.default_dt_s
        target = float(point.pressure)
        if 0.0 <= elapsed_ms <= self.duration_ms:
            if target > self._pressure:
                self._pressure = _lerp(
                    self._pressure,
                    target,
                    _alpha(self.cutoff_hz, dt_s),
                )
        else:
            self._pressure = target
        self._last_time_ms = float(point.time_ms)
        return replace(point, pressure=self._pressure)


def _cumulative_lengths(points: list[InkPoint]) -> list[float]:
    lengths = [0.0]
    for left, right in zip(points, points[1:]):
        lengths.append(
            lengths[-1] + math.hypot(right.x - left.x, right.y - left.y)
        )
    return lengths


def _sample_xy(
    points: list[InkPoint], cumulative: list[float], distance: float
) -> tuple[float, float]:
    if distance <= 0.0:
        return points[0].x, points[0].y
    if distance >= cumulative[-1]:
        return points[-1].x, points[-1].y
    right_index = bisect_right(cumulative, distance)
    left_index = max(0, right_index - 1)
    right_index = min(len(points) - 1, right_index)
    span = cumulative[right_index] - cumulative[left_index]
    if span <= 1e-9:
        return points[right_index].x, points[right_index].y
    amount = (distance - cumulative[left_index]) / span
    return (
        _lerp(points[left_index].x, points[right_index].x, amount),
        _lerp(points[left_index].y, points[right_index].y, amount),
    )


def _sample_point(
    points: list[InkPoint], cumulative: list[float], distance: float
) -> InkPoint:
    if distance <= 0.0:
        return points[0]
    if distance >= cumulative[-1]:
        return points[-1]
    right_index = bisect_right(cumulative, distance)
    left_index = max(0, right_index - 1)
    right_index = min(len(points) - 1, right_index)
    span = cumulative[right_index] - cumulative[left_index]
    if span <= 1e-9:
        return points[right_index]
    amount = (distance - cumulative[left_index]) / span
    left = points[left_index]
    right = points[right_index]
    return InkPoint(
        x=_lerp(left.x, right.x, amount),
        y=_lerp(left.y, right.y, amount),
        pressure=_lerp(left.pressure, right.pressure, amount),
        time_ms=_lerp(left.time_ms, right.time_ms, amount),
    )


def _sample_uniform_xy(
    points: list[tuple[float, float]],
    distances: list[float],
    distance: float,
) -> tuple[float, float]:
    if distance <= 0.0:
        return points[0]
    if distance >= distances[-1]:
        return points[-1]
    right_index = bisect_right(distances, distance)
    left_index = max(0, right_index - 1)
    right_index = min(len(points) - 1, right_index)
    span = distances[right_index] - distances[left_index]
    if span <= 1e-9:
        return points[right_index]
    amount = (distance - distances[left_index]) / span
    return (
        _lerp(points[left_index][0], points[right_index][0], amount),
        _lerp(points[left_index][1], points[right_index][1], amount),
    )


def _corner_protection(
    points: list[tuple[float, float]], index: int, radius: int
) -> float:
    span = max(3, radius)
    left = points[max(0, index - span)]
    current = points[index]
    right = points[min(len(points) - 1, index + span)]
    incoming_x = current[0] - left[0]
    incoming_y = current[1] - left[1]
    outgoing_x = right[0] - current[0]
    outgoing_y = right[1] - current[1]
    incoming_length = math.hypot(incoming_x, incoming_y)
    outgoing_length = math.hypot(outgoing_x, outgoing_y)
    if incoming_length < 2.0 or outgoing_length < 2.0:
        return 0.0
    cosine = (
        incoming_x * outgoing_x + incoming_y * outgoing_y
    ) / (incoming_length * outgoing_length)
    # Measure the turn over several display pixels. That prevents one-sample
    # sensor wobble from masquerading as an intentional corner. Turns of 90
    # degrees or sharper are protected completely; gentler turns blend into the
    # smoothed curve.
    return max(0.0, min(1.0, (0.45 - cosine) / 0.45))


def _mirrored_point(
    points: list[tuple[float, float]], index: int
) -> tuple[float, float]:
    last = len(points) - 1
    if index < 0:
        reflected = min(last, -index)
        return (
            2.0 * points[0][0] - points[reflected][0],
            2.0 * points[0][1] - points[reflected][1],
        )
    if index > last:
        reflected = max(0, 2 * last - index)
        return (
            2.0 * points[last][0] - points[reflected][0],
            2.0 * points[last][1] - points[reflected][1],
        )
    return points[index]


def _smooth_uniform(
    points: list[tuple[float, float]], *, amount: float, radius: int
) -> list[tuple[float, float]]:
    if len(points) < 3 or amount <= 0.0:
        return points
    safe_radius = min(max(2, radius), len(points) - 1)
    output: list[tuple[float, float]] = []
    for index, current in enumerate(points):
        weighted_x = 0.0
        weighted_y = 0.0
        total_weight = 0.0
        for offset in range(-safe_radius, safe_radius + 1):
            weight = float(safe_radius + 1 - abs(offset))
            sample_x, sample_y = _mirrored_point(points, index + offset)
            weighted_x += sample_x * weight
            weighted_y += sample_y * weight
            total_weight += weight
        target_x = weighted_x / total_weight
        target_y = weighted_y / total_weight
        blend = amount * (1.0 - _corner_protection(points, index, safe_radius))
        output.append(
            (
                _lerp(current[0], target_x, blend),
                _lerp(current[1], target_y, blend),
            )
        )
    output[0] = points[0]
    output[-1] = points[-1]
    return output


def refine_stroke(
    points: Iterable[InkPoint],
    *,
    amount: float = 0.42,
    passes: int = 2,
) -> list[InkPoint]:
    """Return a release-time refined centerline with endpoints preserved."""
    refined = list(points)
    if len(refined) < 3:
        return refined
    blend_amount = max(0.0, min(1.0, float(amount)))
    if blend_amount <= 0.0 or passes <= 0:
        return refined

    cumulative = _cumulative_lengths(refined)
    total_length = cumulative[-1]
    if total_length < 2.0:
        return refined

    # Work in approximately one-pixel arc-length steps. Uneven input timing no
    # longer changes the strength of the final cleanup, and wide local windows
    # remove visible hand/sensor wobble without buffering the live stroke.
    uniform_distances = [float(index) for index in range(int(total_length) + 1)]
    if not math.isclose(uniform_distances[-1], total_length):
        uniform_distances.append(total_length)
    uniform_points = [
        _sample_xy(refined, cumulative, distance)
        for distance in uniform_distances
    ]
    radius = max(2, int(round(2.0 + 8.0 * blend_amount)))
    for _ in range(max(0, int(passes))):
        uniform_points = _smooth_uniform(
            uniform_points, amount=blend_amount, radius=radius
        )

    output = []
    for point, distance in zip(refined, cumulative):
        x, y = _sample_uniform_xy(uniform_points, uniform_distances, distance)
        output.append(replace(point, x=x, y=y))
    output[0] = refined[0]
    output[-1] = refined[-1]
    return output


def prepare_replay_stroke(
    points: Iterable[InkPoint],
    *,
    spacing_px: float = 1.0,
    max_tail_px: float = 72.0,
    adaptive_tails: bool = True,
    startup_correction_max_ms: float = 65.0,
    minimum_start_pressure_ratio: float = 0.45,
) -> list[InkPoint]:
    """Densify final replay and infer visible tails from pressure change points.

    The pressure sensor ramps in time. On a quick short gesture that ramp can
    occupy a large fraction of the visible line, even though it is well behaved
    on a longer stroke. Repeated pressure events at the same coordinate are
    collapsed into one distance-domain sample. Endpoint tails are detected from
    local pressure rather than total stroke length. Startup correction is also
    time-gated so a deliberate slow light-to-heavy ramp is left unchanged.
    """
    source = list(points)
    if len(source) < 2:
        return source
    cumulative = _cumulative_lengths(source)
    total_length = cumulative[-1]
    if total_length <= 1e-9:
        return source

    spacing = max(0.5, float(spacing_px))
    distances = [
        min(total_length, index * spacing)
        for index in range(int(math.ceil(total_length / spacing)) + 1)
    ]
    if not math.isclose(distances[-1], total_length):
        distances.append(total_length)
    else:
        distances[-1] = total_length
    dense = [_sample_point(source, cumulative, distance) for distance in distances]

    if not adaptive_tails or total_length < 4.0:
        return dense

    def detect_tail(*, from_start: bool) -> tuple[float, float] | None:
        limit = min(max(8.0, max_tail_px), total_length * 0.45)
        indexed = list(enumerate(dense))
        if not from_start:
            indexed.reverse()
        local: list[tuple[float, int, float]] = []
        for index, point in indexed:
            edge_distance = (
                distances[index]
                if from_start
                else total_length - distances[index]
            )
            if edge_distance > limit:
                break
            local.append((edge_distance, index, point.pressure))
        if len(local) < 4:
            return None

        ordered = sorted(pressure for _, _, pressure in local)
        body = ordered[int(round((len(ordered) - 1) * 0.8))]
        if body <= 1e-9 or local[0][2] >= body * 0.65:
            return None

        smoothed: list[float] = []
        for position in range(len(local)):
            left = max(0, position - 2)
            right = min(len(local), position + 3)
            smoothed.append(
                sum(item[2] for item in local[left:right]) / (right - left)
            )
        for position, (edge_distance, _, pressure) in enumerate(local):
            if edge_distance < 3.0 or smoothed[position] < body * 0.8:
                continue
            sustain_end = min(len(local), position + 7)
            if min(smoothed[position:sustain_end]) < body * 0.6:
                continue
            if from_start:
                elapsed_ms = dense[local[position][1]].time_ms - dense[0].time_ms
                if elapsed_ms < 0.0 or elapsed_ms > startup_correction_max_ms:
                    return None
            return edge_distance, body
        return None

    start_tail = detect_tail(from_start=True)
    end_tail = detect_tail(from_start=False)
    if start_tail is None and end_tail is None:
        return dense

    corrected: list[InkPoint] = []
    for point, distance in zip(dense, distances):
        pressure = point.pressure
        if start_tail is not None and distance < start_tail[0]:
            progress = max(0.0, min(1.0, distance / start_tail[0]))
            start_ratio = max(0.0, min(1.0, minimum_start_pressure_ratio))
            corrected_pressure = start_tail[1] * (
                start_ratio + (1.0 - start_ratio) * progress**0.55
            )
            pressure = max(pressure, corrected_pressure)
        remaining = total_length - distance
        if end_tail is not None and remaining < end_tail[0]:
            progress = max(0.0, min(1.0, remaining / end_tail[0]))
            pressure = end_tail[1] * (0.06 + 0.94 * progress**0.55)
        corrected.append(replace(point, pressure=min(1.0, pressure)))
    return corrected
