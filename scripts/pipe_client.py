"""Simple named-pipe client for Mouse Pressure output."""

from __future__ import annotations

import argparse
import time
from multiprocessing.connection import Client


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Connect to the Mouse Pressure named pipe and print pressure samples."
    )
    p.add_argument(
        "--pipe-name",
        default=r"\\.\pipe\mouse_pressure",
        help=r"Named pipe path (default: \\.\pipe\mouse_pressure).",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="How long to read samples for, in seconds (default: 10).",
    )
    p.add_argument(
        "--connect-timeout",
        type=float,
        default=5.0,
        help="How long to wait for bridge to accept connection (default: 5).",
    )
    p.add_argument(
        "--poll-timeout",
        type=float,
        default=0.5,
        help="Poll timeout while waiting for data (default: 0.5).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    print(f"PIPE client connecting to {args.pipe_name}")

    deadline = time.monotonic() + max(0.1, args.connect_timeout)
    conn = None
    while conn is None and time.monotonic() < deadline:
        try:
            conn = Client(args.pipe_name, family="AF_PIPE")
        except OSError:
            time.sleep(0.1)

    if conn is None:
        print("ERROR: timed out waiting for bridge pipe")
        return 1

    print("PIPE connected")
    start = time.monotonic()
    count = 0
    last = None
    try:
        while True:
            if (time.monotonic() - start) >= args.duration:
                break
            if not conn.poll(args.poll_timeout):
                continue
            sample = conn.recv()
            count += 1
            last = sample
            if "left_raw" in sample or "right_raw" in sample:
                print(
                    f"[{sample.get('t_rel_s', 0):8.3f}s] "
                    f"L raw={sample.get('left_raw')} norm={sample.get('left_norm')} "
                    f"mapped={sample.get('left_mapped')} | "
                    f"R raw={sample.get('right_raw')} norm={sample.get('right_norm')} "
                    f"mapped={sample.get('right_mapped')}"
                )
            else:
                # Backward compatibility with old single-channel payload.
                print(
                    f"[{sample.get('t_rel_s', 0):8.3f}s] "
                    f"raw={sample.get('raw')} "
                    f"norm={sample.get('normalized')} "
                    f"mapped={sample.get('mapped_0_1023')}"
                )
    except KeyboardInterrupt:
        print("Interrupted")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    elapsed = max(1e-9, time.monotonic() - start)
    print("SUMMARY")
    print(f"samples={count} rate={count/elapsed:.2f}Hz")
    if last is not None:
        print(f"last_sample={last}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
