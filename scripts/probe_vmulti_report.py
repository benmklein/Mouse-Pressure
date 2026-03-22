"""Probe VMulti HID report capabilities via Windows HID parser APIs (read-only)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path

OUTPUT_PATH = Path("docs/vmulti_descriptor_probe.txt")

TARGET_PATHS = [
    r"\\?\HID#hid&Col03#1&2d595ca7&0&0002#{4d1e55b2-f16f-11cf-88cb-001111000030}",
    r"\\?\HID#hid&Col04#1&2d595ca7&0&0003#{4d1e55b2-f16f-11cf-88cb-001111000030}",
]

USAGE = ctypes.c_ushort
NTSTATUS = ctypes.c_long

HIDP_REPORT_TYPE_INPUT = 0
HIDP_STATUS_SUCCESS = 0x00110000

FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", USAGE),
        ("UsagePage", USAGE),
        ("InputReportByteLength", wintypes.USHORT),
        ("OutputReportByteLength", wintypes.USHORT),
        ("FeatureReportByteLength", wintypes.USHORT),
        ("Reserved", wintypes.USHORT * 17),
        ("NumberLinkCollectionNodes", wintypes.USHORT),
        ("NumberInputButtonCaps", wintypes.USHORT),
        ("NumberInputValueCaps", wintypes.USHORT),
        ("NumberInputDataIndices", wintypes.USHORT),
        ("NumberOutputButtonCaps", wintypes.USHORT),
        ("NumberOutputValueCaps", wintypes.USHORT),
        ("NumberOutputDataIndices", wintypes.USHORT),
        ("NumberFeatureButtonCaps", wintypes.USHORT),
        ("NumberFeatureValueCaps", wintypes.USHORT),
        ("NumberFeatureDataIndices", wintypes.USHORT),
    ]


class HIDP_VALUE_CAPS_RANGE(ctypes.Structure):
    _fields_ = [
        ("UsageMin", USAGE),
        ("UsageMax", USAGE),
        ("StringMin", wintypes.USHORT),
        ("StringMax", wintypes.USHORT),
        ("DesignatorMin", wintypes.USHORT),
        ("DesignatorMax", wintypes.USHORT),
        ("DataIndexMin", wintypes.USHORT),
        ("DataIndexMax", wintypes.USHORT),
    ]


class HIDP_VALUE_CAPS_NOT_RANGE(ctypes.Structure):
    _fields_ = [
        ("Usage", USAGE),
        ("Reserved1", USAGE),
        ("StringIndex", wintypes.USHORT),
        ("Reserved2", wintypes.USHORT),
        ("DesignatorIndex", wintypes.USHORT),
        ("Reserved3", wintypes.USHORT),
        ("DataIndex", wintypes.USHORT),
        ("Reserved4", wintypes.USHORT),
    ]


class HIDP_VALUE_CAPS_UNION(ctypes.Union):
    _fields_ = [
        ("Range", HIDP_VALUE_CAPS_RANGE),
        ("NotRange", HIDP_VALUE_CAPS_NOT_RANGE),
    ]


class HIDP_VALUE_CAPS(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("UsagePage", USAGE),
        ("ReportID", wintypes.BYTE),
        ("IsAlias", wintypes.BOOLEAN),
        ("BitField", wintypes.USHORT),
        ("LinkCollection", wintypes.USHORT),
        ("LinkUsage", USAGE),
        ("LinkUsagePage", USAGE),
        ("IsRange", wintypes.BOOLEAN),
        ("IsStringRange", wintypes.BOOLEAN),
        ("IsDesignatorRange", wintypes.BOOLEAN),
        ("IsAbsolute", wintypes.BOOLEAN),
        ("HasNull", wintypes.BOOLEAN),
        ("Reserved", wintypes.BYTE),
        ("BitSize", wintypes.USHORT),
        ("ReportCount", wintypes.USHORT),
        ("Reserved2", wintypes.USHORT * 5),
        ("UnitsExp", wintypes.ULONG),
        ("Units", wintypes.ULONG),
        ("LogicalMin", wintypes.LONG),
        ("LogicalMax", wintypes.LONG),
        ("PhysicalMin", wintypes.LONG),
        ("PhysicalMax", wintypes.LONG),
        ("u", HIDP_VALUE_CAPS_UNION),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
hiddll = ctypes.WinDLL("hid", use_last_error=True)

kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

hiddll.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hiddll.HidD_GetPreparsedData.restype = wintypes.BOOLEAN

hiddll.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hiddll.HidD_FreePreparsedData.restype = wintypes.BOOLEAN

hiddll.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hiddll.HidP_GetCaps.restype = NTSTATUS

hiddll.HidP_GetValueCaps.argtypes = [
    ctypes.c_int,
    ctypes.POINTER(HIDP_VALUE_CAPS),
    ctypes.POINTER(wintypes.USHORT),
    ctypes.c_void_p,
]
hiddll.HidP_GetValueCaps.restype = NTSTATUS


def fmt_status(status: int) -> str:
    return f"0x{status & 0xFFFFFFFF:08X}"


def log_factory(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("w", encoding="ascii")

    def log(line: str) -> None:
        print(line)
        fh.write(line + "\n")
        fh.flush()

    def close() -> None:
        fh.close()

    return log, close


def try_open(path: str):
    # Try minimal access first, then read/write as fallback.
    for access in (0, GENERIC_READ | GENERIC_WRITE):
        handle = kernel32.CreateFileW(
            path,
            access,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle and handle != INVALID_HANDLE_VALUE:
            return handle, access
    err = ctypes.get_last_error()
    return None, err


def cap_usage_text(cap: HIDP_VALUE_CAPS) -> str:
    if bool(cap.IsRange):
        return f"UsageMin=0x{cap.Range.UsageMin:04X} UsageMax=0x{cap.Range.UsageMax:04X}"
    return f"Usage=0x{cap.NotRange.Usage:04X}"


def usage_matches(cap: HIDP_VALUE_CAPS, usage_page: int, usage: int) -> bool:
    if cap.UsagePage != usage_page:
        return False
    if bool(cap.IsRange):
        return cap.Range.UsageMin <= usage <= cap.Range.UsageMax
    return cap.NotRange.Usage == usage


def dump_caps(log, caps: HIDP_CAPS) -> None:
    log("HIDP_CAPS:")
    log(f"  Usage=0x{caps.Usage:04X}")
    log(f"  UsagePage=0x{caps.UsagePage:04X}")
    log(f"  InputReportByteLength={caps.InputReportByteLength}")
    log(f"  OutputReportByteLength={caps.OutputReportByteLength}")
    log(f"  FeatureReportByteLength={caps.FeatureReportByteLength}")
    log(f"  NumberLinkCollectionNodes={caps.NumberLinkCollectionNodes}")
    log(f"  NumberInputButtonCaps={caps.NumberInputButtonCaps}")
    log(f"  NumberInputValueCaps={caps.NumberInputValueCaps}")
    log(f"  NumberInputDataIndices={caps.NumberInputDataIndices}")
    log(f"  NumberOutputButtonCaps={caps.NumberOutputButtonCaps}")
    log(f"  NumberOutputValueCaps={caps.NumberOutputValueCaps}")
    log(f"  NumberOutputDataIndices={caps.NumberOutputDataIndices}")
    log(f"  NumberFeatureButtonCaps={caps.NumberFeatureButtonCaps}")
    log(f"  NumberFeatureValueCaps={caps.NumberFeatureValueCaps}")
    log(f"  NumberFeatureDataIndices={caps.NumberFeatureDataIndices}")


def probe_path(log, path: str) -> None:
    log("=" * 100)
    log(f"DevicePath: {path}")

    handle = None
    ppd = ctypes.c_void_p()
    try:
        opened, access_or_err = try_open(path)
        if opened is None:
            log(f"ERROR: CreateFileW failed err={access_or_err} ({ctypes.WinError(access_or_err)})")
            return

        handle = opened
        access = access_or_err
        log(f"CreateFileW: success handle=0x{int(handle):X} access=0x{access:08X}")

        ok = hiddll.HidD_GetPreparsedData(handle, ctypes.byref(ppd))
        if not ok or not ppd.value:
            err = ctypes.get_last_error()
            log(f"ERROR: HidD_GetPreparsedData failed err={err} ({ctypes.WinError(err)})")
            return
        log(f"HidD_GetPreparsedData: success ppd=0x{int(ppd.value):X}")

        caps = HIDP_CAPS()
        status_caps = int(hiddll.HidP_GetCaps(ppd, ctypes.byref(caps)))
        log(f"HidP_GetCaps: status={fmt_status(status_caps)}")
        if (status_caps & 0xFFFFFFFF) != HIDP_STATUS_SUCCESS:
            log("ERROR: HidP_GetCaps did not return HIDP_STATUS_SUCCESS")
            return
        dump_caps(log, caps)

        count = int(caps.NumberInputValueCaps)
        if count <= 0:
            log("Input Value Caps: none")
            return

        arr = (HIDP_VALUE_CAPS * count)()
        count_ref = wintypes.USHORT(count)
        status_vals = int(
            hiddll.HidP_GetValueCaps(
                HIDP_REPORT_TYPE_INPUT,
                arr,
                ctypes.byref(count_ref),
                ppd,
            )
        )
        log(
            f"HidP_GetValueCaps(HidP_Input): status={fmt_status(status_vals)} "
            f"returned_count={count_ref.value}"
        )
        if (status_vals & 0xFFFFFFFF) != HIDP_STATUS_SUCCESS:
            log("ERROR: HidP_GetValueCaps did not return HIDP_STATUS_SUCCESS")
            return

        pressure_cap = None
        x_cap = None
        y_cap = None

        log("Input Value Caps entries:")
        for i in range(count_ref.value):
            cap = arr[i]
            usage_part = cap_usage_text(cap)
            log(
                f"  [{i}] UsagePage=0x{cap.UsagePage:04X} ReportID=0x{cap.ReportID:02X} "
                f"IsRange={int(cap.IsRange)} IsAbsolute={int(cap.IsAbsolute)} "
                f"BitSize={cap.BitSize} ReportCount={cap.ReportCount} "
                f"LogicalMin={cap.LogicalMin} LogicalMax={cap.LogicalMax} "
                f"PhysicalMin={cap.PhysicalMin} PhysicalMax={cap.PhysicalMax} "
                f"{usage_part}"
            )

            if pressure_cap is None and usage_matches(cap, 0x000D, 0x0030):
                pressure_cap = cap
            if x_cap is None and usage_matches(cap, 0x0001, 0x0030):
                x_cap = cap
            if y_cap is None and usage_matches(cap, 0x0001, 0x0031):
                y_cap = cap

        log("Verdict:")
        log(f"  InputReportByteLength={caps.InputReportByteLength}")
        log(f"  Has X usage (0x01:0x30): {x_cap is not None}")
        log(f"  Has Y usage (0x01:0x31): {y_cap is not None}")
        has_pressure = pressure_cap is not None
        log(f"  Has pressure usage (0x0D:0x30): {has_pressure}")
        if pressure_cap is not None:
            log(
                "  Pressure Logical range: "
                f"{pressure_cap.LogicalMin}..{pressure_cap.LogicalMax} "
                f"(BitSize={pressure_cap.BitSize}, ReportID=0x{pressure_cap.ReportID:02X})"
            )
        else:
            log("  Pressure Logical range: <none>")
            if caps.InputReportByteLength <= 7:
                log("  Heuristic: report length <= 7 suggests classic non-pressure digitizer.")
            elif caps.InputReportByteLength >= 9:
                log("  Heuristic: report length >= 9 may allow pressure, but usage missing.")

    finally:
        if ppd.value:
            ok = hiddll.HidD_FreePreparsedData(ppd)
            log(f"HidD_FreePreparsedData: {'success' if ok else 'failed'}")
        if handle and handle != INVALID_HANDLE_VALUE:
            ok = kernel32.CloseHandle(handle)
            log(f"CloseHandle: {'success' if ok else 'failed'}")


def main() -> int:
    log, close = log_factory(OUTPUT_PATH)
    try:
        log("VMulti HID descriptor probe (read-only)")
        for path in TARGET_PATHS:
            probe_path(log, path)
        log("=" * 100)
        log(f"Output saved to {OUTPUT_PATH}")
    finally:
        close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
