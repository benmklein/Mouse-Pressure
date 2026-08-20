"""Generate deterministic release metadata from the active build environment.

The checked-in legal bundle contains the license texts and source offer. This
script records the exact runtime versions used by a release build in a compact
CycloneDX SBOM and records the source revision used for the build.
"""

from __future__ import annotations

import argparse
import json
import ssl
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, metadata, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "dist" / "release-metadata"

RUNTIME_COMPONENTS = (
    {
        "distribution": "hidapi",
        "license": "BSD-3-Clause",
        "purl": "pkg:pypi/hidapi",
    },
    {
        "distribution": "pygame-ce",
        "license": "LGPL-2.1-only",
        "purl": "pkg:pypi/pygame-ce",
    },
    {
        "distribution": "PyInstaller",
        "license_expression": "GPL-2.0-or-later WITH Bootloader-exception",
        "purl": "pkg:pypi/pyinstaller",
    },
    {
        "distribution": "PySide6-Essentials",
        "license_expression": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
        "purl": "pkg:pypi/pyside6-essentials",
    },
    {
        "distribution": "shiboken6",
        "license_expression": "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only",
        "purl": "pkg:pypi/shiboken6",
    },
)

SOURCE_COMPONENTS = (
    {
        "name": "Lucide mouse icon",
        "license": "ISC",
        "type": "library",
        "url": "https://github.com/lucide-icons/lucide",
    },
)

PYGAME_NATIVE_COMPONENTS = (
    ("FLAC", "BSD-3-Clause"),
    ("FluidSynth", "LGPL-2.1-or-later"),
    ("FreeType", "FTL OR GPL-2.0-or-later"),
    ("libjpeg", "LicenseRef-IJG"),
    ("libmodplug", "LicenseRef-Public-Domain"),
    ("mpg123", "LGPL-2.1-only"),
    ("libogg/libvorbis", "BSD-3-Clause"),
    ("Opus", "BSD-3-Clause"),
    ("opusfile", "BSD-3-Clause"),
    ("libpng", "Libpng-2.0"),
    ("PortMidi", "MIT"),
    ("SDL2", "Zlib"),
    ("SDL2_image", "Zlib"),
    ("SDL2_mixer", "Zlib"),
    ("SDL_gfx", "Zlib"),
    ("sse2neon", "MIT"),
    ("libtiff", "libtiff"),
    ("libwebp", "BSD-3-Clause"),
    ("zlib", "Zlib"),
)


def _git_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "source archive (git revision unavailable)"
    return result.stdout.strip()


def _application_version() -> str:
    try:
        return version("mouse-pressure-driver")
    except PackageNotFoundError:
        namespace: dict[str, object] = {}
        init_path = ROOT / "src" / "mouse_pressure" / "__init__.py"
        exec(init_path.read_text(encoding="utf-8"), namespace)
        return str(namespace["__version__"])


def _package_component(spec: dict[str, str]) -> dict[str, object]:
    distribution = spec["distribution"]
    try:
        package_version = version(distribution)
        package_metadata = metadata(distribution)
    except PackageNotFoundError as exc:
        raise RuntimeError(
            f"Required release distribution is not installed: {distribution}"
        ) from exc

    license_entry = (
        {"expression": spec["license_expression"]}
        if "license_expression" in spec
        else {"license": {"id": spec["license"]}}
    )
    component: dict[str, object] = {
        "type": "library",
        "name": distribution,
        "version": package_version,
        "purl": f"{spec['purl']}@{package_version}",
        "licenses": [license_entry],
    }
    homepage = package_metadata.get("Home-page")
    if homepage:
        component["externalReferences"] = [
            {"type": "website", "url": homepage}
        ]
    return component


def build_sbom() -> dict[str, object]:
    app_version = _application_version()
    components = [_package_component(spec) for spec in RUNTIME_COMPONENTS]
    components.append(
        {
            "type": "framework",
            "name": "Python",
            "version": platform_python_version(),
            "licenses": [{"license": {"id": "PSF-2.0"}}],
            "externalReferences": [
                {"type": "website", "url": "https://www.python.org/"}
            ],
        }
    )
    qt_version = version("PySide6-Essentials")
    components.extend(
        (
            {
                "type": "framework",
                "name": "Qt",
                "version": qt_version,
                "licenses": [
                    {
                        "expression": (
                            "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only"
                        )
                    }
                ],
                "externalReferences": [
                    {"type": "website", "url": "https://www.qt.io/"}
                ],
            },
            {
                "type": "library",
                "name": "OpenSSL",
                "version": ssl.OPENSSL_VERSION.split()[1],
                "licenses": [{"license": {"id": "Apache-2.0"}}],
                "externalReferences": [
                    {"type": "website", "url": "https://www.openssl.org/"}
                ],
                "properties": [
                    {"name": "distribution.source", "value": "Python runtime"}
                ],
            },
            {
                "type": "library",
                "name": "libffi",
                "licenses": [{"license": {"id": "MIT"}}],
                "properties": [
                    {"name": "distribution.source", "value": "Python runtime"}
                ],
            },
        )
    )
    for spec in SOURCE_COMPONENTS:
        components.append(
            {
                "type": spec["type"],
                "name": spec["name"],
                "licenses": [{"license": {"id": spec["license"]}}],
                "externalReferences": [
                    {"type": "vcs", "url": spec["url"]}
                ],
            }
        )
    for name, license_expression in PYGAME_NATIVE_COMPONENTS:
        components.append(
            {
                "type": "library",
                "group": "pygame-ce bundled runtime",
                "name": name,
                "licenses": [{"expression": license_expression}],
                "properties": [
                    {
                        "name": "license.notice.directory",
                        "value": "licenses/pygame-ce",
                    }
                ],
            }
        )
    components.sort(key=lambda item: str(item["name"]).casefold())

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "Mouse Pressure",
                "version": app_version,
                "licenses": [{"license": {"id": "MIT"}}],
                "externalReferences": [
                    {
                        "type": "vcs",
                        "url": "https://github.com/benmklein/analog_mouse_pressure",
                    }
                ],
                "properties": [
                    {"name": "source.revision", "value": _git_revision()}
                ],
            }
        },
        "components": components,
    }


def platform_python_version() -> str:
    return ".".join(str(part) for part in sys.version_info[:3])


def generate(output: Path) -> tuple[Path, Path]:
    legal_root = ROOT / "packaging" / "legal"
    required = (
        ROOT / "THIRD_PARTY_NOTICES.md",
        legal_root / "SOURCE_OFFER.md",
        legal_root / "licenses" / "GPL-3.0.txt",
        legal_root / "licenses" / "LGPL-3.0.txt",
        legal_root / "licenses" / "LGPL-2.1.txt",
    )
    missing = [path for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(
            "Release legal bundle is incomplete: "
            + ", ".join(str(path.relative_to(ROOT)) for path in missing)
        )

    output.mkdir(parents=True, exist_ok=True)
    sbom_path = output / "mouse-pressure.cdx.json"
    sbom_path.write_text(
        json.dumps(build_sbom(), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    revision_path = output / "SOURCE_REVISION.txt"
    revision_path.write_text(_git_revision() + "\n", encoding="utf-8")
    return sbom_path, revision_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        sbom_path, revision_path = generate(args.output.resolve())
    except RuntimeError as exc:
        parser.error(str(exc))
    print(f"Wrote {sbom_path}")
    print(f"Wrote {revision_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
