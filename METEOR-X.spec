# -*- mode: python ; coding: utf-8 -*-
"""Portable PyInstaller specification for the METEOR-X GUI."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH).resolve()
datas = [
    (str(root / "_internal" / "assets"), "assets"),
    (str(root / "_internal" / "meteorrx" / "data"), "meteorrx/data"),
]
datas += collect_data_files("certifi")

a = Analysis(
    [str(root / "meteor_x_main.py")],
    pathex=[str(root / "_internal")],
    binaries=[],
    datas=datas,
    hiddenimports=["PySide6.QtNetwork"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineCore", "PySide6.QtPdf"],
    noarchive=False,
    optimize=0,
)
a.binaries = [entry for entry in a.binaries
              if entry[0].lower() not in ("icuuc.dll", "icudt78.dll")]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="METEOR-X",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=[str(root / "_internal" / "assets" / "meteor-x.ico")],
)
