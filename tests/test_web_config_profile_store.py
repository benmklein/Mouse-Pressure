from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from superstrike_pressure.bridge.config import ChannelConfig, RuntimeConfig  # noqa: E402
from superstrike_pressure.web.config_store import ConfigStore, resolve_config_dir  # noqa: E402
from superstrike_pressure.web.models import ProfileNotFoundError, SchemaMismatchError  # noqa: E402
from superstrike_pressure.web.profile_store import ProfileStore  # noqa: E402


def _sample_runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
            linked=False,
            suppress_lmb=True,
            suppress_rmb=True,
        release_teardown=True,
        left=ChannelConfig(raw_min=82, raw_max=180, curve="soft", contact_preset="light"),
        right=ChannelConfig(raw_min=84, raw_max=190, curve="hard", contact_preset="firm"),
        app_profiles={"krita.exe": "krita"},
    )


class ConfigStoreTests(unittest.TestCase):
    def test_load_returns_defaults_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(td)
            loaded = store.load()
            self.assertEqual(loaded.schema_version, 1)
            self.assertTrue(loaded.linked)
            self.assertEqual(loaded.left.raw_min, 320)
            self.assertEqual(loaded.right.raw_max, 740)
            self.assertEqual(loaded.left.pressure_floor, 12)
            self.assertEqual(loaded.left.path_stabilization, 0)
            self.assertEqual(loaded.left.pressure_influence, 85)
            self.assertFalse(loaded.left.onset_buffer)

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
            self.assertTrue(loaded.suppress_lmb)
            self.assertTrue(loaded.suppress_rmb)
            self.assertTrue(loaded.release_teardown)
            self.assertEqual(loaded.left.curve, "soft")
            self.assertEqual(loaded.right.curve, "hard")
            self.assertEqual(loaded.app_profiles["krita.exe"], "krita")

    def test_load_rejects_schema_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(td)
            store.path.parent.mkdir(parents=True, exist_ok=True)
            store.path.write_text(
                json.dumps({"schema_version": 2, "linked": True, "left": {}, "right": {}}),
                encoding="utf-8",
            )
            with self.assertRaises(SchemaMismatchError):
                store.load()

    def test_load_old_config_defaults_new_bridge_flags(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ConfigStore(td)
            store.path.parent.mkdir(parents=True, exist_ok=True)
            store.path.write_text(
                json.dumps({"schema_version": 1, "linked": True, "left": {}, "right": {}}),
                encoding="utf-8",
            )

            loaded = store.load()

            self.assertFalse(loaded.suppress_lmb)
            self.assertFalse(loaded.release_teardown)

    def test_resolve_config_dir_prefers_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            old_value = os.environ.get("SUPERSTRIKE_CONFIG_DIR")
            os.environ["SUPERSTRIKE_CONFIG_DIR"] = td
            try:
                resolved = resolve_config_dir()
                self.assertEqual(resolved, Path(td))
            finally:
                if old_value is None:
                    del os.environ["SUPERSTRIKE_CONFIG_DIR"]
                else:
                    os.environ["SUPERSTRIKE_CONFIG_DIR"] = old_value


class ProfileStoreTests(unittest.TestCase):
    def test_profile_crud_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ProfileStore(td)
            config = _sample_runtime_config()

            store.save("krita", config)
            listing = store.list()
            self.assertEqual(len(listing), 1)
            self.assertEqual(listing[0]["name"], "krita")
            self.assertIsInstance(listing[0]["modified_at"], int)

            loaded = store.load("krita")
            self.assertEqual(loaded.left.curve, "soft")

            exported = store.export_json("krita")
            self.assertIn('"schema_version": 1', exported)

            store.delete("krita")
            self.assertEqual(store.list(), [])

    def test_load_missing_profile_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ProfileStore(td)
            with self.assertRaises(ProfileNotFoundError):
                store.load("missing")

    def test_import_json_generates_name_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ProfileStore(td)
            config_json = json.dumps(
                {
                    "schema_version": 1,
                    "linked": True,
                    "left": {
                        "raw_min": 80,
                        "raw_max": 185,
                        "deadzone_low": 0,
                        "deadzone_high": 0,
                        "curve": "linear",
                        "curve_strength": 1.0,
                        "contact_preset": "medium",
                    },
                    "right": {
                        "raw_min": 80,
                        "raw_max": 185,
                        "deadzone_low": 0,
                        "deadzone_high": 0,
                        "curve": "linear",
                        "curve_strength": 1.0,
                        "contact_preset": "medium",
                    },
                    "app_profiles": {},
                }
            )
            imported_name = store.import_json(config_json)
            self.assertTrue(imported_name.startswith("imported_"))
            loaded = store.load(imported_name)
            self.assertEqual(loaded.schema_version, 1)

    def test_import_json_rejects_schema_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ProfileStore(td)
            payload = json.dumps({"schema_version": 2})
            with self.assertRaises(SchemaMismatchError):
                store.import_json(payload)


if __name__ == "__main__":
    unittest.main()
