"""Building ffmpeg input arguments and listing capture devices per platform."""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys

from .config import Config

NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0  # CREATE_NO_WINDOW


HEADLESS = False   # set by the server: a system service can't reach a desktop PulseAudio/PipeWire session


def default_backend() -> str:
    if sys.platform.startswith("win"):
        return "dshow"
    if sys.platform == "darwin":
        return "avfoundation"
    if HEADLESS:
        return "alsa"
    return "pulse" if shutil.which("pactl") else "alsa"


BACKENDS = ["dshow", "avfoundation", "pulse", "alsa"]


def input_args(cfg: Config) -> list[str]:
    """ffmpeg arguments placed before the output section."""
    if cfg.source_type == "stream":
        url = cfg.stream_url.strip()
        if not url:
            raise ValueError("No stream URL configured")
        args = []
        if url.lower().startswith(("http://", "https://")):
            args += [
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_on_network_error", "1",
                "-reconnect_delay_max", "10",
                "-rw_timeout", "15000000",          # 15 s stall -> error -> supervisor restarts
                "-user_agent", "Airlog/1.0",
            ]
        return args + ["-i", url]

    if cfg.source_type == "device":
        backend = cfg.device_backend or default_backend()
        name = cfg.device_name.strip()
        if backend == "dshow":
            if not name:
                raise ValueError("No input device selected")
            spec = name if name.startswith("audio=") else f"audio={name}"
            return ["-f", "dshow", "-audio_buffer_size", "50", "-i", spec]
        if backend == "avfoundation":
            spec = name or ":0"
            if not spec.startswith(":"):
                spec = ":" + spec
            return ["-f", "avfoundation", "-i", spec]
        if backend in ("pulse", "alsa"):
            return ["-f", backend, "-i", name or "default"]
        raise ValueError(f"Unknown device backend {backend!r}")

    if cfg.source_type == "receive":
        return receive_args(cfg)

    if cfg.source_type == "custom":
        args = shlex.split(cfg.custom_input_args, posix=not sys.platform.startswith("win"))
        if "-i" not in args:
            raise ValueError("Custom input arguments must contain -i")
        return args

    raise ValueError(f"Unknown source type {cfg.source_type!r}")


RECEIVE_LABELS = {"srt": "SRT", "udp": "UDP (MPEG-TS)", "rtp": "RTP", "icecast": "Icecast push"}


def receive_args(cfg: Config) -> list[str]:
    """ffmpeg input for a stream sent to this machine. Icecast push arrives on stdin (see receive.py)."""
    proto, port = cfg.receive_protocol, int(cfg.receive_port)
    if not 1 <= port <= 65535:
        raise ValueError("Receive port must be 1-65535")
    if port == int(cfg.web_port):
        raise ValueError(f"Receive port {port} is the web UI's port; pick another")
    group = cfg.receive_group.strip() or "0.0.0.0"
    if proto == "srt":
        args = ["-mode", "listener", "-latency", str(max(20, int(cfg.receive_latency_ms)) * 1000)]
        if cfg.receive_passphrase:
            if not 10 <= len(cfg.receive_passphrase) <= 79:
                raise ValueError("An SRT passphrase must be 10-79 characters")
            args += ["-passphrase", cfg.receive_passphrase]
        return args + ["-i", f"srt://0.0.0.0:{port}"]
    if proto == "udp":
        return ["-f", "mpegts", "-i", f"udp://{group}:{port}?fifo_size=1000000&overrun_nonfatal=1"]
    if proto == "rtp":
        if cfg.receive_sdp.strip():
            sdp = cfg.effective_spool_dir() / f"receive-{cfg.extra.get('_role', 'main')}.sdp"
            sdp.parent.mkdir(parents=True, exist_ok=True)
            sdp.write_text(cfg.receive_sdp.strip() + "\n", encoding="utf-8")
            return ["-protocol_whitelist", "file,udp,rtp", "-i", str(sdp)]
        return ["-i", f"rtp://{group}:{port}"]
    if proto == "icecast":
        if not cfg.receive_password:
            raise ValueError("Set a receive password: senders must use it as their source password")
        return ["-i", "pipe:0"]
    raise ValueError(f"Unknown receive protocol {proto!r}")


def is_push(cfg: Config) -> bool:
    return cfg.source_type == "receive" and cfg.receive_protocol == "icecast"


def describe(cfg: Config) -> str:
    if cfg.source_type == "receive":
        return f"receive {RECEIVE_LABELS.get(cfg.receive_protocol, cfg.receive_protocol)} on port {cfg.receive_port}"
    if cfg.source_type == "stream":
        return cfg.stream_url or "(no URL)"
    if cfg.source_type == "device":
        return f"{cfg.device_backend or default_backend()}: {cfg.device_name or 'default'}"
    return "custom: " + cfg.custom_input_args


def _run(cmd: list[str], timeout: float = 8) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=NO_WINDOW, errors="replace")
        return (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"ERROR {e}"


def list_devices(backend: str, ffmpeg: str = "ffmpeg") -> list[tuple[str, str]]:
    """Returns [(value_for_config, label)] for audio inputs on this backend."""
    out: list[tuple[str, str]] = []
    if backend == "dshow":
        text = _run([ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])
        # Newer ffmpeg:  [dshow @ ...] "Line In (Realtek)" (audio)
        for m in re.finditer(r'"([^"]+)"\s*\(audio\)', text):
            out.append((m.group(1), m.group(1)))
        if not out:  # older ffmpeg: section headers then quoted names
            section = None
            for line in text.splitlines():
                if "DirectShow audio devices" in line:
                    section = "a"
                elif "DirectShow video devices" in line:
                    section = "v"
                elif section == "a" and "Alternative name" not in line:
                    m = re.search(r'"([^"]+)"', line)
                    if m:
                        out.append((m.group(1), m.group(1)))
    elif backend == "avfoundation":
        text = _run([ffmpeg, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""])
        audio = False
        for line in text.splitlines():
            if "audio devices" in line.lower():
                audio = True
                continue
            if "video devices" in line.lower():
                audio = False
                continue
            m = re.search(r"\[(\d+)\]\s+(.+)$", line)
            if audio and m:
                out.append((":" + m.group(1), m.group(2).strip()))
    elif backend == "pulse":
        text = _run(["pactl", "list", "short", "sources"])
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                label = parts[1] + ("  (monitor)" if parts[1].endswith(".monitor") else "")
                out.append((parts[1], label))
        out.insert(0, ("default", "default"))
    elif backend == "alsa":
        text = _run(["arecord", "-l"])
        for m in re.finditer(r"card (\d+): [^\[]*\[([^\]]+)\], device (\d+): [^\[]*\[([^\]]+)\]", text):
            # plughw lets ALSA convert rate/format if the card can't do 48k stereo natively
            out.append((f"plughw:{m.group(1)},{m.group(3)}",
                        f"{m.group(2)} — {m.group(4)} (plughw:{m.group(1)},{m.group(3)})"))
        out.insert(0, ("default", "default"))
    return out


def ffmpeg_version(ffmpeg: str = "ffmpeg") -> str | None:
    text = _run([ffmpeg, "-hide_banner", "-version"], timeout=5)
    m = re.search(r"ffmpeg version (\S+)", text)
    return m.group(1) if m else None
