# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from pathlib import PurePosixPath
import sys


ROOT = Path(SPECPATH).resolve()
sys.path.insert(0, str(ROOT))

from pet.sprite_config import ACTIVE_SPRITES


# Package only the reviewed runtime allowlist. Retired PNGs remain available in
# the source tree for future art passes without leaking into the product build.
ASSET_DATA = [
    (
        str(ROOT / "assets" / "moonshell" / f"{name}.png"),
        "assets/moonshell",
    )
    for name in ACTIVE_SPRITES
]
ASSET_DATA.append(
    (
        str(ROOT / "assets" / "branding" / "moonshell.ico"),
        "assets/branding",
    )
)

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=ASSET_DATA,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)

# The application only loads PNG sprites plus its Windows ICO. PyInstaller's
# generic Qt hooks otherwise collect every image/platform/TLS backend, including
# an OpenSSL backend that can resolve DLLs from unrelated software on PATH.
_excluded_binary_names = {
    "libcrypto-3-x64.dll",
    "libssl-3-x64.dll",
    "qdirect2d.dll",
    "qgif.dll",
    "qicns.dll",
    "qjpeg.dll",
    "qminimal.dll",
    "qoffscreen.dll",
    "qopensslbackend.dll",
    "qsvg.dll",
    "qsvgicon.dll",
    "qt6svg.dll",
    "qtga.dll",
    "qtiff.dll",
    "qtuiotouchplugin.dll",
    "qwbmp.dll",
    "qwebp.dll",
}


def _entry_name(entry):
    destination = str(entry[0]).replace("\\", "/")
    return PurePosixPath(destination).name.casefold()


a.binaries = [
    entry for entry in a.binaries
    if _entry_name(entry) not in _excluded_binary_names
]

_kept_qt_translations = {
    "qt_zh_cn.qm",
    "qt_zh_tw.qm",
    "qtbase_zh_cn.qm",
    "qtbase_zh_tw.qm",
}
a.datas = [
    entry
    for entry in a.datas
    if (
        "/translations/" not in str(entry[0]).replace("\\", "/").casefold()
        or _entry_name(entry) in _kept_qt_translations
    )
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MoonShell",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "branding" / "moonshell.ico"),
    version=str(ROOT / "packaging" / "windows_version_info.txt"),
    uac_admin=False,
    uac_uiaccess=False,
)

collect = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MoonShell",
)
