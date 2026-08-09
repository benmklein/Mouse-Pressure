"""Real-time dual-channel pressure reader and calibration helpers."""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

from superstrike_pressure.sniff.hidpp_pressure import (
    Feature0CFrame,
    PressureHidppSession,
    extract_mode3_left_pressure_raw,
    extract_mode3_lr_pressure_raw,
    extract_mode3_right_pressure_raw,
    hex_bytes,
    normalize_raw_pressure,
    parse_feature_0c_frame,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Enable the pressure stream on MI_02 Col02, decode both 10-bit ADC "
            "channels, and report normalized travel in real time."
        )
    )
    p.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Optional capture duration in seconds (default: until Ctrl+C).",
    )
    p.add_argument(
        "--log-file",
        default="docs/pressure_realtime_log.txt",
        help="Path for log output (default: docs/pressure_realtime_log.txt).",
    )
    p.add_argument(
        "--raw-min",
        type=int,
        default=312,
        help="LEFT 10-bit ADC minimum for normalization (default: 312).",
    )
    p.add_argument(
        "--raw-max",
        type=int,
        default=616,
        help="LEFT 10-bit ADC maximum for normalization (default: 616).",
    )
    p.add_argument(
        "--right-raw-min",
        type=int,
        default=312,
        help="RIGHT 10-bit ADC minimum (default: 312).",
    )
    p.add_argument(
        "--right-raw-max",
        type=int,
        default=584,
        help="RIGHT 10-bit ADC maximum (default: 584).",
    )
    p.add_argument(
        "--mode",
        type=int,
        default=3,
        help="0x3C mode byte for enable command (default: 3).",
    )
    p.add_argument(
        "--mode-arg",
        type=int,
        default=0,
        help="0x3C third payload byte (default: 0).",
    )
    p.add_argument(
        "--show-all-0c",
        action="store_true",
        help="Print all feature-0x0C frames (not only addr 0x10).",
    )
    p.add_argument(
        "--calibrate",
        action="store_true",
        help=(
            "Interactive LEFT-only / RIGHT-only capture to identify bytes that move "
            "for each button."
        ),
    )
    p.add_argument(
        "--calibrate-right-range",
        action="store_true",
        help="Interactive calibration for RIGHT range on addr0x10.byte06.",
    )
    p.add_argument(
        "--phase-seconds",
        type=float,
        default=8.0,
        help="Capture duration per calibration phase (default: 8s).",
    )
    p.add_argument(
        "--baseline-seconds",
        type=float,
        default=5.0,
        help="Baseline duration for right-range calibration (default: 5s).",
    )
    return p.parse_args()


def _collect_phase_frames(
    session: PressureHidppSession,
    duration_s: float,
    *,
    log,
    label: str,
) -> list[Feature0CFrame]:
    out: list[Feature0CFrame] = []
    start = time.perf_counter()
    while True:
        now = time.perf_counter()
        if now - start >= duration_s:
            break
        item = session.read_next(timeout_s=0.05)
        if item is None:
            continue
        ts, data = item
        frame = parse_feature_0c_frame(
            data,
            ts,
            feature_index=session.pressure_feature_index,
            device_index=session.device_index,
        )
        if frame is None:
            continue
        out.append(frame)
    elapsed = max(1e-9, time.perf_counter() - start)
    log(f"{label}: frames={len(out)} rate={len(out)/elapsed:.2f}Hz")
    return out


def _phase_stats(frames: list[Feature0CFrame]) -> dict[tuple[int, int], dict[str, float]]:
    values: dict[tuple[int, int], list[int]] = defaultdict(list)
    for frame in frames:
        for i, v in enumerate(frame.raw):
            values[(frame.addr, i)].append(v)

    out: dict[tuple[int, int], dict[str, float]] = {}
    for key, vals in values.items():
        lo = min(vals)
        hi = max(vals)
        out[key] = {
            "min": float(lo),
            "max": float(hi),
            "range": float(hi - lo),
            "unique": float(len(set(vals))),
            "count": float(len(vals)),
        }
    return out


