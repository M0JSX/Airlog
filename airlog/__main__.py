"""Entry point:  python -m airlog  [--config path/to/config.json]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(prog="airlog", description="Radio logger with hourly files and instant grabs")
    ap.add_argument("--config", type=Path, help="use this config file instead of the per-user one")
    ap.add_argument("--selftest", type=Path, metavar="REPORT", help=argparse.SUPPRESS)
    ap.add_argument("--classic", action="store_true", help="run the all-in-one desktop window instead of the service UI")
    ap.add_argument("--install-service", action="store_true",
                    help="install and start the recorder service (Windows/Linux: admin; macOS: login agent)")
    ap.add_argument("--uninstall-service", action="store_true", help="stop and remove the recorder service")
    ap.add_argument("--user", help="Linux: the account the service runs as (default: the user running sudo)")
    ap.add_argument("--service", action="store_true", help=argparse.SUPPRESS)       # run by the SCM / systemd / launchd
    ap.add_argument("--no-shortcuts", action="store_true", help=argparse.SUPPRESS)  # Setup.exe makes its own
    ap.add_argument("--from-config", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    # macOS can pass a -psn_0_NNN process serial number when an app is opened from Finder.
    args = ap.parse_args([a for a in sys.argv[1:] if not a.startswith("-psn_")])

    if getattr(sys, "frozen", False):
        # Packaged build: ffmpeg / ffprobe / ffplay ship next to the executable (Windows)
        # or in the bundle's internal folder (macOS / Linux).
        import os
        dirs = [os.path.dirname(sys.executable), getattr(sys, "_MEIPASS", "")]
        os.environ["PATH"] = os.pathsep.join([d for d in dirs if d] + [os.environ.get("PATH", "")])

    if sys.platform.startswith("win"):
        try:  # crisp text on high-DPI displays
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    if args.selftest:
        from .selftest import run
        sys.exit(run(args.selftest))

    windows = sys.platform.startswith("win")
    if windows:
        from . import winservice as svc
    else:
        from . import unixservice as svc  # type: ignore[no-redef]
    if args.service:
        svc.run_service() if windows else svc.run_service(args.config)
        return
    if args.install_service or args.uninstall_service:
        action = "install" if args.install_service else "uninstall"
        if windows:
            sys.exit(svc.admin_command(action, args.from_config, args.data_dir, not args.no_shortcuts))
        sys.exit(svc.admin_command(action, args.from_config, args.data_dir, not args.no_shortcuts, args.user))
    if getattr(sys, "frozen", False) and not args.classic and not args.config:
        # The service does the recording; this just opens its UI.
        sys.exit(svc.launch_ui())

    from .config import Config
    if args.config:
        Config.path = classmethod(lambda cls: args.config)  # type: ignore[method-assign]
    cfg = Config.load()

    from .ui import App
    App(cfg).mainloop()


if __name__ == "__main__":
    main()
