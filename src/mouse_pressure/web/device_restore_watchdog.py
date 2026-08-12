"""Restore temporary mouse hardware settings after an unexpected process exit."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

from mouse_pressure.sniff.hidpp_pressure import PressureHidppSession

SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
CREATE_NO_WINDOW = 0x08000000


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def arm_restore_watchdog(
    *,
    config_dir: Path,
    parent_pid: int,
    settings: dict[str, int],
) -> tuple[subprocess.Popen[bytes], Path]:
    """Persist a recovery snapshot and launch its independent watcher."""
    state_path = config_dir / (
        f"device_restore_{int(parent_pid)}_{uuid.uuid4().hex}.json"
    )
    _write_json_atomic(
        state_path,
        {
            "version": 1,
            "parent_pid": int(parent_pid),
            "settings": {key: int(value) for key, value in settings.items()},
        },
    )
    if getattr(sys, "frozen", False):
        command = [
            sys.executable,
            "--device-restore-watchdog",
        ]
    else:
        command = [
            sys.executable,
            "-m",
            "mouse_pressure.web.device_restore_watchdog",
        ]
    command.extend(
        [
            "--parent-pid",
            str(int(parent_pid)),
            "--state-file",
            str(state_path),
        ]
    )
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception:
        state_path.unlink(missing_ok=True)
        raise
    return process, state_path


def disarm_restore_watchdog(state_path: Path | None) -> None:
    if state_path is not None:
        state_path.unlink(missing_ok=True)


def _read_device_settings(session: PressureHidppSession) -> dict[str, int]:
    discover = getattr(session, "discover_pressure_feature_index", None)
    if callable(discover):
        discover()
    dpi = session.get_dpi()
    left, right = session.get_haptic_levels()
    result = {
        "dpi": int(dpi),
        "haptic_left": int(left),
        "haptic_right": int(right),
    }
    profile_reader = getattr(session, "get_onboard_profile_state", None)
    if callable(profile_reader):
        enabled, sector = profile_reader()
        result["onboard_profiles_enabled"] = int(bool(enabled))
        result["onboard_profile_sector"] = int(sector or 0)
    return result


def restore_device_settings(
    session: PressureHidppSession,
    settings: dict[str, int],
) -> dict[str, int]:
    """Restore the same DPI/haptic/profile snapshot used by normal Stop."""
    current = _read_device_settings(session)
    if int(current["dpi"]) != int(settings["dpi"]):
        if bool(current.get("onboard_profiles_enabled", 0)):
            profile_writer = getattr(session, "set_onboard_profile_state", None)
            if not callable(profile_writer):
                raise RuntimeError("Onboard profile cannot be disabled for DPI restore")
            profile_writer(enabled=False)
            current["onboard_profiles_enabled"] = 0
            current["onboard_profile_sector"] = 0
        session.set_dpi(int(settings["dpi"]))
    if (
        int(current["haptic_left"]) != int(settings["haptic_left"])
        or int(current["haptic_right"]) != int(settings["haptic_right"])
    ):
        session.set_haptic_levels(
            left=int(settings["haptic_left"]),
            right=int(settings["haptic_right"]),
        )

    original_profiles_enabled = bool(settings.get("onboard_profiles_enabled", 0))
    original_profile_sector = int(settings.get("onboard_profile_sector", 0))
    current_profiles_enabled = bool(current.get("onboard_profiles_enabled", 0))
    current_profile_sector = int(current.get("onboard_profile_sector", 0))
    if (
        original_profiles_enabled != current_profiles_enabled
        or (
            original_profiles_enabled
            and original_profile_sector != current_profile_sector
        )
    ):
        profile_writer = getattr(session, "set_onboard_profile_state", None)
        if not callable(profile_writer):
            raise RuntimeError("Onboard profile cannot be restored")
        profile_writer(
            enabled=original_profiles_enabled,
            active_sector=(
                original_profile_sector if original_profiles_enabled else None
            ),
        )
    return {
        "dpi": int(settings["dpi"]),
        "haptic_left": int(settings["haptic_left"]),
        "haptic_right": int(settings["haptic_right"]),
    }


def _wait_for_parent_or_disarm(parent_pid: int, state_path: Path) -> bool:
    """Return True when the parent exited while the recovery file remained armed."""
    if sys.platform != "win32":
        while state_path.exists():
            try:
                os.kill(parent_pid, 0)
            except OSError:
                return True
            time.sleep(0.25)
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(parent_pid))
    if not handle:
        return state_path.exists()
    try:
        while state_path.exists():
            result = int(kernel32.WaitForSingleObject(handle, 250))
            if result == WAIT_OBJECT_0:
                return True
            if result != WAIT_TIMEOUT:
                return True
        return False
    finally:
        kernel32.CloseHandle(handle)


def run_watchdog(
    *,
    parent_pid: int,
    state_path: Path,
    session_factory: Callable[[Callable[[str], None]], PressureHidppSession] = PressureHidppSession,
) -> bool:
    if not _wait_for_parent_or_disarm(parent_pid, state_path):
        return False
    time.sleep(0.4)
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        settings = payload["settings"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return False

    for attempt in range(12):
        session = session_factory(lambda _line: None)
        try:
            session.open()
            restore_device_settings(session, settings)
        except Exception:
            time.sleep(min(1.5, 0.25 + attempt * 0.1))
        else:
            state_path.unlink(missing_ok=True)
            return True
        finally:
            try:
                session.close()
            except Exception:
                pass
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    args = parser.parse_args(argv)
    return 0 if run_watchdog(
        parent_pid=args.parent_pid,
        state_path=args.state_file,
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
