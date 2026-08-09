from __future__ import annotations

import math
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from superstrike_pressure.bridge.synthetic_pen import (  # noqa: E402
    POINTER_FLAG_DOWN,
    POINTER_FLAG_INCONTACT,
    POINTER_FLAG_FIRSTBUTTON,
    POINTER_FLAG_INRANGE,
    POINTER_FLAG_NEW,
    POINTER_FLAG_PRIMARY,
    POINTER_FLAG_UP,
    POINTER_FLAG_UPDATE,
    RAWMOUSE,
    RI_MOUSE_LEFT_BUTTON_DOWN,
    RI_MOUSE_LEFT_BUTTON_UP,
    WM_LBUTTONDOWN,
    WM_MOUSEMOVE,
    WM_RBUTTONDOWN,
    _MouseLmbSuppressor,
    SyntheticPenConfig,
    SyntheticPenEmitter,
)


class _FakePen:
    def __init__(self) -> None:
        self._lmb = False
        self._rmb = False
        self.right_clicks = 0
        self.calls: list[dict[str, int | str]] = []
        self.call_times: list[float] = []
        self.pos = (400, 300)

    def open(self) -> None:
        return

    def close(self) -> None:
        return

    def get_cursor_pos(self) -> tuple[int, int]:
        return self.pos

    def is_lmb_down(self) -> bool:
        return self._lmb

    def is_rmb_down(self) -> bool:
        return self._rmb

    def inject(self, *, flags: int, x: int, y: int, pressure_1024: int, tag: str) -> tuple[bool, int]:
        self.call_times.append(time.perf_counter())
        self.calls.append(
            {
                "flags": int(flags),
                "x": int(x),
                "y": int(y),
                "pressure": int(pressure_1024),
                "tag": str(tag),
            }
        )
        return True, 0

    def emit_left_click(self) -> None:
        return

    def emit_right_click(self) -> None:
        self.right_clicks += 1


