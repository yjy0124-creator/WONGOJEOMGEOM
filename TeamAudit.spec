# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['team_app_desktop.py'],
    pathex=[],
    binaries=[],
    datas=[('team_web', 'team_web'), ('document.schema.json', '.'), ('music_editing_terms.json', '.')],
    # ai_activity_adapter는 importlib.import_module("ai_activity_adapter")로만
    # 동적으로 불러오므로, 정적 분석만으로는 PyInstaller가 이 모듈을 찾지 못해
    # 번들에서 빠진다 — hiddenimports로 명시해야 ANTHROPIC_API_KEY가 있어도
    # AI 활동 추천이 실제로 동작한다.
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
