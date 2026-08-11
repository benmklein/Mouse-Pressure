from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mouse_pressure.driver_payload import (
    DriverPayloadError,
    validate_driver_payload,
)


def _write_payload(root: Path) -> dict[str, object]:
    files = {
        "mouse-pressure-vmulti.inf": (
            "[Version]\n"
            "Provider=Mouse Pressure\n"
            "CatalogFile=mouse-pressure-vmulti.cat\n"
            "[Models]\n"
            "%Device%=Install,ROOT\\MOUSEPRESSUREVMULTI\n"
            "[CopyFiles]\n"
            "mouse-pressure-vmulti.sys\n"
        ).encode(),
        "mouse-pressure-vmulti.cat": b"signed catalog placeholder",
        "mouse-pressure-vmulti.sys": b"signed driver placeholder",
        "MousePressureDriverCtl.exe": b"signed provisioner placeholder",
        "LICENSE-vmulti.txt": b"MIT license placeholder",
        "THIRD_PARTY_NOTICES.md": b"upstream notices placeholder",
    }
    for name, content in files.items():
        (root / name).write_bytes(content)
    manifest: dict[str, object] = {
        "schema_version": 1,
        "package_id": "mouse-pressure-vmulti",
        "hardware_id": "ROOT\\MOUSEPRESSUREVMULTI",
        "inf": "mouse-pressure-vmulti.inf",
        "catalog": "mouse-pressure-vmulti.cat",
        "driver": "mouse-pressure-vmulti.sys",
        "provisioner": "MousePressureDriverCtl.exe",
        "license": "LICENSE-vmulti.txt",
        "third_party_notices": "THIRD_PARTY_NOTICES.md",
        "sha256": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in files.items()
        },
    }
    (root / "driver-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_accepts_project_owned_payload_with_matching_hashes(tmp_path: Path) -> None:
    _write_payload(tmp_path)

    payload = validate_driver_payload(tmp_path)

    assert payload.files["inf"].name == "mouse-pressure-vmulti.inf"
    assert payload.files["provisioner"].name == "MousePressureDriverCtl.exe"


def test_rejects_vendor_or_legacy_installer_files(tmp_path: Path) -> None:
    _write_payload(tmp_path)
    (tmp_path / "devcon.exe").write_bytes(b"must not be redistributed")

    with pytest.raises(DriverPayloadError, match="Forbidden"):
        validate_driver_payload(tmp_path)


def test_rejects_tampered_driver_binary(tmp_path: Path) -> None:
    _write_payload(tmp_path)
    (tmp_path / "mouse-pressure-vmulti.sys").write_bytes(b"tampered")

    with pytest.raises(DriverPayloadError, match="SHA-256 mismatch"):
        validate_driver_payload(tmp_path)


def test_rejects_unexpected_unhashed_file(tmp_path: Path) -> None:
    _write_payload(tmp_path)
    (tmp_path / "extra-tool.exe").write_bytes(b"not declared")

    with pytest.raises(DriverPayloadError, match="unexpected files"):
        validate_driver_payload(tmp_path)


def test_rejects_non_project_hardware_identity(tmp_path: Path) -> None:
    manifest = _write_payload(tmp_path)
    manifest["hardware_id"] = "PENTABLET\\HID"
    (tmp_path / "driver-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    with pytest.raises(DriverPayloadError, match="hardware_id"):
        validate_driver_payload(tmp_path)
