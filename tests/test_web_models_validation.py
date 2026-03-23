from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from superstrike_pressure.web.models import (  # noqa: E402
    deadzone_pct_to_float,
    validate_channel_config,
)


def _valid_channel() -> dict:
    return {
        "raw_min": 80,
        "raw_max": 185,
        "deadzone_low": 0,
        "deadzone_high": 0,
        "curve": "linear",
        "curve_strength": 1.0,
        "contact_preset": "medium",
    }


class ValidationModelTests(unittest.TestCase):
    def test_invalid_raw_range_reports_error(self) -> None:
        ch = _valid_channel()
        ch["raw_min"] = 180
        ch["raw_max"] = 170
        errors = validate_channel_config(ch)
        self.assertTrue(any("raw_min must be strictly less than raw_max" in e for e in errors))

    def test_valid_channel_returns_no_errors(self) -> None:
        self.assertEqual(validate_channel_config(_valid_channel()), [])

    def test_deadzone_pct_conversion(self) -> None:
        self.assertEqual(deadzone_pct_to_float(0), 0.0)
        self.assertEqual(deadzone_pct_to_float(5), 0.05)
        self.assertEqual(deadzone_pct_to_float(100), 1.0)


if __name__ == "__main__":
    unittest.main()
