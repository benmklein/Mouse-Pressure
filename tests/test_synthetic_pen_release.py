from __future__ import annotations

import math
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mouse_pressure.bridge.synthetic_pen import (  # noqa: E402
    BUTTON_ANCHOR_WAIT_S,
    CLEAN_STROKE_ENDING_HOLD_S,
    MOUSE_MOVE_ABSOLUTE,
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
    RI_MOUSE_RIGHT_BUTTON_DOWN,
    RI_MOUSE_RIGHT_BUTTON_UP,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MOUSEMOVE,
    WM_RBUTTONDOWN,
    WM_RBUTTONUP,
    _MouseLmbSuppressor,
    SyntheticPenConfig,
    SyntheticPenEmitter,
    map_1024_to_1023,
)


class _FakePen:
    def __init__(self) -> None:
        self._lmb = False
        self._rmb = False
        self.left_clicks = 0
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

    def inject(
        self,
        *,
        flags: int,
        x: int,
        y: int,
        pressure_1024: int,
        tag: str,
        tilt_x: int | None = None,
    ) -> tuple[bool, int]:
        self.call_times.append(time.perf_counter())
        self.calls.append(
            {
                "flags": int(flags),
                "x": int(x),
                "y": int(y),
                "pressure": int(pressure_1024),
                "tilt_x": int(tilt_x or 0),
                "tag": str(tag),
            }
        )
        return True, 0

    def emit_left_click(self) -> None:
        self.left_clicks += 1

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

    def test_debug_mode_controls_trace_recorder_and_verbose_state_logs(self) -> None:
        with tempfile.TemporaryDirectory() as trace_dir:
            lines: list[str] = []
            config = SyntheticPenConfig(
                contact_source="lmb_and_pressure",
                onset_buffer=False,
                trace_dir=trace_dir,
                debug_mode=False,
            )
            emitter = SyntheticPenEmitter(config, log=lines.append)
            fake = _FakePen()
            fake._lmb = True
            emitter.pen = fake  # type: ignore[assignment]

            self.assertIsNone(emitter._trace)  # noqa: SLF001
            emitter.update(left_mapped=400, right_mapped=0, pressure_fresh=True)
            self.assertFalse(any(line.startswith("STATE ") for line in lines))

            emitter.set_debug_mode(True)
            self.assertIsNotNone(emitter._trace)  # noqa: SLF001
            emitter.set_debug_mode(False)
            self.assertIsNone(emitter._trace)  # noqa: SLF001
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

    def test_pressure_drop_never_ends_button_authoritative_contact(self) -> None:
        config = SyntheticPenConfig(
            contact_threshold=12,
            release_threshold=4,
            onset_buffer=False,
        )
        emitter = SyntheticPenEmitter(config, log=lambda _line: None)
        fake = _FakePen()
        fake._lmb = True
        emitter.pen = fake  # type: ignore[assignment]

        started = emitter.update(left_mapped=500, right_mapped=0, pressure_fresh=True)
        low = emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=True)
        recovered = emitter.update(left_mapped=500, right_mapped=0, pressure_fresh=True)

        self.assertEqual(started.state, "contact")
        self.assertEqual(low.state, "contact")
        self.assertEqual(recovered.state, "contact")
        self.assertFalse(any(call["tag"] == "release_up" for call in fake.calls))

        fake._lmb = False
        released = emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=True)
        self.assertEqual(released.state, "idle")
        self.assertEqual(fake.calls[-1]["tag"], "release_up")

    def test_contact_down_uses_physical_anchor_instead_of_stale_pen_cursor(self) -> None:
        class _AnchorSuppressor:
            enabled = True

            def heartbeat(self) -> None:
                pass

            def is_lmb_down(self) -> bool:
                return True

            def is_rmb_down(self) -> bool:
                return False

            def current_hardware_position(self) -> tuple[int, int]:
                return (1531, 324)

            def drain_hardware_positions(self) -> list[tuple[float, int, int]]:
                return []

        emitter, fake = self._mk_emitter(release_teardown=False)
        emitter.config.onset_buffer = False
        fake.pos = (1265, 306)  # stale VMulti-promoted OS cursor position
        emitter._suppressor = _AnchorSuppressor()  # type: ignore[assignment]  # noqa: SLF001

        emitter.update(left_mapped=500, right_mapped=0, pressure_fresh=True)

        self.assertEqual(fake.calls[0]["tag"], "contact")
        self.assertEqual((fake.calls[0]["x"], fake.calls[0]["y"]), (1531, 324))

    def test_low_startup_pressure_does_not_release_button_contact(self) -> None:
        config = SyntheticPenConfig(
            contact_threshold=10,
            release_threshold=6,
            onset_buffer=False,
            min_contact_pressure=358,
        )
        emitter = SyntheticPenEmitter(config, log=lambda _line: None)
        fake = _FakePen()
        fake._lmb = True
        emitter.pen = fake  # type: ignore[assignment]

        started = emitter.update(
            left_mapped=19,
            right_mapped=0,
            pressure_fresh=True,
        )
        startup_low = emitter.update(
            left_mapped=19,
            right_mapped=0,
            pressure_fresh=True,
        )
        risen = emitter.update(
            left_mapped=393,
            right_mapped=0,
            pressure_fresh=True,
        )

        self.assertEqual(started.state, "contact")
        self.assertEqual(startup_low.state, "contact")
        self.assertEqual(risen.state, "contact")
        self.assertFalse(any(call["tag"] == "release_up" for call in fake.calls))

        still_contact = emitter.update(
            left_mapped=19,
            right_mapped=0,
            pressure_fresh=True,
        )
        self.assertEqual(still_contact.state, "contact")
        fake._lmb = False
        released = emitter.update(
            left_mapped=19,
            right_mapped=0,
            pressure_fresh=True,
        )
        self.assertEqual(released.state, "idle")

    def test_button_up_release_never_stamps_endpoint_or_clicks_through(self) -> None:
        config = SyntheticPenConfig(
            contact_threshold=12,
            release_threshold=4,
            onset_buffer=False,
            suppress_lmb=True,
            no_click_through=False,
        )
        emitter = SyntheticPenEmitter(config, log=lambda _line: None)
        emitter._suppressor = None  # type: ignore[assignment]  # noqa: SLF001
        fake = _FakePen()
        fake._lmb = True
        emitter.pen = fake  # type: ignore[assignment]

        emitter.update(left_mapped=500, right_mapped=0, pressure_fresh=True)
        fake.pos = (450, 300)
        low = emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=True)
        fake._lmb = False
        released = emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=True)

        self.assertEqual(low.state, "contact")
        self.assertEqual(released.state, "idle")
        self.assertFalse(
            any(call["tag"] == "release_final_contact" for call in fake.calls)
        )
        self.assertEqual(fake.left_clicks, 0)

    def test_button_up_drains_trailing_positions_before_release(self) -> None:
        class _TrailingMotionSuppressor:
            def __init__(self) -> None:
                self.down = True
                self.positions: list[tuple[float, int, int]] = []

            def heartbeat(self) -> None:
                return

            def is_lmb_down(self) -> bool:
                return self.down

            def is_rmb_down(self) -> bool:
                return False

            def drain_hardware_positions(self):
                positions = self.positions
                self.positions = []
                return positions

        config = SyntheticPenConfig(
            contact_threshold=12,
            release_threshold=4,
            onset_buffer=False,
            min_contact_pressure=100,
        )
        emitter = SyntheticPenEmitter(config, log=lambda _line: None)
        suppressor = _TrailingMotionSuppressor()
        emitter._suppressor = suppressor  # type: ignore[assignment]  # noqa: SLF001
        fake = _FakePen()
        emitter.pen = fake  # type: ignore[assignment]

        emitter.update(left_mapped=500, right_mapped=0, pressure_fresh=True)
        suppressor.positions = [(time.perf_counter(), 410, 300)]
        moving = emitter.update(
            left_mapped=500,
            right_mapped=0,
            pressure_fresh=False,
        )
        requested = emitter.update(
            left_mapped=0,
            right_mapped=0,
            pressure_fresh=True,
        )

        self.assertEqual(moving.state, "contact")
        self.assertEqual(requested.state, "contact")
        self.assertFalse(any(call["tag"] == "release_up" for call in fake.calls))

        suppressor.down = False
        suppressor.positions = [
            (time.perf_counter(), 420, 305),
            (time.perf_counter(), 430, 310),
        ]
        flushed = emitter.update(
            left_mapped=0,
            right_mapped=0,
            pressure_fresh=False,
        )
        self.assertEqual(flushed.state, "contact")
        self.assertEqual((fake.calls[-1]["x"], fake.calls[-1]["y"]), (430, 310))
        self.assertFalse(any(call["tag"] == "release_up" for call in fake.calls))

        released = emitter.update(
            left_mapped=0,
            right_mapped=0,
            pressure_fresh=False,
        )
        self.assertEqual(released.state, "idle")
        self.assertEqual(fake.calls[-1]["tag"], "release_up")
        self.assertEqual((fake.calls[-1]["x"], fake.calls[-1]["y"]), (430, 310))

    def test_low_then_recovered_pressure_preserves_single_contact(self) -> None:
        config = SyntheticPenConfig(
            contact_threshold=10,
            release_threshold=6,
            onset_buffer=False,
            min_contact_pressure=358,
            debug_mode=True,
        )
        emitter = SyntheticPenEmitter(config, log=lambda _line: None)
        fake = _FakePen()
        fake._lmb = True
        emitter.pen = fake  # type: ignore[assignment]

        emitter.update(left_mapped=500, right_mapped=0, pressure_fresh=True)
        low = emitter.update(
            left_mapped=19,
            right_mapped=0,
            pressure_fresh=True,
        )
        self.assertEqual(low.state, "contact")

        recovered = emitter.update(
            left_mapped=393,
            right_mapped=0,
            pressure_fresh=True,
        )
        self.assertEqual(recovered.state, "contact")
        self.assertEqual(
            sum(bool(int(call["flags"]) & POINTER_FLAG_DOWN) for call in fake.calls),
            1,
        )
        self.assertFalse(any(call["tag"] == "release_up" for call in fake.calls))

    def test_captured_pressure_failures_cannot_change_button_contact_topology(
        self,
    ) -> None:
        captured_sequences = {
            "missing_stroke_startup": [19, 19, 393, 531, 621, 688, 704],
            "half_e_transient": [
                86,
                114,
                86,
                59,
                31,
                19,
                118,
                232,
                342,
                417,
                519,
                641,
                712,
                731,
            ],
        }
        for name, pressures in captured_sequences.items():
            with self.subTest(name=name):
                emitter = SyntheticPenEmitter(
                    SyntheticPenConfig(
                        contact_threshold=10,
                        release_threshold=6,
                        onset_buffer=False,
                        min_contact_pressure=358,
                    ),
                    log=lambda _line: None,
                )
                fake = _FakePen()
                fake._lmb = True
                emitter.pen = fake  # type: ignore[assignment]

                for pressure in pressures:
                    sample = emitter.update(
                        left_mapped=pressure,
                        right_mapped=0,
                        pressure_fresh=True,
                    )
                    self.assertEqual(sample.state, "contact")

                self.assertEqual(
                    sum(
                        bool(int(call["flags"]) & POINTER_FLAG_DOWN)
                        for call in fake.calls
                    ),
                    1,
                )
                self.assertFalse(
                    any(call["tag"] == "release_up" for call in fake.calls)
                )

                fake._lmb = False
                released = emitter.update(
                    left_mapped=pressures[-1],
                    right_mapped=0,
                    pressure_fresh=False,
                )
                self.assertEqual(released.state, "idle")
                self.assertEqual(
                    sum(call["tag"] == "release_up" for call in fake.calls),
                    1,
                )

    def test_repeated_zero_pressure_cannot_split_contact_during_same_hold(
        self,
    ) -> None:
        config = SyntheticPenConfig(
            contact_threshold=12,
            release_threshold=4,
            onset_buffer=False,
            suppress_lmb=True,
            no_click_through=False,
        )
        emitter = SyntheticPenEmitter(config, log=lambda _line: None)
        emitter._suppressor = None  # type: ignore[assignment]  # noqa: SLF001
        fake = _FakePen()
        fake._lmb = True
        emitter.pen = fake  # type: ignore[assignment]

        started = emitter.update(
            left_mapped=500,
            right_mapped=0,
            pressure_fresh=True,
        )
        emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=True)
        emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=True)
        still_contact = emitter.update(
            left_mapped=0,
            right_mapped=0,
            pressure_fresh=True,
        )
        rebound = emitter.update(
            left_mapped=500,
            right_mapped=0,
            pressure_fresh=True,
        )

        self.assertEqual(started.state, "contact")
        self.assertEqual(still_contact.state, "contact")
        self.assertEqual(rebound.state, "contact")
        self.assertFalse(
            any(call["tag"] == "release_final_contact" for call in fake.calls)
        )
        self.assertFalse(any(call["tag"] == "release_up" for call in fake.calls))

        fake._lmb = False
        released = emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=True)
        self.assertEqual(released.state, "idle")
        self.assertEqual(fake.left_clicks, 0)

    def test_auxiliary_right_pressure_modifies_xtilt_without_own_stroke(self) -> None:
        config = SyntheticPenConfig(
            contact_threshold=12,
            release_threshold=4,
            onset_buffer=False,
            rmb_aux_xtilt=True,
        )
        emitter = SyntheticPenEmitter(config, log=lambda _line: None)
        self.assertIsNotNone(emitter._suppressor)  # noqa: SLF001
        self.assertFalse(  # noqa: SLF001
            emitter._suppressor._right_button_owns_contact  # type: ignore[union-attr]
        )
        emitter._suppressor = None  # type: ignore[assignment]  # noqa: SLF001
        fake = _FakePen()
        emitter.pen = fake  # type: ignore[assignment]

        fake._rmb = True
        no_stroke = emitter.update(
            left_mapped=0,
            right_mapped=1023,
            pressure_fresh=True,
        )
        self.assertEqual(no_stroke.state, "idle")
        self.assertIsNone(emitter.active_button)
        self.assertEqual(fake.calls, [])

        fake._lmb = True
        stroke = emitter.update(
            left_mapped=400,
            right_mapped=512,
            pressure_fresh=True,
        )
        self.assertEqual(stroke.state, "contact")
        self.assertEqual(emitter.active_button, "left")
        self.assertEqual(fake.calls[-1]["tilt_x"], 30)
        self.assertGreater(fake.calls[-1]["pressure"], 0)

    def test_recent_synthetic_position_is_not_recaptured_as_hardware(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor.mark_injected_position(410, 305)

        now = time.perf_counter()
        self.assertTrue(suppressor._is_recent_injected_position(410, 305, now))  # noqa: SLF001
        self.assertFalse(suppressor._is_recent_injected_position(411, 305, now))  # noqa: SLF001

    def test_raw_direct_motion_can_be_disabled_for_synthetic_backend(self) -> None:
        suppressor = _MouseLmbSuppressor(
            log=lambda _line: None,
            allow_raw_direct_motion=False,
        )

        self.assertFalse(suppressor._raw_direct_mode)  # noqa: SLF001

    def test_timing_observer_receives_hook_and_raw_events_without_affecting_state(
        self,
    ) -> None:
        suppressor = _MouseLmbSuppressor(
            log=lambda _line: None,
            suppress_left=False,
            suppress_right=False,
        )
        observed: list[tuple[str, float, dict[str, int | float | str]]] = []
        suppressor.set_timing_callback(
            lambda kind, at, fields: observed.append((kind, at, fields))
        )
        hook_at = time.perf_counter()
        suppressor._handle_physical_hook_button(  # noqa: SLF001
            WM_LBUTTONDOWN,
            observed_at=hook_at,
            x=123,
            y=456,
        )
        raw = RAWMOUSE()
        raw.usButtonFlags = RI_MOUSE_LEFT_BUTTON_DOWN
        raw.lLastX = 3
        raw.lLastY = -2
        suppressor._handle_raw_mouse(0, raw)  # noqa: SLF001

        self.assertEqual(
            observed[0],
            ("hook_left_down", hook_at, {"x": 123, "y": 456}),
        )
        self.assertEqual(observed[1][0], "raw_mouse")
        self.assertEqual(observed[1][2]["button_flags"], RI_MOUSE_LEFT_BUTTON_DOWN)
        self.assertTrue(suppressor._lmb_down)  # noqa: SLF001

    def test_zero_motion_button_down_wake_is_opt_in_and_channel_scoped(self) -> None:
        suppressor = _MouseLmbSuppressor(
            log=lambda _line: None,
            suppress_left=False,
            suppress_right=False,
        )
        suppressor.enabled = True
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._cursor_position = lambda: (100, 200)  # type: ignore[method-assign]  # noqa: SLF001
        suppressor._get_raw_device_identity = (  # type: ignore[method-assign]  # noqa: SLF001
            lambda _handle: "VID_046D&PID_C54D"
        )
        wake_left = False
        signals: list[str] = []
        suppressor.set_button_down_wake_callback(
            lambda button: wake_left and button == "left"
        )
        suppressor.set_movement_callback(lambda: signals.append("wake"))

        down = RAWMOUSE()
        down.usButtonFlags = RI_MOUSE_LEFT_BUTTON_DOWN
        suppressor._handle_raw_mouse(0xA1, down)  # noqa: SLF001
        self.assertEqual(signals, [])
        self.assertFalse(suppressor._input_ready.is_set())  # noqa: SLF001

        up = RAWMOUSE()
        up.usButtonFlags = RI_MOUSE_LEFT_BUTTON_UP
        suppressor._handle_raw_mouse(0xA1, up)  # noqa: SLF001
        signals.clear()
        suppressor._input_ready.clear()  # noqa: SLF001

        wake_left = True
        suppressor._handle_raw_mouse(0xA1, down)  # noqa: SLF001

        # Raw Input won the queue race, so the experimental path waits for the
        # low-level hook's authoritative desktop anchor before waking output.
        self.assertEqual(signals, [])
        self.assertFalse(suppressor.is_lmb_down())
        suppressor._handle_physical_hook_button(  # noqa: SLF001
            WM_LBUTTONDOWN,
            observed_at=time.perf_counter(),
            x=120,
            y=240,
        )

        self.assertEqual(signals, ["wake"])
        self.assertTrue(suppressor._input_ready.is_set())  # noqa: SLF001
        self.assertTrue(suppressor.is_lmb_down())
        self.assertEqual((suppressor._raw_x, suppressor._raw_y), (120, 240))  # noqa: SLF001
        self.assertEqual(
            suppressor.motion_diagnostics()["anchor_wait_completed"],
            1,
        )

    def test_immediate_wake_consumes_down_packet_motion_at_hook_anchor(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor.enabled = True
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._get_raw_device_identity = (  # type: ignore[method-assign]  # noqa: SLF001
            lambda _handle: "VID_046D&PID_C54D"
        )
        signals: list[str] = []
        suppressor.set_button_down_wake_callback(lambda button: button == "left")
        suppressor.set_movement_callback(lambda: signals.append("wake"))
        suppressor._handle_physical_hook_button(  # noqa: SLF001
            WM_LBUTTONDOWN,
            observed_at=time.perf_counter(),
            x=500,
            y=400,
        )

        down = RAWMOUSE()
        down.usButtonFlags = RI_MOUSE_LEFT_BUTTON_DOWN
        down.lLastX = 3
        down.lLastY = -2
        suppressor._handle_raw_mouse(0xA1, down)  # noqa: SLF001

        self.assertEqual(signals, ["wake"])
        self.assertEqual((suppressor._raw_x, suppressor._raw_y), (500, 400))  # noqa: SLF001
        self.assertEqual(suppressor.drain_hardware_positions(), [])
        self.assertEqual(
            suppressor.motion_diagnostics()["immediate_button_wake"],
            1,
        )

    def test_immediate_wake_anchor_wait_has_bounded_cursor_fallback(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor.enabled = True
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._cursor_position = lambda: (640, 360)  # type: ignore[method-assign]  # noqa: SLF001
        suppressor._get_raw_device_identity = (  # type: ignore[method-assign]  # noqa: SLF001
            lambda _handle: "VID_046D&PID_C54D"
        )
        suppressor.set_button_down_wake_callback(lambda button: button == "left")

        down = RAWMOUSE()
        down.usButtonFlags = RI_MOUSE_LEFT_BUTTON_DOWN
        suppressor._handle_raw_mouse(0xA1, down)  # noqa: SLF001
        suppressor._button_anchor_wait_started_at -= (  # noqa: SLF001
            BUTTON_ANCHOR_WAIT_S + 0.001
        )

        self.assertTrue(suppressor.is_lmb_down())
        self.assertEqual((suppressor._raw_x, suppressor._raw_y), (640, 360))  # noqa: SLF001
        self.assertEqual(
            suppressor.motion_diagnostics()["anchor_wait_timeout"],
            1,
        )

    def test_auxiliary_right_hold_does_not_own_consecutive_left_contacts(self) -> None:
        suppressor = _MouseLmbSuppressor(
            log=lambda _line: None,
            right_button_owns_contact=False,
        )
        suppressor.enabled = True
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._raw_direct_mode = True  # noqa: SLF001
        suppressor._get_raw_device_identity = (  # type: ignore[method-assign]  # noqa: SLF001
            lambda _handle: "VID_046D&PID_C54D"
        )
        suppressor.set_button_down_wake_callback(lambda button: button == "left")

        suppressor._handle_physical_hook_button(  # noqa: SLF001
            WM_RBUTTONDOWN,
            observed_at=time.perf_counter(),
            x=100,
            y=100,
        )
        right_down = RAWMOUSE()
        right_down.usButtonFlags = RI_MOUSE_RIGHT_BUTTON_DOWN
        suppressor._handle_raw_mouse(0xA1, right_down)  # noqa: SLF001

        self.assertTrue(suppressor._rmb_down)  # noqa: SLF001
        self.assertFalse(suppressor._raw_contact_active)  # noqa: SLF001

        suppressor._handle_physical_hook_button(  # noqa: SLF001
            WM_LBUTTONDOWN,
            observed_at=time.perf_counter(),
            x=500,
            y=400,
        )
        left_down = RAWMOUSE()
        left_down.usButtonFlags = RI_MOUSE_LEFT_BUTTON_DOWN
        suppressor._handle_raw_mouse(0xA1, left_down)  # noqa: SLF001
        self.assertTrue(suppressor._raw_contact_active)  # noqa: SLF001
        self.assertEqual((suppressor._raw_x, suppressor._raw_y), (500, 400))  # noqa: SLF001

        move = RAWMOUSE()
        move.lLastX = 10
        move.lLastY = 5
        suppressor._handle_raw_mouse(0xA1, move)  # noqa: SLF001
        self.assertGreater(suppressor._accepted_motion_count, 0)  # noqa: SLF001

        suppressor._handle_physical_hook_button(  # noqa: SLF001
            WM_LBUTTONUP,
            observed_at=time.perf_counter(),
            x=510,
            y=405,
        )
        left_up = RAWMOUSE()
        left_up.usButtonFlags = RI_MOUSE_LEFT_BUTTON_UP
        suppressor._handle_raw_mouse(0xA1, left_up)  # noqa: SLF001

        self.assertTrue(suppressor._rmb_down)  # noqa: SLF001
        self.assertFalse(suppressor._raw_contact_active)  # noqa: SLF001

        suppressor._handle_physical_hook_button(  # noqa: SLF001
            WM_LBUTTONDOWN,
            observed_at=time.perf_counter(),
            x=800,
            y=600,
        )
        suppressor._handle_raw_mouse(0xA1, left_down)  # noqa: SLF001

        self.assertEqual(suppressor._accepted_motion_count, 0)  # noqa: SLF001
        self.assertEqual((suppressor._raw_x, suppressor._raw_y), (800, 600))  # noqa: SLF001

        suppressor._handle_physical_hook_button(  # noqa: SLF001
            WM_RBUTTONUP,
            observed_at=time.perf_counter(),
            x=800,
            y=600,
        )
        right_up = RAWMOUSE()
        right_up.usButtonFlags = RI_MOUSE_RIGHT_BUTTON_UP
        suppressor._handle_raw_mouse(0xA1, right_up)  # noqa: SLF001
        self.assertFalse(suppressor._rmb_down)  # noqa: SLF001

    def test_raw_input_selects_motion_device_and_ignores_other_devices(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._raw_direct_mode = False  # noqa: SLF001
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
        diagnostics = suppressor.motion_diagnostics()
        self.assertEqual(diagnostics["raw_seen"], 3)
        self.assertEqual(diagnostics["raw_selected"], 2)
        self.assertEqual(diagnostics["wrong_device"], 1)
        self.assertEqual(diagnostics["raw_dx"], 7)
        self.assertEqual(diagnostics["raw_dy"], 3)
        self.assertEqual(diagnostics["hook_correlated"], 2)
        self.assertEqual(diagnostics["published"], 2)
        self.assertEqual(diagnostics["duplicate"], 0)

        up = RAWMOUSE()
        up.usButtonFlags = RI_MOUSE_LEFT_BUTTON_UP
        suppressor._handle_raw_mouse(0xA1, up)  # noqa: SLF001
        ignored_after_up = RAWMOUSE()
        ignored_after_up.lLastX = 10
        suppressor._handle_raw_mouse(0xA1, ignored_after_up)  # noqa: SLF001
        self.assertEqual(suppressor.drain_hardware_positions(), [])

    def test_button_anchor_reconciles_idle_raw_drift_at_next_stroke(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._raw_direct_mode = True  # noqa: SLF001
        suppressor._raw_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_motion_device_handle = 0xA1  # noqa: SLF001
        suppressor._selected_raw_identity = "VID_046D&PID_C54D"  # noqa: SLF001
        suppressor._raw_device_identities[0xA1] = "VID_046D&PID_C54D"  # noqa: SLF001
        suppressor._logical_position_initialized = True  # noqa: SLF001
        suppressor._raw_x = 500  # noqa: SLF001
        suppressor._raw_y = 400  # noqa: SLF001

        idle_move = RAWMOUSE()
        idle_move.lLastX = 120
        idle_move.lLastY = -45
        suppressor._handle_raw_mouse(0xA1, idle_move)  # noqa: SLF001

        self.assertEqual(suppressor.drain_hardware_positions(), [])
        self.assertEqual((suppressor._raw_x, suppressor._raw_y), (620, 355))  # noqa: SLF001
        self.assertTrue(suppressor._idle_raw_position_fresh)  # noqa: SLF001

        suppressor._button_anchor = (time.perf_counter(), 615, 360)  # noqa: SLF001
        down = RAWMOUSE()
        down.usButtonFlags = RI_MOUSE_LEFT_BUTTON_DOWN
        suppressor._handle_raw_mouse(0xA1, down)  # noqa: SLF001

        self.assertEqual((suppressor._raw_x, suppressor._raw_y), (615, 360))  # noqa: SLF001
        self.assertFalse(suppressor._idle_raw_position_fresh)  # noqa: SLF001

    def test_late_hook_down_reconciles_raw_input_contact_before_motion(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._raw_direct_mode = True  # noqa: SLF001
        suppressor._raw_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_motion_device_handle = 0xA1  # noqa: SLF001
        suppressor._selected_raw_identity = "VID_046D&PID_C54D"  # noqa: SLF001
        suppressor._raw_device_identities[0xA1] = "VID_046D&PID_C54D"  # noqa: SLF001
        suppressor._logical_position_initialized = True  # noqa: SLF001
        suppressor._idle_raw_position_fresh = True  # noqa: SLF001
        suppressor._raw_x = 900  # noqa: SLF001
        suppressor._raw_y = 500  # noqa: SLF001

        down = RAWMOUSE()
        down.usButtonFlags = RI_MOUSE_LEFT_BUTTON_DOWN
        suppressor._handle_raw_mouse(0xA1, down)  # noqa: SLF001
        self.assertEqual((suppressor._raw_x, suppressor._raw_y), (900, 500))  # noqa: SLF001

        suppressor._handle_physical_hook_button(  # noqa: SLF001
            WM_LBUTTONDOWN,
            observed_at=time.perf_counter(),
            x=1320,
            y=740,
        )

        self.assertEqual((suppressor._raw_x, suppressor._raw_y), (1320, 740))  # noqa: SLF001
        self.assertEqual(suppressor.drain_hardware_positions(), [])
        self.assertEqual(
            suppressor.motion_diagnostics().get("contact_anchor_corrected"),
            1,
        )

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

    def test_hook_up_waits_for_raw_input_up_after_final_motion(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor.enabled = True
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._raw_direct_mode = True  # noqa: SLF001
        suppressor._raw_contact_active = True  # noqa: SLF001
        suppressor._raw_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_motion_device_handle = 0xA1  # noqa: SLF001
        suppressor._selected_raw_identity = "VID_046D&PID_C54D"  # noqa: SLF001
        suppressor._raw_device_identities[0xA1] = "VID_046D&PID_C54D"  # noqa: SLF001
        suppressor._logical_position_initialized = True  # noqa: SLF001
        suppressor._raw_x = 500  # noqa: SLF001
        suppressor._raw_y = 400  # noqa: SLF001
        suppressor._lmb_down = True  # noqa: SLF001

        now = time.perf_counter()
        suppressor._handle_physical_hook_button(  # noqa: SLF001
            WM_LBUTTONUP,
            observed_at=now,
            x=500,
            y=400,
        )
        self.assertTrue(suppressor.is_lmb_down())

        final_packet = RAWMOUSE()
        final_packet.usButtonFlags = RI_MOUSE_LEFT_BUTTON_UP
        final_packet.lLastX = 9
        suppressor._handle_raw_mouse(0xA1, final_packet)  # noqa: SLF001

        self.assertFalse(suppressor.is_lmb_down())
        self.assertEqual(
            [(x, y) for _ts, x, y in suppressor.drain_hardware_positions()],
            [(509, 400)],
        )
        diagnostics = suppressor.motion_diagnostics()
        self.assertEqual(diagnostics["hook_up_deferred"], 1)
        self.assertEqual(diagnostics["raw_up_received"], 1)
        self.assertEqual(diagnostics["hook_up_timeout"], 0)

    def test_hook_up_fails_open_if_raw_input_up_is_lost(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor.enabled = True
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._raw_contact_active = True  # noqa: SLF001
        suppressor._lmb_down = True  # noqa: SLF001

        now = time.perf_counter()
        suppressor._handle_physical_hook_button(  # noqa: SLF001
            WM_LBUTTONUP,
            observed_at=now,
            x=500,
            y=400,
        )
        suppressor._resolve_deferred_hook_releases(  # noqa: SLF001
            now + 0.020
        )

        self.assertFalse(suppressor.is_lmb_down())
        self.assertFalse(suppressor._raw_contact_active)  # noqa: SLF001
        self.assertEqual(
            suppressor.motion_diagnostics()["hook_up_timeout"],
            1,
        )

    def test_native_hook_positions_are_ordered_screen_coordinates(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._raw_direct_mode = False  # noqa: SLF001
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
        suppressor._raw_direct_mode = False  # noqa: SLF001
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

    def test_synthetic_cursor_jump_does_not_displace_logical_physical_path(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._lmb_down = True  # noqa: SLF001
        suppressor._raw_contact_active = True  # noqa: SLF001
        suppressor._raw_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_motion_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_x = 100  # noqa: SLF001
        suppressor._raw_y = 100  # noqa: SLF001
        suppressor._logical_position_initialized = True  # noqa: SLF001
        suppressor._cursor_baseline_x = 100  # noqa: SLF001
        suppressor._cursor_baseline_y = 100  # noqa: SLF001
        suppressor._cursor_baseline_initialized = True  # noqa: SLF001

        suppressor._handle_native_mouse_move(  # noqa: SLF001
            time.perf_counter(), 110, 100, injected=False
        )
        first = RAWMOUSE()
        first.lLastX = 10
        suppressor._handle_raw_mouse(0xA1, first)  # noqa: SLF001
        self.assertEqual(
            [(x, y) for _ts, x, y in suppressor.drain_hardware_positions()],
            [(110, 100)],
        )

        suppressor.mark_injected_position(300, 300)
        suppressor._handle_native_mouse_move(  # noqa: SLF001
            time.perf_counter(), 300, 300, injected=True
        )
        suppressor._handle_native_mouse_move(  # noqa: SLF001
            time.perf_counter(), 305, 300, injected=False
        )
        second = RAWMOUSE()
        second.lLastX = 5
        suppressor._handle_raw_mouse(0xA1, second)  # noqa: SLF001

        self.assertEqual(
            [(x, y) for _ts, x, y in suppressor.drain_hardware_positions()],
            [(115, 100)],
        )

    def test_unmarked_vmulti_feedback_does_not_reverse_next_physical_delta(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._raw_direct_mode = False  # noqa: SLF001
        suppressor._lmb_down = True  # noqa: SLF001
        suppressor._raw_contact_active = True  # noqa: SLF001
        suppressor._raw_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_motion_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_x = 300  # noqa: SLF001
        suppressor._raw_y = 500  # noqa: SLF001
        suppressor._logical_position_initialized = True  # noqa: SLF001
        suppressor._cursor_baseline_x = 300  # noqa: SLF001
        suppressor._cursor_baseline_y = 500  # noqa: SLF001
        suppressor._cursor_baseline_initialized = True  # noqa: SLF001

        # The VMulti report is promoted as an unmarked hardware hook event,
        # rounded by one pixel. It must update only the OS baseline.
        suppressor.mark_injected_position(320, 520)
        suppressor._handle_native_mouse_move(  # noqa: SLF001
            time.perf_counter(), 321, 519, injected=False
        )
        self.assertEqual((suppressor._raw_x, suppressor._raw_y), (300, 500))  # noqa: SLF001

        # The next verified physical move is upward and must remain upward.
        suppressor._handle_native_mouse_move(  # noqa: SLF001
            time.perf_counter(), 321, 509, injected=False
        )
        raw = RAWMOUSE()
        raw.lLastY = -10
        suppressor._handle_raw_mouse(0xA1, raw)  # noqa: SLF001

        self.assertEqual(
            [(x, y) for _ts, x, y in suppressor.drain_hardware_positions()],
            [(300, 490)],
        )

    def test_foreign_virtual_button_cannot_replace_selected_raw_mouse(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor._raw_contact_active = True  # noqa: SLF001
        suppressor._raw_device_handle = 0xA1  # noqa: SLF001
        suppressor._selected_raw_identity = "VID_046D&PID_C54D"  # noqa: SLF001
        suppressor._raw_device_identities[0xB2] = "VID_1234&PID_5678"  # noqa: SLF001

        promoted = RAWMOUSE()
        promoted.usButtonFlags = RI_MOUSE_LEFT_BUTTON_DOWN
        suppressor._handle_raw_mouse(0xB2, promoted)  # noqa: SLF001

        self.assertEqual(suppressor._raw_device_handle, 0xA1)  # noqa: SLF001
        self.assertEqual(  # noqa: SLF001
            suppressor._selected_raw_identity,
            "VID_046D&PID_C54D",
        )

    def test_one_to_one_raw_mode_uses_only_selected_device_deltas(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._raw_direct_mode = True  # noqa: SLF001
        suppressor._lmb_down = True  # noqa: SLF001
        suppressor._raw_contact_active = True  # noqa: SLF001
        suppressor._raw_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_motion_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_x = 500  # noqa: SLF001
        suppressor._raw_y = 400  # noqa: SLF001
        suppressor._logical_position_initialized = True  # noqa: SLF001

        # A large virtual-pen cursor promotion must not alter the path.
        suppressor.mark_injected_position(900, 700)
        suppressor._handle_native_mouse_move(  # noqa: SLF001
            time.perf_counter(), 900, 700, injected=False
        )

        physical = RAWMOUSE()
        physical.lLastX = 12
        physical.lLastY = -7
        suppressor._handle_raw_mouse(0xA1, physical)  # noqa: SLF001

        self.assertEqual(
            [(x, y) for _ts, x, y in suppressor.drain_hardware_positions()],
            [(512, 393)],
        )
        diagnostics = suppressor.motion_diagnostics()
        self.assertEqual(diagnostics.get("raw_direct"), 1)
        self.assertEqual(diagnostics.get("pen_feedback_filtered"), 1)

    def test_absolute_raw_pointer_feedback_is_never_used_as_mouse_delta(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._raw_direct_mode = True  # noqa: SLF001
        suppressor._lmb_down = True  # noqa: SLF001
        suppressor._raw_contact_active = True  # noqa: SLF001
        suppressor._raw_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_motion_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_x = 2750  # noqa: SLF001
        suppressor._raw_y = 436  # noqa: SLF001

        promoted = RAWMOUSE()
        promoted.usFlags = MOUSE_MOVE_ABSOLUTE
        promoted.lLastX = 2751
        promoted.lLastY = 436
        suppressor._handle_raw_mouse(0xA1, promoted)  # noqa: SLF001

        self.assertEqual(suppressor.drain_hardware_positions(), [])
        self.assertEqual((suppressor._raw_x, suppressor._raw_y), (2750, 436))  # noqa: SLF001
        diagnostics = suppressor.motion_diagnostics()
        self.assertEqual(diagnostics.get("raw_absolute_ignored"), 1)
        self.assertEqual(diagnostics.get("raw_selected"), 0)

    def test_unflagged_desktop_coordinate_echo_is_not_used_as_mouse_delta(self) -> None:
        suppressor = _MouseLmbSuppressor(log=lambda _line: None)
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._raw_direct_mode = True  # noqa: SLF001
        suppressor._lmb_down = True  # noqa: SLF001
        suppressor._raw_contact_active = True  # noqa: SLF001
        suppressor._raw_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_motion_device_handle = 0xA1  # noqa: SLF001
        suppressor._raw_x = 2750  # noqa: SLF001
        suppressor._raw_y = 436  # noqa: SLF001
        suppressor._logical_position_initialized = True  # noqa: SLF001

        promoted = RAWMOUSE()
        promoted.lLastX = 2751
        promoted.lLastY = 436
        suppressor._handle_raw_mouse(0xA1, promoted)  # noqa: SLF001

        self.assertEqual(suppressor.drain_hardware_positions(), [])
        self.assertEqual((suppressor._raw_x, suppressor._raw_y), (2750, 436))  # noqa: SLF001
        self.assertEqual(
            suppressor.motion_diagnostics().get("raw_absolute_ignored"),
            1,
        )

    def test_driver_injected_move_is_accepted_when_not_our_pen_feedback(self) -> None:
        lines: list[str] = []
        suppressor = _MouseLmbSuppressor(log=lines.append)
        suppressor._raw_input_active = True  # noqa: SLF001
        suppressor._raw_direct_mode = False  # noqa: SLF001
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
        suppressor._raw_direct_mode = False  # noqa: SLF001
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

    def test_release_does_not_repeat_pressure_at_same_endpoint(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        emitter.state = "contact"
        emitter.contact_frame_no = 5
        emitter.prev_contact_pressure = 400
        emitter._last_contact_position = fake.pos  # noqa: SLF001
        fake._lmb = False

        sample = emitter.update(left_mapped=400, right_mapped=0)

        self.assertEqual(sample.state, "idle")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["tag"], "release_up")
        self.assertEqual(fake.calls[0]["pressure"], 0)

    def test_low_latency_release_uses_last_emitted_point_without_final_dab(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        emitter.config.true_low_latency = True
        emitter.state = "contact"
        emitter.contact_frame_no = 5
        emitter.prev_contact_pressure = 400
        emitter._last_contact_position = (390, 295)  # noqa: SLF001
        fake.pos = (400, 300)
        fake._lmb = False

        sample = emitter.update(left_mapped=400, right_mapped=0)

        self.assertEqual(sample.state, "idle")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["tag"], "release_up")
        self.assertEqual((fake.calls[0]["x"], fake.calls[0]["y"]), (390, 295))

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
        emitter._event_driven_movement = True  # noqa: SLF001
        fake._lmb = True

        first = emitter.update(left_mapped=40, right_mapped=0, pressure_fresh=True)

        self.assertEqual(first.state, "contact")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["pressure"], 123)
        self.assertFalse(emitter.onset_catchup_pending)
        visible_floor = map_1024_to_1023(123)
        self.assertEqual(emitter._pressure_interp_value, visible_floor)  # noqa: SLF001
        self.assertEqual(  # noqa: SLF001
            emitter._pressure_interp_start_value,
            visible_floor,
        )
        self.assertGreaterEqual(  # noqa: SLF001
            emitter._pressure_interp_target,
            visible_floor,
        )

        # A newer hardware sample must ramp from the pressure already visible
        # in Krita, rather than from a hidden zero state below the floor.
        emitter.update(left_mapped=400, right_mapped=0, pressure_fresh=True)
        self.assertGreaterEqual(  # noqa: SLF001
            emitter._pressure_interp_value,
            visible_floor,
        )
        self.assertGreaterEqual(  # noqa: SLF001
            emitter._pressure_interp_start_value,
            visible_floor,
        )
        self.assertEqual(emitter._pressure_interp_target, 400.0)  # noqa: SLF001

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
        self.assertEqual(emitter._pressure_interp_target, 0.0)  # noqa: SLF001
        self.assertEqual(fake.calls, [])

    def test_opted_in_stationary_pressure_change_repaints_current_point(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        emitter.config.stationary_pressure_updates = True
        emitter.config.true_low_latency = True
        emitter._event_driven_movement = True  # noqa: SLF001
        fake._lmb = True
        emitter.state = "contact"
        emitter.contact_warmup_done = True
        emitter.prev_contact_pressure = 400
        emitter._last_contact_position = fake.pos  # noqa: SLF001
        emitter._pressure_interp_initialized = True  # noqa: SLF001
        emitter._pressure_interp_value = 400.0  # noqa: SLF001
        emitter._pressure_interp_target = 400.0  # noqa: SLF001
        emitter._stationary_anchor_started_at = time.perf_counter() - 0.1  # noqa: SLF001
        emitter._stationary_dab_emitted = True  # noqa: SLF001

        sample = emitter.update(left_mapped=700, right_mapped=0, pressure_fresh=True)

        self.assertEqual(sample.state, "contact")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(
            [(call["x"], call["y"]) for call in fake.calls],
            [(fake.pos[0] + 1, fake.pos[1]), fake.pos],
        )
        self.assertGreaterEqual(fake.calls[-1]["pressure"], 700)

    def test_opted_in_stationary_down_emits_closed_dab_path(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        emitter.config.stationary_pressure_updates = True
        emitter.config.onset_buffer = False
        emitter._event_driven_movement = True  # noqa: SLF001
        fake._lmb = True

        sample = emitter.update(left_mapped=400, right_mapped=0, pressure_fresh=True)

        self.assertEqual(sample.state, "contact")
        self.assertEqual([call["tag"] for call in fake.calls], ["contact"])

        emitter._stationary_anchor_started_at = time.perf_counter() - 0.1  # noqa: SLF001
        emitter.update(left_mapped=400, right_mapped=0, pressure_fresh=False)

        self.assertEqual(
            [call["tag"] for call in fake.calls],
            ["contact", "stationary_contact", "stationary_contact"],
        )
        self.assertEqual(
            [(call["x"], call["y"]) for call in fake.calls],
            [fake.pos, (fake.pos[0] + 1, fake.pos[1]), fake.pos],
        )
        self.assertTrue(int(fake.calls[0]["flags"]) & POINTER_FLAG_DOWN)
        self.assertTrue(int(fake.calls[1]["flags"]) & POINTER_FLAG_UPDATE)

    def test_stationary_pressure_option_ignores_small_sensor_jitter(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        emitter.config.stationary_pressure_updates = True
        emitter.config.true_low_latency = True
        emitter._event_driven_movement = True  # noqa: SLF001
        fake._lmb = True
        emitter.state = "contact"
        emitter.contact_warmup_done = True
        emitter.prev_contact_pressure = 400
        emitter._last_contact_position = fake.pos  # noqa: SLF001
        emitter._pressure_interp_initialized = True  # noqa: SLF001
        emitter._pressure_interp_value = 400.0  # noqa: SLF001
        emitter._pressure_interp_target = 400.0  # noqa: SLF001
        emitter._stationary_anchor_started_at = time.perf_counter() - 0.1  # noqa: SLF001
        emitter._stationary_dab_emitted = True  # noqa: SLF001

        emitter.update(left_mapped=405, right_mapped=0, pressure_fresh=True)

        self.assertEqual(fake.calls, [])
        self.assertEqual(emitter.prev_contact_pressure, 400)

    def test_right_stationary_option_repaints_auxiliary_xtilt_change(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        emitter.config.rmb_aux_xtilt = True
        emitter.config.right_stationary_pressure_updates = True
        emitter.config.true_low_latency = True
        emitter._event_driven_movement = True  # noqa: SLF001
        fake._lmb = True
        emitter.state = "contact"
        emitter.active_button = "left"
        emitter.contact_warmup_done = True
        emitter.prev_contact_pressure = 400
        emitter._last_contact_position = fake.pos  # noqa: SLF001
        emitter._pressure_interp_initialized = True  # noqa: SLF001
        emitter._pressure_interp_value = 400.0  # noqa: SLF001
        emitter._pressure_interp_target = 400.0  # noqa: SLF001
        emitter._stationary_anchor_started_at = time.perf_counter() - 0.1  # noqa: SLF001
        emitter._stationary_dab_emitted = True  # noqa: SLF001

        emitter.update(left_mapped=400, right_mapped=512, pressure_fresh=True)

        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[-1]["pressure"], 400)
        self.assertEqual(fake.calls[-1]["tilt_x"], 30)
        self.assertEqual(
            [(call["x"], call["y"]) for call in fake.calls],
            [(fake.pos[0] + 1, fake.pos[1]), fake.pos],
        )

    def test_real_motion_cancels_stationary_dab_during_active_stroke(self) -> None:
        class _MovingSuppressor:
            def heartbeat(self) -> None:
                return

            def is_lmb_down(self) -> bool:
                return True

            def drain_hardware_positions(self):
                return [(time.perf_counter(), 420, 300)]

        emitter, fake = self._mk_emitter(release_teardown=False)
        emitter.config.stationary_pressure_updates = True
        emitter.config.true_low_latency = True
        emitter._event_driven_movement = True  # noqa: SLF001
        emitter._suppressor = _MovingSuppressor()  # type: ignore[assignment]  # noqa: SLF001
        fake._lmb = True
        emitter.state = "contact"
        emitter.contact_warmup_done = True
        emitter.prev_contact_pressure = 400
        emitter._last_contact_position = fake.pos  # noqa: SLF001
        emitter._stationary_anchor_started_at = time.perf_counter() - 0.1  # noqa: SLF001

        emitter.update(left_mapped=700, right_mapped=0, pressure_fresh=True)

        self.assertEqual(len(fake.calls), 1)
        self.assertEqual((fake.calls[0]["x"], fake.calls[0]["y"]), (420, 300))
        self.assertNotEqual(fake.calls[0]["tag"], "stationary_contact")
        self.assertFalse(emitter._stationary_dab_emitted)  # noqa: SLF001

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

    def test_direct_onset_ramp_interpolates_pressure_without_bending_path(self) -> None:
        class _FakeSuppressor:
            def heartbeat(self) -> None:
                return

            def is_lmb_down(self) -> bool:
                return True

            def drain_hardware_positions(self):
                # Fast motion can advance much farther than the old 32-pixel
                # onset window before a newer pressure report is available.
                return [(time.perf_counter(), 460, 300)]

        emitter, fake = self._mk_emitter(release_teardown=False)
        emitter.config.onset_buffer = False
        emitter.config.path_stabilization = 0
        emitter._event_driven_movement = True  # noqa: SLF001
        emitter._suppressor = _FakeSuppressor()  # type: ignore[assignment]  # noqa: SLF001
        fake._lmb = True
        emitter.state = "contact"
        emitter.contact_warmup_done = True
        emitter.contact_start_x = 400
        emitter.contact_start_y = 300
        emitter.prev_contact_pressure = 200
        emitter._last_contact_position = (400, 300)  # noqa: SLF001
        emitter._pressure_interp_initialized = True  # noqa: SLF001
        emitter._pressure_interp_value = 800.0  # noqa: SLF001
        emitter._pressure_interp_start_value = 800.0  # noqa: SLF001
        emitter._pressure_interp_target = 800.0  # noqa: SLF001

        emitter.update(left_mapped=800, right_mapped=0, pressure_fresh=False)

        self.assertGreater(len(fake.calls), 2)
        self.assertEqual((fake.calls[-1]["x"], fake.calls[-1]["y"]), (460, 300))
        self.assertTrue(all(int(call["y"]) == 300 for call in fake.calls))
        x_values = [int(call["x"]) for call in fake.calls]
        pressures = [int(call["pressure"]) for call in fake.calls]
        self.assertEqual(x_values, sorted(x_values))
        self.assertEqual(pressures, sorted(pressures))
        self.assertGreater(pressures[-1], pressures[0])

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

    def test_low_latency_direct_path_keeps_only_newest_coordinate(self) -> None:
        emitter, _fake = self._mk_emitter(release_teardown=False)
        emitter._last_contact_position = (0, 0)  # noqa: SLF001
        captured = [(5, 2), (11, 7), (18, 3)]

        direct = emitter._prepare_direct_contact_path(  # noqa: SLF001
            captured,
            endpoint=captured[-1],
            latest_only=True,
        )

        self.assertEqual(direct, [captured[-1]])

    def test_low_latency_pressure_bypasses_interpolation(self) -> None:
        emitter, _fake = self._mk_emitter(release_teardown=False)
        emitter._pressure_interp_initialized = True  # noqa: SLF001
        emitter._pressure_interp_value = 100.0  # noqa: SLF001
        emitter._pressure_interp_target = 100.0  # noqa: SLF001

        pressure = emitter._interpolate_pressure(  # noqa: SLF001
            900,
            pressure_fresh=True,
            instant=True,
            now=1.0,
        )

        self.assertEqual(pressure, 900)
        self.assertEqual(emitter._pressure_interp_value, 900.0)  # noqa: SLF001
        self.assertEqual(emitter._pressure_interp_remaining, 0)  # noqa: SLF001

    def test_pressure_influence_compresses_variation_but_preserves_pen_up(self) -> None:
        emitter, _fake = self._mk_emitter(release_teardown=False)
        emitter.config.pressure_influence = 50

        self.assertEqual(emitter._apply_pressure_influence(0), 0)  # noqa: SLF001
        self.assertEqual(emitter._apply_pressure_influence(512), 512)  # noqa: SLF001
        self.assertEqual(emitter._apply_pressure_influence(912), 712)  # noqa: SLF001
        self.assertEqual(emitter._apply_pressure_influence(112), 312)  # noqa: SLF001

    def test_clean_stroke_endings_discards_a_short_release_pressure_ramp(self) -> None:
        emitter, _fake = self._mk_emitter(release_teardown=False)

        self.assertEqual(
            emitter._apply_clean_stroke_ending(800, enabled=True, pressure_fresh=True, button_down=True, now=1.0),  # noqa: SLF001
            800,
        )
        self.assertEqual(
            emitter._apply_clean_stroke_ending(250, enabled=True, pressure_fresh=True, button_down=True, now=1.010),  # noqa: SLF001
            800,
        )
        self.assertEqual(
            emitter._apply_clean_stroke_ending(0, enabled=True, pressure_fresh=True, button_down=False, now=1.015),  # noqa: SLF001
            800,
        )

    def test_clean_stroke_endings_commits_an_intentional_pressure_drop(self) -> None:
        emitter, _fake = self._mk_emitter(release_teardown=False)
        emitter._apply_clean_stroke_ending(800, enabled=True, pressure_fresh=True, button_down=True, now=2.0)  # noqa: SLF001

        held = emitter._apply_clean_stroke_ending(300, enabled=True, pressure_fresh=True, button_down=True, now=2.001)  # noqa: SLF001
        committed = emitter._apply_clean_stroke_ending(300, enabled=True, pressure_fresh=False, button_down=True, now=2.001 + CLEAN_STROKE_ENDING_HOLD_S + 0.000001)  # noqa: SLF001

        self.assertEqual(held, 800)
        self.assertEqual(committed, 300)

    def test_clean_stroke_endings_keeps_pressure_increases_immediate(self) -> None:
        emitter, _fake = self._mk_emitter(release_teardown=False)
        emitter._apply_clean_stroke_ending(300, enabled=True, pressure_fresh=True, button_down=True, now=3.0)  # noqa: SLF001

        increased = emitter._apply_clean_stroke_ending(700, enabled=True, pressure_fresh=True, button_down=True, now=3.001)  # noqa: SLF001

        self.assertEqual(increased, 700)

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
        self.assertGreater(glitch.pen_1024, 0)
        self.assertFalse(any(call["tag"] == "release_up" for call in fake.calls))

        recovered = emitter.update(left_mapped=800, right_mapped=0, pressure_fresh=True)
        self.assertEqual(recovered.state, "contact")

    def test_repeated_fresh_lows_wait_for_button_up(self) -> None:
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
        self.assertEqual(third.state, "contact")
        self.assertFalse(any(call["tag"] == "release_up" for call in fake.calls))

        fake._lmb = False
        released = emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=False)
        self.assertEqual(released.state, "idle")
        release_calls = [call for call in fake.calls if str(call["tag"]).startswith("release_")]
        self.assertEqual(len(release_calls), 1)
        self.assertEqual(release_calls[0]["tag"], "release_up")
        self.assertEqual(release_calls[0]["pressure"], 0)

    def test_held_contact_zero_respects_floor_until_pen_up(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        emitter.config.min_contact_pressure = 154
        emitter.config.true_low_latency = True
        emitter._event_driven_movement = True  # noqa: SLF001
        fake._lmb = True
        emitter.state = "contact"
        emitter.contact_warmup_done = True
        emitter.prev_contact_pressure = 400
        emitter._last_contact_position = (400, 300)  # noqa: SLF001

        class _MovingSuppressor:
            def __init__(self) -> None:
                self.x = 400
                self.moves_remaining = 3
                self.down = True

            def heartbeat(self) -> None:
                return

            def is_lmb_down(self) -> bool:
                return self.down

            def drain_hardware_positions(self):
                if self.moves_remaining <= 0:
                    return []
                self.moves_remaining -= 1
                self.x += 20
                return [(time.perf_counter(), self.x, 300)]

        emitter._suppressor = _MovingSuppressor()  # type: ignore[assignment]  # noqa: SLF001

        first = emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=True)
        second = emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=True)
        third = emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=True)

        self.assertEqual(first.state, "contact")
        self.assertEqual(second.state, "contact")
        self.assertEqual(third.state, "contact")
        emitter._suppressor.down = False  # type: ignore[union-attr]  # noqa: SLF001
        released = emitter.update(left_mapped=0, right_mapped=0, pressure_fresh=False)
        self.assertEqual(released.state, "idle")
        contact_calls = [
            call for call in fake.calls if int(call["flags"]) & POINTER_FLAG_INCONTACT
        ]
        self.assertTrue(contact_calls)
        self.assertTrue(
            all(int(call["pressure"]) >= 154 for call in contact_calls),
            contact_calls,
        )
        self.assertEqual(fake.calls[-1]["tag"], "release_up")
        self.assertEqual(fake.calls[-1]["pressure"], 0)


if __name__ == "__main__":
    unittest.main()
