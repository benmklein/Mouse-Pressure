from __future__ import annotations

from superstrike_pressure.sniff.hidpp_pressure import (
    Feature0CFrame,
    PressureHidppSession,
    build_monitoring_lease_report,
    extract_mode3_lr_pressure_raw,
    parse_feature_0c_frame,
)


class _LeaseDevice:
    def __init__(self) -> None:
        self.writes: list[list[int]] = []
        self.responses: list[list[int]] = []

    def write(self, report: list[int]) -> int:
        row = list(report)
        self.writes.append(row)
        feature_index = row[2]
        address = row[3]
        if feature_index == 0x00 and address == 0x08:
            payload = [0x17, 0x00, 0x00]
        elif feature_index == 0x17 and address == 0x48:
            payload = [0x01]
        else:
            payload = row[4:]
        response = [0x11, 0x01, feature_index, address, *payload]
        response.extend([0] * (20 - len(response)))
        self.responses.append(response[:20])
        return len(row)

    def read(self, _size: int) -> list[int]:
        return self.responses.pop(0) if self.responses else []


def _event(left_code: int, right_code: int, *, feature_index: int = 0x0C) -> list[int]:
    channels = [left_code, right_code, 0, 0, 0, 0, 0, 0]
    payload: list[int] = []
    for code in channels:
        raw_u16 = code << 6
        payload.extend([(raw_u16 >> 8) & 0xFF, raw_u16 & 0xFF])
    return [0x11, 0x01, feature_index, 0x10, *payload]


def test_full_adc_decoder_preserves_low_byte_resolution() -> None:
    frame = parse_feature_0c_frame(_event(325, 574), 1.25)

    assert frame is not None
    assert extract_mode3_lr_pressure_raw(frame) == (325, 574)


def test_empty_adc_event_is_not_treated_as_released_buttons() -> None:
    frame = parse_feature_0c_frame(_event(0, 0), 1.25)

    assert frame is not None
    assert extract_mode3_lr_pressure_raw(frame) == (None, None)


def test_parser_accepts_dynamically_discovered_feature_index() -> None:
    report = _event(324, 560, feature_index=0x17)

    assert parse_feature_0c_frame(report, 1.0) is None
    frame = parse_feature_0c_frame(report, 1.0, feature_index=0x17)

    assert frame is not None
    assert extract_mode3_lr_pressure_raw(frame) == (324, 560)


def test_monitoring_lease_uses_function_3_and_short_expiry() -> None:
    report = build_monitoring_lease_report(feature_index=0x17, flags=0x02, lease_seconds=8)

    assert len(report) == 20
    assert report[:6] == [0x11, 0x01, 0x17, 0x38, 0x02, 0x08]


def test_decoder_rejects_truncated_channel_pair() -> None:
    frame = Feature0CFrame(timestamp_s=0.0, raw=[], addr=0x10, payload=[0x51])

    left, right = extract_mode3_lr_pressure_raw(frame)

    assert left is None
    assert right is None


def test_session_discovers_feature_renews_lease_and_restores_flags() -> None:
    dev = _LeaseDevice()
    session = PressureHidppSession(log=lambda _line: None)
    session.dev = dev  # type: ignore[assignment]

    session.enable_pressure_stream(mode=3, mode_arg=0)

    assert session.pressure_feature_index == 0x17
    assert dev.writes[0][2:6] == [0x00, 0x08, 0x1B, 0x0C]
    assert dev.writes[1][2:4] == [0x17, 0x48]
    assert dev.writes[2][2:6] == [0x17, 0x38, 0x03, 0x08]

    session._next_lease_renewal = 0.0  # noqa: SLF001
    assert session.maintain_pressure_stream() is True
    assert dev.writes[-1][2:6] == [0x17, 0x38, 0x03, 0x08]

    session.disable_pressure_stream()
    assert dev.writes[-1][2:6] == [0x17, 0x38, 0x01, 60]
