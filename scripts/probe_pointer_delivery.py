"""Neutral WM_POINTER receiver with an optional synthetic-pen proof sequence.

The receiver records the message stream and GetPointerPenInfoHistory output.
It can therefore distinguish events delivered separately from events coalesced
into pointer history.  VMulti can be tested by running this receiver without
``--synthetic`` and drawing over its window with the normal driver UI.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import statistics
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from mouse_pressure.bridge.synthetic_pen import (
    POINTER_FLAG_DOWN,
    POINTER_FLAG_FIRSTBUTTON,
    POINTER_FLAG_INCONTACT,
    POINTER_FLAG_INRANGE,
    POINTER_FLAG_NEW,
    POINTER_FLAG_PRIMARY,
    POINTER_FLAG_UPDATE,
    POINTER_FLAG_UP,
    POINTER_PEN_INFO,
    WNDCLASSW,
    WNDPROC,
    _SyntheticPenInjector,
)
from mouse_pressure.bridge.native_synthetic import NativeSyntheticPenInjector

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_POINTERUPDATE = 0x0245
WM_POINTERDOWN = 0x0246
WM_POINTERUP = 0x0247
SW_SHOW = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=8.0)
    backend = parser.add_mutually_exclusive_group()
    backend.add_argument("--synthetic", action="store_true")
    backend.add_argument("--native-synthetic", action="store_true")
    parser.add_argument("--interval-ms", type=float, default=0.25)
    parser.add_argument("--points", type=int, default=24)
    parser.add_argument(
        "--output",
        default="work/pointer_probe/pointer-delivery.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    events: list[dict[str, Any]] = []
    injected: list[dict[str, Any]] = []
    start = time.perf_counter()

    get_history = user32.GetPointerPenInfoHistory
    get_history.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(POINTER_PEN_INFO),
    ]
    get_history.restype = wintypes.BOOL
    get_pen_info = user32.GetPointerPenInfo
    get_pen_info.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(POINTER_PEN_INFO),
    ]
    get_pen_info.restype = wintypes.BOOL

    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = ctypes.c_ushort
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    user32.UnregisterClassW.restype = wintypes.BOOL
    user32.CreateWindowExW.argtypes = [
        ctypes.c_uint32,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.DestroyWindow.argtypes = [ctypes.c_void_p]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.DefWindowProcW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
    ]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.PostMessageW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetFocus.argtypes = [ctypes.c_void_p]
    user32.SetFocus.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p

    @WNDPROC
    def window_proc(hwnd: int, message: int, w_param: int, l_param: int) -> int:
        if message in (WM_POINTERDOWN, WM_POINTERUPDATE, WM_POINTERUP):
            pointer_id = int(w_param) & 0xFFFF
            latest = POINTER_PEN_INFO()
            count = ctypes.c_uint32(1)
            if get_pen_info(pointer_id, ctypes.byref(latest)):
                count.value = max(1, int(latest.pointerInfo.historyCount))
            history: list[dict[str, Any]] = []
            if count.value:
                buffer = (POINTER_PEN_INFO * count.value)()
                if get_history(pointer_id, ctypes.byref(count), buffer):
                    for item in buffer[: count.value]:
                        info = item.pointerInfo
                        history.append(
                            {
                                "x": int(info.ptPixelLocation.x),
                                "y": int(info.ptPixelLocation.y),
                                "pressure": int(item.pressure),
                                "flags": int(info.pointerFlags),
                                "dw_time": int(info.dwTime),
                                "performance_count": int(info.PerformanceCount),
                                "history_count": int(info.historyCount),
                            }
                        )
            events.append(
                {
                    "message": int(message),
                    "pointer_id": pointer_id,
                    "received_ms": (time.perf_counter() - start) * 1000.0,
                    "history": history,
                }
            )
            return 0
        if message == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return int(user32.DefWindowProcW(hwnd, message, w_param, l_param))

    class_name = f"MousePressurePointerProbe_{id(events):X}"
    module = kernel32.GetModuleHandleW(None)
    window_class = WNDCLASSW()
    window_class.lpfnWndProc = window_proc
    window_class.hInstance = module
    window_class.lpszClassName = class_name
    atom = user32.RegisterClassW(ctypes.byref(window_class))
    if not atom:
        raise ctypes.WinError(ctypes.get_last_error())
    hwnd = user32.CreateWindowExW(
        0,
        class_name,
        "Mouse Pressure pointer delivery probe",
        0x00CF0000,
        160,
        160,
        900,
        600,
        None,
        None,
        module,
        None,
    )
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    user32.ShowWindow(hwnd, SW_SHOW)
    user32.UpdateWindow(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetFocus(hwnd)

    def inject_sequence() -> None:
        time.sleep(0.75)
        injector = (
            NativeSyntheticPenInjector(log=lambda _line: None)
            if args.native_synthetic
            else _SyntheticPenInjector(log=lambda _line: None)
        )
        injector.open()
        try:
            count = max(2, int(args.points))
            for index in range(count):
                x = 300 + index * 14
                y = 330 + round(70.0 * (index / (count - 1)))
                if index == 0:
                    flags = (
                        POINTER_FLAG_NEW
                        | POINTER_FLAG_DOWN
                        | POINTER_FLAG_INRANGE
                        | POINTER_FLAG_INCONTACT
                        | POINTER_FLAG_FIRSTBUTTON
                        | POINTER_FLAG_PRIMARY
                    )
                else:
                    flags = (
                        POINTER_FLAG_UPDATE
                        | POINTER_FLAG_INRANGE
                        | POINTER_FLAG_INCONTACT
                        | POINTER_FLAG_FIRSTBUTTON
                        | POINTER_FLAG_PRIMARY
                    )
                pressure = 128 + round(index * (768 / (count - 1)))
                sent_at = time.perf_counter()
                ok, error = injector.inject(
                    flags=flags,
                    x=x,
                    y=y,
                    pressure_1024=pressure,
                    tag="pointer_probe",
                )
                injected.append(
                    {
                        "seq": index,
                        "sent_ms": (sent_at - start) * 1000.0,
                        "x": x,
                        "y": y,
                        "pressure": pressure,
                        "ok": bool(ok),
                        "error": int(error),
                    }
                )
                # Release the GIL while pacing. A Python busy-wait prevents this
                # process's WM_POINTER receiver from recording asynchronous
                # native-relay delivery and makes the worker look artificially
                # slower than the synchronous baseline.
                interval_s = max(0.0, float(args.interval_ms)) / 1000.0
                if interval_s > 0.0:
                    time.sleep(interval_s)
            injector.inject(
                flags=POINTER_FLAG_UP | POINTER_FLAG_PRIMARY,
                x=injected[-1]["x"],
                y=injected[-1]["y"],
                pressure_1024=0,
                tag="pointer_probe_up",
            )
        finally:
            injector.close()

    if args.synthetic or args.native_synthetic:
        threading.Thread(target=inject_sequence, daemon=True).start()

    timer = threading.Timer(
        float(args.duration),
        lambda: user32.PostMessageW(hwnd, WM_CLOSE, 0, 0),
    )
    timer.daemon = True
    timer.start()
    message = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
    finally:
        timer.cancel()
        user32.UnregisterClassW(class_name, module)

    history_points = sum(len(event["history"]) for event in events)
    first_delivery: dict[tuple[int, int, int], float] = {}
    for event in events:
        for point in event["history"]:
            key = (int(point["x"]), int(point["y"]), int(point["pressure"]))
            first_delivery.setdefault(key, float(event["received_ms"]))
    delivery_ms = [
        first_delivery[(int(item["x"]), int(item["y"]), int(item["pressure"]))]
        - float(item["sent_ms"])
        for item in injected
        if (int(item["x"]), int(item["y"]), int(item["pressure"])) in first_delivery
    ]
    sorted_delivery = sorted(delivery_ms)
    p95_index = max(
        0,
        min(
            len(sorted_delivery) - 1,
            round(len(sorted_delivery) * 0.95) - 1,
        ),
    )
    backend_name = (
        "native_synthetic"
        if args.native_synthetic
        else "synthetic"
        if args.synthetic
        else "external"
    )
    payload = {
        "schema_version": 1,
        "backend": backend_name,
        "interval_ms": (
            float(args.interval_ms)
            if args.synthetic or args.native_synthetic
            else None
        ),
        "sent": injected,
        "received_messages": events,
        "summary": {
            "sent_points": len(injected),
            "received_messages": len(events),
            "history_points": history_points,
            "messages_with_coalesced_history": sum(
                len(event["history"]) > 1 for event in events
            ),
            "matched_delivery_points": len(delivery_ms),
            "delivery_median_ms": (
                round(statistics.median(delivery_ms), 3) if delivery_ms else None
            ),
            "delivery_p95_ms": (
                round(sorted_delivery[p95_index], 3) if sorted_delivery else None
            ),
            "delivery_max_ms": (
                round(max(delivery_ms), 3) if delivery_ms else None
            ),
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2))
    print(f"Saved {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
