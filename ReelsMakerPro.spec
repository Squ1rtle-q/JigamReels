# -*- mode: python ; coding: utf-8 -*-
# Сборка: pyinstaller ReelsMakerPro.spec
# Перед сборкой положите в bin/: ffmpeg.exe, ffprobe.exe (и при необходимости yt-dlp.exe).

import os

try:
    _SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
except NameError:
    _SPEC_DIR = os.getcwd()

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

_datas = []
_datas += collect_data_files('qtawesome')

if os.path.isdir(os.path.join(_SPEC_DIR, 'resources')):
    _datas.append((os.path.join(_SPEC_DIR, 'resources'), 'resources'))

for _name in ('ffmpeg.exe', 'ffprobe.exe', 'yt-dlp.exe'):
    _p = os.path.join(_SPEC_DIR, 'bin', _name)
    if os.path.isfile(_p):
        _datas.append((_p, 'bin'))

_whisper_hi = [
    'faster_whisper',
    'ctranslate2',
    'av',
    'av.audio',
    'tiktoken',
    'tiktoken_ext',
    'whisper',
    'whisper.normalizers',
    'whisper.model',
]
try:
    _whisper_hi += collect_submodules('faster_whisper')
except Exception:
    pass

_a = Analysis(
    ['main.py'],
    pathex=[_SPEC_DIR],
    binaries=[],
    datas=_datas,
    hiddenimports=[
        'PyQt5.sip',
        'PyQt5.QtCore',
        'PyQt5.QtGui',
        'PyQt5.QtWidgets',
        'google.api_core',
        'google.auth',
        'google.oauth2',
        'google_auth_oauthlib',
        'googleapiclient',
        'googleapiclient.discovery_cache',
        'googleapiclient.discovery_cache.file_cache',
        'uritemplate',
    ] + _whisper_hi,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(_a.pure, _a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    _a.scripts,
    _a.binaries,
    _a.zipfiles,
    _a.datas,
    [],
    name='ReelsMakerPro',
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
)
