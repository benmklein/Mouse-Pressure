"""Refresh environment-supplied notices and validate the release license set.

The refresh operation copies authoritative texts available from the pinned
build environment and normalizes line endings. Standard GNU and pygame native
component texts are checked in from their pinned upstream source trees. CI uses
the default check mode so an upgrade cannot silently change the legal bundle.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "packaging" / "legal" / "licenses"


def _dist_license(distribution_name: str, relative: str) -> Path:
    try:
        dist = distribution(distribution_name)
    except PackageNotFoundError as exc:
        raise RuntimeError(f"Missing build distribution: {distribution_name}") from exc
    path = Path(dist.locate_file(relative))
    if not path.is_file():
        raise RuntimeError(f"License file is missing from {distribution_name}: {path}")
    return path


def sources() -> dict[str, Path]:
    return {
        "Mouse-Pressure-MIT.txt": ROOT / "LICENSE",
        "Lucide-ISC.txt": ROOT / "src" / "mouse_pressure" / "assets" / "LUCIDE_LICENSE.txt",
        "HIDAPI-BSD-3-Clause.txt": _dist_license(
            "hidapi", "hidapi-0.15.0.dist-info/licenses/LICENSE-bsd.txt"
        ),
        "GPL-3.0.txt": _dist_license(
            "hidapi", "hidapi-0.15.0.dist-info/licenses/LICENSE-gpl3.txt"
        ),
        "LGPL-2.1.txt": _dist_license(
            "pygame-ce", "pygame/docs/generated/LGPL.txt"
        ),
        "Python-PSF-2.0.txt": Path(sys.base_prefix) / "LICENSE.txt",
        "PyInstaller-Bootloader-Exception.txt": _dist_license(
            "PyInstaller", "pyinstaller-6.22.0.dist-info/licenses/COPYING.txt"
        ),
    }


EXPECTED_SHA256 = {
    "Mouse-Pressure-MIT.txt": "2af0ad225db193bc95abe912134d4cf05977b9115399285b7e640bfee2b92d93",
    "Lucide-ISC.txt": "b5f7e04bebd3538d064ffbffdfc4ed83d971e5a15d3388bb802f9f0190244a2d",
    "HIDAPI-BSD-3-Clause.txt": "5b4ef7439d2f82a2af57f1fb4a90366dd888a3d56444e5835441dd17ff31c716",
    "GPL-2.0.txt": "8177f97513213526df2cf6184d8ff986c675afb514d4e68a404010521b880643",
    "GPL-3.0.txt": "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903",
    "LGPL-3.0.txt": "da7eabb7bafdf7d3ae5e9f223aa5bdc1eece45ac569dc21b3b037520b4464768",
    "LGPL-2.1.txt": "76568dd3f7e12b18900ce0e767b99e59f4956a2e709a33de899508693a6425d4",
    "Python-PSF-2.0.txt": "bea187a467a1d78100a00dc904c232c9f2e1b35deaec44d40f790c2f33a2ebe8",
    "PyInstaller-Bootloader-Exception.txt": "dcf75fdb959db1e3b41c0f8505069d2ece781b5ec6b3d0a4d30975cfc6580245",
}

PYGAME_COMPONENT_SHA256 = {
    "LICENSE.FLAC.txt": "eafd0d3fac93964d4274a2f99be928d41c167a925a9ddb8621ab9aee065f8fee",
    "LICENSE.fluidsynth.txt": "9b872a8a070b8ad329c4bd380fb1bf0000f564c75023ec8e1e6803f15364b9e9",
    "LICENSE.freetype.txt": "08c135755dd589039470f1fdbb400daaabaaa50d0b366d19cebff4d22986baa1",
    "LICENSE.jpeg.txt": "9958e43a298e61bd287d89d382dafbb2d8346dcb3b7d166b8d70e21ab6d18cf8",
    "LICENSE.modplug.txt": "708581155da4f72895bb8a5ac80609d75774c29d0c5e05e41417bc21b6ed294c",
    "LICENSE.mpg123.txt": "da82453e8ebf465c0662d199d32c2e71e78094cb6f29a69d0ee190d32cc34381",
    "LICENSE.numpy.txt": "faaa516b85dbf609b0296918d61924e3145c9d2b8d91ea5be93e7ba073a78aa1",
    "LICENSE.ogg-vorbis.txt": "414bfd5d8ee64395a9231d6386188461500052c10915bee08b18bfa9489dd0b3",
    "LICENSE.opus.txt": "8338ce8d922bb4416ce3dd1e5680173332435e3f0755007ac7801ccd674fe682",
    "LICENSE.opusfile.txt": "0267ae795ab744c4e0f9c45e249440fdf2e75dac8c804f36066b28649bf74aaf",
    "LICENSE.png.txt": "bf5e22b9dce8464064ae17a48ea1133c3369ac9e1d80ef9e320e5219aa14ea9b",
    "LICENSE.portmidi.txt": "eb5d0724b2ae76a94ae804c44b3d6cfaaca822b7d42907aefa9256cb93db6fe0",
    "LICENSE.sdl_gfx.txt": "63744690fa28ecf2a9dedacafb08d1209031e7eff1820430d952264f678a1bcf",
    "LICENSE.sdl2_image.txt": "13d8725c1eec72984ac99449d2bc0f674ad956d67ae5819e787620e6c7b509e2",
    "LICENSE.sdl2_mixer.txt": "c70bbdde99ecb6d521d7806256b49dcc1a014bf5e28fca06ac031e1eeaea87f2",
    "LICENSE.sdl2.txt": "63af28e6db4bad012b6afbbca8311e9610635892439bd72e677dea3fa70f8c1e",
    "LICENSE.sse2neon-h.txt": "7869415c158160a6fc6b9bd4881f3455900af4af14f656c0df18d9a0bc7962f0",
    "LICENSE.tiff.txt": "92b72ba97e6c2749c2a94bc0ef646b47080217f1e772a482b33cf5a5f98a6506",
    "LICENSE.webp.txt": "e293d1dddc9785200b1f58a4f5293543cf8566d9e0b8a3c02fad955035b19f42",
    "LICENSE.zlib.txt": "464352f7afcece608041241e20b28e8006ae170446898e9713fb076323d1487d",
}

QT_LICENSE_MANIFEST = OUTPUT / "qtbase-6.11.1.sha256"
QT_LICENSE_MANIFEST_SHA256 = (
    "534d389cfb9ea1324a0d5c269d274c4ccf6aea5ccd4997802c426f82e20aeb11"
)


def normalized(path: Path) -> bytes:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return (text.rstrip() + "\n").encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def refresh() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, source in sources().items():
        if not source.is_file():
            raise RuntimeError(f"License source is missing: {source}")
        data = normalized(source)
        actual = digest(data)
        expected = EXPECTED_SHA256[name]
        if actual != expected:
            raise RuntimeError(
                f"Unexpected license text for {name}: {actual}; review the dependency update"
            )
        (OUTPUT / name).write_bytes(data)
        print(f"Wrote {OUTPUT / name}")


def check() -> None:
    missing: list[str] = []
    changed: list[str] = []
    for name, expected in EXPECTED_SHA256.items():
        path = OUTPUT / name
        if not path.is_file():
            missing.append(name)
        elif digest(normalized(path)) != expected:
            changed.append(name)
    pygame_root = OUTPUT / "pygame-ce"
    for name, expected in PYGAME_COMPONENT_SHA256.items():
        path = pygame_root / name
        label = f"pygame-ce/{name}"
        if not path.is_file():
            missing.append(label)
        elif digest(normalized(path)) != expected:
            changed.append(label)
    if not QT_LICENSE_MANIFEST.is_file():
        missing.append(QT_LICENSE_MANIFEST.name)
    # Git may materialize this text manifest with LF or CRLF depending on the
    # Windows checkout configuration. Its entries and the referenced upstream
    # license bytes are authoritative; newline representation is not.
    elif digest(normalized(QT_LICENSE_MANIFEST)) != QT_LICENSE_MANIFEST_SHA256:
        changed.append(QT_LICENSE_MANIFEST.name)
    else:
        qt_root = OUTPUT / "qtbase-6.11.1"
        expected_qt_files: set[str] = set()
        for line in QT_LICENSE_MANIFEST.read_text(encoding="utf-8").splitlines():
            expected, name = line.split("  ", 1)
            expected_qt_files.add(name)
            path = qt_root / name
            label = f"qtbase-6.11.1/{name}"
            if not path.is_file():
                missing.append(label)
            elif digest(path.read_bytes()) != expected:
                changed.append(label)
        if qt_root.is_dir():
            actual_qt_files = {path.name for path in qt_root.iterdir() if path.is_file()}
            for name in sorted(actual_qt_files - expected_qt_files):
                changed.append(f"qtbase-6.11.1/{name} (unexpected)")
    if missing or changed:
        parts = []
        if missing:
            parts.append("missing: " + ", ".join(missing))
        if changed:
            parts.append("changed: " + ", ".join(changed))
        raise RuntimeError("Release license bundle failed validation (" + "; ".join(parts) + ")")
    print("Release license bundle is complete and hash-verified.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    try:
        refresh() if args.refresh else check()
    except RuntimeError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
