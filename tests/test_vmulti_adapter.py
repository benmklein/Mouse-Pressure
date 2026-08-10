from __future__ import annotations

from dataclasses import dataclass

from superstrike_pressure.bridge.tablet_emitter import (
    POINTER_FLAG_DOWN,
    POINTER_FLAG_INCONTACT,
    POINTER_FLAG_INRANGE,
    POINTER_FLAG_UP,
    VMultiPenEmitter,
    VMultiPenInjectorAdapter,
    WriteResult,
    enumerate_vmulti_candidates,
    resolve_vmulti_path,
)


class _DesktopInput:
    def get_cursor_pos(self) -> tuple[int, int]:
        return (320, 240)

    def is_lmb_down(self) -> bool:
        return True

    def is_rmb_down(self) -> bool:
        return False

    def emit_left_click(self) -> None:
        pass

    def emit_right_click(self) -> None:
        pass


@dataclass
class _FakeVMulti:
    screen_left: int = 0
    screen_top: int = 0
    screen_w: int = 1920
    screen_h: int = 1080
    last_report: dict | None = None
    reports: list[dict] | None = None

    def emit_report(self, **report):
        self.last_report = report
        if self.reports is None:
            self.reports = []
        self.reports.append(report)
        return WriteResult(method="write", wrote=65, bytes_sent=[])

    def send_out_of_range(self, **_kwargs) -> None:
        pass

    def close(self) -> None:
        pass


def test_project_owned_vmulti_identity_and_control_collection_are_preferred(
    monkeypatch,
) -> None:
    pen_path = rb"\\?\hid#vid_f055&pid_0001&col01#pen"
    control_path = rb"\\?\hid#vid_f055&pid_0001&col02#control"
    monkeypatch.setattr(
        "superstrike_pressure.bridge.tablet_emitter.hid.enumerate",
        lambda: [
            {
                "path": pen_path,
                "vendor_id": 0xF055,
                "product_id": 0x0001,
                "usage_page": 0x000D,
                "usage": 0x0002,
                "manufacturer_string": "Superstrike",
                "product_string": "Superstrike VMulti Virtual Pen",
            },
            {
                "path": control_path,
                "vendor_id": 0xF055,
                "product_id": 0x0001,
                "usage_page": 0xFF00,
                "usage": 0x0001,
                "manufacturer_string": "Superstrike",
                "product_string": "Superstrike VMulti Virtual Pen",
            },
        ],
    )

    candidates = enumerate_vmulti_candidates()

    assert {candidate.path for candidate in candidates} == {pen_path, control_path}
    assert resolve_vmulti_path(requested_path=None, log=lambda _line: None) == control_path


def test_legacy_vmulti_identity_remains_available(monkeypatch) -> None:
    legacy_path = rb"\\?\hid#vid_00ff&pid_bacc&col05#legacy"
    monkeypatch.setattr(
        "superstrike_pressure.bridge.tablet_emitter.hid.enumerate",
        lambda: [
            {
                "path": legacy_path,
                "vendor_id": 0x00FF,
                "product_id": 0xBACC,
                "usage_page": 0xFF00,
                "usage": 0x0001,
            }
        ],
    )

    candidates = enumerate_vmulti_candidates()

    assert [candidate.path for candidate in candidates] == [legacy_path]


def test_vmulti_format_a_places_signed_tilt_in_descriptor_bytes() -> None:
    emitter = VMultiPenEmitter(
        device_path=None,
        write_mode="auto",
        log=lambda _line: None,
    )

    packet = emitter._build_control_report(  # noqa: SLF001
        report_format="format_a",
        status=0x11,
        x=100,
        y=200,
        pressure=300,
        tilt_x=85,
        tilt_y=-20,
    )

    assert packet[:2] == [0x40, 0x0A]
    assert packet[10] == 85
    assert packet[11] == 236


def test_vmulti_adapter_maps_contact_and_pressure_to_hid_report() -> None:
    adapter = VMultiPenInjectorAdapter(
        desktop_input=_DesktopInput(),
        log=lambda _line: None,
    )
    fake = _FakeVMulti()
    adapter._vmulti = fake  # type: ignore[assignment]  # noqa: SLF001

    ok, error = adapter.inject(
        flags=POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT,
        x=960,
        y=540,
        pressure_1024=512,
        tag="contact",
    )

    assert (ok, error) == (True, 0)
    assert fake.last_report is not None
    assert fake.last_report["status"] == 0x11
    assert 16380 <= fake.last_report["x"] <= 16400
    assert 16390 <= fake.last_report["y"] <= 16410
    assert 4090 <= fake.last_report["pressure"] <= 4100


