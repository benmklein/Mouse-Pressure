"""Reject raw or machine-identifying diagnostics from the public source tree."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTIC_SUFFIXES = {".csv", ".json", ".log", ".md", ".txt"}
FORBIDDEN_DIAGNOSTIC_FILES = (
    re.compile(r"\.pcap(?:ng)?$", re.IGNORECASE),
    re.compile(r"(?:^|/)(?:stroke-|capture[_-]).*\.json$", re.IGNORECASE),
    re.compile(r"(?:^|/)archive/", re.IGNORECASE),
    re.compile(r"^docs/(?!investigation/results/).*\.csv$", re.IGNORECASE),
    re.compile(
        r"^docs/(?:ghub_payloads.*|.*(?:_log|_stdout|_probe|_enum|_test)\.(?:txt|json))$",
        re.IGNORECASE,
    ),
)
SENSITIVE_TEXT = (
    (
        "Windows user profile path",
        re.compile(r"[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\s]+"),
    ),
    (
        "machine-specific HID instance path",
        re.compile(r"HID#[^\r\n]*#\d+&[0-9A-Fa-f]{6,}&\d+&\d{4}#"),
    ),
    (
        "runtime handle or pointer",
        re.compile(r"(?:handle|ppd)=0x[0-9A-Fa-f]+", re.IGNORECASE),
    ),
)


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    paths = {ROOT / item.decode() for item in result.stdout.split(b"\0") if item}
    # Include newly authored documentation before it is staged so the release
    # check behaves the same locally and in CI.
    paths.update(path for path in (ROOT / "docs").rglob("*") if path.is_file())
    return sorted(paths)


def scan(paths: list[Path]) -> list[str]:
    problems: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(ROOT).as_posix()
        except ValueError:
            continue
        if relative.startswith("docs/"):
            for pattern in FORBIDDEN_DIAGNOSTIC_FILES:
                if pattern.search(relative):
                    problems.append(f"{relative}: raw diagnostic artifact is not publishable")
                    break
        if not relative.startswith("docs/") or path.suffix.lower() not in DIAGNOSTIC_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            problems.append(f"{relative}: could not read ({exc})")
            continue
        for description, pattern in SENSITIVE_TEXT:
            match = pattern.search(text)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"{relative}:{line}: {description}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    paths = [path.resolve() for path in args.paths] if args.paths else tracked_files()
    problems = scan(paths)
    if problems:
        print("Public-artifact privacy check failed:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("Public-artifact privacy check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
