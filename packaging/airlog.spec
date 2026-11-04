# PyInstaller spec for Windows, Linux and macOS.
#   pyinstaller --noconfirm packaging/airlog.spec      (run from the project root; build_windows.ps1 /
#                                                       build_unix.sh do this for you)
# Produces a one-folder build (fast start, fewer antivirus false positives):
#   Windows: dist/Airlog/Airlog.exe        (ffmpeg is copied next to it by build_windows.ps1)
#   Linux:   dist/Airlog/Airlog            (ffmpeg bundled from $AIRLOG_FFMPEG_DIR)
#   macOS:   dist/Airlog.app               (ffmpeg bundled from $AIRLOG_FFMPEG_DIR)
# The same executable is the UI launcher, the recorder service (--service) and the classic desktop
# app (--classic).
import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent
WIN = sys.platform.startswith("win")
MAC = sys.platform == "darwin"
VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "airlog" / "__init__.py").read_text()).group(1)

# Song identification (unofficial Shazam client) and its native fingerprinting module.
shz_datas, shz_bins, shz_hidden = [], [], []
for pkg in ("shazamio", "shazamio_core", "zeroconf", "ifaddr"):   # + mDNS announcement for Airlog Viewer
    d, b, h = collect_all(pkg)
    shz_datas += d; shz_bins += b; shz_hidden += h
# Sound card outputs: sounddevice + PortAudio. Windows/macOS wheels carry PortAudio in _sounddevice_data;
# Linux uses the system's libportaudio2 (a dependency of the .deb).
for pkg in ("sounddevice", "_sounddevice", "_sounddevice_data"):
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue
    shz_datas += d; shz_bins += b; shz_hidden += h + [pkg]

hidden = ["airlog.ui", "airlog.settings", "airlog.osutil", "airlog.selftest", "airlog.server",
          "airlog.servicecore", "airlog.nowplaying", "airlog.discovery", "waitress"]
if WIN:
    hidden += ["airlog.winservice", "servicemanager", "win32serviceutil", "win32service", "win32event", "win32timezone"]
else:
    hidden += ["airlog.unixservice"]

# macOS / Linux: ffmpeg, ffprobe (and ffplay if present) go inside the bundle; the app puts
# sys._MEIPASS on PATH at start-up. build_unix.sh sets AIRLOG_FFMPEG_DIR.
ff_bins = []
if not WIN:
    ff_dir = os.environ.get("AIRLOG_FFMPEG_DIR")
    if not ff_dir:
        raise SystemExit("Set AIRLOG_FFMPEG_DIR to a folder containing ffmpeg and ffprobe (build_unix.sh does this)")
    for tool in ("ffmpeg", "ffprobe", "ffplay"):
        p = Path(ff_dir) / tool
        if p.exists():
            ff_bins.append((str(p), "."))
        elif tool != "ffplay":
            raise SystemExit(f"{p} not found")

datas = [(str(ROOT / "airlog" / "assets"), "airlog/assets"), (str(ROOT / "airlog" / "web"), "airlog/web")]
lic = os.environ.get("AIRLOG_LICENSE_DIR")      # ffmpeg licence/notice, prepared by build_unix.sh
if lic and Path(lic).is_dir():
    datas.append((lic, "licenses"))

a = Analysis(
    [str(ROOT / "run_airlog.pyw")],
    pathex=[str(ROOT)],
    binaries=shz_bins + ff_bins,
    datas=datas + shz_datas,
    hiddenimports=hidden + shz_hidden,
    excludes=["PIL", "numpy", "airlog.viewer", "keyring"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Airlog",
    icon=str(ROOT / "packaging" / ("airlog.ico" if WIN else "airlog.icns")) if (WIN or MAC) else None,
    version=str(ROOT / "packaging" / "version_info.txt") if WIN else None,
    console=not (WIN or MAC),      # Linux: keep stdout so --install-service / the journal show output
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Airlog", upx=False)

if MAC:
    app = BUNDLE(
        coll,
        name="Airlog.app",
        icon=str(ROOT / "packaging" / "airlog.icns"),
        bundle_identifier="radio.m0jsx.airlog",
        version=VERSION,
        info_plist={
            "CFBundleName": "Airlog",
            "CFBundleDisplayName": "Airlog",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            # Shown when the recorder first captures from a sound card.
            "NSMicrophoneUsageDescription": "Airlog records the station from your sound card input.",
        },
    )
