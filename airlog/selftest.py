"""Self-test for packaged builds:  Airlog.exe --selftest result.txt

Checks Tk, the bundled ffmpeg, and runs a 4-second capture from a test tone
through the real engine, then grabs 1 second. Writes a report and exits 0/1.
Used by the Windows build to verify the .exe before it's published.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import traceback
import wave
from pathlib import Path


def run(report: Path) -> int:
    lines: list[str] = []
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            detail = fn()
            lines.append(f"PASS  {name}  {detail or ''}".rstrip())
        except Exception as e:  # noqa: BLE001
            ok = False
            lines.append(f"FAIL  {name}  {e}")
            lines.append(traceback.format_exc())

    def tk_check():
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        img = tk.PhotoImage(file=str(Path(__file__).with_name("assets") / "icon.png"))
        v = f"Tk {tk.TkVersion}, icon {img.width()}x{img.height()}"
        root.destroy()
        return v

    def ffmpeg_check():
        from . import sources
        v = sources.ffmpeg_version("ffmpeg")
        if not v:
            raise RuntimeError("ffmpeg not found on PATH / next to the exe")
        return f"ffmpeg {v} at {shutil.which('ffmpeg')}; ffplay {'found' if shutil.which('ffplay') else 'MISSING'}"

    def engine_check():
        from .config import Config
        from .engine import Engine
        tmp = Path(tempfile.mkdtemp(prefix="airlog_selftest_"))
        try:
            cfg = Config(station_name="Selftest", source_type="custom",
                         custom_input_args="-re -f lavfi -i sine=frequency=440:sample_rate=48000",
                         archive_dir=str(tmp / "arch"), grabs_dir=str(tmp / "grabs"),
                         spool_dir=str(tmp / "spool"), buffer_minutes=5)
            e = Engine(cfg)
            e.start()
            deadline = time.time() + 15
            while time.time() < deadline and (e.live_time() is None or e.live_time() - (e.buffer_start() or 0) < 3):
                time.sleep(0.2)
            lt = e.live_time()
            if lt is None:
                raise RuntimeError("no audio captured; log: " + " | ".join(e.logs))
            g = e.grab(lt - 2, lt - 1)
            with wave.open(str(g)) as w:
                secs = w.getnframes() / w.getframerate()
            e.stop()
            time.sleep(1.5)
            files = list((tmp / "arch").rglob("*.mp3"))
            if not files or files[0].stat().st_size == 0:
                raise RuntimeError("archive file missing")
            if abs(secs - 1.0) > 0.01:
                raise RuntimeError(f"grab length {secs}")
            return f"captured, archive {files[0].name} {files[0].stat().st_size} bytes, grab {secs:.2f}s"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def web_check():
        import sys
        from .config import Config
        from .server import Holder, create_app
        tmp = Path(tempfile.mkdtemp(prefix="airlog_selftest_"))
        try:
            cfg = Config(station_name="Selftest", archive_dir=str(tmp / "arch"), grabs_dir=str(tmp / "grabs"),
                         spool_dir=str(tmp / "spool"))
            client = create_app(Holder(cfg, tmp / "config.json")).test_client()
            page = client.get("/")
            status = client.get("/api/status")
            icon = client.get("/favicon.ico")
            if page.status_code != 200 or b"Airlog" not in page.data:
                raise RuntimeError(f"/ returned {page.status_code}")
            if status.status_code != 200 or status.get_json().get("station") != "Selftest":
                raise RuntimeError(f"/api/status returned {status.status_code}")
            if icon.status_code != 200:
                raise RuntimeError(f"/favicon.ico returned {icon.status_code}")
            detail = f"UI page {len(page.data)} bytes, /api/status OK"
            if sys.platform.startswith("win"):
                import servicemanager, win32serviceutil  # noqa: F401,E401  (service host)
                import waitress  # noqa: F401
                detail += ", service modules OK"
            return detail
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    check("tkinter", tk_check)
    check("ffmpeg", ffmpeg_check)
    check("engine", engine_check)
    check("web", web_check)
    lines.append("RESULT  " + ("OK" if ok else "FAILED"))
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if ok else 1
