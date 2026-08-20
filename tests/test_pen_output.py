from __future__ import annotations

from mouse_pressure.bridge.pen_output import PenOutput
from mouse_pressure.bridge.synthetic_pen import SyntheticPenConfig


class _Emitter:
    def __init__(self, config, _log) -> None:
        self.config = config
        self.events: list[str] = []
        self.pen = None
        self.native_input_capture = None
        self.movement_callback = None
        self.force_stop_callback = None

    def set_native_input_capture(self, capture) -> None:
        self.native_input_capture = capture

    def set_movement_callback(self, callback) -> None:
        self.movement_callback = callback
        self.events.append("movement_attached" if callback else "movement_detached")

    def set_force_stop_callback(self, callback) -> None:
        self.force_stop_callback = callback
        self.events.append("force_stop_attached" if callback else "force_stop_detached")

    def open_unarmed(self) -> None:
        self.events.append("open_unarmed")

    def arm_input(self) -> None:
        self.events.append("arm")

    def update(self, **_fields):
        self.events.append("update")
        return None

    def set_debug_mode(self, enabled: bool) -> None:
        self.events.append(f"debug={int(enabled)}")

    def sync_button_modes(self) -> None:
        self.events.append("sync_modes")

    def fail_open(self, reason: str) -> None:
        self.events.append(f"fail_open={reason}")

    def release(self) -> None:
        self.events.append("release")

    def close(self) -> None:
        self.events.append("close")


class _DetachFailureEmitter(_Emitter):
    def set_movement_callback(self, callback) -> None:
        if callback is None:
            raise RuntimeError("detach failed")
        super().set_movement_callback(callback)


def _make_output(emitter_type=_Emitter):
    holder = {}

    def factory(config, log):
        emitter = emitter_type(config, log)
        holder["emitter"] = emitter
        return emitter

    output = PenOutput(
        SyntheticPenConfig(),
        lambda _line: None,
        emitter_factory=factory,
        injector_factory=lambda _log: object(),
        capture_factory=lambda _log: object(),
        movement_callback=lambda: None,
        force_stop_callback=lambda _reason: None,
    )
    return output, holder


def test_deferred_output_arms_only_after_the_first_update() -> None:
    output, holder = _make_output()
    emitter = holder["emitter"]

    output.open()
    assert output.ready is False
    output.update(100, 0)
    assert output.ready is True

    assert emitter.events[:5] == [
        "movement_attached",
        "force_stop_attached",
        "open_unarmed",
        "update",
        "arm",
    ]


def test_reconfigure_and_close_preserve_lifecycle_order() -> None:
    output, holder = _make_output()
    emitter = holder["emitter"]
    output.open()
    replacement = SyntheticPenConfig(debug_mode=False, suppress_rmb=True)

    output.reconfigure(replacement)
    output.fail_open("stalled")
    output.close()
    output.close()

    assert emitter.config is replacement
    assert emitter.events[-7:] == [
        "debug=0",
        "sync_modes",
        "fail_open=stalled",
        "movement_detached",
        "force_stop_detached",
        "release",
        "close",
    ]


def test_close_reaches_the_native_adapter_when_callback_detach_fails() -> None:
    output, holder = _make_output(_DetachFailureEmitter)

    try:
        output.close()
    except RuntimeError as exc:
        assert str(exc) == "detach failed"
    else:
        raise AssertionError("callback detach should fail")

    assert holder["emitter"].events[-1] == "close"
