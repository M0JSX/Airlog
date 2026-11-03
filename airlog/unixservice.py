"""Recorder service on Linux (systemd) and macOS (launchd login agent).

    airlog                      open the UI in an app window (offers to install / starts the service)
    airlog --install-service    install and start the service
    airlog --uninstall-service  stop and remove the service (settings and recordings are kept)
    airlog --service            what systemd / launchd runs

Linux: a system-wide systemd unit (airlog.service) that runs as the user who installed it, from
boot, whether or not anyone is logged in. Installing needs root (pkexec or sudo). Settings live in
/var/lib/airlog/config.json; output goes to the journal (journalctl -u airlog).

macOS: a launchd agent in the user's login session (~/Library/LaunchAgents). It starts at login and
is restarted if it stops. Running in the login session is what lets macOS grant it microphone
access for sound-card capture; for unattended use, turn on automatic login. No admin rights needed.
Settings live in ~/Library/Application Support/Airlog/service.json.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import signal
import subprocess
import sys
import threading
import traceback
from pathlib import Path

from .config import config_dir, default_data_dir
from .servicecore import ask, config_port, migrate_config, open_window, responding, serve_until, stamp, tell, wait_until

IS_MAC = sys.platform == "darwin"
FROZEN = getattr(sys, "frozen", False)

# ---------------------------------------------------------------- Linux layout
UNIT_NAME = "airlog.service"
UNIT_PATH = Path("/etc/systemd/system") / UNIT_NAME
LINUX_INSTALL_DIR = Path("/opt/airlog")
STATE_DIR = Path("/var/lib/airlog")
LINUX_CONFIG = STATE_DIR / "config.json"
BIN_LINK = Path("/usr/local/bin/airlog")
DEB_BIN = Path("/usr/bin/airlog")                       # installed by the .deb
DESKTOP_FILE = Path("/usr/local/share/applications/airlog.desktop")
ICON_FILE = Path("/usr/local/share/icons/hicolor/256x256/apps/airlog.png")
DEB_LIST = Path("/var/lib/dpkg/info/airlog.list")       # present when the .deb owns /opt/airlog

# ---------------------------------------------------------------- macOS layout
LABEL = "radio.m0jsx.airlog"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
MAC_CONFIG = config_dir() / "service.json"
MAC_LOG = Path.home() / "Library/Logs/Airlog/service.log"

CONFIG = MAC_CONFIG if IS_MAC else LINUX_CONFIG


def _env() -> dict:
    """Environment for system tools we run: without the frozen build's LD_LIBRARY_PATH."""
    env = dict(os.environ)
    if FROZEN and not IS_MAC:
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig is not None:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def _run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL, env=_env())
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed ({p.returncode}): {(p.stdout + p.stderr).strip()}")
    return p


def self_cmd() -> list[str]:
    """How to run this program again (packaged build, or `python -m airlog` from source)."""
    if FROZEN:
        return [sys.executable]
    return [sys.executable, "-m", "airlog"]


def source_root() -> str:
    return str(Path(__file__).resolve().parents[1])


# ---------------------------------------------------------------- service host
def _log_to_file(log: Path) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists() and log.stat().st_size > 5_000_000:
        log.replace(log.with_suffix(".old.log"))
    f = open(log, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = f


def run_service(config: Path | None = None) -> None:
    """Runs until SIGTERM (systemctl stop / launchctl bootout)."""
    if IS_MAC:
        _log_to_file(MAC_LOG)          # Linux: stdout goes to the journal
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())

    def wait():
        while not stop.wait(1):
            pass

    try:
        serve_until(config or CONFIG, wait)
    except Exception:
        traceback.print_exc()
        os._exit(1)                    # systemd / launchd restart us


# ---------------------------------------------------------------- state
def installed() -> bool:
    return (PLIST if IS_MAC else UNIT_PATH).exists()


def running() -> bool:
    if IS_MAC:
        p = _run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"])
        return p.returncode == 0 and "state = running" in p.stdout
    return _run(["systemctl", "is-active", "--quiet", UNIT_NAME]).returncode == 0


