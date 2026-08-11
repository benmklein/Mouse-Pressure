# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

ROOT = Path.cwd()
SOURCE = ROOT / "src"
PACKAGE = SOURCE / "mouse_pressure"

a = Analysis(
    [str(SOURCE / "mouse_pressure_sandbox" / "main.py")],
    pathex=[str(SOURCE)],
    binaries=[],
    datas=[
        (
            str(SOURCE / "mouse_pressure_sandbox" / "THIRD_PARTY_NOTICES.md"),
            ".",
        ),
    ],
    hiddenimports=["pygame", "pygame._sdl2"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["IPython", "matplotlib", "numpy", "PySide6", "pytest", "tkinter"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MousePressureSandbox",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PACKAGE / "assets" / "lucide_mouse.ico"),
    version=str(
        ROOT
        / "packaging"
        / "windows"
        / "mouse_pressure_sandbox_version_info.txt"
    ),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MousePressureSandbox",
)
