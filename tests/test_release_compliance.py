from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_license_bundle_is_complete_and_verified() -> None:
    licenses = _load_script("vendor_release_licenses")
    licenses.check()


def test_generated_sbom_lists_runtime_and_source_components(tmp_path: Path) -> None:
    metadata = _load_script("generate_release_metadata")
    sbom_path, revision_path = metadata.generate(tmp_path)

    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    names = {component["name"] for component in sbom["components"]}
    assert {
        "hidapi",
        "pygame-ce",
        "PyInstaller",
        "PySide6-Essentials",
        "shiboken6",
        "Python",
        "Qt",
        "OpenSSL",
        "libffi",
        "SDL2",
        "FreeType",
        "libpng",
    } <= names
    pyinstaller = next(
        component for component in sbom["components"]
        if component["name"] == "PyInstaller"
    )
    assert pyinstaller["licenses"] == [
        {"expression": "GPL-2.0-or-later WITH Bootloader-exception"}
    ]
    assert revision_path.read_text(encoding="utf-8").strip()


def test_sbom_does_not_advertise_removed_driver_or_app_plugin() -> None:
    metadata = _load_script("generate_release_metadata")
    regular_names = {item["name"] for item in metadata.build_sbom()["components"]}
    assert "Mouse Pressure VMulti" not in regular_names
    assert "Mouse Pressure Krita plugin" not in regular_names


def test_public_artifact_scan_rejects_private_device_data(
    tmp_path: Path, monkeypatch
) -> None:
    privacy = _load_script("check_public_artifacts")
    monkeypatch.setattr(privacy, "ROOT", tmp_path)
    diagnostic = tmp_path / "docs" / "sample.txt"
    diagnostic.parent.mkdir()
    diagnostic.write_text(
        r"path=\\?\HID#VID_046D&PID_C54D#7&20812e2d&0&0001#" + "\n"
        r"profile=C:\Users\private-user\.mouse-pressure",
        encoding="utf-8",
    )

    problems = privacy.scan([diagnostic])
    assert any("machine-specific HID instance path" in item for item in problems)
    assert any("Windows user profile path" in item for item in problems)


def test_public_artifact_scan_rejects_raw_capture_names(
    tmp_path: Path, monkeypatch
) -> None:
    privacy = _load_script("check_public_artifacts")
    monkeypatch.setattr(privacy, "ROOT", tmp_path)
    capture = tmp_path / "docs" / "ghub_payloads.csv"
    capture.parent.mkdir()
    capture.write_text("timestamp,payload\n", encoding="utf-8")
    assert any("not publishable" in item for item in privacy.scan([capture]))


def test_windows_packages_include_release_legal_material() -> None:
    installer = (ROOT / "packaging" / "windows" / "mouse_pressure.iss").read_text(
        encoding="utf-8"
    )
    spec = (ROOT / "packaging" / "windows" / "mouse_pressure.spec").read_text(
        encoding="utf-8"
    )
    assert "THIRD_PARTY_NOTICES.md" in installer
    assert "dist\\release-metadata" in installer
    assert "legal\\source\\krita-plugin" not in installer
    assert "IncludeVMultiDriver" not in installer
    assert "[Components]" not in installer
    assert "Components: application" not in installer
    assert "packaging\" / \"legal" in spec
    assert "release-metadata" in spec
