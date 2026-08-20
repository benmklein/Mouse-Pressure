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

from mouse_pressure.runtime.device_settings import (
    DeviceSettingsSnapshot,
    restore_device_settings,
)
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
    settings: DeviceSettingsSnapshot,
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
            "settings": settings.to_dict(),
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
            "mouse_pressure.runtime.device_restore_watchdog",
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
        settings = DeviceSettingsSnapshot.from_mapping(payload["settings"])
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
