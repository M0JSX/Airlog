# PyInstaller spec for the Windows build.
#   pyinstaller --noconfirm packaging/airlog.spec      (run from the project root)
# Produces dist/Airlog/Airlog.exe (one-folder build: fast start, fewer antivirus false positives).
# The same exe is the UI launcher, the Windows service (--service) and the classic desktop app (--classic).
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

# Song identification (unofficial Shazam client) and its native fingerprinting module.
shz_datas, shz_bins, shz_hidden = [], [], []
for pkg in ("shazamio", "shazamio_core"):
    d, b, h = collect_all(pkg)
    shz_datas += d; shz_bins += b; shz_hidden += h

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "run_airlog.pyw")],
    pathex=[str(ROOT)],
    binaries=shz_bins,
    datas=[(str(ROOT / "airlog" / "assets"), "airlog/assets"),
           (str(ROOT / "airlog" / "web"), "airlog/web")] + shz_datas,
    hiddenimports=["airlog.ui", "airlog.settings", "airlog.osutil", "airlog.selftest",
                   "airlog.server", "airlog.winservice", "airlog.nowplaying", "waitress",
                   "servicemanager", "win32serviceutil", "win32service", "win32event", "win32timezone"] + shz_hidden,
    excludes=["PIL", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Airlog",
    icon=str(ROOT / "packaging" / "airlog.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Airlog", upx=False)
