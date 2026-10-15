"""Opening files and folders with the platform's tools."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _reset_dll_search() -> None:
    """The packaged exe adds its _internal folder to the DLL search path, and programs we launch
    inherit it (an editor would load our VCRUNTIME140.dll instead of its own). Undo that first."""
    if sys.platform.startswith("win") and getattr(sys, "frozen", False):
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)


def open_file(path: Path, editor: str = "") -> None:
    path = Path(path)
    _reset_dll_search()
    if editor:
        if sys.platform == "darwin" and editor.endswith(".app"):
            subprocess.Popen(["open", "-a", editor, str(path)])
        else:
            subprocess.Popen([editor, str(path)])
        return
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def reveal(path: Path) -> None:
    path = Path(path)
    if sys.platform.startswith("win"):
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)] if path.is_file() else ["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent if path.is_file() else path)])


def find_ffplay(ffmpeg: str) -> str | None:
    """ffplay alongside the configured ffmpeg, else on PATH."""
    p = Path(ffmpeg)
    if p.parent != Path("."):
        cand = p.with_name("ffplay" + p.suffix)
        if cand.exists():
            return str(cand)
    return shutil.which("ffplay")
