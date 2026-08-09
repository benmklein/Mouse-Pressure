"""Real-time Superstrike dual-pressure visualization (mode 3 byte4 + byte6)."""

from __future__ import annotations

from collections import deque
from queue import Empty, SimpleQueue
from threading import Event, Thread
import time

from superstrike_pressure.bridge.curves import PressureConfig, map_normalized_pressure
from superstrike_pressure.sniff.hidpp_pressure import (
    PressureHidppSession,
    extract_mode3_lr_pressure_raw,
    normalize_raw_pressure,
    parse_feature_0c_frame,
)


def run_visualize() -> None:
    try:
        from matplotlib.animation import FuncAnimation
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is not installed.")
        print("Install optional dev dependencies: uv sync --extra dev")
        return

    left_cfg = PressureConfig(raw_min=312, raw_max=616, out_min=0, out_max=1023, curve="s_curve")
    right_cfg = PressureConfig(raw_min=312, raw_max=584, out_min=0, out_max=1023, curve="s_curve")

    points_max = 500
    frame_interval_ms = 16  # ~60fps

    left_min_seen = 1024
    left_max_seen = 0
    right_min_seen = 1024
    right_max_seen = 0
    current_left_raw = left_cfg.raw_min
    current_right_raw = right_cfg.raw_min
    current_left_norm = 0.0
    current_right_norm = 0.0
    current_left_mapped = 0
    current_right_mapped = 0
    sample_count = 0
    left_values = deque(maxlen=points_max)
    right_values = deque(maxlen=points_max)
    update_errors = 0
    frame_count = 0
    last_update_monotonic = time.monotonic()
    last_sample_monotonic = time.monotonic()
    reader_errors = 0
    reader_recoveries = 0

    sample_queue: SimpleQueue[tuple[float, int, int]] = SimpleQueue()
    stop_reader = Event()

    def log(msg: str) -> None:
        print(msg)

    session = PressureHidppSession(log=log)

    fig = plt.figure(figsize=(13, 8))
    gs = fig.add_gridspec(2, 2, height_ratios=[2.2, 1.0], width_ratios=[2.0, 1.2])
    ax_raw = fig.add_subplot(gs[0, :])
    ax_mapped = fig.add_subplot(gs[1, 0])
    ax_curve = fig.add_subplot(gs[1, 1])

    (left_line,) = ax_raw.plot([], [], color="#1565C0", linewidth=2.0, label="Left (byte4)")
    (right_line,) = ax_raw.plot([], [], color="#2E7D32", linewidth=2.0, label="Right (byte6)")
    ax_raw.set_title("Superstrike Mode-3 Pressure Channels (addr 0x10)")
    ax_raw.set_xlabel("Recent Samples")
    ax_raw.set_ylabel("Raw Pressure")
    ax_raw.grid(True, alpha=0.3)
    ax_raw.set_xlim(0, points_max - 1)
    y_min = min(left_cfg.raw_min, right_cfg.raw_min) - 2
    y_max = max(left_cfg.raw_max, right_cfg.raw_max) + 2
    ax_raw.set_ylim(y_min, y_max)
    ax_raw.legend(loc="upper left")

    left_bar = ax_mapped.barh([1], [0], color="#D32F2F", height=0.45, label="Left")[0]
    right_bar = ax_mapped.barh([0], [0], color="#7B1FA2", height=0.45, label="Right")[0]
    ax_mapped.set_yticks([0, 1], labels=["Right", "Left"])
    ax_mapped.set_xlabel("Mapped Pressure (0..1023)")
    ax_mapped.set_xlim(0, 1023)
    ax_mapped.legend(loc="lower right")
    stats_text = ax_mapped.text(
        0.01,
        0.93,
        "",
        transform=ax_mapped.transAxes,
        ha="left",
        va="top",
        family="monospace",
    )

    curve_x = [index / 255.0 for index in range(256)]
    left_curve_y = [map_normalized_pressure(x, left_cfg) for x in curve_x]
    right_curve_y = [map_normalized_pressure(x, right_cfg) for x in curve_x]
    ax_curve.plot(curve_x, left_curve_y, color="#D32F2F", linewidth=2.0, label="Left curve")
    ax_curve.plot(curve_x, right_curve_y, color="#7B1FA2", linewidth=1.6, linestyle="--", label="Right curve")
    (left_curve_point,) = ax_curve.plot([0.0], [0.0], marker="o", color="#D32F2F", markersize=7)
    (right_curve_point,) = ax_curve.plot([0.0], [0.0], marker="o", color="#7B1FA2", markersize=7)
    ax_curve.set_title("Curve Preview")
    ax_curve.set_xlabel("Normalized Input")
    ax_curve.set_ylabel("Mapped Output")
    ax_curve.set_xlim(0.0, 1.0)
    ax_curve.set_ylim(0, 1023)
    ax_curve.grid(True, alpha=0.3)
    ax_curve.legend(loc="upper left")

    fig.tight_layout()
    closed = False
    backend = str(plt.get_backend()).lower()
    supports_blit = bool(getattr(fig.canvas, "supports_blit", False))
    use_blit = supports_blit and ("tkagg" not in backend) and ("wx" not in backend)
    print(f"VIS backend={backend} supports_blit={supports_blit} use_blit={use_blit}")

    def _on_close(_evt) -> None:
        nonlocal closed
        closed = True
        stop_reader.set()

    close_cid = fig.canvas.mpl_connect("close_event", _on_close)

    def _reader_loop() -> None:
        nonlocal last_sample_monotonic, reader_errors, reader_recoveries
        next_recover_monotonic = time.monotonic() + 1.5
        while not stop_reader.is_set():
            try:
                item = session.read_next(timeout_s=0.05)
                now = time.monotonic()
                if item is None:
                    if (
                        (now - last_sample_monotonic) > 1.5
                        and now >= next_recover_monotonic
                        and not stop_reader.is_set()
                    ):
                        reader_recoveries += 1
                        next_recover_monotonic = now + 3.0
                        print(
                            f"VIS reader idle for {now - last_sample_monotonic:0.2f}s; "
                            f"re-enabling mode 3 (attempt {reader_recoveries})"
                        )
                        session.enable_pressure_stream(mode=3, mode_arg=0)
                        last_sample_monotonic = now
                    continue

                ts, data = item
                frame = parse_feature_0c_frame(
                    data,
                    timestamp_s=ts,
                    feature_index=session.pressure_feature_index,
                    device_index=session.device_index,
                )
                if frame is None:
                    continue
                left_raw, right_raw = extract_mode3_lr_pressure_raw(frame)
                if left_raw is None or right_raw is None:
                    continue
                sample_queue.put((ts, left_raw, right_raw))
                last_sample_monotonic = now
            except Exception as e:
                reader_errors += 1
                if reader_errors <= 5:
                    print(f"VIS reader error ({reader_errors}): {type(e).__name__}: {e}")
                time.sleep(0.05)

    reader_thread: Thread | None = None

    def _init_artists():
        left_line.set_data([], [])
        right_line.set_data([], [])
        left_bar.set_width(0)
        right_bar.set_width(0)
        left_curve_point.set_data([0.0], [0.0])
        right_curve_point.set_data([0.0], [0.0])
        stats_text.set_text("waiting for mode-3 addr 0x10 frames...")
        return (
            left_line,
            right_line,
            left_bar,
            right_bar,
            left_curve_point,
            right_curve_point,
            stats_text,
        )

    def update(_frame_idx: int):
        nonlocal current_left_raw, current_right_raw
        nonlocal current_left_norm, current_right_norm
        nonlocal current_left_mapped, current_right_mapped
        nonlocal left_min_seen, left_max_seen, right_min_seen, right_max_seen
        nonlocal sample_count, update_errors, frame_count, last_update_monotonic
        frame_count += 1
        last_update_monotonic = time.monotonic()
        try:
            for _ in range(2048):
                ts, left_raw, right_raw = sample_queue.get_nowait()
                current_left_raw = left_raw
                current_right_raw = right_raw
                current_left_norm = normalize_raw_pressure(left_raw, left_cfg.raw_min, left_cfg.raw_max)
                current_right_norm = normalize_raw_pressure(
                    right_raw, right_cfg.raw_min, right_cfg.raw_max
                )
                current_left_mapped = map_normalized_pressure(current_left_norm, left_cfg)
                current_right_mapped = map_normalized_pressure(current_right_norm, right_cfg)
                left_min_seen = min(left_min_seen, left_raw)
                left_max_seen = max(left_max_seen, left_raw)
                right_min_seen = min(right_min_seen, right_raw)
                right_max_seen = max(right_max_seen, right_raw)
                sample_count += 1
                left_values.append(left_raw)
                right_values.append(right_raw)
        except Empty:
            pass
        except Exception as e:
            update_errors += 1
            if update_errors <= 5:
                print(f"VIS update error ({update_errors}): {type(e).__name__}: {e}")

        if left_values:
            y_left = list(left_values)
            y_right = list(right_values)
            x_offset = points_max - len(y_left)
            x = list(range(x_offset, points_max))
            left_line.set_data(x, y_left)
            right_line.set_data(x, y_right)

        left_bar.set_width(current_left_mapped)
        right_bar.set_width(current_right_mapped)
        left_curve_point.set_data([current_left_norm], [current_left_mapped])
        right_curve_point.set_data([current_right_norm], [current_right_mapped])
        if sample_count > 0:
            sample_idle = time.monotonic() - last_sample_monotonic
            stats_text.set_text(
                f"L raw={current_left_raw:3d} norm={current_left_norm:0.4f} map={current_left_mapped:4d} "
                f"seen=[{left_min_seen:3d},{left_max_seen:3d}]\n"
                f"R raw={current_right_raw:3d} norm={current_right_norm:0.4f} map={current_right_mapped:4d} "
                f"seen=[{right_min_seen:3d},{right_max_seen:3d}]\n"
                f"samples={sample_count} upd_err={update_errors} "
                f"rd_err={reader_errors} recov={reader_recoveries} idle={sample_idle:0.2f}s"
            )
        else:
            stats_text.set_text("waiting for mode-3 addr 0x10 frames...")

        return (
            left_line,
            right_line,
            left_bar,
            right_bar,
            left_curve_point,
            right_curve_point,
            stats_text,
        )

    ani = None
    heartbeat = fig.canvas.new_timer(interval=5000)

    def _heartbeat_tick() -> None:
        frame_idle = time.monotonic() - last_update_monotonic
        sample_idle = time.monotonic() - last_sample_monotonic
        print(
            f"VIS heartbeat frames={frame_count} samples={sample_count} "
            f"upd_err={update_errors} rd_err={reader_errors} recov={reader_recoveries} "
            f"frame_idle={frame_idle:0.2f}s sample_idle={sample_idle:0.2f}s"
        )

    heartbeat.add_callback(_heartbeat_tick)
    try:
        session.open()
        session.enable_pressure_stream(mode=3, mode_arg=0)
        reader_thread = Thread(target=_reader_loop, name="ss-vis-reader", daemon=True)
        reader_thread.start()

        ani = FuncAnimation(
            fig,
            update,
            init_func=_init_artists,
            interval=frame_interval_ms,
            blit=use_blit,
            cache_frame_data=False,
        )
        fig._ani = ani
        heartbeat.start()
        plt.show()
    except KeyboardInterrupt:
        print("Interrupted")
    except Exception as e:
        print(f"ERROR {type(e).__name__}: {e}")
    finally:
        stop_reader.set()
        if reader_thread is not None and reader_thread.is_alive():
            reader_thread.join(timeout=2.0)
        try:
            heartbeat.stop()
        except Exception:
            pass
        session.close()
        try:
            fig.canvas.mpl_disconnect(close_cid)
        except Exception:
            pass
        if not closed and plt.fignum_exists(fig.number):
            plt.close(fig)

    print("Summary:")
    print(f"  samples={sample_count}")
    if sample_count:
        print(
            f"  left_raw_min_seen={left_min_seen} left_raw_max_seen={left_max_seen} "
            f"right_raw_min_seen={right_min_seen} right_raw_max_seen={right_max_seen}"
        )
