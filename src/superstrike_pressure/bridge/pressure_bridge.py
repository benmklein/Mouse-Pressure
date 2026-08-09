"""Pressure bridge: mode-3 HID++ left/right pressure -> curved 0..1023 output."""

from __future__ import annotations

import argparse
import platform
import sys
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import Listener
from pathlib import Path

from superstrike_pressure.bridge.curves import (
    PressureConfig,
    map_normalized_pressure,
    normalize_curve_name,
)
from superstrike_pressure.sniff.hidpp_pressure import (
    PressureHidppSession,
    extract_mode3_lr_pressure_raw,
    normalize_raw_pressure,
    parse_feature_0c_frame,
)


@dataclass(frozen=True)
class PressureSample:
    t_rel_s: float
    left_raw: int
    left_norm: float
    left_mapped: int
    right_raw: int
    right_norm: float
    right_mapped: int


class NamedPipeEmitter:
    """Simple Windows pressure emitter over named pipe (pickled dict payloads)."""

    def __init__(self, pipe_name: str, log) -> None:
        self.pipe_name = pipe_name
        self.log = log
        self.listener: Listener | None = None
        self.client = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()

    def start(self) -> None:
        if platform.system().lower() != "windows":
            self.log("PIPE disabled: named pipe emitter is Windows-only")
            return
        self.listener = Listener(self.pipe_name, family="AF_PIPE")
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        self.log(f"PIPE listening on {self.pipe_name}")

    def _accept_loop(self) -> None:
        assert self.listener is not None
        while self._running:
            try:
                conn = self.listener.accept()
            except Exception:
                if self._running:
                    self.log("PIPE accept loop ended")
                break
            with self._lock:
                if self.client is not None:
                    try:
                        self.client.close()
                    except Exception:
                        pass
                self.client = conn
            self.log("PIPE client connected")

    def emit(self, sample: PressureSample) -> None:
        payload = {
            "t_rel_s": round(sample.t_rel_s, 6),
            "left_raw": sample.left_raw,
            "left_norm": round(sample.left_norm, 6),
            "left_mapped": sample.left_mapped,
            "right_raw": sample.right_raw,
            "right_norm": round(sample.right_norm, 6),
            "right_mapped": sample.right_mapped,
        }
        with self._lock:
            conn = self.client
        if conn is None:
            return
        try:
            conn.send(payload)
        except Exception:
            with self._lock:
                if self.client is conn:
                    self.client = None
            self.log("PIPE client disconnected")

    def stop(self) -> None:
        self._running = False
        with self._lock:
            if self.client is not None:
                try:
                    self.client.close()
                except Exception:
                    pass
                self.client = None
        if self.listener is not None:
            try:
                self.listener.close()
            except Exception:
                pass
            self.listener = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Run the Superstrike event-1 stream with full 10-bit left/right ADC decoding, "
            "apply pressure curve, emit mapped 0..1023 values."
        )
    )
    p.add_argument("--raw-min", type=int, default=312, help="LEFT 10-bit ADC min (default: 312)")
    p.add_argument("--raw-max", type=int, default=616, help="LEFT 10-bit ADC max (default: 616)")
    p.add_argument(
        "--right-raw-min",
        type=int,
        default=312,
        help="RIGHT 10-bit ADC min (default: 312)",
    )
    p.add_argument(
        "--right-raw-max",
        type=int,
        default=584,
        help="RIGHT 10-bit ADC max (default: 584)",
    )
    p.add_argument("--mode", type=int, default=3, help="Feature 0x0C mode (default: 3)")
    p.add_argument("--mode-arg", type=int, default=0, help="Mode arg byte (default: 0)")
    curve_choices = [
        "linear",
        "soft",
        "hard",
        "scurve",
        # Deprecated legacy aliases
        "ease_in",
        "ease_out",
        "s_curve",
    ]
    p.add_argument(
        "--curve",
        choices=curve_choices,
        default="scurve",
        help="Pressure curve (default: scurve)",
    )
    p.add_argument(
        "--curve-strength",
        type=float,
        default=2.0,
        help="Curve strength gamma (default: 2.0)",
    )
    p.add_argument(
        "--deadzone-low",
        type=float,
        default=0.05,
        help="Low deadzone in normalized space (default: 0.05)",
    )
    p.add_argument(
        "--deadzone-high",
        type=float,
        default=0.95,
        help="High deadzone in normalized space (default: 0.95)",
    )
    p.add_argument(
        "--pipe-name",
        default=r"\\.\pipe\superstrike_pressure",
        help=r"Named pipe for sample emission (default: \\.\pipe\superstrike_pressure)",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Optional runtime duration (seconds).",
    )
    p.add_argument(
        "--log-file",
        default="docs/bridge_log.txt",
        help="Path for bridge log output (default: docs/bridge_log.txt).",
    )
    return p.parse_args()


