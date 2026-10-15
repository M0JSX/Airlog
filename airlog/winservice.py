"""Windows service: the recorder runs headless under the Service Control Manager
and keeps recording whether or not anyone has the UI open (or is even signed in).

    Airlog.exe                      open the UI in an app window (starts the service if needed)
    Airlog.exe --install-service    copy to Program Files, register and start the service (admin)
    Airlog.exe --uninstall-service  stop and remove the service (admin)
    Airlog.exe --service            what the Service Control Manager runs

The service runs as LocalSystem, so its settings live machine-wide in
%ProgramData%\\Airlog\\config.json rather than in a user's %APPDATA%.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from .config import Config, config_dir, default_data_dir

SERVICE_NAME = "AirlogRecorder"
DISPLAY_NAME = "Airlog Recorder"
DESCRIPTION = "Records the station into hourly files and keeps the grab buffer. UI: Airlog.exe or http://<this-pc>:<port>/"
FIREWALL_RULE = "Airlog Recorder"

INSTALL_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Airlog"
DATA_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Airlog"
CONFIG = DATA_DIR / "config.json"
LOG = DATA_DIR / "logs" / "service.log"
INSTALL_LOG = DATA_DIR / "logs" / "install.log"

NO_WINDOW = 0x08000000


def stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL,
                       creationflags=NO_WINDOW)
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed ({p.returncode}): {(p.stdout + p.stderr).strip()}")
    return p


def _powershell(script: str, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], check=check)


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


# ---------------------------------------------------------------- service host
def _log_to_file() -> None:
    """A service has no console; send print()/tracebacks to a log file."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 5_000_000:
        LOG.replace(LOG.with_suffix(".old.log"))
    f = open(LOG, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = f


def serve_until(wait) -> None:
    """Run the recorder and web UI until wait() returns."""
    from waitress import create_server

    from .server import Holder, create_app

    cfg = Config.load(CONFIG)
    if not CONFIG.exists():
        cfg.save(CONFIG)
    holder = Holder(cfg, CONFIG)
    if cfg.autostart:
        holder.engine.start()
    srv = create_server(create_app(holder), host=cfg.web_host, port=cfg.web_port, threads=12, channel_timeout=600)
    threading.Thread(target=srv.run, daemon=True, name="web").start()
    print(f"{stamp()} Service started; UI on port {cfg.web_port}", flush=True)
    try:
        wait()
    finally:
        print(f"{stamp()} Service stopping", flush=True)
        srv.close()
        holder.engine.stop()


def run_service() -> None:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class AirlogService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = DISPLAY_NAME

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            try:
                serve_until(lambda: win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE))
            except Exception:
                traceback.print_exc()
                os._exit(1)   # die so the SCM recovery policy restarts us

    _log_to_file()
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(AirlogService)
    servicemanager.StartServiceCtrlDispatcher()


# ---------------------------------------------------------------- service state
def service_state() -> str | None:
    """'RUNNING', 'STOPPED', 'START_PENDING'… or None if not installed."""
    p = _run(["sc.exe", "query", SERVICE_NAME])
    if p.returncode:
        return None
    for line in p.stdout.splitlines():
        if "STATE" in line:
            return line.split()[-1]
    return "UNKNOWN"


def service_port() -> int:
    try:
        return int(json.loads(CONFIG.read_text(encoding="utf-8")).get("web_port", 8080))
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


# ---------------------------------------------------------------- install / uninstall (elevated)
def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _stop_service() -> None:
    if service_state() not in (None, "STOPPED"):
        _run(["sc.exe", "stop", SERVICE_NAME])
        wait_until(lambda: service_state() in (None, "STOPPED"), 30)


def _migrate_config(from_config: Path | None, data_dir: Path | None) -> None:
    """First install: carry the desktop app's settings over to the machine-wide config."""
    if CONFIG.exists():
        return
    data = data_dir or default_data_dir()
    from_config = from_config or config_dir() / "config.json"   # e.g. run by Setup.exe: the installing user's
    if from_config.exists():
        cfg = Config.load(from_config)
    else:
        cfg = Config(archive_dir=str(data / "Archive"), grabs_dir=str(data / "Grabs"))
    if not cfg.spool_dir:   # LocalSystem's home is inside System32; pin the buffer to the user's data folder
        cfg.spool_dir = str(data / ".spool")
    cfg.save(CONFIG)


def _shortcuts(exe: Path, create: bool) -> None:
    programs = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs"
    desktop = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"
    for lnk in (programs / "Airlog.lnk", desktop / "Airlog.lnk"):
        if create:
            _powershell(f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(lnk)}); "
                        f"$s.TargetPath = {_ps_quote(exe)}; $s.WorkingDirectory = {_ps_quote(exe.parent)}; "
                        f"$s.Description = 'Airlog'; $s.Save()")
        elif lnk.exists():
            lnk.unlink()


