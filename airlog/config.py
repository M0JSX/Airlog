"""Configuration handling for Airlog.

Settings live in a JSON file in the per-user config directory:
  Windows: %APPDATA%\\Airlog\\config.json
  macOS:   ~/Library/Application Support/Airlog/config.json
  Linux:   ~/.config/airlog/config.json
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "Airlog"


def config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME.lower()


def default_data_dir() -> Path:
    return Path.home() / "Airlog"


@dataclass
class Config:
    # --- Station ---
    station_name: str = "Station"

    # --- Source ---
    # "stream"  : internet stream URL (Icecast/Shoutcast/HLS/anything ffmpeg reads)
    # "device"  : sound card input (dshow / avfoundation / pulse / alsa)
    # "custom"  : raw ffmpeg input arguments, for anything else
    source_type: str = "stream"
    stream_url: str = ""
    device_backend: str = ""          # blank = platform default
    device_name: str = ""             # e.g. 'Line In (USB Audio CODEC)', ':0', 'default', 'hw:1,0'
    custom_input_args: str = ""       # e.g. '-f lavfi -i sine=frequency=440'
    ffmpeg_path: str = "ffmpeg"

    # Streams arrive behind real time (encoder + server buffering). Set this to
    # the delay in seconds and timestamps/file splits are shifted back to match air.
    source_delay_s: float = 0.0

    # --- Capture format (everything is resampled to this internally) ---
    sample_rate: int = 48000
    channels: int = 2

    # --- Hourly archive ---
    archive_enabled: bool = True
    archive_dir: str = str(default_data_dir() / "Archive")
    # strftime tokens are filled from each file's start time; {station} is substituted.
    archive_pattern: str = "%Y-%m-%d/{station}_%Y-%m-%d_%H-%M"
    archive_format: str = "mp3"       # mp3 | flac | wav | ogg
    archive_bitrate_kbps: int = 192
    split_minutes: int = 60           # 60 = split on the hour; also 30 / 15
    archive_keep_days: int = 0        # 0 = keep forever

    # --- Grab buffer ---
    buffer_minutes: int = 120         # how far back you can grab from
    spool_dir: str = ""               # blank = inside data dir

    # --- Grabs ---
    grabs_dir: str = str(default_data_dir() / "Grabs")
    grab_format: str = "wav"          # wav | mp3 | flac
    grab_pattern: str = "{station}_%Y-%m-%d_%H-%M-%S"
    grab_preroll_s: float = 0.0       # extra audio added before a grab's in-point
    editor_path: str = ""             # blank = open with system default app
    open_after_grab: bool = False

    # --- Song identification (Shazam, unofficial API) ---
    identify_songs: bool = True
    identify_interval_s: int = 30     # how often a sample is checked (minimum 15)

    # --- Behaviour ---
    autostart: bool = True
    silence_alert_db: float = -50.0
    silence_alert_s: int = 15
    view_minutes: int = 15

    # --- Web server (airlog-server) ---
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    web_password: str = ""            # blank = no login (trusted LAN only)

    # Unknown keys from newer/older versions are preserved here.
    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    def effective_spool_dir(self) -> Path:
        if self.spool_dir:
            return Path(self.spool_dir)
        return default_data_dir() / ".spool"

    @classmethod
    def path(cls) -> Path:
        return config_dir() / "config.json"

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        p = path or cls.path()
        cfg = cls()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return cfg
            known = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in known and k != "extra":
                    setattr(cfg, k, v)
                else:
                    cfg.extra[k] = v
        return cfg

    def save(self, path: Path | None = None) -> None:
        p = path or self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        extra = data.pop("extra", {})
        data.update(extra)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(p)
