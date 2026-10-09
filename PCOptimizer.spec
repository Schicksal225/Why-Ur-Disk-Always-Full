# -*- mode: python ; coding: utf-8 -*-
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Conda keeps the native libraries behind _ctypes, _ssl, _hashlib, pyexpat,
# _lzma and _bz2 in Library\bin, which PyInstaller does not search. Without
# them the exe dies with "DLL load failed while importing _ctypes".
_NATIVE = (
    "ffi.dll",
    "ffi-7.dll",
    "ffi-8.dll",
    "libexpat.dll",
    "liblzma.dll",
    "libbz2.dll",
    "libcrypto-3-x64.dll",
    "libssl-3-x64.dll",
)


def _conda_native():
    found = []
    for base in {sys.prefix, getattr(sys, "base_prefix", sys.prefix)}:
        folder = os.path.join(base, "Library", "bin")
        for name in _NATIVE:
            path = os.path.join(folder, name)
            if os.path.isfile(path) and all(os.path.basename(src).lower() != name for src, _ in found):
                found.append((path, "."))
    is_conda = any(
        os.path.isdir(os.path.join(base, "conda-meta"))
        for base in {sys.prefix, getattr(sys, "base_prefix", sys.prefix)}
    )
    if is_conda and not any(
        os.path.basename(src).lower().startswith("ffi") for src, _ in found
    ):
        raise SystemExit("ffi.dll not found under Library\\bin; the exe would fail to import _ctypes.")
    return found


datas = [
    ("web/index.html", "web"),
    ("web/app.css", "web"),
    ("web/app.js", "web"),
    ("VERSION", "."),
    ("assets", "assets"),
]
datas += collect_data_files("webview")
hidden = [name for name in collect_submodules("webview") if ".android" not in name and ".cocoa" not in name and ".gtk" not in name and ".qt" not in name] + ["psutil"]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=_conda_native(),
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "numpy", "PIL", "pygments", "jinja2", "mako", "IPython", "matplotlib", "pandas", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="PCOptimizer",
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
    icon="assets/icon.ico",
)
