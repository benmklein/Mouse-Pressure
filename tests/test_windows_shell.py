from __future__ import annotations

import sys
import uuid

import pytest

from mouse_pressure.ui.windows_shell import SingleInstanceGuard


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex")
def test_single_instance_guard_rejects_a_second_process_slot() -> None:
    name = rf"Local\MousePressure.Test.{uuid.uuid4()}"
    first = SingleInstanceGuard(name)
    try:
        assert first.acquired is True
        second = SingleInstanceGuard(name)
        assert second.acquired is False
        second.close()
    finally:
        first.close()

    replacement = SingleInstanceGuard(name)
    try:
        assert replacement.acquired is True
    finally:
        replacement.close()
