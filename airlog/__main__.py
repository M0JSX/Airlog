"""Entry point:  python -m airlog  [--config path/to/config.json]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(prog="airlog", description="Radio logger with hourly files and instant grabs")
    ap.add_argument("--config", type=Path, help="use this config file instead of the per-user one")
    ap.add_argument("--selftest", type=Path, metavar="REPORT", help=argparse.SUPPRESS)
    ap.add_argument("--classic", action="store_true", help="Windows: run the all-in-one desktop window instead of the service UI")
    ap.add_argument("--install-service", action="store_true", help="Windows: install and start the recorder service (admin)")
    ap.add_argument("--uninstall-service", action="store_true", help="Windows: stop and remove the recorder service (admin)")
    ap.add_argument("--service", action="store_true", help=argparse.SUPPRESS)       # run by the Service Control Manager
    ap.add_argument("--no-shortcuts", action="store_true", help=argparse.SUPPRESS)  # Setup.exe makes its own
    ap.add_argument("--from-config", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if getattr(sys, "frozen", False):
        # Packaged build: ffmpeg.exe / ffplay.exe ship next to Airlog.exe.
        import os
        exe_dir = os.path.dirname(sys.executable)
        os.environ["PATH"] = exe_dir + os.pathsep + os.environ.get("PATH", "")

    if sys.platform.startswith("win"):
        try:  # crisp text on high-DPI displays
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    if args.selftest:
        from .selftest import run
        sys.exit(run(args.selftest))

    if sys.platform.startswith("win"):
        from . import winservice
        if args.service:
            winservice.run_service()
            return
        if args.install_service or args.uninstall_service:
            sys.exit(winservice.admin_command("install" if args.install_service else "uninstall",
                                              args.from_config, args.data_dir, not args.no_shortcuts))
        if getattr(sys, "frozen", False) and not args.classic and not args.config:
            # The service does the recording; this just opens its UI.
            sys.exit(winservice.launch_ui())

    from .config import Config
    if args.config:
        Config.path = classmethod(lambda cls: args.config)  # type: ignore[method-assign]
    cfg = Config.load()

    from .ui import App
    App(cfg).mainloop()


if __name__ == "__main__":
    main()
