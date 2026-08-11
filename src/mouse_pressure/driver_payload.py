"""Validate the project-owned virtual tablet driver release payload.

The application installer may only embed a driver package that satisfies this
contract.  In particular, this prevents a locally installed third-party VMulti
package from being copied into a public release by accident.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path


SCHEMA_VERSION = 1
PACKAGE_ID = "mouse-pressure-vmulti"
HARDWARE_ID = r"ROOT\MOUSEPRESSUREVMULTI"
MANIFEST_NAME = "driver-manifest.json"
REQUIRED_ROLES = (
    "inf",
    "catalog",
    "driver",
    "provisioner",
    "license",
    "third_party_notices",
)
FORBIDDEN_FILENAMES = {
    "devcon.exe",
    "difxapi.dll",
    "difxcmd.exe",
    "wdfcoinstaller01009.dll",
    "wintab32.dll",
}


class DriverPayloadError(ValueError):
    """Raised when a driver payload is incomplete or unsafe to package."""


@dataclass(frozen=True)
class DriverPayload:
    root: Path
    manifest_path: Path
    files: dict[str, Path]
    hashes: dict[str, str]


def _safe_filename(value: object, role: str) -> str:
    name = str(value or "").strip()
    path = Path(name)
    if not name or path.name != name or path.is_absolute() or ".." in path.parts:
        raise DriverPayloadError(f"Manifest role {role!r} must be a plain filename")
    return name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_driver_payload(root: str | Path) -> DriverPayload:
    payload_root = Path(root).expanduser().resolve()
    manifest_path = payload_root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise DriverPayloadError(f"Missing {MANIFEST_NAME}: {manifest_path}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriverPayloadError(f"Invalid driver manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise DriverPayloadError("Driver manifest must contain a JSON object")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise DriverPayloadError(
            f"Unsupported driver manifest schema: {manifest.get('schema_version')!r}"
        )
    if manifest.get("package_id") != PACKAGE_ID:
        raise DriverPayloadError(f"Expected package_id {PACKAGE_ID!r}")
    if str(manifest.get("hardware_id", "")).upper() != HARDWARE_ID:
        raise DriverPayloadError(f"Expected hardware_id {HARDWARE_ID!r}")

    declared_hashes = manifest.get("sha256")
    if not isinstance(declared_hashes, dict):
        raise DriverPayloadError("Manifest sha256 must be an object")

    files: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    for role in REQUIRED_ROLES:
        filename = _safe_filename(manifest.get(role), role)
        path = payload_root / filename
        if not path.is_file():
            raise DriverPayloadError(f"Missing {role} file: {filename}")
        expected = str(declared_hashes.get(filename, "")).strip().lower()
        if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            raise DriverPayloadError(f"Missing or invalid SHA-256 for {filename}")
        actual = _sha256(path)
        if actual != expected:
            raise DriverPayloadError(
                f"SHA-256 mismatch for {filename}: expected {expected}, got {actual}"
            )
        files[role] = path
        hashes[filename] = actual

    entries = list(payload_root.iterdir())
    directories = sorted(path.name for path in entries if path.is_dir())
    if directories:
        raise DriverPayloadError(
            "Driver payload must be flat; unexpected directories: "
            + ", ".join(directories)
        )
    present_names = {path.name.lower() for path in entries if path.is_file()}
    forbidden = sorted(present_names & FORBIDDEN_FILENAMES)
    if forbidden:
        raise DriverPayloadError(
            "Forbidden legacy/vendor installer files present: " + ", ".join(forbidden)
        )
    expected_names = {MANIFEST_NAME.lower()} | {
        path.name.lower() for path in files.values()
    }
    unexpected = sorted(present_names - expected_names)
    missing = sorted(expected_names - present_names)
    if unexpected or missing:
        details: list[str] = []
        if unexpected:
            details.append("unexpected files: " + ", ".join(unexpected))
        if missing:
            details.append("missing files: " + ", ".join(missing))
        raise DriverPayloadError(
            "Driver payload contents do not match manifest ("
            + "; ".join(details)
            + ")"
        )
    declared_names = {str(name).lower() for name in declared_hashes}
    required_hash_names = {path.name.lower() for path in files.values()}
    if declared_names != required_hash_names:
        raise DriverPayloadError(
            "Manifest sha256 entries must exactly match the packaged driver files"
        )

    if files["inf"].suffix.lower() != ".inf":
        raise DriverPayloadError("The inf role must reference an .inf file")
    if files["catalog"].suffix.lower() != ".cat":
        raise DriverPayloadError("The catalog role must reference a .cat file")
    if files["driver"].suffix.lower() != ".sys":
        raise DriverPayloadError("The driver role must reference a .sys file")
    if files["provisioner"].suffix.lower() != ".exe":
        raise DriverPayloadError("The provisioner role must reference an .exe file")

    inf_text = files["inf"].read_text(encoding="utf-8", errors="ignore").lower()
    if HARDWARE_ID.lower() not in inf_text:
        raise DriverPayloadError(f"INF does not declare {HARDWARE_ID}")
    if files["catalog"].name.lower() not in inf_text:
        raise DriverPayloadError("INF does not reference the declared catalog")
    if files["driver"].name.lower() not in inf_text:
        raise DriverPayloadError("INF does not reference the declared driver binary")
    if "mouse pressure" not in inf_text:
        raise DriverPayloadError("INF provider/product identity is not project-specific")

    return DriverPayload(
        root=payload_root,
        manifest_path=manifest_path,
        files=files,
        hashes=hashes,
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m mouse_pressure.driver_payload PAYLOAD_DIR")
        return 2
    try:
        payload = validate_driver_payload(args[0])
    except DriverPayloadError as exc:
        print(f"VMulti payload rejected: {exc}", file=sys.stderr)
        return 1
    print(f"VMulti payload accepted: {payload.root}")
    for role in REQUIRED_ROLES:
        print(f"  {role}: {payload.files[role].name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
