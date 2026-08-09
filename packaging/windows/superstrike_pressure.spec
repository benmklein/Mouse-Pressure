# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

ROOT = Path.cwd()
SOURCE = ROOT / "src"
PACKAGE = SOURCE / "superstrike_pressure"

hidden_imports = [
    "pystray",
    "pystray._win32",
    "superstrike_pressure.web.device_restore_watchdog",
]

a = Analysis(
    [str(PACKAGE / "app.py")],
    pathex=[str(SOURCE)],
    binaries=[],
    datas=[
        (str(PACKAGE / "assets"), "superstrike_pressure/assets"),
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "matplotlib",
        "numpy",
        "pystray._appindicator",
        "pystray._darwin",
        "pystray._gtk",
        "pystray._xorg",
        "pytest",
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
    name="SuperstrikePressure",
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SuperstrikePressure",
)
