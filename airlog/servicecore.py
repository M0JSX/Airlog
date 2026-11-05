"""What every platform's recorder service shares: hosting the engine + web UI,
checking whether the service is answering, and opening the UI in a window.

Platform specifics (installing, starting, stopping) live in winservice.py
(Windows Service Control Manager) and unixservice.py (systemd / launchd).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import lifecycle
from .config import Config

NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0


def stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def serve_until(config_path: Path, wait) -> None:
    """Run the recorder and web UI from config_path until wait() returns."""
    from waitress import create_server

    from . import sources
    from .server import Holder, create_app

    sources.HEADLESS = True     # a service can't reach a desktop PulseAudio/PipeWire session
    cfg = Config.load(config_path)
    if not config_path.exists():
        cfg.save(config_path)
    holder = Holder(cfg, config_path)
    if cfg.autostart:
        holder.engine.start()
    holder.start_announcing()
    srv = create_server(create_app(holder), host=cfg.web_host, port=cfg.web_port, threads=12, channel_timeout=600)
    threading.Thread(target=srv.run, daemon=True, name="web").start()
    print(f"{stamp()} Service started; UI on port {cfg.web_port}", flush=True)
    try:
        wait()
    finally:
        lifecycle.begin_shutdown()
        print(f"{stamp()} Service stopping", flush=True)
        # The web server isn't closed: closing its sockets under its own loop is what logged the
        # "Bad file descriptor" tracebacks, and it's a daemon thread that ends with the process.
        if holder.advertiser:
            holder.advertiser.close()
        holder.engine.stop()


def config_port(config_path: Path) -> int:
    try:
        return int(json.loads(config_path.read_text(encoding="utf-8")).get("web_port", 8080))
    except (OSError, ValueError):
        return 8080


def responding(port: int, timeout: float = 2) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=timeout):
            return True
    except urllib.error.HTTPError as e:
        return e.code == 401          # password protected, but it's up
    except OSError:
        return False


def wait_until(pred, secs: float) -> bool:
    deadline = time.time() + secs
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.5)
    return pred()


def migrate_config(target: Path, from_config: Path | None, data_dir: Path) -> None:
    """First install: carry the desktop app's settings over to the service's config."""
    if target.exists():
        return
    if from_config and from_config.exists():
        cfg = Config.load(from_config)
    else:
        cfg = Config(archive_dir=str(data_dir / "Archive"), grabs_dir=str(data_dir / "Grabs"))
    if not cfg.spool_dir:   # the service account's home may not be the user's; pin the buffer to the data folder
        cfg.spool_dir = str(data_dir / ".spool")
    target.parent.mkdir(parents=True, exist_ok=True)
    cfg.save(target)


# ---------------------------------------------------------------- UI window
def _find_chromium() -> list[str] | None:
    """A Chromium-family browser that supports --app (a chrome-less window), as a command prefix."""
    if sys.platform.startswith("win"):
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base:
                p = Path(base) / "Microsoft/Edge/Application/msedge.exe"
                if p.exists():
                    return [str(p)]
        w = shutil.which("msedge")
        return [w] if w else None
    if sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            if Path(f"/Applications/{app}.app").exists() or (Path.home() / f"Applications/{app}.app").exists():
                return ["open", "-na", app, "--args"]
        return None
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
                 "brave-browser"):
        w = shutil.which(name)
        if w:
            return [w]
    return None


def app_window_browser() -> str | None:
    """Name of the browser open_url() would use for an app window, or None (default browser)."""
    b = _find_chromium()
    if not b:
        return None
    return b[2] if b[0] == "open" else Path(b[0]).stem


def open_window(port: int) -> None:
    open_url(f"http://localhost:{port}/")


def open_url(url: str, app_window: bool = True) -> None:
    """Open url in a chrome-less Chrome/Edge window if one is installed (and wanted), else the default browser."""
    _clean_env_for_children()
    browser = _find_chromium() if app_window else None
    if browser:
        try:
            subprocess.Popen(browser + [f"--app={url}", "--window-size=1400,900"], creationflags=NO_WINDOW,
                             start_new_session=not sys.platform.startswith("win"))
            return
        except OSError:
            pass
    import webbrowser
    webbrowser.open(url)


def _clean_env_for_children() -> None:
    """A PyInstaller build on Linux points LD_LIBRARY_PATH at its own libraries; a browser we launch
    must not inherit that (it would load our older libssl/libstdc++ instead of the system's)."""
    if getattr(sys, "frozen", False) and sys.platform.startswith("linux"):
        orig = os.environ.get("LD_LIBRARY_PATH_ORIG")
        if orig is not None:
            os.environ["LD_LIBRARY_PATH"] = orig
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)


# ---------------------------------------------------------------- dialogs (macOS / Linux)
def ask(text: str) -> bool:
    """Yes/No dialog. Falls back to the terminal when there's no display."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        try:
            return bool(messagebox.askyesno("Airlog", text, parent=root))
        finally:
            root.destroy()
    except Exception:
        try:
            return input(text + " [y/N] ").strip().lower().startswith("y")
        except EOFError:
            return False


def tell(text: str, error: bool = False) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        try:
            (messagebox.showerror if error else messagebox.showinfo)("Airlog", text, parent=root)
        finally:
            root.destroy()
    except Exception:
        print(text, file=sys.stderr if error else sys.stdout)