def _run_calibration(session: PressureHidppSession, *, log, phase_seconds: float) -> None:
    log("CALIBRATION begin")
    print("")
    input("Press Enter when ready for baseline (no button press)...")
    baseline = _collect_phase_frames(session, phase_seconds, log=log, label="baseline")

    print("")
    input("Press Enter then press ONLY LEFT button during capture...")
    left = _collect_phase_frames(session, phase_seconds, log=log, label="left_only")

    print("")
    input("Press Enter then press ONLY RIGHT button during capture...")
    right = _collect_phase_frames(session, phase_seconds, log=log, label="right_only")

    s_base = _phase_stats(baseline)
    s_left = _phase_stats(left)
    s_right = _phase_stats(right)

    keys = sorted(set(s_base) | set(s_left) | set(s_right))
    left_only: list[str] = []
    right_only: list[str] = []
    both: list[str] = []

    log("CALIBRATION moved-bytes summary:")
    for addr, idx in keys:
        b_range = s_base.get((addr, idx), {}).get("range", 0.0)
        l_range = s_left.get((addr, idx), {}).get("range", 0.0)
        r_range = s_right.get((addr, idx), {}).get("range", 0.0)

        l_delta = max(0.0, l_range - b_range)
        r_delta = max(0.0, r_range - b_range)
        if l_delta < 1.0 and r_delta < 1.0:
            continue

        msg = (
            f"  addr=0x{addr:02X} byte{idx:02d}: "
            f"base_range={b_range:.1f} left_range={l_range:.1f} right_range={r_range:.1f} "
            f"left_delta={l_delta:.1f} right_delta={r_delta:.1f}"
        )
        log(msg)

        if l_delta >= 4.0 and r_delta <= 1.0:
            left_only.append(msg.strip())
        elif r_delta >= 4.0 and l_delta <= 1.0:
            right_only.append(msg.strip())
        elif l_delta >= 2.0 or r_delta >= 2.0:
            both.append(msg.strip())

    log("CALIBRATION classification:")
    if left_only:
        log("  LEFT-only candidates:")
        for row in left_only:
            log(f"    {row}")
    else:
        log("  LEFT-only candidates: <none>")

    if right_only:
        log("  RIGHT-only candidates:")
        for row in right_only:
            log(f"    {row}")
    else:
        log("  RIGHT-only candidates: <none>")

    if both:
        log("  BOTH/ambiguous movers:")
        for row in both:
            log(f"    {row}")
    else:
        log("  BOTH/ambiguous movers: <none>")
    log("CALIBRATION end")


def _channel_values(
    frames: list[Feature0CFrame],
    extractor,
) -> list[int]:
    out: list[int] = []
    for frame in frames:
        v = extractor(frame)
        if v is not None:
            out.append(v)
    return out


def _run_right_range_calibration(
    session: PressureHidppSession,
    *,
    log,
    baseline_seconds: float,
    phase_seconds: float,
) -> None:
    log("CALIBRATION_RIGHT_RANGE begin")
    print("")
    input("Press Enter for baseline (do not press any button)...")
    baseline = _collect_phase_frames(session, baseline_seconds, log=log, label="right_baseline")

    print("")
    input("Press Enter, then press ONLY RIGHT and sweep full range...")
    right_only = _collect_phase_frames(session, phase_seconds, log=log, label="right_sweep")

    base_right = _channel_values(baseline, extract_mode3_right_pressure_raw)
    sweep_right = _channel_values(right_only, extract_mode3_right_pressure_raw)
    base_left = _channel_values(baseline, extract_mode3_left_pressure_raw)
    sweep_left = _channel_values(right_only, extract_mode3_left_pressure_raw)

    if not base_right or not sweep_right:
        log("CALIBRATION_RIGHT_RANGE error: insufficient right-channel samples")
        log("CALIBRATION_RIGHT_RANGE end")
        return

    right_min = min(base_right + sweep_right)
    right_max = max(base_right + sweep_right)
    log(
        "RIGHT channel addr0x10.byte06 "
        f"baseline_min={min(base_right)} baseline_max={max(base_right)} "
        f"sweep_min={min(sweep_right)} sweep_max={max(sweep_right)}"
    )
    log(f"RIGHT channel suggested_range min={right_min} max={right_max}")

    if base_left and sweep_left:
        left_min = min(base_left + sweep_left)
        left_max = max(base_left + sweep_left)
        log(
            "LEFT channel addr0x10.byte04 "
            f"baseline_min={min(base_left)} baseline_max={max(base_left)} "
            f"sweep_min={min(sweep_left)} sweep_max={max(sweep_left)}"
        )
        log(f"LEFT channel observed_range min={left_min} max={left_max}")
    log("CALIBRATION_RIGHT_RANGE end")


