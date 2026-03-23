from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from superstrike_pressure.bridge.synthetic_pen import (  # noqa: E402
    POINTER_FLAG_INRANGE,
    POINTER_FLAG_UP,
    POINTER_FLAG_UPDATE,
    SyntheticPenConfig,
    SyntheticPenEmitter,
)


class _FakePen:
    def __init__(self) -> None:
        self._lmb = False
        self.calls: list[dict[str, int | str]] = []
        self.pos = (400, 300)

    def open(self) -> None:
        return

    def close(self) -> None:
        return

    def get_cursor_pos(self) -> tuple[int, int]:
        return self.pos

    def is_lmb_down(self) -> bool:
        return self._lmb

    def inject(self, *, flags: int, x: int, y: int, pressure_1024: int, tag: str) -> tuple[bool, int]:
        self.calls.append(
            {
                "flags": int(flags),
                "x": int(x),
                "y": int(y),
                "pressure": int(pressure_1024),
                "tag": str(tag),
            }
        )
        return True, 0

    def emit_left_click(self) -> None:
        return


class SyntheticPenReleaseTests(unittest.TestCase):
    def _mk_emitter(self, *, release_teardown: bool) -> tuple[SyntheticPenEmitter, _FakePen]:
        cfg = SyntheticPenConfig(
            contact_source="lmb_and_pressure",
            contact_threshold=12,
            release_threshold=4,
            suppress_lmb=False,
            release_teardown=release_teardown,
        )
        emitter = SyntheticPenEmitter(cfg, log=lambda _line: None)
        fake = _FakePen()
        emitter.pen = fake  # type: ignore[assignment]
        return emitter, fake

    def test_release_without_teardown_sends_single_up(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=False)
        emitter.state = "contact"
        emitter.contact_frame_no = 5
        fake._lmb = False

        sample = emitter.update(left_mapped=400, right_mapped=0)

        self.assertEqual(sample.state, "idle")
        self.assertTrue(sample.injected)
        self.assertFalse(sample.failed)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["flags"], POINTER_FLAG_UP | POINTER_FLAG_INRANGE)
        self.assertEqual(fake.calls[0]["pressure"], 0)

    def test_release_with_teardown_sends_up_hover_endhover(self) -> None:
        emitter, fake = self._mk_emitter(release_teardown=True)
        emitter.state = "contact"
        emitter.contact_frame_no = 5
        fake._lmb = False

        sample = emitter.update(left_mapped=400, right_mapped=0)

        self.assertEqual(sample.state, "idle")
        self.assertTrue(sample.injected)
        self.assertFalse(sample.failed)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(fake.calls[0]["flags"], POINTER_FLAG_UP | POINTER_FLAG_INRANGE)
        self.assertEqual(fake.calls[1]["flags"], POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE)
        self.assertEqual(fake.calls[2]["flags"], POINTER_FLAG_UPDATE)
        self.assertEqual(fake.calls[0]["pressure"], 0)
        self.assertEqual(fake.calls[1]["pressure"], 0)
        self.assertEqual(fake.calls[2]["pressure"], 0)


if __name__ == "__main__":
    unittest.main()
