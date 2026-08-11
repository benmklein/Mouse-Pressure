# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

ROOT = Path.cwd()
SOURCE = ROOT / "src"
PACKAGE = SOURCE / "mouse_pressure"

hidden_imports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "mouse_pressure.web.device_restore_watchdog",
]

a = Analysis(
    [str(PACKAGE / "app.py")],
    pathex=[str(SOURCE)],
    binaries=[],
    datas=[
        (str(PACKAGE / "assets"), "mouse_pressure/assets"),
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "matplotlib",
        "numpy",
        "pytest",
        "tkinter",
        "websockets",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MousePressure",
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
    version=str(ROOT / "packaging" / "windows" / "mouse_pressure_version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MousePressure",
)