def run_pressure_reader(
    *,
    duration_s: float | None = None,
    log_file: str = "docs/pressure_realtime_log.txt",
    raw_min: int = 312,
    raw_max: int = 616,
    right_raw_min: int | None = None,
    right_raw_max: int | None = None,
    mode: int = 3,
    mode_arg: int = 0,
    show_all_0c: bool = False,
    calibrate: bool = False,
    calibrate_right_range: bool = False,
    phase_seconds: float = 8.0,
    baseline_seconds: float = 5.0,
) -> int:
    right_raw_min = raw_min if right_raw_min is None else right_raw_min
    right_raw_max = raw_max if right_raw_max is None else right_raw_max

    left_seen: set[int] = set()
    right_seen: set[int] = set()
    left_lo = 1024
    left_hi = 0
    right_lo = 1024
    right_hi = 0
    left_norm_lo = 1.0
    left_norm_hi = 0.0
    right_norm_lo = 1.0
    right_norm_hi = 0.0
    frame_count = 0
    addr_counts: dict[int, int] = {}

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="ascii") as fh:

        def log(line: str) -> None:
            print(line)
            fh.write(line + "\n")
            fh.flush()

        session = PressureHidppSession(log=log)
        start = time.perf_counter()
        try:
            session.open()
            session.enable_pressure_stream(mode=mode, mode_arg=mode_arg)

            if calibrate:
                _run_calibration(session, log=log, phase_seconds=phase_seconds)
                return 0
            if calibrate_right_range:
                _run_right_range_calibration(
                    session,
                    log=log,
                    baseline_seconds=baseline_seconds,
                    phase_seconds=phase_seconds,
                )
                return 0

            log("READ begin (Ctrl+C to stop)")
            while True:
                now = time.perf_counter()
                if duration_s is not None and (now - start) >= duration_s:
                    log("READ duration reached")
                    break

                item = session.read_next(timeout_s=0.1)
                if item is None:
                    continue
                ts, data = item
                frame = parse_feature_0c_frame(
                    data,
                    ts,
                    feature_index=session.pressure_feature_index,
                    device_index=session.device_index,
                )
                if frame is None:
                    continue

                addr_counts[frame.addr] = addr_counts.get(frame.addr, 0) + 1
                if show_all_0c:
                    elapsed = ts - start
                    log(
                        f"[{elapsed:8.3f}s] feature0x0C addr=0x{frame.addr:02X} "
                        f"raw={hex_bytes(frame.raw)}"
                    )

                left_raw, right_raw = extract_mode3_lr_pressure_raw(frame)
                if left_raw is None or right_raw is None:
                    continue

                frame_count += 1
                left_lo = min(left_lo, left_raw)
                left_hi = max(left_hi, left_raw)
                right_lo = min(right_lo, right_raw)
                right_hi = max(right_hi, right_raw)
                left_seen.add(left_raw)
                right_seen.add(right_raw)

                left_norm = normalize_raw_pressure(left_raw, raw_min, raw_max)
                right_norm = normalize_raw_pressure(right_raw, right_raw_min, right_raw_max)
                left_norm_lo = min(left_norm_lo, left_norm)
                left_norm_hi = max(left_norm_hi, left_norm)
                right_norm_lo = min(right_norm_lo, right_norm)
                right_norm_hi = max(right_norm_hi, right_norm)

                elapsed = ts - start
                log(
                    f"[{elapsed:8.3f}s] "
                    f"left_raw={left_raw:3d} left_norm={left_norm:0.4f} "
                    f"right_raw={right_raw:3d} right_norm={right_norm:0.4f} "
                    f"left_range=[{raw_min},{raw_max}] "
                    f"right_range=[{right_raw_min},{right_raw_max}]"
                )

        except KeyboardInterrupt:
            log("Interrupted")
        except Exception as e:
            log(f"ERROR {type(e).__name__}: {e}")
            return 1
        finally:
            session.close()

        elapsed = max(1e-9, time.perf_counter() - start)
        log("")
        log("SUMMARY")
        log(f"mode=0x{mode:02X} mode_arg=0x{mode_arg:02X}")
        log("decoded_channels=left(addr0x10.byte4),right(addr0x10.byte6)")
        log(
            f"left_raw_calibration_min={raw_min} left_raw_calibration_max={raw_max} "
            f"right_raw_calibration_min={right_raw_min} right_raw_calibration_max={right_raw_max}"
        )
        if addr_counts:
            parts = [f"0x{k:02X}:{v}" for k, v in sorted(addr_counts.items())]
            log("feature0x0C_addr_counts=" + ",".join(parts))
        log(f"lr_frames={frame_count} lr_rate={frame_count/elapsed:.2f}Hz")
        if frame_count:
            log(f"left_raw_min_seen={left_lo} left_raw_max_seen={left_hi}")
            log(f"right_raw_min_seen={right_lo} right_raw_max_seen={right_hi}")
            log(f"left_norm_min_seen={left_norm_lo:.4f} left_norm_max_seen={left_norm_hi:.4f}")
            log(f"right_norm_min_seen={right_norm_lo:.4f} right_norm_max_seen={right_norm_hi:.4f}")
            log("left_raw_values_seen=" + ",".join(str(v) for v in sorted(left_seen)))
            log("right_raw_values_seen=" + ",".join(str(v) for v in sorted(right_seen)))
        else:
            log("No mode-3 LR frames decoded (addr0x10.byte4/byte6).")

    return 0


def main() -> int:
    args = parse_args()
    return run_pressure_reader(
        duration_s=args.duration,
        log_file=args.log_file,
        raw_min=args.raw_min,
        raw_max=args.raw_max,
        right_raw_min=args.right_raw_min,
        right_raw_max=args.right_raw_max,
        mode=args.mode,
        mode_arg=args.mode_arg,
        show_all_0c=args.show_all_0c,
        calibrate=args.calibrate,
        calibrate_right_range=args.calibrate_right_range,
        phase_seconds=args.phase_seconds,
        baseline_seconds=args.baseline_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
