"""Configuration handling for Airlog.

Settings live in a JSON file in the per-user config directory:
  Windows: %APPDATA%\\Airlog\\config.json
  macOS:   ~/Library/Application Support/Airlog/config.json
  Linux:   ~/.config/airlog/config.json
"""
from __future__ import annotations

import copy
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


# Source settings that the backup source has its own copy of (as backup_<name>).
BACKUP_KEYS = ("source_type", "stream_url", "device_backend", "device_name", "custom_input_args", "source_delay_s",
               "receive_protocol", "receive_port", "receive_latency_ms", "receive_passphrase", "receive_password",
               "receive_group", "receive_sdp")
# The alternative (internet-safe) input has its own copy of these, as alt_<name>.
ALT_KEYS = BACKUP_KEYS + ("files_dir",)
ALT_MODES = ("manual", "schedule", "permanent")


@dataclass
class Config:
    # --- Station ---
    station_name: str = "Station"

    # --- Source ---
    # "stream"  : internet stream URL (Icecast/Shoutcast/HLS/anything ffmpeg reads)
    # "device"  : sound card input (dshow / avfoundation / pulse / alsa)
    # "custom"  : raw ffmpeg input arguments, for anything else
    # "receive" : a stream sent *to* Airlog (SRT listener, UDP, RTP or an Icecast-style push)
    # "files"   : a folder of audio files played in a loop (used for the alternative input)
    source_type: str = "stream"
    files_dir: str = ""
    stream_url: str = ""
    device_backend: str = ""          # blank = platform default
    device_name: str = ""             # e.g. 'Line In (USB Audio CODEC)', ':0', 'default', 'hw:1,0'
    custom_input_args: str = ""       # e.g. '-f lavfi -i sine=frequency=440'
    ffmpeg_path: str = "ffmpeg"

    # --- Receive a stream (source_type "receive") ---
    receive_protocol: str = "srt"     # srt | udp | rtp | icecast
    receive_port: int = 9000
    receive_latency_ms: int = 200     # SRT
    receive_passphrase: str = ""      # SRT (10-79 characters, blank = unencrypted)
    receive_password: str = ""        # Icecast push: the source password senders must use
    receive_group: str = ""           # UDP/RTP: multicast group to join (blank = unicast)
    receive_sdp: str = ""             # RTP: SDP from the sender (needed for L16, Opus, AAC)

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
    trusted_lan: bool = False         # no password: let any machine on the network change settings
    announce: bool = True             # advertise on the LAN (mDNS) so Airlog Viewer can find it

    # --- Backup source: runs alongside the main one and takes over if it fails ---
    backup_enabled: bool = False
    backup_source_type: str = "stream"
    backup_stream_url: str = ""
    backup_device_backend: str = ""
    backup_device_name: str = ""
    backup_custom_input_args: str = ""
    backup_source_delay_s: float = 0.0
    backup_receive_protocol: str = "srt"
    backup_receive_port: int = 9001
    backup_receive_latency_ms: int = 200
    backup_receive_passphrase: str = ""
    backup_receive_password: str = ""
    backup_receive_group: str = ""
    backup_receive_sdp: str = ""
    failover_after_s: float = 5.0     # main gives no audio for this long -> switch to the backup
    failover_silence_s: int = 60      # main silent this long while the backup isn't -> backup (0 = off)
    failover_return_s: int = 30       # main healthy again for this long -> switch back

    # --- Alternative input for internet outputs (e.g. football commentary that can't go online) ---
    # Outputs whose feed is "switch" carry this instead of the programme while it's switched on;
    # the recording, archive and grabs always keep the programme.
    alt_enabled: bool = False
    alt_mode: str = "manual"          # manual (button) | schedule (times, with override) | permanent
    alt_override: str = ""            # "" | "programme" | "alternative": the button's state, kept across restarts
    alt_title: str = ""               # now-playing text on switched outputs (blank: the file playing / station)
    alt_schedule: list = field(default_factory=list)   # [{"days": "Sat Sun", "date": "", "start": "14:55",
                                                         #   "end": "17:10", "label": "..."}]
    alt_source_type: str = "files"
    alt_stream_url: str = ""
    alt_device_backend: str = ""
    alt_device_name: str = ""
    alt_custom_input_args: str = ""
    alt_source_delay_s: float = 0.0
    alt_receive_protocol: str = "srt"
    alt_receive_port: int = 9002
    alt_receive_latency_ms: int = 200
    alt_receive_passphrase: str = ""
    alt_receive_password: str = ""
    alt_receive_group: str = ""
    alt_receive_sdp: str = ""
    alt_files_dir: str = ""

    # --- Encoder outputs: send what's recorded on to Icecast / Shoutcast / SRT / UDP / RTP ---
    outputs: list = field(default_factory=list)

    # Unknown keys from newer/older versions are preserved here.
    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    def backup_config(self) -> "Config":
        """The settings the backup source's capture runs with: these, with the source fields swapped."""
        c = copy.copy(self)
        for k in BACKUP_KEYS:
            setattr(c, k, getattr(self, "backup_" + k))
        c.extra = dict(self.extra, _role="backup")
        return c

    def alt_config(self) -> "Config":
        """The settings the alternative input's capture runs with."""
        c = copy.copy(self)
        for k in ALT_KEYS:
            setattr(c, k, getattr(self, "alt_" + k))
        c.extra = dict(self.extra, _role="alt")
        return c

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
