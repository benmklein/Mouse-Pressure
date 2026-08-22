from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mouse_pressure.bridge.config import ChannelConfig, RuntimeConfig  # noqa: E402
from mouse_pressure.runtime.config_store import (  # noqa: E402
    ConfigStore,
    resolve_config_dir,
)
from mouse_pressure.runtime.models import (  # noqa: E402
    SchemaMismatchError,
    ValidationError,
)


def _sample_runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        linked=False,
        left_enabled=False,
        right_enabled=True,
        suppress_lmb=True,
        suppress_rmb=True,
        debug_mode=False,
        minimize_to_tray=False,
        session_dpi=1600,
        session_haptic_left=0,
        session_haptic_right=3,
        session_device_settings_follow_normal=False,
        remap_mode="hold",
        remap_hold_hotkey="Alt+F11",
        activation_hotkey="Ctrl+F11",
        deactivation_hotkey="Ctrl+Shift+F11",
        left=ChannelConfig(
            output_target="mouse_sensitivity",
            sensitivity_light=125,
            sensitivity_firm=40,
            raw_min=82,
            raw_max=180,
            curve="soft",
            actuation_level=2,
            stationary_pressure_updates=True,
        ),
        right=ChannelConfig(
            output_target="x_tilt",
            raw_min=84,
            raw_max=190,
            curve="hard",
            actuation_level=8,
        ),
    )


