# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs, collect_data_files, copy_metadata

hiddenimports = ['pyvisa', 'pyvisa_py', 'usb', 'serial', 'usb.backend.libusb1']
hiddenimports += collect_submodules('pyvisa_py', filter=lambda name: 'testsuite' not in name)
hiddenimports += collect_submodules('pyvisa', filter=lambda name: 'testsuite' not in name)


a = Analysis(
    ['run.py'],
    pathex=[str(Path('.runtime').resolve())] if Path('.runtime').exists() else [],
    binaries=collect_dynamic_libs('libusb_package'),
    datas=[('scopehost.ico', '.')] + collect_data_files('libusb_package') + copy_metadata('PyVISA'),
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Qt 6.11 在 Windows 使用系统 ICU。宿主工具链的同名 ICU DLL
# 导出带版本后缀，会遮蔽系统 ICU；Windows API/UCRT 也交给系统。
a.binaries = [entry for entry in a.binaries
              if Path(entry[0]).name.lower() not in {'icuuc.dll', 'icudt78.dll', 'ucrtbase.dll'}
              and not Path(entry[0]).name.lower().startswith('api-ms-win-')]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ScopeHost',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['scopehost.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ScopeHost',
)
