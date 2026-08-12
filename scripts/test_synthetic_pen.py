"""Synthetic pen/touch injection test using Win32 userspace APIs.

Writes a detailed log to docs/synthetic_pen_test.txt.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path


OUTPUT_PATH = Path("docs/synthetic_pen_test.txt")

# Pointer types
PT_TOUCH = 2
PT_PEN = 3

# Feedback modes
POINTER_FEEDBACK_DEFAULT = 1
TOUCH_FEEDBACK_DEFAULT = 1

# Pen flags/mask
PEN_FLAG_NONE = 0x00000000
PEN_MASK_PRESSURE = 0x00000001

# Pointer flags
POINTER_FLAG_NEW = 0x00000001
POINTER_FLAG_INRANGE = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
POINTER_FLAG_DOWN = 0x00010000
POINTER_FLAG_UPDATE = 0x00020000
POINTER_FLAG_UP = 0x00040000

# Touch
TOUCH_FLAG_NONE = 0x00000000
TOUCH_MASK_CONTACTAREA = 0x00000001
TOUCH_MASK_ORIENTATION = 0x00000002
TOUCH_MASK_PRESSURE = 0x00000004

# Virtual keys / metrics
VK_LBUTTON = 0x01
SM_CXSCREEN = 0
SM_CYSCREEN = 1

FPS = 60.0
TEST_SECONDS = 10.0


class POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerType", wintypes.DWORD),
        ("pointerId", wintypes.UINT),
        ("frameId", wintypes.UINT),
        ("pointerFlags", wintypes.DWORD),
        ("sourceDevice", wintypes.HANDLE),
        ("hwndTarget", wintypes.HWND),
        ("ptPixelLocation", wintypes.POINT),
        ("ptHimetricLocation", wintypes.POINT),
        ("ptPixelLocationRaw", wintypes.POINT),
        ("ptHimetricLocationRaw", wintypes.POINT),
        ("dwTime", wintypes.DWORD),
        ("historyCount", wintypes.UINT),
        ("InputData", ctypes.c_int),
        ("dwKeyStates", wintypes.DWORD),
        ("PerformanceCount", ctypes.c_uint64),
        ("ButtonChangeType", wintypes.DWORD),
    ]


class POINTER_PEN_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("penFlags", wintypes.DWORD),
        ("penMask", wintypes.DWORD),
        ("pressure", wintypes.UINT),
        ("rotation", wintypes.UINT),
        ("tiltX", ctypes.c_int),
        ("tiltY", ctypes.c_int),
    ]


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("touchFlags", wintypes.DWORD),
        ("touchMask", wintypes.DWORD),
        ("rcContact", wintypes.RECT),
        ("rcContactRaw", wintypes.RECT),
        ("orientation", wintypes.UINT),
        ("pressure", wintypes.UINT),
    ]


class POINTER_TYPE_UNION(ctypes.Union):
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("touchInfo", POINTER_TOUCH_INFO),
        ("penInfo", POINTER_PEN_INFO),
    ]


class POINTER_TYPE_INFO(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", POINTER_TYPE_UNION),
    ]


def _winerr_text(err: int) -> str:
    if err == 0:
        return "0 (ERROR_SUCCESS)"
    try:
        return f"{err} ({ctypes.WinError(err)})"
    except Exception:
        return str(err)


def _log_factory(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("w", encoding="ascii", errors="ignore")

    def log(line: str) -> None:
        print(line)
        fh.write(line + "\n")
        fh.flush()

    def close() -> None:
        fh.close()

    return log, close


def _get_dpi_for_system(user32) -> int:
    try:
        fn = user32.GetDpiForSystem
    except AttributeError:
        return 96
    fn.argtypes = []
    fn.restype = wintypes.UINT
    val = int(fn())
    return val if val > 0 else 96


def _to_himetric(px: int, dpi: int) -> int:
    # 1 HIMETRIC = 0.01 mm. pixels -> HIMETRIC via 2540 / dpi.
    return int(round(float(px) * 2540.0 / float(dpi)))


def _fill_common_pointer_info(
    pi: POINTER_INFO,
    *,
    pointer_type: int,
    pointer_id: int,
    flags: int,
    x: int,
    y: int,
    dpi: int,
) -> None:
    pi.pointerType = pointer_type
    pi.pointerId = pointer_id
    pi.frameId = 0
    pi.pointerFlags = flags
    pi.sourceDevice = None
    pi.hwndTarget = None
    pi.ptPixelLocation = wintypes.POINT(x, y)
    pi.ptHimetricLocation = wintypes.POINT(_to_himetric(x, dpi), _to_himetric(y, dpi))
    pi.ptPixelLocationRaw = wintypes.POINT(x, y)
    pi.ptHimetricLocationRaw = wintypes.POINT(_to_himetric(x, dpi), _to_himetric(y, dpi))
    pi.dwTime = 0
    pi.historyCount = 1
    pi.InputData = 0
    pi.dwKeyStates = 0
    pi.PerformanceCount = 0
    pi.ButtonChangeType = 0


def _cursor_pos(user32) -> tuple[int, int]:
    pt = wintypes.POINT()
    ok = bool(user32.GetCursorPos(ctypes.byref(pt)))
    if not ok:
        return (0, 0)
    return (int(pt.x), int(pt.y))


def _lmb_down(user32) -> bool:
    return bool(int(user32.GetAsyncKeyState(VK_LBUTTON)) & 0x8000)


def _inject_pen_frame(
    *,
    user32,
    device,
    pointer_id: int,
    flags: int,
    x: int,
    y: int,
    pressure: int,
    dpi: int,
    log,
    tag: str,
) -> bool:
    pinfo = POINTER_TYPE_INFO()
    pinfo.type = PT_PEN
    _fill_common_pointer_info(
        pinfo.penInfo.pointerInfo,
        pointer_type=PT_PEN,
        pointer_id=pointer_id,
        flags=flags,
        x=x,
        y=y,
        dpi=dpi,
    )
    pinfo.penInfo.penFlags = PEN_FLAG_NONE
    pinfo.penInfo.penMask = PEN_MASK_PRESSURE
    pinfo.penInfo.pressure = max(0, min(1024, int(pressure)))
    pinfo.penInfo.rotation = 0
    pinfo.penInfo.tiltX = 0
    pinfo.penInfo.tiltY = 0

    ctypes.set_last_error(0)
    ok = bool(user32.InjectSyntheticPointerInput(device, ctypes.byref(pinfo), 1))
    err = ctypes.get_last_error()
    log(
        f"PEN {tag}: ok={int(ok)} err={_winerr_text(err)} "
        f"flags=0x{flags:08X} pos=({x},{y}) pressure={int(pinfo.penInfo.pressure)}"
    )
    return ok


def _inject_touch_frame(
    *,
    user32,
    pointer_id: int,
    flags: int,
    x: int,
    y: int,
    pressure: int,
    dpi: int,
    log,
    tag: str,
) -> bool:
    tinfo = POINTER_TOUCH_INFO()
    _fill_common_pointer_info(
        tinfo.pointerInfo,
        pointer_type=PT_TOUCH,
        pointer_id=pointer_id,
        flags=flags,
        x=x,
        y=y,
        dpi=dpi,
    )
    tinfo.touchFlags = TOUCH_FLAG_NONE
    tinfo.touchMask = TOUCH_MASK_CONTACTAREA | TOUCH_MASK_ORIENTATION | TOUCH_MASK_PRESSURE
    tinfo.rcContact = wintypes.RECT(x - 2, y - 2, x + 2, y + 2)
    tinfo.rcContactRaw = wintypes.RECT(x - 2, y - 2, x + 2, y + 2)
    tinfo.orientation = 90
    tinfo.pressure = max(0, min(1024, int(pressure)))

    ctypes.set_last_error(0)
    ok = bool(user32.InjectTouchInput(1, ctypes.byref(tinfo)))
    err = ctypes.get_last_error()
    log(
        f"TOUCH {tag}: ok={int(ok)} err={_winerr_text(err)} "
        f"flags=0x{flags:08X} pos=({x},{y}) pressure={int(tinfo.pressure)}"
    )
    return ok


def main() -> int:
    if sys.platform != "win32":
        print("This script is Windows-only.")
        return 1

    log, close_log = _log_factory(OUTPUT_PATH)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    # user32 signatures
    user32.CreateSyntheticPointerDevice.argtypes = [
        wintypes.DWORD,  # POINTER_INPUT_TYPE
        ctypes.c_ulong,  # maxCount
        wintypes.DWORD,  # POINTER_FEEDBACK_MODE
    ]
    user32.CreateSyntheticPointerDevice.restype = wintypes.HANDLE

    user32.InjectSyntheticPointerInput.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(POINTER_TYPE_INFO),
        wintypes.UINT,
    ]
    user32.InjectSyntheticPointerInput.restype = wintypes.BOOL

    # available on supported builds; guard for older SDK/runtime mismatch
    destroy_fn = getattr(user32, "DestroySyntheticPointerDevice", None)
    if destroy_fn is not None:
        destroy_fn.argtypes = [wintypes.HANDLE]
        destroy_fn.restype = None

    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int

    user32.InitializeTouchInjection.argtypes = [wintypes.UINT, wintypes.DWORD]
    user32.InitializeTouchInjection.restype = wintypes.BOOL
    user32.InjectTouchInput.argtypes = [wintypes.UINT, ctypes.POINTER(POINTER_TOUCH_INFO)]
    user32.InjectTouchInput.restype = wintypes.BOOL

    device = None
    try:
        dpi = _get_dpi_for_system(user32)
        sx = int(user32.GetSystemMetrics(SM_CXSCREEN))
        sy = int(user32.GetSystemMetrics(SM_CYSCREEN))
        log(
            f"Synthetic pen test start: screen={sx}x{sy} dpi={dpi} "
            f"duration={TEST_SECONDS:.1f}s fps={FPS:.1f}"
        )

        ctypes.set_last_error(0)
        device = user32.CreateSyntheticPointerDevice(PT_PEN, 1, POINTER_FEEDBACK_DEFAULT)
        err_create = ctypes.get_last_error()
        if not device:
            log(f"CreateSyntheticPointerDevice failed: err={_winerr_text(err_create)}")
            log("This may require a newer Windows build, permissions, or unsupported environment.")
            return 1

        log(f"CreateSyntheticPointerDevice succeeded: handle=0x{int(device):X}")

        x0, y0 = _cursor_pos(user32)
        log(f"Initial cursor position: ({x0},{y0})")
        log("Move mouse over Krita canvas and hold left click. Starting in 3s...")
        for n in range(3, 0, -1):
            log(f"  {n}...")
            time.sleep(1.0)

        pointer_id = 1
        in_contact = False
        next_t = time.perf_counter()
        end_t = next_t + TEST_SECONDS

        while True:
            now = time.perf_counter()
            if now >= end_t:
                break

            x, y = _cursor_pos(user32)
            down = _lmb_down(user32)

            if down:
                if not in_contact:
                    flags = POINTER_FLAG_NEW | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT | POINTER_FLAG_DOWN
                    in_contact = True
                    tag = "DOWN"
                else:
                    flags = POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
                    tag = "UPDATE"
                pressure = 512
            else:
                if in_contact:
                    flags = POINTER_FLAG_UP | POINTER_FLAG_INRANGE
                    in_contact = False
                    tag = "UP"
                    pressure = 0
                else:
                    flags = POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE
                    tag = "HOVER"
                    pressure = 0

            _inject_pen_frame(
                user32=user32,
                device=device,
                pointer_id=pointer_id,
                flags=flags,
                x=x,
                y=y,
                pressure=pressure,
                dpi=dpi,
                log=log,
                tag=tag,
            )

            next_t += 1.0 / FPS
            sleep_s = next_t - time.perf_counter()
            if sleep_s > 0:
                time.sleep(sleep_s)

        # Ensure a clean release
        if in_contact:
            x, y = _cursor_pos(user32)
            _inject_pen_frame(
                user32=user32,
                device=device,
                pointer_id=pointer_id,
                flags=POINTER_FLAG_UP | POINTER_FLAG_INRANGE,
                x=x,
                y=y,
                pressure=0,
                dpi=dpi,
                log=log,
                tag="FINAL_UP",
            )

        log("Synthetic pen sequence complete.")

        # Fallback: touch injection quick smoke test.
        ctypes.set_last_error(0)
        ok_init_touch = bool(user32.InitializeTouchInjection(1, TOUCH_FEEDBACK_DEFAULT))
        err_init_touch = ctypes.get_last_error()
        log(
            f"InitializeTouchInjection(maxCount=1, mode=TOUCH_FEEDBACK_DEFAULT): "
            f"ok={int(ok_init_touch)} err={_winerr_text(err_init_touch)}"
        )
        if ok_init_touch:
            tx, ty = _cursor_pos(user32)
            log(f"Touch fallback at cursor ({tx},{ty})")
            _inject_touch_frame(
                user32=user32,
                pointer_id=2,
                flags=POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT,
                x=tx,
                y=ty,
                pressure=512,
                dpi=dpi,
                log=log,
                tag="DOWN",
            )
            for _ in range(30):
                _inject_touch_frame(
                    user32=user32,
                    pointer_id=2,
                    flags=POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT,
                    x=tx,
                    y=ty,
                    pressure=512,
                    dpi=dpi,
                    log=log,
                    tag="UPDATE",
                )
                time.sleep(1.0 / FPS)
            _inject_touch_frame(
                user32=user32,
                pointer_id=2,
                flags=POINTER_FLAG_UP,
                x=tx,
                y=ty,
                pressure=0,
                dpi=dpi,
                log=log,
                tag="UP",
            )
            log("Touch fallback sequence complete.")

        log("DONE")
        return 0
    finally:
        if device and destroy_fn is not None:
            try:
                destroy_fn(device)
                log("DestroySyntheticPointerDevice called.")
            except Exception as exc:
                log(f"DestroySyntheticPointerDevice failed: {exc!r}")
        close_log()


if __name__ == "__main__":
    raise SystemExit(main())