def install(from_config: Path | None, data_dir: Path | None, shortcuts: bool = True) -> None:
    src = Path(sys.executable).parent
    exe = INSTALL_DIR / "Airlog.exe"
    print(f"{stamp()} Installing from {src}")
    _stop_service()
    if src.resolve() != INSTALL_DIR.resolve():
        shutil.copytree(src, INSTALL_DIR, dirs_exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _migrate_config(from_config, data_dir)

    bin_path = f'"{exe}" --service'
    if service_state() is None:
        _run(["sc.exe", "create", SERVICE_NAME, "binPath=", bin_path, "start=", "auto",
              "DisplayName=", DISPLAY_NAME], check=True)
    else:
        _run(["sc.exe", "config", SERVICE_NAME, "binPath=", bin_path, "start=", "auto"], check=True)
    _run(["sc.exe", "description", SERVICE_NAME, DESCRIPTION])
    _run(["sc.exe", "failure", SERVICE_NAME, "reset=", "86400", "actions=", "restart/5000/restart/10000/restart/30000"],
         check=True)
    _run(["sc.exe", "failureflag", SERVICE_NAME, "1"])

    # Let signed-in users start/stop the service without a UAC prompt (Airlog.exe starts it if it's down).
    sd = _run(["sc.exe", "sdshow", SERVICE_NAME]).stdout.strip()
    if "(A;;CCLCSWLOCRRC;;;IU)" in sd:
        _run(["sc.exe", "sdset", SERVICE_NAME, sd.replace("(A;;CCLCSWLOCRRC;;;IU)", "(A;;CCLCSWRPWPDTLOCRRC;;;IU)")])

    port = service_port()
    _powershell(f"Get-NetFirewallRule -DisplayName {_ps_quote(FIREWALL_RULE)} -ErrorAction SilentlyContinue | "
                f"Remove-NetFirewallRule; New-NetFirewallRule -DisplayName {_ps_quote(FIREWALL_RULE)} -Direction Inbound "
                f"-Action Allow -Program {_ps_quote(exe)} -Protocol TCP -LocalPort {port} -Profile Private,Domain | Out-Null")
    if shortcuts:   # Setup.exe makes its own
        _shortcuts(exe, create=True)

    _run(["sc.exe", "start", SERVICE_NAME])
    if not wait_until(lambda: responding(port), 30):
        raise RuntimeError(f"Service installed but not answering on port {port}; see {LOG}")
    print(f"{stamp()} Installed; running from {exe}")


def uninstall() -> None:
    _stop_service()
    if service_state() is not None:
        _run(["sc.exe", "delete", SERVICE_NAME], check=True)
    _powershell(f"Get-NetFirewallRule -DisplayName {_ps_quote(FIREWALL_RULE)} -ErrorAction SilentlyContinue | "
                f"Remove-NetFirewallRule", check=False)
    _shortcuts(INSTALL_DIR / "Airlog.exe", create=False)
    print(f"{stamp()} Uninstalled (settings and recordings kept)")


def admin_command(action: str, from_config: Path | None = None, data_dir: Path | None = None,
                  shortcuts: bool = True) -> int:
    """Entry point for --install-service / --uninstall-service. Logs to install.log for the caller."""
    if not is_admin():
        args = [f"--{action}-service"] + ([] if shortcuts else ["--no-shortcuts"])
        if action == "install":   # the elevated copy may run as another account; point it at this user's settings
            args += ["--from-config", str(from_config or config_dir() / "config.json"),
                     "--data-dir", str(data_dir or default_data_dir())]
        return run_elevated(args)
    INSTALL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(INSTALL_LOG, "w", encoding="utf-8") as f:
        sys.stdout = sys.stderr = f
        try:
            install(from_config, data_dir, shortcuts) if action == "install" else uninstall()
            return 0
        except Exception:
            traceback.print_exc()
            return 1


def run_elevated(args: list[str]) -> int:
    """Re-run this exe with admin rights (UAC prompt) and wait for it."""
    arglist = " ".join(f'"{a}"' if " " in a else a for a in args)
    p = _powershell(f"try {{ $p = Start-Process -FilePath {_ps_quote(sys.executable)} -ArgumentList {_ps_quote(arglist)} "
                    f"-Verb RunAs -Wait -PassThru; exit $p.ExitCode }} catch {{ exit 1223 }}", check=False)
    return p.returncode


# ---------------------------------------------------------------- UI launcher
MB_ICONERROR, MB_ICONQUESTION, MB_ICONINFO, MB_YESNO, IDYES = 0x10, 0x20, 0x40, 0x4, 6


def msgbox(text: str, flags: int = MB_ICONINFO) -> int:
    return ctypes.windll.user32.MessageBoxW(None, text, "Airlog", flags)


def find_edge() -> str | None:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = Path(base) / "Microsoft/Edge/Application/msedge.exe"
            if p.exists():
                return str(p)
    return shutil.which("msedge")


def open_window(port: int) -> None:
    url = f"http://localhost:{port}/"
    edge = find_edge()
    if edge:
        subprocess.Popen([edge, f"--app={url}", "--window-size=1400,900"], creationflags=NO_WINDOW)
    else:
        import webbrowser
        webbrowser.open(url)


def install_with_prompt() -> bool:
    if msgbox("The Airlog Recorder service isn't installed.\n\n"
              "Install it now? It records in the background from boot, whether or not this window is open. "
              "Windows will ask for administrator permission.", MB_ICONQUESTION | MB_YESNO) != IDYES:
        return False
    rc = admin_command("install")
    if rc == 1223:
        return False   # UAC cancelled
    if rc:
        detail = INSTALL_LOG.read_text(encoding="utf-8", errors="replace")[-1500:] if INSTALL_LOG.exists() else ""
        msgbox(f"Installing the service failed.\n\n{detail}", MB_ICONERROR)
        return False
    return True


def launch_ui() -> int:
    port = service_port()
    if responding(port):
        open_window(port)
        return 0
    state = service_state()
    if state is None:
        if not install_with_prompt():
            return 1
        port = service_port()
    elif state != "RUNNING":
        _run(["sc.exe", "start", SERVICE_NAME])
    if not wait_until(lambda: responding(port), 30):
        msgbox(f"The Airlog Recorder service isn't answering on port {port}.\n\nLog: {LOG}", MB_ICONERROR)
        return 1
    open_window(port)
    return 0