def start() -> bool:
    if IS_MAC:
        if _run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"]).returncode:
            _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)])
        return _run(["launchctl", "kickstart", f"gui/{os.getuid()}/{LABEL}"]).returncode == 0
    # Ordinary users get a polkit password prompt on a desktop.
    if _run(["systemctl", "start", UNIT_NAME]).returncode == 0:
        return True
    if shutil.which("pkexec") and _graphical():
        return _run(["pkexec", "systemctl", "start", UNIT_NAME]).returncode == 0
    return False


def _graphical() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


# ---------------------------------------------------------------- Linux install
def _linux_user(user: str | None):
    import pwd
    name = user or os.environ.get("SUDO_USER")
    if not name and os.environ.get("PKEXEC_UID"):
        name = pwd.getpwuid(int(os.environ["PKEXEC_UID"])).pw_name
    if not name or name == "root":
        raise RuntimeError("Say which user the recorder should run as:  --install-service --user NAME")
    return pwd.getpwnam(name)


def _unit_text(user: str, cmd: list[str]) -> str:
    import shlex
    env = "Environment=PYTHONUNBUFFERED=1\n"
    if not FROZEN:
        env += f"Environment=PYTHONPATH={source_root()}\n"
    return f"""[Unit]
Description=Airlog Recorder (hourly archive + grab buffer, web UI)
Documentation=https://github.com/M0JSX/airlog
# No RTC on some machines: wait for NTP so file names and grab times are right after a power cut.
Wants=network-online.target time-sync.target
After=network-online.target time-sync.target sound.target

[Service]
Type=simple
User={user}
SupplementaryGroups=audio
StateDirectory=airlog
StateDirectoryMode=0750
{env}ExecStart={shlex.join(cmd + ["--service", "--config", str(LINUX_CONFIG)])}
Restart=always
RestartSec=3
TimeoutStopSec=20
Nice=-5

[Install]
WantedBy=multi-user.target
"""


def _firewall(port: int, allow: bool) -> None:
    """Open the web port if a host firewall is active (ufw or firewalld)."""
    if shutil.which("ufw") and "Status: active" in _run(["ufw", "status"]).stdout:
        _run(["ufw", "allow" if allow else "delete", *([] if allow else ["allow"]), f"{port}/tcp"]
             + (["comment", "Airlog"] if allow else []))
    if shutil.which("firewall-cmd") and _run(["firewall-cmd", "--state"]).returncode == 0:
        _run(["firewall-cmd", "--permanent", f"--{'add' if allow else 'remove'}-port={port}/tcp"])
        _run(["firewall-cmd", "--reload"])


def _desktop_entry(exe: str, create: bool) -> None:
    """Menu entry + `airlog` command for tarball installs (the .deb ships its own)."""
    if DEB_BIN.exists() and create:
        return
    if create:
        BIN_LINK.parent.mkdir(parents=True, exist_ok=True)
        if BIN_LINK.is_symlink() or BIN_LINK.exists():
            BIN_LINK.unlink()
        BIN_LINK.symlink_to(exe)
        ICON_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(__file__).with_name("assets") / "icon.png", ICON_FILE)
        DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        DESKTOP_FILE.write_text(DESKTOP_ENTRY.format(exe=BIN_LINK), encoding="utf-8")
        _run(["update-desktop-database", str(DESKTOP_FILE.parent)])
    else:
        for p in (DESKTOP_FILE, ICON_FILE):
            p.unlink(missing_ok=True)
        if BIN_LINK.is_symlink():
            BIN_LINK.unlink()


DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=Airlog
Comment=Radio station logger: hourly archive and instant grabs
Exec={exe}
Icon=airlog
Terminal=false
Categories=AudioVideo;Audio;Recorder;
"""


def _linux_install(user: str | None, from_config: Path | None, data_dir: Path | None, shortcuts: bool) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Installing the service needs root: run it with sudo")
    pw = _linux_user(user)
    home = Path(pw.pw_dir)
    print(f"{stamp()} Installing the Airlog service for user {pw.pw_name}")
    _run(["systemctl", "stop", UNIT_NAME])

    if FROZEN:
        src = Path(sys.executable).resolve().parent
        if src != LINUX_INSTALL_DIR.resolve():
            print(f"  copying {src} -> {LINUX_INSTALL_DIR}")
            shutil.copytree(src, LINUX_INSTALL_DIR, dirs_exist_ok=True, symlinks=True)
        cmd = [str(LINUX_INSTALL_DIR / "Airlog")]
    else:
        cmd = [sys.executable, "-m", "airlog"]

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    migrate_config(LINUX_CONFIG, from_config or home / ".config/airlog/config.json", data_dir or home / "Airlog")
    for p in (STATE_DIR, LINUX_CONFIG):
        os.chown(p, pw.pw_uid, pw.pw_gid)
    STATE_DIR.chmod(0o750)
    LINUX_CONFIG.chmod(0o600)              # holds the web password

    UNIT_PATH.write_text(_unit_text(pw.pw_name, cmd), encoding="utf-8")
    _run(["systemctl", "daemon-reload"], check=True)
    _run(["systemctl", "enable", UNIT_NAME], check=True)
    # Wait for NTP at boot on machines without a real-time clock (harmless elsewhere).
    _run(["systemctl", "enable", "systemd-time-wait-sync.service"])

    port = config_port(LINUX_CONFIG)
    _firewall(port, allow=True)
    if FROZEN and shortcuts:
        _desktop_entry(cmd[0], create=True)

    _run(["systemctl", "restart", UNIT_NAME], check=True)
    if not wait_until(lambda: responding(port), 30):
        raise RuntimeError(f"Service installed but not answering on port {port}; see: journalctl -u airlog")
    print(f"{stamp()} Installed and running. Open http://localhost:{port}/ (or http://<this-machine>:{port}/)")


def _linux_uninstall(purge_app: bool = True) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Removing the service needs root: run it with sudo")
    port = config_port(LINUX_CONFIG)
    _run(["systemctl", "disable", "--now", UNIT_NAME])
    UNIT_PATH.unlink(missing_ok=True)
    _run(["systemctl", "daemon-reload"])
    _firewall(port, allow=False)
    _desktop_entry("", create=False)
    if purge_app and not DEB_LIST.exists() and LINUX_INSTALL_DIR.exists():
        shutil.rmtree(LINUX_INSTALL_DIR, ignore_errors=True)    # tarball install: nothing else will remove it
    print(f"{stamp()} Uninstalled. Settings ({STATE_DIR}) and recordings are kept.")


# ---------------------------------------------------------------- macOS install
def _app_bundle() -> Path | None:
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None


def _mac_install(from_config: Path | None, data_dir: Path | None) -> None:
    print(f"{stamp()} Installing the Airlog login agent")
    uid = os.getuid()
    _run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"])
    env = {"PYTHONUNBUFFERED": "1"}

    if FROZEN:
        app = _app_bundle()
        if app is None:
            raise RuntimeError("Run this from Airlog.app")
        apps = (Path("/Applications"), Path.home() / "Applications")
        if not any(app.parent.resolve() == d.resolve() for d in apps if d.exists()):
            dest_dir = apps[0] if os.access(apps[0], os.W_OK) else apps[1]
            dest_dir.mkdir(exist_ok=True)
            dest = dest_dir / "Airlog.app"
            print(f"  copying {app} -> {dest}")
            if dest.exists():
                shutil.rmtree(dest)
            _run(["ditto", str(app), str(dest)], check=True)
            # The user has already chosen to open this app; launchd won't show a Gatekeeper prompt for it.
            _run(["xattr", "-dr", "com.apple.quarantine", str(dest)])
            app = dest
        cmd = [str(app / "Contents/MacOS/Airlog")]
    else:
        cmd = [sys.executable, "-m", "airlog"]
        env["PYTHONPATH"] = source_root()
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"   # Homebrew ffmpeg

    migrate_config(MAC_CONFIG, from_config or config_dir() / "config.json", data_dir or default_data_dir())
    MAC_CONFIG.chmod(0o600)
    MAC_LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    with open(PLIST, "wb") as f:
        plistlib.dump({
            "Label": LABEL,
            "ProgramArguments": cmd + ["--service", "--config", str(MAC_CONFIG)],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 5,
            "ProcessType": "Interactive",       # no App Nap / timer coalescing: recording is real-time
            "LimitLoadToSessionType": "Aqua",   # the login session, where microphone permission applies
            "EnvironmentVariables": env,
            "StandardErrorPath": str(MAC_LOG.with_name("launchd.log")),
        }, f)

    _run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], check=True)
    _run(["launchctl", "enable", f"gui/{uid}/{LABEL}"])
    port = config_port(MAC_CONFIG)
    if not wait_until(lambda: responding(port), 30):
        raise RuntimeError(f"Service installed but not answering on port {port}; see {MAC_LOG}")
    print(f"{stamp()} Installed and running. Open http://localhost:{port}/")


def _mac_uninstall() -> None:
    _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])
    PLIST.unlink(missing_ok=True)
    print(f"{stamp()} Uninstalled. Settings ({MAC_CONFIG.parent}) and recordings are kept; "
          f"drag Airlog.app to the Bin to remove the app.")


# ---------------------------------------------------------------- command-line entry
def admin_command(action: str, from_config: Path | None = None, data_dir: Path | None = None,
                  shortcuts: bool = True, user: str | None = None) -> int:
    """--install-service / --uninstall-service. On Linux, re-runs itself with sudo if needed."""
    if not IS_MAC and os.geteuid() != 0:
        return run_elevated(_install_args(action, from_config, data_dir, shortcuts, user), interactive=True)
    try:
        if IS_MAC:
            _mac_install(from_config, data_dir) if action == "install" else _mac_uninstall()
        else:
            _linux_install(user, from_config, data_dir, shortcuts) if action == "install" else _linux_uninstall()
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        if not isinstance(e, RuntimeError):
            traceback.print_exc()
        return 1


def _install_args(action, from_config, data_dir, shortcuts, user) -> list[str]:
    import getpass
    args = [f"--{action}-service"] + ([] if shortcuts else ["--no-shortcuts"])
    if action == "install":
        # The elevated copy runs as root: point it at this user's settings and home.
        args += ["--user", user or getpass.getuser(),
                 "--from-config", str(from_config or config_dir() / "config.json"),
                 "--data-dir", str(data_dir or default_data_dir())]
    return args


def run_elevated(args: list[str], interactive: bool) -> int:
    """Linux: run this program as root. pkexec (graphical prompt) or sudo (terminal)."""
    cmd = self_cmd() + args
    if not FROZEN:
        cmd = ["/usr/bin/env", f"PYTHONPATH={source_root()}"] + cmd
    if interactive and sys.stdin.isatty() and shutil.which("sudo"):
        return subprocess.call(["sudo"] + cmd, env=_env())
    if shutil.which("pkexec"):
        p = subprocess.run(["pkexec"] + cmd, capture_output=True, text=True, errors="replace", env=_env())
        sys.stdout.write(p.stdout)
        sys.stderr.write(p.stderr)
        run_elevated.output = (p.stdout + p.stderr).strip()   # type: ignore[attr-defined]
        return p.returncode
    print("Need root to install the service. Run:  sudo " + " ".join(cmd), file=sys.stderr)
    return 1


# ---------------------------------------------------------------- UI launcher
def launch_ui() -> int:
    port = config_port(CONFIG)
    if responding(port):
        open_window(port)
        return 0
    if not installed():
        if not ask("The Airlog Recorder service isn't installed.\n\n"
                   + ("Install it now? It records in the background whenever you're logged in, "
                      "whether or not this window is open." if IS_MAC else
                      "Install it now? It records in the background from boot, whether or not this window is open. "
                      "You'll be asked for an administrator password.")):
            return 1
        if IS_MAC:
            rc = admin_command("install")
            detail = MAC_LOG.read_text(errors="replace")[-1500:] if rc and MAC_LOG.exists() else ""
        else:
            run_elevated.output = ""   # type: ignore[attr-defined]
            rc = run_elevated(_install_args("install", None, None, True, None), interactive=False)
            if rc in (126, 127):       # pkexec: dismissed / not authorised
                return 1
            detail = getattr(run_elevated, "output", "")[-1500:]
        if rc:
            tell(f"Installing the service failed.\n\n{detail}", error=True)
            return 1
        port = config_port(CONFIG)
    elif not running():
        start()
    if not wait_until(lambda: responding(port), 30):
        where = MAC_LOG if IS_MAC else "journalctl -u airlog"
        tell(f"The Airlog Recorder service isn't answering on port {port}.\n\nLog: {where}", error=True)
        return 1
    open_window(port)
    return 0
