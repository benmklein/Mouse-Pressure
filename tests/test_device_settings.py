from __future__ import annotations

from mouse_pressure.sniff.hidpp_pressure import PressureHidppSession


def _reply(feature: int, address: int, payload: list[int]) -> list[int]:
    body = payload[:16] + [0] * (16 - len(payload[:16]))
    return [0x11, 0x01, feature, address] + body


class _FakeDevice:
    def __init__(self, replies: list[list[int]]) -> None:
        self.replies = list(replies)
        self.writes: list[list[int]] = []

    def write(self, report: list[int]) -> int:
        self.writes.append(list(report))
        return len(report)

    def read(self, _size: int) -> list[int]:
        return self.replies.pop(0) if self.replies else []


def test_haptics_can_be_detected_without_writing() -> None:
    fake = _FakeDevice(
        [
            _reply(0x0C, 0x2F, [0x00, 0x04, 0x05, 0x08]),
            _reply(0x0C, 0x2F, [0x01, 0x1C, 0x08, 0x14]),
        ]
    )
    session = PressureHidppSession(log=lambda _line: None)
    session.dev = fake  # type: ignore[assignment]

    assert session.get_haptic_levels() == (2, 5)
    assert all(row[3] == 0x2F for row in fake.writes)


def test_dpi_can_be_detected_without_writing() -> None:
    current = [0x00, 0x06, 0x40, 0x06, 0x40, 0x06, 0x40, 0x06, 0x40, 0x02]
    fake = _FakeDevice([_reply(0x09, 0x5F, current)])
    session = PressureHidppSession(log=lambda _line: None)
    session.dev = fake  # type: ignore[assignment]

    assert session.get_dpi() == 1600
    assert len(fake.writes) == 1
    assert fake.writes[0][3] == 0x5F


def test_onboard_profile_state_can_switch_to_host_and_restore() -> None:
    fake = _FakeDevice(
        [
            _reply(0x00, 0x08, [0x0E, 0x00]),
            _reply(0x0E, 0x2F, [0x01]),
            _reply(0x0E, 0x4F, [0x00, 0x02]),
            _reply(0x0E, 0x1F, [0x00]),
            _reply(0x0E, 0x1F, [0x00]),
            _reply(0x0E, 0x3F, [0x00]),
        ]
    )
    session = PressureHidppSession(log=lambda _line: None)
    session.dev = fake  # type: ignore[assignment]

    assert session.get_onboard_profile_state() == (True, 2)
    assert session.set_onboard_profile_state(enabled=False) == (False, None)
    assert session.set_onboard_profile_state(enabled=True, active_sector=2) == (True, 2)

    profile_writes = [row for row in fake.writes if row[2] == 0x0E]
    assert profile_writes[-3][3:6] == [0x1F, 0x02, 0x00]
    assert profile_writes[-2][3:6] == [0x1F, 0x01, 0x00]
    assert profile_writes[-1][3:7] == [0x3F, 0x00, 0x02, 0x00]


def test_haptics_preserve_other_hits_settings() -> None:
    replies = [
        _reply(0x0C, 0x2F, [0x00, 0x04, 0x05, 0x14]),
        _reply(0x0C, 0x2F, [0x01, 0x1C, 0x08, 0x14]),
        _reply(0x0F, 0x00, [0x00, 0x01, 0x00]),
        _reply(0x0C, 0x1F, [0x00, 0x04, 0x05, 0x00]),
        _reply(0x0C, 0x1F, [0x01, 0x1C, 0x08, 0x0C]),
        _reply(0x0F, 0x00, [0x00, 0x00, 0x00]),
    ]
    fake = _FakeDevice(replies)
    session = PressureHidppSession(log=lambda _line: None)
    session.dev = fake  # type: ignore[assignment]

    assert session.set_haptic_levels(left=0, right=3) == (0, 3)

    haptic_writes = [row for row in fake.writes if row[2] == 0x0C and row[3] == 0x1F]
    assert haptic_writes[0][4:8] == [0x00, 0x04, 0x05, 0x00]
    assert haptic_writes[1][4:8] == [0x01, 0x1C, 0x08, 0x0C]


def test_dpi_write_preserves_lod_and_verifies_result() -> None:
    current = [0x00, 0x03, 0x20, 0x03, 0x20, 0x03, 0x20, 0x03, 0x20, 0x02]
    updated = [0x00, 0x06, 0x40, 0x06, 0x40, 0x06, 0x40, 0x06, 0x40, 0x02]
    replies = [
        _reply(0x09, 0x5F, current),
        _reply(0x09, 0x6F, [0x00, 0x06, 0x40, 0x06, 0x40, 0x02]),
        _reply(0x09, 0x5F, updated),
    ]
    fake = _FakeDevice(replies)
    session = PressureHidppSession(log=lambda _line: None)
    session.dev = fake  # type: ignore[assignment]

    assert session.set_dpi(1600) == 1600

    dpi_write = next(row for row in fake.writes if row[2] == 0x09 and row[3] == 0x6F)
    assert dpi_write[4:10] == [0x00, 0x06, 0x40, 0x06, 0x40, 0x02]


def test_dpi_write_keeps_unsupported_y_axis_at_zero() -> None:
    current = [0x00, 0x03, 0x20, 0x03, 0x20, 0x00, 0x00, 0x00, 0x00, 0x01]
    updated = [0x00, 0x06, 0x40, 0x06, 0x40, 0x00, 0x00, 0x00, 0x00, 0x01]
    fake = _FakeDevice(
        [
            _reply(0x09, 0x5F, current),
            _reply(0x09, 0x6F, [0x00, 0x06, 0x40, 0x00, 0x00, 0x01]),
            _reply(0x09, 0x5F, updated),
        ]
    )
    session = PressureHidppSession(log=lambda _line: None)
    session.dev = fake  # type: ignore[assignment]

    assert session.set_dpi(1600) == 1600
    dpi_write = next(row for row in fake.writes if row[2] == 0x09 and row[3] == 0x6F)
    assert dpi_write[4:10] == [0x00, 0x06, 0x40, 0x00, 0x00, 0x01]
