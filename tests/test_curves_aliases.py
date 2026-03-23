from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from superstrike_pressure.bridge.curves import normalize_curve_name  # noqa: E402


class CurveAliasTests(unittest.TestCase):
    def test_protocol_names_map_to_canonical(self) -> None:
        self.assertEqual(normalize_curve_name("linear"), "linear")
        self.assertEqual(normalize_curve_name("soft"), "ease_in")
        self.assertEqual(normalize_curve_name("hard"), "ease_out")
        self.assertEqual(normalize_curve_name("scurve"), "s_curve")

    def test_legacy_aliases_still_work(self) -> None:
        self.assertEqual(normalize_curve_name("ease_in"), "ease_in")
        self.assertEqual(normalize_curve_name("ease_out"), "ease_out")
        self.assertEqual(normalize_curve_name("s_curve"), "s_curve")

    def test_unknown_curve_raises(self) -> None:
        with self.assertRaises(ValueError):
            normalize_curve_name("banana")


if __name__ == "__main__":
    unittest.main()
