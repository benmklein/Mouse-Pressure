"""Check whether the VMulti virtual HID driver/device is installed on Windows."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

import hid

KNOWN_IDENTITIES = {
    (0xF055, 0x0001),
    (0x00FF, 0xBACC),
    (0x00FF, 0xCAFE),
}
KNOWN_HWIDS = [
    r"ROOT\MOUSEPRESSUREVMULTI",
    "VID_F055&PID_0001",
    "VID_00FF&PID_BACC",
    "VID_00FF&PID_CAFE",
]
STRING_TOKENS = ["mouse pressure virtual pen", "vmulti", "virtualhid"]


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="ignore")
        except Exception:
            return repr(value)
    return str(value)


def _matches_device_fields(d: dict) -> bool:
    vid = int(d.get("vendor_id", -1))
    pid = int(d.get("product_id", -1))
    fields = " ".join(
        [
            _text(d.get("manufacturer_string")),
            _text(d.get("product_string")),
            _text(d.get("serial_number")),
            _text(d.get("path")),
        ]
    ).lower()
    if (vid, pid) in KNOWN_IDENTITIES:
        return True
    if any(tok in fields for tok in STRING_TOKENS):
        return True
    if any(hwid.lower() in fields for hwid in KNOWN_HWIDS):
        return True
    return False


@dataclass(frozen=True)
class HidCandidate:
    path: str
    vid: int
    pid: int
    usage_page: int | None
    usage: int | None
    manufacturer: str
    product: str


def enum_vmulti_hid_candidates() -> list[HidCandidate]:
    out: list[HidCandidate] = []
    for d in hid.enumerate():
        if not _matches_device_fields(d):
            continue
        out.append(
            HidCandidate(
                path=_text(d.get("path")),
                vid=int(d.get("vendor_id", 0)),
                pid=int(d.get("product_id", 0)),
                usage_page=d.get("usage_page"),
                usage=d.get("usage"),
                manufacturer=_text(d.get("manufacturer_string")),
                product=_text(d.get("product_string")),
            )
        )
    return out


def _run_command(args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
    except FileNotFoundError:
        return (127, "")
    text = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return (proc.returncode, text)


def check_pnputil() -> list[str]:
    rc, out = _run_command(["pnputil", "/enum-devices", "/connected", "/class", "HIDClass"])
    if rc != 0 or not out.strip():
        return []

    blocks = [b.strip() for b in out.split("\n\n") if b.strip()]
    matches: list[str] = []
    for block in blocks:
        low = block.lower()
        if any(hwid.lower() in low for hwid in KNOWN_HWIDS) or any(
            tok in low for tok in STRING_TOKENS
        ):
            lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
            snippet = " | ".join(lines[:4])
            matches.append(snippet)
    return matches


def check_registry() -> list[str]:
    roots = [
        r"HKLM\SYSTEM\CurrentControlSet\Enum\HID",
        r"HKLM\SYSTEM\CurrentControlSet\Enum\ROOT",
    ]
    terms = KNOWN_HWIDS + ["Mouse Pressure Virtual Pen", "vmulti", "VirtualHID"]
    matches: list[str] = []
    for root in roots:
        for term in terms:
            rc, out = _run_command(["reg", "query", root, "/s", "/f", term])
            if rc != 0 or not out.strip():
                continue
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            # keep just a compact snippet from each successful search
            first = ""
            for ln in lines:
                if ln.upper().startswith("HKEY_"):
                    first = ln
                    break
            if not first and lines:
                first = lines[0]
            if first:
                matches.append(f"{root} term={term} -> {first}")
    return matches


def main() -> int:
    print("Checking VMulti installation status...")
    print("")

    hid_matches = enum_vmulti_hid_candidates()
    pnputil_matches = check_pnputil()
    registry_matches = check_registry()

    if hid_matches:
        print("HID candidates:")
        for i, d in enumerate(hid_matches, start=1):
            print(
                f"  [{i}] VID:PID={d.vid:04X}:{d.pid:04X} "
                f"usage_page={d.usage_page} usage={d.usage}"
            )
            print(f"      path={d.path}")
            if d.manufacturer or d.product:
                print(f"      manufacturer={d.manufacturer!r} product={d.product!r}")
    else:
        print("HID candidates: none found")

    print("")
    if pnputil_matches:
        print("pnputil matches:")
        for row in pnputil_matches:
            print(f"  - {row}")
    else:
        print("pnputil matches: none found")

    print("")
    if registry_matches:
        print("Registry matches:")
        for row in registry_matches[:20]:
            print(f"  - {row}")
        if len(registry_matches) > 20:
            print(f"  ... and {len(registry_matches) - 20} more")
    else:
        print("Registry matches: none found")

    installed = bool(hid_matches or pnputil_matches or registry_matches)
    print("")
    if installed:
        print("RESULT: VMulti appears to be installed.")
        if hid_matches:
            print("Use one of the HID device paths above for emitter binding.")
        return 0

    print("RESULT: VMulti not detected.")
    print("A compatible virtual tablet driver is not installed.")
    print("Use the synthetic backend for development, or install the project-owned")
    print("Microsoft-signed driver when it becomes available in a release installer.")
    print("Do not redistribute the unrelated Pentablet/X9VoiD binary package.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
