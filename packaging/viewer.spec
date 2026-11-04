# PyInstaller spec for Airlog Viewer (Windows, Linux, macOS).
#   pyinstaller --noconfirm packaging/viewer.spec      (build_windows.ps1 / build_unix.sh run this)
#   Windows: dist/AirlogViewer/AirlogViewer.exe
#   Linux:   dist/AirlogViewer/AirlogViewer
#   macOS:   dist/Airlog Viewer.app
# The viewer records nothing: no ffmpeg, Flask or Shazam, just Tk, zeroconf and keyring.
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent
WIN = sys.platform.startswith("win")
MAC = sys.platform == "darwin"
VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "airlog" / "__init__.py").read_text()).group(1)

datas, binaries, hidden = [], [], []
for pkg in ("zeroconf", "ifaddr", "keyring"):          # keyring backends are found through entry points
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h
if sys.platform.startswith("linux"):
    for pkg in ("secretstorage", "jeepney"):             # Secret Service (GNOME Keyring / KWallet)
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hidden += h

a = Analysis(
    [str(ROOT / "run_viewer.pyw")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=[(str(ROOT / "airlog" / "assets" / "viewer.png"), "airlog/assets")] + datas,
    hiddenimports=["airlog.viewer", "airlog.discovery", "airlog.servicecore"] + hidden,
    # servicecore can host the recorder; the viewer never does, so leave the recorder out.
    excludes=["flask", "werkzeug", "jinja2", "waitress", "shazamio", "shazamio_core", "aiohttp", "pydub",
              "sounddevice", "_sounddevice", "_sounddevice_data",
              "airlog.server", "airlog.engine", "airlog.ui", "airlog.settings", "airlog.nowplaying",
              "airlog.sources", "airlog.winservice", "airlog.unixservice", "PIL", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AirlogViewer",
    icon=str(ROOT / "packaging" / ("viewer.ico" if WIN else "viewer.icns")) if (WIN or MAC) else None,
    version=str(ROOT / "packaging" / "viewer_version_info.txt") if WIN else None,
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="AirlogViewer", upx=False)

if MAC:
    app = BUNDLE(
        coll,
        name="Airlog Viewer.app",
        icon=str(ROOT / "packaging" / "viewer.icns"),
        bundle_identifier="radio.m0jsx.airlog.viewer",
        version=VERSION,
        info_plist={
            "CFBundleName": "Airlog Viewer",
            "CFBundleDisplayName": "Airlog Viewer",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            # macOS 14+: asked once, the first time it looks for recorders on the network.
            "NSLocalNetworkUsageDescription": "Airlog Viewer looks for Airlog recorders on your network.",
            "NSBonjourServices": ["_airlog._tcp"],
        },
    )
