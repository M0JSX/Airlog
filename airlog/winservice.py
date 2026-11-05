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
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

from . import lifecycle
from .config import config_dir, default_data_dir
from .servicecore import NO_WINDOW, config_port, migrate_config, open_window, responding, serve_until, stamp, wait_until

SERVICE_NAME = "AirlogRecorder"
DISPLAY_NAME = "Airlog Recorder"
DESCRIPTION = "Records the station into hourly files and keeps the grab buffer. UI: Airlog.exe or http://<this-pc>:<port>/"
FIREWALL_RULE = "Airlog Recorder"

INSTALL_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Airlog"
DATA_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Airlog"
CONFIG = DATA_DIR / "config.json"
LOG = DATA_DIR / "logs" / "service.log"
INSTALL_LOG = DATA_DIR / "logs" / "install.log"


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
            lifecycle.begin_shutdown()
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            try:
                serve_until(CONFIG, lambda: win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE))
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
    migrate_config(CONFIG, from_config or config_dir() / "config.json", data_dir or default_data_dir())

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

    port = config_port(CONFIG)
    _powershell(firewall_script(INSTALL_DIR))
    if shortcuts:   # Setup.exe makes its own
        _shortcuts(exe, create=True)

    _run(["sc.exe", "start", SERVICE_NAME])
    if not wait_until(lambda: responding(port), 30):
        raise RuntimeError(f"Service installed but not answering on port {port}; see {LOG}")
    print(f"{stamp()} Installed; running from {exe}")


def firewall_script(install_dir: Path) -> str:
    """PowerShell that (re)creates the inbound rules, on private/domain networks only, TCP and UDP, for
    every program in Airlog that listens: Airlog.exe (web UI, Icecast-style push input) and the bundled
    ffmpeg.exe (SRT / UDP / RTP receive, SRT-listener outputs). A service gets no "allow?" prompt, so
    anything not covered here is silently blocked."""
    rules = [f"New-NetFirewallRule -DisplayName {_ps_quote(FIREWALL_RULE)} -Direction Inbound -Action Allow "
             f"-Program {_ps_quote(install_dir / prog)} -Protocol {proto} -Profile Private,Domain | Out-Null"
             for prog in ("Airlog.exe", "ffmpeg.exe") for proto in ("TCP", "UDP")]
    return (f"Get-NetFirewallRule -DisplayName {_ps_quote(FIREWALL_RULE)} -ErrorAction SilentlyContinue | "
            "Remove-NetFirewallRule; " + "; ".join(rules))


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
    port = config_port(CONFIG)
    if responding(port):
        open_window(port)
        return 0
    state = service_state()
    if state is None:
        if not install_with_prompt():
            return 1
        port = config_port(CONFIG)
    elif state != "RUNNING":
        _run(["sc.exe", "start", SERVICE_NAME])
    if not wait_until(lambda: responding(port), 30):
        msgbox(f"The Airlog Recorder service isn't answering on port {port}.\n\nLog: {LOG}", MB_ICONERROR)
        return 1
    open_window(port)
    return 0
