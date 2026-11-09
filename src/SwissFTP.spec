# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['swiss_ftp.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['mock_swiss', 'swiss_client', 'swiss_ftp_gateway', 'ftplib'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PIL', 'numpy', 'matplotlib', 'pytest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SwissFTP',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='version_info.txt',
    icon=['assets/swissftp.ico'],
)
