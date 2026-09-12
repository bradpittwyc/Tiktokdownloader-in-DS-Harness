# -*- mode: python ; coding: utf-8 -*-
"""Build the portable Windows application from any working directory.

Run installer/build.ps1; all source paths are relative to this tracked spec.
The app launches the installed Chrome or Edge, so browser profiles and browser
downloads must never be copied into the EXE.
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


# PyInstaller sets SPECPATH to the folder containing this spec.
ROOT = Path(SPECPATH).resolve().parent
APP = ROOT / "outputs" / "TikTokBatchMVP"
ICON = APP / "ui" / "tiktok-logo.png"

datas = [(str(APP / "ui"), "ui")]
binaries = []
hiddenimports = ["webview.platforms.edgechromium", "session_store", "profile_pagination"]
for package in ("yt_dlp", "curl_cffi", "playwright", "webview", "docx"):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_imports)

a = Analysis(
    [str(APP / "web_app.py")],
    pathex=[str(APP)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "installer" / "frozen_self_test.py")],
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
    name="TikTokBatchMVP",
    icon=str(ICON),
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