class SyntheticPenReleaseTests(unittest.TestCase):
    def _mk_emitter(self, *, release_teardown: bool) -> tuple[SyntheticPenEmitter, _FakePen]:
        cfg = SyntheticPenConfig(
            contact_source="lmb_and_pressure",
            contact_threshold=12,
            release_threshold=4,
            pressure_interp_steps=4,
            suppress_lmb=False,
            release_teardown=release_teardown,
        )
        emitter = SyntheticPenEmitter(cfg, log=lambda _line: None)
        fake = _FakePen()
        emitter.pen = fake  # type: ignore[assignment]
        return emitter, fake

    def test_movement_hook_observes_but_never_blocks_native_mouse_move(self) -> None:
        self.assertFalse(
            _MouseLmbSuppressor._should_block_message(WM_MOUSEMOVE, injected=False)
        )
        self.assertFalse(
            _MouseLmbSuppressor._should_block_message(WM_MOUSEMOVE, injected=True)
        )
        self.assertTrue(
            _MouseLmbSuppressor._should_block_message(WM_LBUTTONDOWN, injected=False)
        )
        self.assertFalse(
            _MouseLmbSuppressor._should_block_message(WM_LBUTTONDOWN, injected=True)
        )
        self.assertTrue(
            _MouseLmbSuppressor._should_block_message(
                WM_RBUTTONDOWN,
                injected=False,
                suppress_left=False,
                suppress_right=True,
            )
        )

    def test_right_button_uses_right_pressure_and_channel_settings(self) -> None:
        config = SyntheticPenConfig(
            contact_threshold=900,
            release_threshold=800,
            right_contact_threshold=12,
            right_release_threshold=4,
            right_min_contact_pressure=205,
            right_onset_buffer=False,
        )
        emitter = SyntheticPenEmitter(config, log=lambda _line: None)
        fake = _FakePen()
        fake._rmb = True
        emitter.pen = fake  # type: ignore[assignment]

        sample = emitter.update(left_mapped=0, right_mapped=300, pressure_fresh=True)

        self.assertEqual(emitter.active_button, "right")
        self.assertEqual(sample.state, "contact")
        self.assertGreaterEqual(sample.pen_1024, 205)

        fake._rmb = False
        released = emitter.update(left_mapped=900, right_mapped=300, pressure_fresh=True)
        self.assertEqual(released.state, "idle")
        self.assertIsNone(emitter.active_button)

    def test_recent_synthetic_position_is_not_recaptured_as_hardware(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor.mark_injected_position(410, 305)

        now = time.perf_counter()
        self.assertTrue(suppressor._is_recent_injected_position(410, 305, now))  # noqa: SLF001
        self.assertFalse(suppressor._is_recent_injected_position(411, 305, now))  # noqa: SLF001

    def test_raw_input_selects_motion_device_and_ignores_other_devices(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._cursor_position = lambda: (100, 200)  # type: ignore[method-assign]  # noqa: SLF001
        suppressor._get_raw_device_identity = (  # type: ignore[method-assign]  # noqa: SLF001
            lambda handle: {0xA1: "VID_046D&PID_C54D", 0xB2: "VID_1234&PID_5678"}.get(
                handle, ""
            )
        )
        down = RAWMOUSE()
        down.usButtonFlags = RI_MOUSE_LEFT_BUTTON_DOWN
        suppressor._handle_raw_mouse(0xA1, down)  # noqa: SLF001
        suppressor._lmb_down = True  # noqa: SLF001

        wrong_device = RAWMOUSE()
        wrong_device.lLastX = 500
        suppressor._handle_raw_mouse(0xB2, wrong_device)  # noqa: SLF001

        first = RAWMOUSE()
        first.lLastX = 4
        first.lLastY = -2
        suppressor._handle_native_mouse_move(  # noqa: SLF001
            time.perf_counter(), 104, 198, injected=False
        )
        suppressor._handle_raw_mouse(0xA1, first)  # noqa: SLF001
        second = RAWMOUSE()
        second.lLastX = 3
        second.lLastY = 5
        suppressor._handle_native_mouse_move(  # noqa: SLF001
            time.perf_counter(), 107, 203, injected=False
        )
        suppressor._handle_raw_mouse(0xA1, second)  # noqa: SLF001

        self.assertEqual(suppressor._raw_motion_device_handle, 0xA1)  # noqa: SLF001
        self.assertEqual(
            [(x, y) for _ts, x, y in suppressor.drain_hardware_positions()],
            [(104, 198), (107, 203)],
        )

        up = RAWMOUSE()
        up.usButtonFlags = RI_MOUSE_LEFT_BUTTON_UP
        suppressor._handle_raw_mouse(0xA1, up)  # noqa: SLF001
        ignored_after_up = RAWMOUSE()
        ignored_after_up.lLastX = 10
        suppressor._handle_raw_mouse(0xA1, ignored_after_up)  # noqa: SLF001
        self.assertEqual(suppressor.drain_hardware_positions(), [])

    def test_raw_input_associates_composite_motion_handle_by_vid_pid(self) -> None:
        lines: list[str] = []
        suppressor = _MouseLmbSuppressor(log=lines.append)
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._cursor_position = lambda: (300, 400)  # type: ignore[method-assign]  # noqa: SLF001
        suppressor._get_raw_device_identity = (  # type: ignore[method-assign]  # noqa: SLF001
            lambda handle: {
                0xA1: "VID_046D&PID_C54D",
                0xA2: "VID_046D&PID_C54D",
            }.get(handle, "")
        )

        down = RAWMOUSE()
        down.usButtonFlags = RI_MOUSE_LEFT_BUTTON_DOWN
        suppressor._handle_raw_mouse(0xA1, down)  # noqa: SLF001
        suppressor._lmb_down = True  # noqa: SLF001

        movement = RAWMOUSE()
        movement.lLastX = 8
        movement.lLastY = -3
        suppressor._handle_native_mouse_move(  # noqa: SLF001
            time.perf_counter(), 308, 397, injected=False
        )
        suppressor._handle_raw_mouse(0xA2, movement)  # noqa: SLF001

        self.assertEqual(suppressor._raw_motion_device_handle, 0xA2)  # noqa: SLF001
        self.assertTrue(any("RAW motion device handle=0xA2" in line for line in lines))
        self.assertEqual(
            [(x, y) for _ts, x, y in suppressor.drain_hardware_positions()],
            [(308, 397)],
        )

    def test_native_hook_positions_are_ordered_screen_coordinates(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._lmb_down = True  # noqa: SLF001
        callbacks: list[bool] = []
        suppressor.set_movement_callback(lambda: callbacks.append(True))

        now = time.perf_counter()
        suppressor._handle_native_mouse_move(  # noqa: SLF001
            now, 104, 198, injected=False
        )
        raw_first = RAWMOUSE()
        raw_first.lLastX = 1
        suppressor._raw_contact_active = True  # noqa: SLF001
        suppressor._raw_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_motion_device_handle = 0xA1  # noqa: SLF001
        suppressor._handle_raw_mouse(0xA1, raw_first)  # noqa: SLF001
        suppressor._handle_native_mouse_move(  # noqa: SLF001
            now + 0.001, 107, 203, injected=False
        )
        raw_second = RAWMOUSE()
        raw_second.lLastX = 1
        suppressor._handle_raw_mouse(0xA1, raw_second)  # noqa: SLF001

        first = suppressor.drain_hardware_positions(max_count=1)
        second = suppressor.drain_hardware_positions(max_count=1)
        self.assertEqual([(x, y) for _ts, x, y in first], [(104, 198)])
        self.assertEqual([(x, y) for _ts, x, y in second], [(107, 203)])
        self.assertLess(first[0][0], second[0][0])
        self.assertEqual(len(callbacks), 2)

        suppressor.mark_injected_position(5000, 5000)
        suppressor._handle_native_mouse_move(  # noqa: SLF001
            time.perf_counter(), 5000, 5000, injected=True
        )
        self.assertEqual(suppressor.drain_hardware_positions(), [])

    def test_native_physical_move_is_not_confused_with_matching_pen_point(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._lmb_down = True  # noqa: SLF001
        suppressor.mark_injected_position(410, 305)

        suppressor._handle_native_mouse_move(  # noqa: SLF001
            time.perf_counter(), 410, 305, injected=False
        )
        suppressor._raw_contact_active = True  # noqa: SLF001
        suppressor._raw_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_motion_device_handle = 0xA1  # noqa: SLF001
        raw = RAWMOUSE()
        raw.lLastX = 1
        suppressor._handle_raw_mouse(0xA1, raw)  # noqa: SLF001

        positions = suppressor.drain_hardware_positions()
        self.assertEqual([(x, y) for _ts, x, y in positions], [(410, 305)])

    def test_driver_injected_move_is_accepted_when_not_our_pen_feedback(self) -> None:
        lines: list[str] = []
        suppressor = _MouseLmbSuppressor(log=lines.append)
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._lmb_down = True  # noqa: SLF001

        suppressor._handle_native_mouse_move(  # noqa: SLF001
            time.perf_counter(), 640, 360, injected=True
        )
        suppressor._raw_contact_active = True  # noqa: SLF001
        suppressor._raw_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_motion_device_handle = 0xA1  # noqa: SLF001
        raw = RAWMOUSE()
        raw.lLastX = 1
        suppressor._handle_raw_mouse(0xA1, raw)  # noqa: SLF001

        positions = suppressor.drain_hardware_positions()
        self.assertEqual([(x, y) for _ts, x, y in positions], [(640, 360)])
        self.assertIn("MOTION correlated hook coordinates with Raw Input device", lines)

    def test_unvalidated_pen_feedback_is_replaced_by_next_physical_hook_point(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._lmb_down = True  # noqa: SLF001
        suppressor._raw_contact_active = True  # noqa: SLF001
        suppressor._raw_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_motion_device_handle = 0xA1  # noqa: SLF001

        # Pen promotion creates a hook coordinate but no Raw Input packet.
        suppressor._handle_native_mouse_move(  # noqa: SLF001
            time.perf_counter(), 404, 300, injected=False
        )
        # The next real move contributes both a hook coordinate and Raw Input.
        suppressor._handle_native_mouse_move(  # noqa: SLF001
            time.perf_counter(), 410, 302, injected=False
        )
        raw = RAWMOUSE()
        raw.lLastX = 4
        raw.lLastY = 2
        suppressor._handle_raw_mouse(0xA1, raw)  # noqa: SLF001

        self.assertEqual(
            [(x, y) for _ts, x, y in suppressor.drain_hardware_positions()],
            [(410, 302)],
        )

    def test_raw_input_ignores_synthetic_zero_handle_button_packets(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)

        down = RAWMOUSE()
        down.usButtonFlags = RI_MOUSE_LEFT_BUTTON_DOWN
        suppressor._handle_raw_mouse(0, down)  # noqa: SLF001

        self.assertFalse(suppressor._raw_contact_active)  # noqa: SLF001
        self.assertEqual(suppressor._raw_device_handle, 0)  # noqa: SLF001

    def test_release_without_teardown_sends_single_up(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        emitter.state = "contact"
        emitter.contact_frame_no = 5
        emitter.prev_contact_pressure = 400
        fake._lmb = False

        sample = emitter.update(left_mapped=400, right_mapped=0)

        self.assertEqual(sample.state, "idle")
        self.assertTrue(sample.injected)
        self.assertFalse(sample.failed)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(
            fake.calls[0]["flags"],
            POINTER_FLAG_UPDATE
            | POINTER_FLAG_INRANGE
            | POINTER_FLAG_INCONTACT
            | POINTER_FLAG_FIRSTBUTTON
            | POINTER_FLAG_PRIMARY,
        )
        self.assertEqual(fake.calls[0]["pressure"], 400)
        self.assertEqual(fake.calls[1]["flags"], POINTER_FLAG_UP | POINTER_FLAG_PRIMARY)
        self.assertEqual(fake.calls[1]["pressure"], 0)
        self.assertEqual(
            (fake.calls[0]["x"], fake.calls[0]["y"]),
            (fake.calls[1]["x"], fake.calls[1]["y"]),
        )

    def test_release_with_teardown_sends_up_hover_endhover(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=True)
        emitter.state = "contact"
        emitter.contact_frame_no = 5
        emitter.prev_contact_pressure = 400
        fake._lmb = False

        sample = emitter.update(left_mapped=400, right_mapped=0)

        self.assertEqual(sample.state, "idle")
        self.assertTrue(sample.injected)
        self.assertFalse(sample.failed)
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual(
            fake.calls[0]["flags"],
            POINTER_FLAG_UPDATE
            | POINTER_FLAG_INRANGE
            | POINTER_FLAG_INCONTACT
            | POINTER_FLAG_FIRSTBUTTON
            | POINTER_FLAG_PRIMARY,
        )
        self.assertEqual(
            fake.calls[1]["flags"],
            POINTER_FLAG_UP | POINTER_FLAG_INRANGE | POINTER_FLAG_PRIMARY,
        )
        self.assertEqual(
            fake.calls[2]["flags"],
            POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_PRIMARY,
        )
        self.assertEqual(fake.calls[3]["flags"], POINTER_FLAG_UPDATE | POINTER_FLAG_PRIMARY)
        self.assertEqual(fake.calls[0]["pressure"], 400)
        self.assertEqual(fake.calls[1]["pressure"], 0)
        self.assertEqual(fake.calls[2]["pressure"], 0)
        self.assertEqual(fake.calls[3]["pressure"], 0)

    def test_contact_buffers_one_pressure_sample_and_catches_up_smoothly(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        emitter.config.path_stabilization = 60
        fake._lmb = True

        first = emitter.update(left_mapped=40, right_mapped=0, pressure_fresh=True)
        self.assertEqual(first.state, "hovering")
        self.assertEqual(fake.calls, [])

        # High-rate cursor ticks must not paint a long thin held-pressure tail
        # while waiting for the next real pressure report.
        fake.pos = (410, 300)
        held = emitter.update(left_mapped=40, right_mapped=0, pressure_fresh=False)
        self.assertEqual(held.state, "hovering")
        self.assertEqual(fake.calls, [])

        fake.pos = (420, 300)
        down = emitter.update(left_mapped=800, right_mapped=0, pressure_fresh=True)
        self.assertEqual(down.state, "contact")
        self.assertEqual((fake.calls[-1]["x"], fake.calls[-1]["y"]), (400, 300))
        self.assertLess(fake.calls[-1]["pressure"], 100)
        self.assertEqual(
            fake.calls[-1]["flags"],
            POINTER_FLAG_NEW
            | POINTER_FLAG_DOWN
            | POINTER_FLAG_INRANGE
            | POINTER_FLAG_INCONTACT
            | POINTER_FLAG_FIRSTBUTTON
            | POINTER_FLAG_PRIMARY,
        )

        catchup = emitter.update(left_mapped=800, right_mapped=0, pressure_fresh=False)
        self.assertEqual(catchup.state, "contact")
        self.assertEqual((fake.calls[-1]["x"], fake.calls[-1]["y"]), (420, 300))
        self.assertGreater(fake.calls[-1]["pressure"], 790)
        catchup_calls = fake.calls[1:]
        self.assertGreaterEqual(len(catchup_calls), 5)
        self.assertEqual(
            [int(call["pressure"]) for call in catchup_calls],
            sorted(int(call["pressure"]) for call in catchup_calls),
        )
        self.assertLessEqual(
            max(
                math.hypot(
                    int(current["x"]) - int(previous["x"]),
                    int(current["y"]) - int(previous["y"]),
                )
                for previous, current in zip(fake.calls, fake.calls[1:])
            ),
            4.0,
        )

        pressures = [int(call["pressure"]) for call in fake.calls[1:]]
        self.assertEqual(pressures, sorted(pressures))
        self.assertGreater(pressures[-1], 790)
        self.assertLessEqual(max(b - a for a, b in zip(pressures, pressures[1:])), 200)

    def test_low_latency_onset_starts_immediately_at_pressure_floor(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        emitter.config.onset_buffer = False
        emitter.config.min_contact_pressure = 123
        fake._lmb = True

        first = emitter.update(left_mapped=40, right_mapped=0, pressure_fresh=True)

        self.assertEqual(first.state, "contact")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["pressure"], 123)
        self.assertFalse(emitter.onset_catchup_pending)

    def test_native_mouse_move_path_is_preserved_between_pen_ticks(self) -> None:
        class _FakeSuppressor:
            def heartbeat(self) -> None:
                return

            def is_lmb_down(self) -> bool:
                return True

            def drain_hardware_positions(self):
                now = time.perf_counter()
                return [
                    (now, 402, 301),
                    (now, 405, 303),
                    (now, 409, 306),
                    (now, 414, 310),
                ]

        emitter, fake = self._mk_emitter(release_teardown=False)
        fake._lmb = True
        emitter._suppressor = _FakeSuppressor()  # type: ignore[assignment]  # noqa: SLF001
        emitter.state = "contact"
        emitter._event_driven_movement = True  # noqa: SLF001
        emitter.contact_warmup_done = True
        emitter.prev_contact_pressure = 400
        emitter._pressure_interp_initialized = True  # noqa: SLF001
        emitter._pressure_interp_value = 400.0  # noqa: SLF001
        emitter._pressure_interp_target = 400.0  # noqa: SLF001

        sample = emitter.update(left_mapped=400, right_mapped=0, pressure_fresh=False)

        self.assertEqual(sample.state, "contact")
        self.assertEqual(
            [(call["x"], call["y"]) for call in fake.calls],
            [(402, 301), (405, 303), (409, 306), (414, 310)],
        )
        intervals = [
            current - previous
            for previous, current in zip(fake.call_times, fake.call_times[1:])
        ]
        self.assertTrue(all(interval >= 0.0001 for interval in intervals))

    def test_event_driven_update_drains_accumulated_motion_batch(self) -> None:
        class _FakeSuppressor:
            def __init__(self) -> None:
                self.max_counts: list[int | None] = []

            def heartbeat(self) -> None:
                return

            def is_lmb_down(self) -> bool:
                return True

            def drain_hardware_positions(self, max_count=None):
                self.max_counts.append(max_count)
                now = time.perf_counter()
                return [
                    (now, 402, 301),
                    (now, 405, 303),
                    (now, 409, 306),
                ]

        emitter, fake = self._mk_emitter(release_teardown=False)
        suppressor = _FakeSuppressor()
        fake._lmb = True
        emitter._suppressor = suppressor  # type: ignore[assignment]  # noqa: SLF001
        emitter._event_driven_movement = True  # noqa: SLF001
        emitter.state = "contact"
        emitter.contact_warmup_done = True
        emitter.prev_contact_pressure = 400
        emitter._last_contact_position = (400, 300)  # noqa: SLF001
        emitter._pressure_interp_initialized = True  # noqa: SLF001
        emitter._pressure_interp_value = 400.0  # noqa: SLF001
        emitter._pressure_interp_start_value = 400.0  # noqa: SLF001
        emitter._pressure_interp_target = 400.0  # noqa: SLF001

        emitter.update(left_mapped=400, right_mapped=0, pressure_fresh=False)

        self.assertEqual(suppressor.max_counts, [None])
        self.assertEqual((fake.calls[-1]["x"], fake.calls[-1]["y"]), (409, 306))

    def test_adjacent_movement_batches_drop_duplicate_and_backtracking_join(self) -> None:
        class _FakeSuppressor:
            def __init__(self) -> None:
                now = time.perf_counter()
                self.batches = [
                    [(now, 402, 301), (now, 409, 306), (now, 414, 310)],
                    [
                        (now, 414, 310),
                        (now, 409, 305),  # delayed feedback spike
                        (now, 418, 313),
                        (now, 423, 316),
                    ],
                    [],
                ]

            def heartbeat(self) -> None:
                return

            def is_lmb_down(self) -> bool:
                return True

            def drain_hardware_positions(self):
                return self.batches.pop(0)

        emitter, fake = self._mk_emitter(release_teardown=False)
        fake._lmb = True
        fake.pos = (423, 316)
        emitter._suppressor = _FakeSuppressor()  # type: ignore[assignment]  # noqa: SLF001
        emitter.state = "contact"
        emitter.contact_warmup_done = True
        emitter.prev_contact_pressure = 400
        emitter._pressure_interp_initialized = True  # noqa: SLF001
        emitter._pressure_interp_value = 400.0  # noqa: SLF001
        emitter._pressure_interp_target = 400.0  # noqa: SLF001

        emitter.update(left_mapped=400, right_mapped=0, pressure_fresh=False)
        first_batch_count = len(fake.calls)
        emitter.update(left_mapped=400, right_mapped=0, pressure_fresh=False)

        joined = [(call["x"], call["y"]) for call in fake.calls[first_batch_count:]]
        self.assertEqual(joined[-1], (423, 316))
        self.assertNotIn((409, 305), joined)
        self.assertEqual(joined, [(418, 313), (423, 316)])

        # An unchanged position and unchanged pressure should not deposit a
        # scheduler-rate stack of extra brush dabs.
        before_stationary = len(fake.calls)
        stationary = emitter.update(left_mapped=400, right_mapped=0, pressure_fresh=False)
        self.assertEqual(len(fake.calls), before_stationary)
        self.assertFalse(stationary.injected)

    def test_midstroke_pressure_changes_are_distributed_over_four_ticks(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        fake._lmb = True
        emitter.state = "contact"
        emitter.contact_warmup_done = True
        emitter.prev_contact_pressure = 100

        emitter.update(left_mapped=100, right_mapped=0, pressure_fresh=True)
        rising: list[int] = []
        for fresh in (True, False, False, False):
            emitter.update(left_mapped=900, right_mapped=0, pressure_fresh=fresh)
            rising.append(int(fake.calls[-1]["pressure"]))

        self.assertEqual(rising, sorted(rising))
        self.assertLessEqual(max(b - a for a, b in zip(rising, rising[1:])), 205)
        self.assertGreater(rising[-1], 890)

        falling: list[int] = []
        for fresh in (True, False, False, False):
            emitter.update(left_mapped=100, right_mapped=0, pressure_fresh=fresh)
            falling.append(int(fake.calls[-1]["pressure"]))

        self.assertEqual(falling, sorted(falling, reverse=True))
        self.assertLessEqual(max(a - b for a, b in zip(falling, falling[1:])), 205)
        self.assertLess(falling[-1], 110)

    def test_contact_pressure_floor_prevents_extended_hairline(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        emitter.config = SyntheticPenConfig(
            **{
                **emitter.config.__dict__,
                "min_contact_pressure": 123,
            }
        )
        fake._lmb = True
        fake.pos = (420, 300)
        emitter.state = "contact"
        emitter.contact_warmup_done = True
        emitter.prev_contact_pressure = 200

        sample = emitter.update(left_mapped=50, right_mapped=0, pressure_fresh=True)

        self.assertEqual(sample.state, "contact")
        self.assertEqual(fake.calls[-1]["pressure"], 123)

    def test_event_driven_pressure_only_tick_is_deferred_until_movement(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        fake._lmb = True
        fake.pos = (360, 240)
        emitter.state = "contact"
        emitter._event_driven_movement = True  # noqa: SLF001
        emitter.contact_warmup_done = True
        emitter.prev_contact_pressure = 300
        emitter._last_contact_position = (420, 300)  # noqa: SLF001
        emitter._pressure_interp_initialized = True  # noqa: SLF001
        emitter._pressure_interp_value = 400.0  # noqa: SLF001
        emitter._pressure_interp_target = 400.0  # noqa: SLF001

        emitter.update(left_mapped=600, right_mapped=0, pressure_fresh=True)

        self.assertEqual(fake.calls, [])
        self.assertEqual(emitter.prev_contact_pressure, 300)
        self.assertEqual(emitter._last_contact_position, (420, 300))  # noqa: SLF001

    def test_event_driven_first_fresh_zero_starts_taper_without_releasing(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        fake._lmb = True
        emitter.state = "contact"
        emitter._event_driven_movement = True  # noqa: SLF001
        emitter.contact_warmup_done = True
        emitter.prev_contact_pressure = 267
        emitter._last_contact_position = (420, 300)  # noqa: SLF001
        emitter._pressure_interp_initialized = True  # noqa: SLF001
        emitter._pressure_interp_value = 267.0  # noqa: SLF001
        emitter._pressure_interp_start_value = 267.0  # noqa: SLF001
        emitter._pressure_interp_target = 267.0  # noqa: SLF001
        emitter._pressure_interp_started_at = time.perf_counter()  # noqa: SLF001

        first_low = emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=True)

        self.assertEqual(first_low.state, "contact")
        self.assertEqual(emitter.low_pressure_fresh_frames, 1)
        self.assertEqual(emitter._pressure_interp_target, 0.0)  # noqa: SLF001
        self.assertEqual(fake.calls, [])

    def test_event_driven_onset_spreads_new_pressure_without_reversal(self) -> None:
        class _FakeSuppressor:
            def heartbeat(self) -> None:
                return

            def is_lmb_down(self) -> bool:
                return True

            def drain_hardware_positions(self):
                return [(time.perf_counter(), 410, 300)]

        emitter, fake = self._mk_emitter(release_teardown=False)
        fake._lmb = True
        emitter._suppressor = _FakeSuppressor()  # type: ignore[assignment]  # noqa: SLF001
        emitter._event_driven_movement = True  # noqa: SLF001
        emitter.state = "contact"
        emitter.onset_catchup_pending = True
        emitter.contact_warmup_done = True
        emitter.prev_contact_pressure = 11
        emitter.contact_start_x = 400
        emitter.contact_start_y = 300
        emitter._last_contact_position = (400, 300)  # noqa: SLF001
        emitter._pressure_interp_initialized = True  # noqa: SLF001
        emitter._pressure_interp_value = 13.0  # noqa: SLF001
        emitter._pressure_interp_start_value = 13.0  # noqa: SLF001
        emitter._pressure_interp_target = 232.0  # noqa: SLF001
        emitter._pressure_interp_started_at = time.perf_counter()  # noqa: SLF001
        emitter._pressure_interp_duration_s = 1.0  # noqa: SLF001

        emitter.update(left_mapped=232, right_mapped=0, pressure_fresh=False)

        pressures = [int(call["pressure"]) for call in fake.calls]
        self.assertTrue(pressures)
        self.assertEqual(pressures, sorted(pressures))
        self.assertGreaterEqual(pressures[-1], 230)
        self.assertEqual(emitter._pressure_interp_value, 232.0)  # noqa: SLF001
        self.assertEqual(emitter._pressure_interp_start_value, 232.0)  # noqa: SLF001
        self.assertEqual(emitter._pressure_interp_target, 232.0)  # noqa: SLF001
        after = emitter._interpolate_pressure(  # noqa: SLF001
            232,
            pressure_fresh=False,
            now=time.perf_counter(),
        )
        self.assertEqual(after, 232)

    def test_event_driven_pressure_interpolation_uses_elapsed_time(self) -> None:
        emitter, _fake = self._mk_emitter(release_teardown=False)
        emitter._event_driven_movement = True  # noqa: SLF001

        initial = emitter._interpolate_pressure(  # noqa: SLF001
            100, pressure_fresh=True, now=1.0
        )
        target_received = emitter._interpolate_pressure(  # noqa: SLF001
            900, pressure_fresh=True, now=1.0 + (1.0 / 60.0)
        )
        ramp = [
            emitter._interpolate_pressure(  # noqa: SLF001
                900,
                pressure_fresh=False,
                now=1.0 + (1.0 / 60.0) + offset,
            )
            for offset in (0.003, 0.006, 0.009, 0.012, 0.018)
        ]

        self.assertEqual(initial, 100)
        self.assertEqual(target_received, 100)
        self.assertEqual(ramp, sorted(ramp))
        self.assertGreater(len(set(ramp)), 3)
        self.assertEqual(ramp[-1], 900)

    def test_event_driven_contact_never_stamps_preinterpolation_pressure(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        fake._lmb = True
        emitter._event_driven_movement = True  # noqa: SLF001

        emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=True)
        candidate = emitter.update(
            left_mapped=464,
            right_mapped=0,
            pressure_fresh=False,
        )
        down = emitter.update(
            left_mapped=464,
            right_mapped=0,
            pressure_fresh=True,
        )

        self.assertEqual(candidate.state, "hovering")
        self.assertEqual(down.state, "contact")
        self.assertEqual(len(fake.calls), 1)
        self.assertLessEqual(
            int(fake.calls[0]["pressure"]),
            int(emitter.config.contact_threshold) + 2,
        )

    def test_long_known_segment_is_subdivided_for_spatial_pressure_ramp(self) -> None:
        emitter, _fake = self._mk_emitter(release_teardown=False)

        dense = emitter._densify_contact_path(  # noqa: SLF001
            [(40, 0)],
            anchor=(0, 0),
            max_spacing_px=4.0,
        )

        self.assertEqual(dense[-1], (40, 0))
        self.assertGreaterEqual(len(dense), 10)
        self.assertTrue(all(x2 > x1 for (x1, _), (x2, _) in zip(dense, dense[1:])))
        self.assertLessEqual(
            max(math.hypot(x2 - x1, y2 - y1) for (x1, y1), (x2, y2) in zip([(0, 0)] + dense, dense)),
            4.0,
        )

    def test_default_contact_path_spacing_is_pixel_dense(self) -> None:
        emitter, _fake = self._mk_emitter(release_teardown=False)

        dense = emitter._densify_contact_path(  # noqa: SLF001
            [(20, 0)],
            anchor=(0, 0),
        )

        self.assertEqual(dense[-1], (20, 0))
        self.assertEqual(len(dense), 20)
        self.assertLessEqual(
            max(
                math.hypot(x2 - x1, y2 - y1)
                for (x1, y1), (x2, y2) in zip([(0, 0)] + dense, dense)
            ),
            1.0,
        )

    def test_contact_point_budget_is_small_for_stable_short_segment(self) -> None:
        emitter, _fake = self._mk_emitter(release_teardown=False)

        budget = emitter._contact_point_budget(  # noqa: SLF001
            [(20, 0)],
            anchor=(0, 0),
            pressure_start=500,
            pressure_end=500,
        )

        self.assertEqual(budget, 8)

    def test_contact_point_budget_expands_for_pressure_and_caps_fast_geometry(self) -> None:
        emitter, _fake = self._mk_emitter(release_teardown=False)

        pressure_budget = emitter._contact_point_budget(  # noqa: SLF001
            [(10, 0)],
            anchor=(0, 0),
            pressure_start=100,
            pressure_end=550,
        )
        geometry_budget = emitter._contact_point_budget(  # noqa: SLF001
            [(500, 0)],
            anchor=(0, 0),
            pressure_start=500,
            pressure_end=500,
        )

        self.assertEqual(pressure_budget, 25)
        self.assertEqual(geometry_budget, 48)

    def test_path_stabilization_softens_wiggle_and_bounds_lag(self) -> None:
        emitter, _fake = self._mk_emitter(release_teardown=False)
        emitter.config.path_stabilization = 60
        raw = [(0, 0), (10, 5), (20, 0), (30, 5)]

        stabilized = emitter._stabilize_contact_path(raw)  # noqa: SLF001

        self.assertEqual(stabilized[0], raw[0])
        self.assertLess(stabilized[1][1], raw[1][1])
        self.assertLess(stabilized[3][1], raw[3][1])
        self.assertLessEqual(
            math.hypot(stabilized[-1][0] - raw[-1][0], stabilized[-1][1] - raw[-1][1]),
            9.2,
        )

    def test_zero_path_stabilization_preserves_raw_points(self) -> None:
        emitter, _fake = self._mk_emitter(release_teardown=False)
        emitter.config.path_stabilization = 0
        raw = [(0, 0), (10, 5), (20, 0)]

        self.assertEqual(emitter._stabilize_contact_path(raw), raw)  # noqa: SLF001

    def test_direct_contact_path_preserves_captured_coordinates_without_densifying(self) -> None:
        emitter, _fake = self._mk_emitter(release_teardown=False)
        emitter._last_contact_position = (0, 0)  # noqa: SLF001
        captured = [(5, 2), (11, 7), (18, 3)]

        direct = emitter._prepare_direct_contact_path(  # noqa: SLF001
            captured,
            endpoint=captured[-1],
        )

        self.assertEqual(direct, captured)

    def test_pressure_influence_compresses_variation_but_preserves_pen_up(self) -> None:
        emitter, _fake = self._mk_emitter(release_teardown=False)
        emitter.config.pressure_influence = 50

        self.assertEqual(emitter._apply_pressure_influence(0), 0)  # noqa: SLF001
        self.assertEqual(emitter._apply_pressure_influence(512), 512)  # noqa: SLF001
        self.assertEqual(emitter._apply_pressure_influence(912), 712)  # noqa: SLF001
        self.assertEqual(emitter._apply_pressure_influence(112), 312)  # noqa: SLF001

    def test_cubic_join_continues_previous_direction_without_prediction(self) -> None:
        emitter, _fake = self._mk_emitter(release_teardown=False)
        emitter._contact_path_direction = (1.0, 0.0)  # noqa: SLF001

        dense = emitter._densify_contact_path(  # noqa: SLF001
            [(20, 20)],
            anchor=(0, 0),
            max_spacing_px=4.0,
        )

        self.assertEqual(dense[-1], (20, 20))
        self.assertGreater(dense[0][0], dense[0][1])
        direction = emitter._contact_path_direction  # noqa: SLF001
        self.assertIsNotNone(direction)
        assert direction is not None
        self.assertAlmostEqual(direction[0], 2**-0.5, places=5)
        self.assertAlmostEqual(direction[1], 2**-0.5, places=5)

    def test_single_zero_pressure_glitch_does_not_release_held_lmb(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        fake._lmb = True
        emitter.state = "contact"
        emitter.contact_warmup_done = True
        emitter.prev_contact_pressure = 800
        emitter._pressure_interp_initialized = True  # noqa: SLF001
        emitter._pressure_interp_value = 800.0  # noqa: SLF001
        emitter._pressure_interp_target = 800.0  # noqa: SLF001

        glitch = emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=True)

        self.assertEqual(glitch.state, "contact")
        self.assertEqual(emitter.low_pressure_fresh_frames, 1)
        self.assertGreater(glitch.pen_1024, 700)
        self.assertFalse(any(call["tag"] == "release_up" for call in fake.calls))

        recovered = emitter.update(left_mapped=800, right_mapped=0, pressure_fresh=True)
        self.assertEqual(recovered.state, "contact")
        self.assertEqual(emitter.low_pressure_fresh_frames, 0)

    def test_pressure_fallback_requires_three_fresh_lows_and_never_stamps_blob(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        fake._lmb = True
        emitter.state = "contact"
        emitter.contact_warmup_done = True
        emitter.prev_contact_pressure = 800

        first = emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=True)
        second = emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=True)
        third = emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=True)

        self.assertEqual(first.state, "contact")
        self.assertEqual(second.state, "contact")
        self.assertEqual(third.state, "idle")
        release_calls = [call for call in fake.calls if str(call["tag"]).startswith("release_")]
        self.assertEqual(len(release_calls), 1)
        self.assertEqual(release_calls[0]["tag"], "release_up")
        self.assertEqual(release_calls[0]["pressure"], 0)


if __name__ == "__main__":
    unittest.main()