def test_vmulti_adapter_positions_hover_before_new_contact() -> None:
    adapter = VMultiPenInjectorAdapter(
        desktop_input=_DesktopInput(),
        log=lambda _line: None,
    )
    fake = _FakeVMulti()
    adapter._vmulti = fake  # type: ignore[assignment]  # noqa: SLF001

    ok, error = adapter.inject(
        flags=POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT,
        x=1400,
        y=700,
        pressure_1024=700,
        tag="contact",
    )

    assert (ok, error) == (True, 0)
    assert fake.reports is not None
    assert len(fake.reports) == 2
    hover, contact = fake.reports
    assert hover["label"] == "contact.hover_anchor"
    assert hover["status"] == 0x10
    assert hover["pressure"] == 0
    assert (hover["x"], hover["y"]) == (contact["x"], contact["y"])
    assert contact["status"] == 0x11
    assert contact["pressure"] > 0


def test_vmulti_adapter_forces_zero_pressure_on_release() -> None:
    adapter = VMultiPenInjectorAdapter(
        desktop_input=_DesktopInput(),
        log=lambda _line: None,
    )
    fake = _FakeVMulti()
    adapter._vmulti = fake  # type: ignore[assignment]  # noqa: SLF001

    ok, error = adapter.inject(
        flags=POINTER_FLAG_INRANGE | POINTER_FLAG_UP,
        x=960,
        y=540,
        pressure_1024=900,
        tag="up",
    )

    assert (ok, error) == (True, 0)
    assert fake.last_report is not None
    assert fake.last_report["status"] == 0x10
    assert fake.last_report["pressure"] == 0


def test_vmulti_adapter_maps_synthetic_degrees_to_x_tilt_report_value() -> None:
    adapter = VMultiPenInjectorAdapter(
        desktop_input=_DesktopInput(),
        log=lambda _line: None,
    )
    fake = _FakeVMulti()
    adapter._vmulti = fake  # type: ignore[assignment]  # noqa: SLF001

    ok, error = adapter.inject(
        flags=POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT,
        x=960,
        y=540,
        pressure_1024=512,
        tilt_x=60,
        tag="contact",
    )

    assert (ok, error) == (True, 0)
    assert fake.last_report is not None
    assert fake.last_report["tilt_x"] == 85


def test_vmulti_adapter_uses_hid_subpixels_between_mouse_coordinates() -> None:
    adapter = VMultiPenInjectorAdapter(
        desktop_input=_DesktopInput(),
        log=lambda _line: None,
    )
    fake = _FakeVMulti()
    adapter._vmulti = fake  # type: ignore[assignment]  # noqa: SLF001

    adapter.inject(
        flags=POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT,
        x=100,
        y=100,
        pressure_1024=500,
        tag="down",
    )
    integer_100 = fake.last_report["x"]  # type: ignore[index]
    adapter.inject(
        flags=POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT,
        x=101,
        y=100,
        pressure_1024=500,
        tag="move_x",
    )
    half_x = fake.last_report["x"]  # type: ignore[index]
    integer_101 = round(101 / (fake.screen_w - 1) * 0x7FFF)

    assert integer_100 < half_x < integer_101

    adapter.inject(
        flags=POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT,
        x=101,
        y=101,
        pressure_1024=500,
        tag="move_y",
    )
    diagonal_report = dict(fake.last_report or {})
    integer_y_100 = round(100 / (fake.screen_h - 1) * 0x7FFF)
    integer_y_101 = round(101 / (fake.screen_h - 1) * 0x7FFF)
    assert integer_y_100 < diagonal_report["y"] < integer_y_101

    # Pressure-only reports must retain the reconstructed subpixel instead of
    # creeping toward the integer desktop coordinate.
    adapter.inject(
        flags=POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT,
        x=101,
        y=101,
        pressure_1024=550,
        tag="pressure_only",
    )
    assert fake.last_report is not None
    assert (fake.last_report["x"], fake.last_report["y"]) == (
        diagonal_report["x"],
        diagonal_report["y"],
    )

    adapter.inject(
        flags=POINTER_FLAG_UP,
        x=101,
        y=101,
        pressure_1024=0,
        tag="up",
    )
    assert fake.last_report is not None
    assert (fake.last_report["x"], fake.last_report["y"]) == (
        diagonal_report["x"],
        diagonal_report["y"],
    )