def run_bridge() -> int:
    args = parse_args()
    right_raw_min = args.right_raw_min
    right_raw_max = args.right_raw_max
    curve_name = normalize_curve_name(args.curve)

    left_cfg = PressureConfig(
        raw_min=args.raw_min,
        raw_max=args.raw_max,
        out_min=0,
        out_max=1023,
        deadzone_low=args.deadzone_low,
        deadzone_high=args.deadzone_high,
        curve=curve_name,
        curve_strength=args.curve_strength,
    )
    right_cfg = PressureConfig(
        raw_min=right_raw_min,
        raw_max=right_raw_max,
        out_min=0,
        out_max=1023,
        deadzone_low=args.deadzone_low,
        deadzone_high=args.deadzone_high,
        curve=curve_name,
        curve_strength=args.curve_strength,
    )

    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="ascii") as fh:

        def log(line: str) -> None:
            print(line)
            fh.write(line + "\n")
            fh.flush()

        session = PressureHidppSession(log=log)
        emitter = NamedPipeEmitter(args.pipe_name, log=log)

        start = time.perf_counter()
        n_samples = 0
        left_lo = 1024
        left_hi = 0
        right_lo = 1024
        right_hi = 0
        last_print = 0.0
        last_left = -1
        last_right = -1
        try:
            session.open()
            session.enable_pressure_stream(mode=args.mode, mode_arg=args.mode_arg)
            emitter.start()
            log(
                f"BRIDGE running mode=0x{args.mode:02X} "
                f"left_range=[{left_cfg.raw_min},{left_cfg.raw_max}] "
                f"right_range=[{right_cfg.raw_min},{right_cfg.raw_max}] "
                f"curve={curve_name} strength={args.curve_strength:.2f}"
            )
            while True:
                now = time.perf_counter()
                if args.duration is not None and (now - start) >= args.duration:
                    log("BRIDGE duration reached")
                    break

                item = session.read_next(timeout_s=0.02)
                if item is None:
                    continue
                ts, data = item
                frame = parse_feature_0c_frame(
                    data,
                    ts,
                    feature_index=session.pressure_feature_index,
                )
                if frame is None:
                    continue

                left_raw, right_raw = extract_mode3_lr_pressure_raw(frame)
                if left_raw is None or right_raw is None:
                    continue

                left_norm = normalize_raw_pressure(left_raw, left_cfg.raw_min, left_cfg.raw_max)
                right_norm = normalize_raw_pressure(right_raw, right_cfg.raw_min, right_cfg.raw_max)
                left_mapped = map_normalized_pressure(left_norm, left_cfg)
                right_mapped = map_normalized_pressure(right_norm, right_cfg)

                sample = PressureSample(
                    t_rel_s=ts - start,
                    left_raw=left_raw,
                    left_norm=left_norm,
                    left_mapped=left_mapped,
                    right_raw=right_raw,
                    right_norm=right_norm,
                    right_mapped=right_mapped,
                )
                emitter.emit(sample)
                n_samples += 1
                left_lo = min(left_lo, left_raw)
                left_hi = max(left_hi, left_raw)
                right_lo = min(right_lo, right_raw)
                right_hi = max(right_hi, right_raw)

                if (
                    left_mapped != last_left
                    or right_mapped != last_right
                    or (ts - last_print) >= 0.2
                ):
                    last_print = ts
                    last_left = left_mapped
                    last_right = right_mapped
                    log(
                        f"[{sample.t_rel_s:8.3f}s] "
                        f"L raw={left_raw:3d} norm={left_norm:0.4f} mapped={left_mapped:4d} | "
                        f"R raw={right_raw:3d} norm={right_norm:0.4f} mapped={right_mapped:4d}"
                    )

        except KeyboardInterrupt:
            log("Interrupted")
        except Exception as e:
            log(f"ERROR {type(e).__name__}: {e}")
            return 1
        finally:
            emitter.stop()
            session.close()

        elapsed = max(1e-9, time.perf_counter() - start)
        log("")
        log("SUMMARY")
        log(f"samples={n_samples} rate={n_samples/elapsed:.2f}Hz")
        if n_samples:
            log(f"left_raw_min_seen={left_lo} left_raw_max_seen={left_hi}")
            log(f"right_raw_min_seen={right_lo} right_raw_max_seen={right_hi}")
        log(f"log_file={log_path}")
        log(f"pipe_name={args.pipe_name}")

    return 0


if __name__ == "__main__":
    sys.exit(run_bridge())
