# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['launch_meteor_x.py'],
    pathex=['_internal'],
    binaries=[],
    datas=[('_internal/meteorrx/data', 'meteorrx/data'), ('_internal/assets', 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Qt 6.12 imports Windows' system icuuc.dll. A development PATH containing
# another ICU build can make PyInstaller bundle an incompatible copy instead.
a.binaries = [entry for entry in a.binaries if entry[0].lower() != 'icuuc.dll']
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='METEOR-X-Mosaic',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['_internal/assets/meteor-x.ico'],
)
