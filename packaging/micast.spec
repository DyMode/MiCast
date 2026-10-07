# PyInstaller spec for the MiCast Windows app.
# Build with: pyinstaller packaging/micast.spec  (see scripts/build-windows.ps1)

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent  # repo root

datas = [(str(ROOT / "licenses"), "licenses")]
web_dist = ROOT / "web" / "dist"
if web_dist.is_dir():
    datas.append((str(web_dist), "web/dist"))
else:
    raise SystemExit("web/dist missing — run `npm run build` in web/ first")
datas.append((str(ROOT / "assets" / "icons" / "windows" / "micast-64.png"), "assets/icons/windows"))

# No bundled ffmpeg.exe: transcoding is in-process via PyAV, whose wheels
# already ship the libavcodec/libavfilter shared libraries (collected below).

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("zeroconf")
    + collect_submodules("webview")
    + collect_submodules("pystray")
)

a = Analysis(
    [str(ROOT / "micast" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=collect_dynamic_libs("av"),
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "ruff"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MiCast",
    debug=False,
    strip=False,
    # UPX squeezes the pyd/dll payload (~30% off). The upx.exe location comes
    # from PyInstaller's --upx-dir CLI flag (build script passes it);
    # set MICAST_UPX=0 there to skip entirely.
    upx=os.environ.get("MICAST_UPX", "1") != "0",
    console=False,  # desktop app: WebView2 window + tray; logs go to micast.log
    icon=str(ROOT / "assets" / "icons" / "windows" / "micast.ico")
    if (ROOT / "assets" / "icons" / "windows" / "micast.ico").is_file()
    else None,
)