class ConfigStoreTests(unittest.TestCase):
    """Verify persisted application configuration behavior."""

    def test_default_profile_matches_the_recommended_current_profile(self) -> None:
        defaults = RuntimeConfig()

        self.assertTrue(defaults.left_enabled)
        self.assertTrue(defaults.right_enabled)
        self.assertEqual(defaults.left.output_target, "pressure")
        self.assertEqual(defaults.right.output_target, "pressure")
        self.assertFalse(defaults.debug_mode)
        self.assertEqual(defaults.remap_mode, "always")
        self.assertEqual(defaults.remap_hold_hotkey, "Mouse 5")
        self.assertEqual(defaults.activation_hotkey, "Ctrl+F12")
        self.assertEqual(defaults.deactivation_hotkey, "Ctrl+Shift+F12")
        self.assertEqual(defaults.left.sensitivity_light, 100)
        self.assertEqual(defaults.left.sensitivity_firm, 35)

    def test_load_returns_defaults_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(td)
            loaded = store.load()
            self.assertEqual(loaded.schema_version, 1)
            self.assertFalse(loaded.linked)
            self.assertTrue(loaded.left_enabled)
            self.assertTrue(loaded.right_enabled)
            self.assertEqual(loaded.left.raw_min, 325)
            self.assertEqual(loaded.right.raw_min, 325)
            self.assertEqual(loaded.right.raw_max, 700)
            self.assertEqual(loaded.left.pressure_floor, 15)
            self.assertEqual(loaded.left.path_stabilization, 0)
            self.assertEqual(loaded.left.pressure_influence, 100)
            self.assertFalse(loaded.left.onset_buffer)
            self.assertFalse(loaded.left.stationary_pressure_updates)
            self.assertEqual(loaded.left.curve, "linear")
            self.assertEqual(loaded.left.curve_strength, 1.0)
            self.assertEqual(loaded.right.curve_strength, 1.0)
            self.assertFalse(loaded.debug_mode)
            self.assertTrue(loaded.minimize_to_tray)
            self.assertTrue(loaded.session_device_settings_follow_normal)

    def test_load_migrates_legacy_byte_calibration_to_adc_codes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(td)
            store.path.parent.mkdir(parents=True, exist_ok=True)
            store.path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "left": {"raw_min": 80, "raw_max": 185},
                        "right": {"raw_min": 79, "raw_max": 170},
                    }
                ),
                encoding="utf-8",
            )

            loaded = store.load()

            self.assertEqual((loaded.left.raw_min, loaded.left.raw_max), (320, 740))
            self.assertEqual((loaded.right.raw_min, loaded.right.raw_max), (316, 680))

    def test_save_then_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(td)
            config = _sample_runtime_config()
            store.save(config)
            loaded = store.load()
            self.assertEqual(loaded.linked, config.linked)
            self.assertFalse(loaded.left_enabled)
            self.assertTrue(loaded.right_enabled)
            self.assertTrue(loaded.suppress_lmb)
            self.assertTrue(loaded.suppress_rmb)
            self.assertEqual(loaded.right.output_target, "x_tilt")
            self.assertFalse(loaded.debug_mode)
            self.assertFalse(loaded.minimize_to_tray)
            self.assertEqual(loaded.session_dpi, 1600)
            self.assertEqual(loaded.session_haptic_left, 0)
            self.assertEqual(loaded.session_haptic_right, 3)
            self.assertEqual(loaded.left.output_target, "mouse_sensitivity")
            self.assertEqual(loaded.left.sensitivity_light, 125)
            self.assertEqual(loaded.left.sensitivity_firm, 40)
            self.assertFalse(loaded.session_device_settings_follow_normal)
            self.assertEqual(loaded.remap_mode, "hold")
            self.assertEqual(loaded.remap_hold_hotkey, "Alt+F11")
            self.assertEqual(loaded.activation_hotkey, "Ctrl+F11")
            self.assertEqual(loaded.deactivation_hotkey, "Ctrl+Shift+F11")
            self.assertEqual(loaded.left.curve, "soft")
            self.assertEqual(loaded.right.curve, "hard")
            self.assertTrue(loaded.left.stationary_pressure_updates)
            self.assertFalse(loaded.right.stationary_pressure_updates)

    def test_load_rejects_schema_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(td)
            store.path.parent.mkdir(parents=True, exist_ok=True)
            store.path.write_text(
                json.dumps(
                    {"schema_version": 2, "linked": True, "left": {}, "right": {}}
                ),
                encoding="utf-8",
            )
            with self.assertRaises(SchemaMismatchError):
                store.load()

    def test_load_old_config_defaults_new_bridge_flags(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(td)
            store.path.parent.mkdir(parents=True, exist_ok=True)
            store.path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "linked": True,
                        "release_teardown": True,
                        "application_mode": "selected",
                        "pressure_applications": ["krita.exe"],
                        "left": {},
                        "right": {},
                    }
                ),
                encoding="utf-8",
            )

            loaded = store.load()

            self.assertTrue(loaded.suppress_lmb)
            self.assertTrue(loaded.left_enabled)
            self.assertTrue(loaded.right_enabled)
            self.assertEqual(loaded.left.output_target, "pressure")
            self.assertEqual(loaded.right.output_target, "pressure")
            self.assertFalse(loaded.debug_mode)
            self.assertTrue(loaded.minimize_to_tray)
            self.assertEqual(loaded.session_dpi, 800)
            self.assertEqual(loaded.session_haptic_left, 3)
            self.assertEqual(loaded.session_haptic_right, 3)
            self.assertTrue(loaded.session_device_settings_follow_normal)
            self.assertEqual(loaded.left.sensitivity_light, 100)
            self.assertEqual(loaded.left.sensitivity_firm, 35)
            self.assertFalse(loaded.left.stationary_pressure_updates)
            self.assertFalse(loaded.right.stationary_pressure_updates)

    def test_load_rejects_duplicate_global_shortcuts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(td)
            store.path.parent.mkdir(parents=True, exist_ok=True)
            store.path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "activation_hotkey": "Ctrl+F10",
                        "deactivation_hotkey": "Ctrl+F10",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValidationError, "must be different"):
                store.load()

    def test_load_rejects_unknown_remap_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(td)
            store.path.parent.mkdir(parents=True, exist_ok=True)
            store.path.write_text(
                json.dumps({"schema_version": 1, "remap_mode": "automatic"}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValidationError, "remap_mode"):
                store.load()

    def test_load_ignores_removed_stroke_timing_options(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(td)
            store.path.parent.mkdir(parents=True, exist_ok=True)
            store.path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "left": {
                            "immediate_button_wake": False,
                            "clean_stroke_endings": False,
                        },
                        "right": {
                            "immediate_button_wake": "invalid legacy value",
                            "clean_stroke_endings": "invalid legacy value",
                        },
                    }
                ),
                encoding="utf-8",
            )

            loaded = store.load()

            self.assertFalse(hasattr(loaded.left, "immediate_button_wake"))
            self.assertFalse(hasattr(loaded.left, "clean_stroke_endings"))

    def test_load_ignores_removed_legacy_right_xtilt_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(td)
            store.path.parent.mkdir(parents=True, exist_ok=True)
            store.path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "rmb_aux_xtilt": True,
                        "left": {},
                        "right": {},
                    }
                ),
                encoding="utf-8",
            )

            loaded = store.load()

            self.assertEqual(loaded.left.output_target, "pressure")
            self.assertEqual(loaded.right.output_target, "pressure")

    def test_resolve_config_dir_prefers_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            old_value = os.environ.get("MOUSE_PRESSURE_CONFIG_DIR")
            os.environ["MOUSE_PRESSURE_CONFIG_DIR"] = td
            try:
                resolved = resolve_config_dir()
                self.assertEqual(resolved, Path(td))
            finally:
                if old_value is None:
                    del os.environ["MOUSE_PRESSURE_CONFIG_DIR"]
                else:
                    os.environ["MOUSE_PRESSURE_CONFIG_DIR"] = old_value


if __name__ == "__main__":
    unittest.main()
