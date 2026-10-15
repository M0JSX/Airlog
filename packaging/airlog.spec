# PyInstaller spec for the Windows build.
#   pyinstaller --noconfirm packaging/airlog.spec      (run from the project root)
# Produces dist/Airlog/Airlog.exe (one-folder build: fast start, fewer antivirus false positives).
# The same exe is the UI launcher, the Windows service (--service) and the classic desktop app (--classic).
from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "run_airlog.pyw")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "airlog" / "assets"), "airlog/assets"),
           (str(ROOT / "airlog" / "web"), "airlog/web")],
    hiddenimports=["airlog.ui", "airlog.settings", "airlog.osutil", "airlog.selftest",
                   "airlog.server", "airlog.winservice", "waitress",
                   "servicemanager", "win32serviceutil", "win32service", "win32event", "win32timezone"],
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
