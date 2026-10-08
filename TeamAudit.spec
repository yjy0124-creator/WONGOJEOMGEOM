# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['team_app_desktop.py'],
    pathex=[],
    binaries=[],
    datas=[('team_web', 'team_web'), ('document.schema.json', '.'), ('music_editing_terms.json', '.'), ('team_data/curricula', 'team_data/curricula')],
    hiddenimports=['ai_activity_adapter'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='TeamAudit',
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
)
