# Recovery and removal

Mouse Pressure suppresses a physical mouse click only while replacing it with
pen output. Recovery paths are designed to restore ordinary input and temporary
mouse settings even after a failure.

## Immediate recovery

1. Press **Ctrl+Shift+F12** to Force Stop pressure output.
2. If the interface is responsive, choose **Stop** and close Mouse Pressure.
3. If necessary, press **Ctrl+Shift+Esc**, select `MousePressure.exe`, and end
   the task. The independent watchdog should restore the pre-start DPI and
   haptic values.
4. Disconnect and reconnect the mouse if its firmware settings do not respond.

The click suppressor is fail-open: if its pressure heartbeat stalls, native
mouse clicks are restored rather than remaining blocked.

## After an unexpected exit

Restart Mouse Pressure once. At startup, it processes any remaining local
restore state. Verify DPI and both haptic channels on the Mouse page before
starting output again. Temporary recovery files are stored under
`%USERPROFILE%\.mouse-pressure` and are removed after successful restoration.

## Driver removal

Use Windows **Installed apps > Mouse Pressure > Uninstall**. The official
uninstaller stops output, removes the project-owned root-enumerated virtual pen,
and removes its driver package. Reboot if Windows reports that removal is
pending.

If normal uninstall fails, do not download third-party driver-removal tools.
Collect the installer log and open a support issue. Advanced manual removal with
`pnputil` should be performed only against the exact published INF name shown by
the official installer diagnostics.

## Reporting a failure

Include the release version, Windows build, wired/wireless state, backend, and
the action immediately before the failure. Do not publish raw `.pcapng` files,
full HID instance paths, usernames, or unreviewed stroke traces. Follow
[SECURITY.md](../SECURITY.md) for private vulnerability reports.
