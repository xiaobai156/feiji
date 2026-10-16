# -*- mode: python ; coding: utf-8 -*-

from importlib.util import find_spec
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata, collect_submodules


source_script = Path(SPECPATH) / 'telegram_caption_downloader_gui.py'


paddlex_config_data = collect_data_files('paddlex', includes=['configs/**'])
paddle_binaries = collect_dynamic_libs('paddle')
if find_spec('nvidia') is not None:
    paddle_binaries += collect_dynamic_libs('nvidia')
ocr_dependency_metadata = []
for package_name in (
    'paddlepaddle-gpu',
    'paddleocr',
    'paddlex',
    'imagesize',
    'opencv-contrib-python',
    'pyclipper',
    'pypdfium2',
    'python-bidi',
    'shapely',
):
    ocr_dependency_metadata += copy_metadata(package_name)


a = Analysis(
    [str(source_script)],
    pathex=[],
    binaries=paddle_binaries,
    datas=paddlex_config_data + ocr_dependency_metadata,
    hiddenimports=['cryptg', 'paddleocr'] + collect_submodules('telethon'),
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
    [],
    name='登录飞机提取图片',
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
    exclude_binaries=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='登录飞机提取图片',
)
