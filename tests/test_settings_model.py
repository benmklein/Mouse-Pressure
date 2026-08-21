from __future__ import annotations

import pytest

from mouse_pressure.bridge.config import ChannelConfig, RuntimeConfig
from mouse_pressure.runtime.device_settings import SessionDeviceSettings
from mouse_pressure.ui.settings_model import SettingsDraft


def _draft(config: RuntimeConfig) -> SettingsDraft:
    return SettingsDraft(
        config=config,
        injection_hz=240.0,
        normal_device=SessionDeviceSettings(
            dpi=800,
            haptic_left=3,
            haptic_right=3,
        ),
    )


def test_linking_uses_left_settings_without_overwriting_saved_right_settings() -> None:
    left = ChannelConfig(raw_min=410, raw_max=690, curve="soft", curve_strength=2.0)
    right = ChannelConfig(raw_min=330, raw_max=650, curve="hard", curve_strength=3.0)
    draft = _draft(RuntimeConfig(linked=True, left=left, right=right))

    patch = draft.runtime_patch()

    assert draft.effective_channel("right") is left
    assert patch["right"]["raw_min"] == 330
    assert patch["right"]["curve"] == "hard"


def test_x_tilt_requires_an_enabled_pressure_channel() -> None:
    config = RuntimeConfig(
        left=ChannelConfig(output_target="x_tilt"),
        right=ChannelConfig(output_target="x_tilt"),
    )

    with pytest.raises(ValueError, match="At least one enabled button"):
        _draft(config).validate()


def test_y_tilt_requires_an_enabled_pressure_channel() -> None:
    config = RuntimeConfig(
        left=ChannelConfig(output_target="y_tilt"),
        right=ChannelConfig(output_target="y_tilt"),
    )

    with pytest.raises(ValueError, match="At least one enabled button"):
        _draft(config).validate()


def test_rotation_requires_an_enabled_pressure_channel() -> None:
    config = RuntimeConfig(
        left=ChannelConfig(output_target="rotation"),
        right=ChannelConfig(output_target="rotation"),
    )

    with pytest.raises(ValueError, match="At least one enabled button"):
        _draft(config).validate()


def test_runtime_patch_detects_when_session_settings_follow_normal() -> None:
    draft = _draft(
        RuntimeConfig(
            session_dpi=800,
            session_haptic_left=3,
            session_haptic_right=3,
            session_device_settings_follow_normal=False,
        )
    )

    assert draft.runtime_patch()["session_device_settings_follow_normal"] is True


def test_resetting_one_channel_preserves_the_other_channel() -> None:
    left = ChannelConfig(raw_min=450)
    right = ChannelConfig(raw_min=320, raw_max=660, pressure_floor=25)
    draft = _draft(
        RuntimeConfig(
            suppress_lmb=False,
            suppress_rmb=False,
            left=left,
            right=right,
        )
    )

    reset = draft.reset_channel("left")

    assert reset.config.left == RuntimeConfig().left
    assert reset.config.suppress_lmb is RuntimeConfig().suppress_lmb
    assert reset.config.right is right
    assert reset.config.suppress_rmb is False


def test_mapping_points_and_live_pressure_use_the_same_floor() -> None:
    channel = ChannelConfig(
        raw_min=320,
        raw_max=680,
        pressure_floor=20,
        pressure_influence=100,
    )
    draft = _draft(RuntimeConfig(left=channel))

    live = draft.effective_pressure("left", 321)
    points = dict(draft.mapping_points("left", raw_start=321, raw_end=321))

    assert live == round(20 * 1024 / 100)
    assert points[321] == live
