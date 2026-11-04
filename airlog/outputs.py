"""Encoder outputs: send what Airlog is recording on to somewhere else, live.

    icecast    Icecast 2 source (HTTP PUT, falling back to the legacy SOURCE method), optional TLS
    shoutcast  Shoutcast v1 source protocol (DNAS 1.x, or 2.x in legacy mode)
    device     a sound card output (PortAudio via `sounddevice`): feed a transmitter, a desk, monitors
    srt        SRT caller or listener (MPEG-TS): another Airlog, an SRT-capable codec, OBS…
    udp        MPEG-TS over UDP, unicast or multicast
    rtp        RTP (L16, MPEG audio, Opus or AAC), with an SDP description for the receiver

Each output is an ffmpeg encoder fed from the engine's PCM through a short queue. If an output
can't keep up, its oldest audio is dropped: an output can never hold up the recording.
For Icecast and Shoutcast, ffmpeg only encodes and Airlog does the server protocol itself, so it
can say exactly what went wrong (wrong password, mount in use…) and send song titles.
"""
from __future__ import annotations

import base64
import copy
import queue
import re
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
import uuid
from typing import TYPE_CHECKING

from . import __version__
from .sources import NO_WINDOW

if TYPE_CHECKING:
    from .engine import Engine

MASK = "********"
QUEUE_BLOCKS = 20            # 2 s of audio at 10 blocks/s; older audio is dropped beyond that
KINDS = ("icecast", "shoutcast", "srt", "udp", "rtp", "device")
KIND_LABELS = {"icecast": "Icecast", "shoutcast": "Shoutcast", "srt": "SRT", "udp": "UDP (MPEG-TS)", "rtp": "RTP",
               "device": "Sound card"}
CODECS = {
    "icecast": ("mp3", "aac", "ogg", "opus"),
    "shoutcast": ("mp3", "aac"),
    "srt": ("aac", "mp2", "mp3", "opus", "pcm"),
    "udp": ("aac", "mp2", "mp3", "opus", "pcm"),
    "rtp": ("pcm", "mp2", "mp3", "opus", "aac"),
    "device": ("pcm",),
}
DEFAULT_PORT = {"icecast": 8000, "shoutcast": 8000, "srt": 9000, "udp": 5004, "rtp": 5004, "device": 0}
DEVICE_BACKLOG_BLOCKS = 3    # sound card: never let more than ~0.3 s pile up ahead of the card (keeps latency fixed)
BITRATE_RANGE = {"mp3": (32, 320), "aac": (32, 512), "ogg": (48, 500), "opus": (16, 510), "mp2": (32, 384)}
DEFAULTS = {
    "id": "", "name": "", "enabled": True, "kind": "icecast", "codec": "mp3", "bitrate_kbps": 128,
    "host": "", "port": 8000, "tls": False,
    # Icecast / Shoutcast
    "mount": "/live", "user": "source", "password": "", "stream_name": "", "genre": "", "description": "",
    "url": "", "public": False, "send_titles": True,
    # SRT
    "mode": "caller", "latency_ms": 200, "passphrase": "",
    # UDP / RTP multicast
    "ttl": 16,
    # Sound card
    "device": "", "buffer_ms": 200,
    # Processing (this output only; the archive and grabs stay as they aired)
    "processing": "off", "ceiling_db": -1.0,
    "comp_threshold_db": -20.0, "comp_ratio": 4.0, "comp_attack_ms": 10.0, "comp_release_ms": 200.0,
    "comp_makeup_db": 6.0,
    # Multiband: per-band level trims (dB) after compression, and the AGC/compressor drive
    "mb_bass_db": 0.0, "mb_lowmid_db": 0.0, "mb_presence_db": 0.0, "mb_air_db": 0.0, "mb_drive_db": 0.0,
}

# Compressor presets: threshold dB, ratio, attack ms, release ms, make-up gain dB.
PRESETS = {
    # Make-up gain is set so loud passages come out at about the level they went in, and quiet
    # passages are brought up (measured in tests/test_processing.py).
    "gentle": (-18.0, 2.0, 20.0, 300.0, 5.5),       # evens out levels; barely audible
    "broadcast": (-22.0, 4.0, 10.0, 200.0, 11.5),   # consistent, denser "radio" level
    "heavy": (-28.0, 8.0, 5.0, 120.0, 18.5),        # loud and dense; for noisy listening
}
# Multiband broadcast processing: high-pass -> slow wideband AGC -> 4 bands, each with its own
# compressor -> sum -> peak limiter. The crossover (Linkwitz-Riley, 4th order) sums back flat, and each
# band only compresses itself, so a loud bass line no longer pulls the vocals and treble down.
MB_SPLITS = (120, 800, 4000)                      # bass | low-mid | presence | air (Hz)
MB_BANDS = ("bass", "lowmid", "presence", "air")
# Per preset: AGC (threshold dB, ratio, attack ms, release ms, make-up dB), then per band
# (threshold dB, ratio, attack ms, release ms, make-up dB). Make-up is set (measured in
# tests/test_processing.py) so loud passages leave at about the level they came in.
MB_PRESETS = {
    "mb_smooth": {
        "agc": (-20.0, 1.5, 2000.0, 8000.0, 0.0),
        "bands": ((-26.0, 2.5, 40.0, 400.0, 11.5), (-26.0, 2.5, 25.0, 300.0, 11.5),
                  (-28.0, 2.5, 12.0, 220.0, 11.5), (-30.0, 2.5, 6.0, 160.0, 11.5)),
    },
    "mb_punchy": {
        "agc": (-20.0, 1.7, 2000.0, 8000.0, 0.0),
        "bands": ((-28.0, 4.0, 50.0, 300.0, 14.5), (-28.0, 4.0, 20.0, 200.0, 14.5),
                  (-30.0, 4.0, 8.0, 150.0, 14.5), (-32.0, 4.0, 4.0, 110.0, 14.5)),
    },
    "mb_loud": {
        "agc": (-22.0, 2.0, 1500.0, 6000.0, 0.0),
        "bands": ((-34.0, 8.0, 20.0, 220.0, 22.0), (-34.0, 8.0, 10.0, 160.0, 22.0),
                  (-36.0, 8.0, 5.0, 110.0, 22.0), (-38.0, 8.0, 2.0, 80.0, 22.0)),
    },
}
PROCESSING = ("off", "limiter", "gentle", "broadcast", "heavy", "custom", "mb_smooth", "mb_punchy", "mb_loud")
PROCESSING_LABELS = {"gentle": "gentle", "broadcast": "broadcast", "heavy": "heavy", "custom": "custom",
                     "mb_smooth": "multiband smooth", "mb_punchy": "multiband punchy", "mb_loud": "multiband loud"}
SECRETS = ("password", "passphrase")


# ---------------------------------------------------------------- settings
def normalize_outputs(raw, previous: list | None = None) -> list[dict]:
    """Validate the outputs list from the browser. Masked secrets keep their previous value."""
    if not isinstance(raw, list):
        raise ValueError("outputs must be a list")
    prev = {o.get("id"): o for o in (previous or []) if isinstance(o, dict)}
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("bad output")
        o = copy.deepcopy(DEFAULTS)
        for k, v in item.items():
            # A blank field (e.g. a setting added in a newer version, shown empty) keeps its default.
            if k in o and not (v == "" and isinstance(DEFAULTS[k], (int, float)) and not isinstance(DEFAULTS[k], bool)):
                o[k] = v
        o["id"] = str(o["id"] or uuid.uuid4().hex[:8])
        old = prev.get(o["id"], {})
        for k in SECRETS:
            if o[k] == MASK:
                o[k] = old.get(k, "")
        try:
            for k in ("enabled", "tls", "public", "send_titles"):
                o[k] = bool(o[k])
            for k in ("bitrate_kbps", "port", "latency_ms", "ttl", "buffer_ms"):
                o[k] = int(float(o[k]))
            for k in ("ceiling_db", "comp_threshold_db", "comp_ratio", "comp_attack_ms", "comp_release_ms",
                      "comp_makeup_db", "mb_bass_db", "mb_lowmid_db", "mb_presence_db", "mb_air_db", "mb_drive_db"):
                o[k] = float(o[k])
            for k in ("name", "kind", "codec", "host", "mount", "user", "password", "stream_name", "genre",
                      "description", "url", "mode", "passphrase", "processing", "device"):
                o[k] = str(o[k]).strip() if k not in SECRETS else str(o[k])
        except (TypeError, ValueError):
            raise ValueError(f"Output {i + 1}: a number field isn't a number") from None
        label = o["name"] or f"Output {i + 1}"
        if o["kind"] not in KINDS:
            raise ValueError(f"{label}: unknown type {o['kind']!r}")
        if o["codec"] not in CODECS[o["kind"]]:
            o["codec"] = CODECS[o["kind"]][0]
        if not o["name"]:
            o["name"] = f"{KIND_LABELS[o['kind']]} {i + 1}"
        if o["kind"] == "device":
            o["port"], o["host"] = 0, ""
            o["buffer_ms"] = max(20, min(2000, o["buffer_ms"]))
        elif not 1 <= o["port"] <= 65535:
            raise ValueError(f"{label}: port must be 1–65535")
        lo, hi = BITRATE_RANGE.get(o["codec"], (0, 10_000))
        if o["codec"] in BITRATE_RANGE:
            o["bitrate_kbps"] = max(lo, min(hi, o["bitrate_kbps"]))
        listener = (o["kind"] == "srt" and o["mode"] == "listener") or o["kind"] == "device"
        if o["enabled"] and not o["host"] and not listener:
            raise ValueError(f"{label}: enter the server or receiver address")
        if re.search(r"[\s/?#@]", o["host"]):
            raise ValueError(f"{label}: the address should be just a host name or IP, e.g. stream.example.org")
        if o["kind"] == "srt":
            if o["mode"] not in ("caller", "listener"):
                o["mode"] = "caller"
            if o["passphrase"] and not 10 <= len(o["passphrase"]) <= 79:
                raise ValueError(f"{label}: an SRT passphrase must be 10–79 characters")
            o["latency_ms"] = max(20, min(8000, o["latency_ms"]))
        if o["kind"] in ("icecast", "shoutcast") and o["enabled"] and not o["password"]:
            raise ValueError(f"{label}: enter the source password")
        if o["kind"] == "icecast":
            o["mount"] = "/" + o["mount"].lstrip("/") if o["mount"].strip("/") else "/live"
        o["ttl"] = max(1, min(255, o["ttl"]))
        if o["processing"] not in PROCESSING:
            o["processing"] = "off"
        # Keep within what ffmpeg's acompressor / alimiter accept.
        o["ceiling_db"] = max(-24.0, min(0.0, o["ceiling_db"]))
        o["comp_threshold_db"] = max(-60.0, min(0.0, o["comp_threshold_db"]))
        o["comp_ratio"] = max(1.0, min(20.0, o["comp_ratio"]))
        o["comp_attack_ms"] = max(0.1, min(2000.0, o["comp_attack_ms"]))
        o["comp_release_ms"] = max(1.0, min(9000.0, o["comp_release_ms"]))
        o["comp_makeup_db"] = max(0.0, min(36.0, o["comp_makeup_db"]))
        for b in MB_BANDS:
            o[f"mb_{b}_db"] = max(-12.0, min(12.0, o[f"mb_{b}_db"]))
        o["mb_drive_db"] = max(-12.0, min(12.0, o["mb_drive_db"]))
        out.append(o)
    return out


def mask_outputs(outputs: list) -> list[dict]:
    """Outputs for the browser: every field present (with defaults for settings added since they
    were saved) and secrets masked."""
    res = []
    for o in outputs or []:
        o = {**copy.deepcopy(DEFAULTS), **o}
        for k in SECRETS:
            if o.get(k):
                o[k] = MASK
        res.append(o)
    return res


def describe(o: dict) -> str:
    k = o["kind"]
    if k == "device":
        return "sound card: " + (o["device"].split(" | ")[0] if o["device"] else "default output")
    if k == "icecast":
        return f"{'https' if o['tls'] else 'http'}://{o['host']}:{o['port']}{o['mount']}"
    if k == "shoutcast":
        return f"{o['host']}:{o['port']}"
    if k == "srt" and o["mode"] == "listener":
        return f"SRT listener on port {o['port']}"
    return f"{k}://{o['host']}:{o['port']}"


# ---------------------------------------------------------------- processing
def _comp(thr: float, ratio: float, att: float, rel: float, mk: float, knee: float = 2.8) -> str:
    return (f"acompressor=threshold={thr:g}dB:ratio={ratio:g}:attack={att:g}:release={rel:g}"
            f":makeup={max(0.0, mk):g}dB:knee={knee:g}")


def multiband_filter(o: dict, preset: dict) -> str:
    """High-pass, AGC, 4-band compression with trims, sum, limiter."""
    drive = o.get("mb_drive_db", 0.0)
    pre = ["highpass=f=30:poles=2"]
    if drive:
        pre.append(f"volume={drive:g}dB")
    pre.append(_comp(*preset["agc"], knee=6))
    split = " ".join(str(f) for f in MB_SPLITS)
    graph = [",".join(pre) + f",acrossover=split={split}:order=4th[b0][b1][b2][b3]"]
    for i, (band, params) in enumerate(zip(MB_BANDS, preset["bands"])):
        trim = o.get(f"mb_{band}_db", 0.0)
        thr, ratio, att, rel, mk = params
        # A positive trim beyond the compressor's 0-36 dB make-up range is applied as plain gain.
        stage = _comp(thr, ratio, att, rel, mk)
        if trim:
            stage += f",volume={trim:g}dB"
        graph.append(f"[b{i}]{stage}[c{i}]")
    ceiling = o.get("ceiling_db", -1.0)
    graph.append("[c0][c1][c2][c3]amix=inputs=4:normalize=0,"
                 f"alimiter=limit={ceiling:g}dB:attack=5:release=50:level=0")
    return ";".join(graph)


def process_filter(o: dict) -> str | None:
    """ffmpeg -af chain for this output's processing: compressor (preset, custom or multiband), then a
    peak limiter at the ceiling. The limiter's auto-level is off, so it only ever turns peaks down."""
    mode = o.get("processing", "off")
    if mode == "off":
        return None
    if mode in MB_PRESETS:
        return multiband_filter(o, MB_PRESETS[mode])
    chain = []
    if mode in PRESETS or mode == "custom":
        thr, ratio, att, rel, mk = PRESETS[mode] if mode in PRESETS else (
            o["comp_threshold_db"], o["comp_ratio"], o["comp_attack_ms"], o["comp_release_ms"], o["comp_makeup_db"])
        chain.append(f"acompressor=threshold={thr:g}dB:ratio={ratio:g}:attack={att:g}:release={rel:g}"
                     f":makeup={mk:g}dB:knee=2.8")
    ceiling = o.get("ceiling_db", -1.0)
    chain.append(f"alimiter=limit={ceiling:g}dB:attack=5:release=50:level=0")
    return ",".join(chain)


def describe_processing(o: dict) -> str:
    mode = o.get("processing", "off")
    if mode == "off":
        return ""
    if mode == "limiter":
        return f"limiter {o['ceiling_db']:g} dBFS"
    return f"{PROCESSING_LABELS.get(mode, mode)} compression, limiter {o['ceiling_db']:g} dBFS"


# ---------------------------------------------------------------- encoding
def codec_args(o: dict, sr: int, ch: int) -> tuple[list[str], str]:
    """ffmpeg encoder arguments and the MIME type for Icecast/Shoutcast."""
    c, br = o["codec"], f"{o['bitrate_kbps']}k"
    kind = o["kind"]
    if c == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", br], "audio/mpeg"
    if c == "aac":
        return ["-c:a", "aac", "-b:a", br], "audio/aac"
    if c == "ogg":
        return ["-c:a", "libvorbis", "-b:a", br], "audio/ogg"
    if c == "opus":
        return ["-c:a", "libopus", "-b:a", br, "-ar", "48000"], "audio/ogg"
    if c == "mp2":
        return ["-c:a", "mp2", "-b:a", br], "audio/mpeg"
    if c == "pcm":
        if kind == "rtp":
            return ["-c:a", "pcm_s16be"], ""                                      # L16
        return ["-c:a", "s302m", "-strict", "-2", "-ar", "48000", "-ac", str(max(2, ch))], ""  # SMPTE 302M in TS
    raise ValueError(f"unsupported codec {c}")


def container(o: dict) -> str:
    if o["kind"] in ("icecast", "shoutcast"):
        return {"mp3": "mp3", "aac": "adts", "ogg": "ogg", "opus": "ogg"}[o["codec"]]
    return "rtp" if o["kind"] == "rtp" else "mpegts"


# ---------------------------------------------------------------- one output
class OutputError(Exception):
    pass


class Output:
    def __init__(self, engine: "Engine", spec: dict):
        self.engine = engine
        self.o = spec
        self.q: queue.Queue[bytes] = queue.Queue(maxsize=QUEUE_BLOCKS)
        self.state = "off" if not spec["enabled"] else "starting"
        self.detail = ""
        self.since = time.time()
        self.sent = 0                     # bytes sent to the network (Icecast/Shoutcast)
        self.dropped = 0                  # blocks dropped because the output fell behind
        self.sdp = ""
        self.sdp_path = None              # RTP: where ffmpeg writes the SDP (it can appear a moment after connecting)
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.stream = None                # sound card output stream
        self.underruns = 0                # sound card: blocks of silence played because no audio was ready
        self.running = False
        self._last_logged = ("", "")
        self._title_sent = None

    # -- engine side: never blocks
    def feed(self, pcm: bytes) -> None:
        try:
            self.q.put_nowait(pcm)
        except queue.Full:
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            self.dropped += 1
            try:
                self.q.put_nowait(pcm)
            except queue.Full:
                pass

    def status(self) -> dict:
        o = self.o
        if self.sdp_path is not None and not self.sdp and self.state == "sending":
            try:
                self.sdp = self.sdp_path.read_text(encoding="utf-8")
            except OSError:
                pass
        return {"id": o["id"], "name": o["name"], "kind": o["kind"], "codec": o["codec"], "underruns": self.underruns,
                "processing": describe_processing(o),
                "target": describe(o), "state": self.state, "detail": self.detail, "since": self.since,
                "sent": self.sent, "dropped": self.dropped, "sdp": self.sdp}

    def _set(self, state: str, detail: str = "", error: bool = False) -> None:
        if (state, detail) != (self.state, self.detail):
            self.since = time.time()
        self.state, self.detail = state, detail
        if (state, detail) != self._last_logged and state not in ("starting",):
            self._last_logged = (state, detail)
            words = {"connected": "connected", "sending": "sending", "waiting": "waiting", "playing": "playing on",
                     "connecting": "connecting", "error": "problem", "off": "stopped"}
            self.engine.log(f"Output \"{self.o['name']}\" {words.get(state, state)}"
                            + (f": {detail}" if detail else ""), error=error)

    # -- lifecycle
    def start(self) -> None:
        if not self.o["enabled"]:
            return
        self.running = True
        threading.Thread(target=self._run, name=f"output-{self.o['id']}", daemon=True).start()

    def stop(self) -> None:
        self.running = False
        self._kill()
        self.state = "off"

    def _kill(self) -> None:
        st = self.stream
        if st is not None:
            try:
                st.abort()                # unblocks a write in progress
            except Exception:
                pass
        p, s = self.proc, self.sock
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
        if p is not None and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def _sleep(self, secs: float) -> None:
        end = time.monotonic() + secs
        while self.running and time.monotonic() < end:
            time.sleep(0.2)
            self._drain()                 # keep the queue fresh while not sending

    def _drain(self) -> None:
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def _run(self) -> None:
        backoff = 2.0
        while self.running:
            ok = False
            try:
                ok = self._session()
            except OutputError as e:
                self._set("error", str(e), error=True)
            except Exception as e:  # noqa: BLE001 - keep the output thread alive whatever happens
                self._set("error", f"{type(e).__name__}: {e}"[:200], error=True)
            finally:
                self._kill()
            if not self.running:
                break
            if ok and self.state != "error":
                self._set("connecting", "reconnecting")
            backoff = 2.0 if ok else min(backoff * 2, 30.0)
            self._sleep(backoff)

    # -- one connection
    def _encoder(self, target: list[str], loglevel: str) -> subprocess.Popen:
        cfg = self.engine.cfg
        args, _ = codec_args(self.o, self.engine.sr, self.engine.ch)
        af = process_filter(self.o)
        cmd = [cfg.ffmpeg_path, "-hide_banner", "-nostdin", "-nostats", "-loglevel", loglevel,
               "-f", "s16le", "-ar", str(self.engine.sr), "-ac", str(self.engine.ch), "-i", "pipe:0",
               *(["-af", af] if af else []), *args, "-f", container(self.o), *target]
        try:
            return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=NO_WINDOW)
        except OSError as e:
            raise OutputError(f"can't run ffmpeg: {e}") from None

    def _pump_pcm(self, proc: subprocess.Popen) -> None:
        """Queue -> encoder stdin, until the encoder or the output stops."""
        self._drain()                     # start with live audio, not a backlog
        while self.running and proc.poll() is None:
            try:
                block = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                proc.stdin.write(block)
            except (OSError, ValueError):
                break
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    def _session(self) -> bool:
        if self.o["kind"] == "device":
            return self._device_session()
        if self.o["kind"] in ("icecast", "shoutcast"):
            return self._server_session()
        return self._ffmpeg_session()

    # Sound card: PortAudio plays the PCM; ffmpeg only runs if there's processing to apply
    def _device_session(self) -> bool:
        o, e = self.o, self.engine
        sd = sounddevice_module()
        index, label, extra = resolve_device(sd, o["device"])
        self._set("connecting", label)
        try:
            stream = sd.RawOutputStream(samplerate=e.sr, channels=e.ch, dtype="int16", device=index,
                                        latency=o["buffer_ms"] / 1000, extra_settings=extra)
            stream.start()
        except Exception as ex:
            raise OutputError(f"can't open {label}: {ex}"
                              + (" (it may not support " + f"{e.sr} Hz {e.ch}-channel; try another entry for the same "
                                 "card, e.g. 'default' or the MME/DirectSound one)" if "rate" in str(ex).lower()
                                 or "channel" in str(ex).lower() else "")) from None
        self.stream = stream
        src: queue.Queue = self.q
        af = process_filter(o)
        if af:
            # ffmpeg as a filter only: PCM in, processed PCM out, at the same rate and channels.
            cmd = [e.cfg.ffmpeg_path, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error",
                   "-f", "s16le", "-ar", str(e.sr), "-ac", str(e.ch), "-i", "pipe:0",
                   "-af", af, "-f", "s16le", "-ar", str(e.sr), "-ac", str(e.ch), "pipe:1"]
            try:
                proc = self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                    stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            except OSError as ex:
                raise OutputError(f"can't run ffmpeg for processing: {ex}") from None
            threading.Thread(target=self._pump_pcm, args=(proc,), daemon=True).start()
            processed: queue.Queue[bytes] = queue.Queue(maxsize=QUEUE_BLOCKS)
            threading.Thread(target=self._read_processed, args=(proc, processed), daemon=True).start()
            src = processed
        else:
            self._drain()                 # start with live audio, not a backlog
        self._set("playing", label)
        block_frames = e.block_frames
        silence = bytes(block_frames * e.bpf)
        # A cushion of audio in the card absorbs timing jitter: fill it to the buffer setting before
        # playing, top it up as audio arrives, and only feed silence when the card is about to run dry
        # (the source has stalled). After a gap, build the cushion up again before resuming.
        prefill = max(2, round(o["buffer_ms"] / 100))
        card_frames = int(o["buffer_ms"] / 1000 * e.sr)
        filling = True
        while self.running:
            ready = src.qsize()
            if ready >= (prefill if filling else 1):
                filling = False
                while src.qsize() > prefill + DEVICE_BACKLOG_BLOCKS:    # a burst: keep the delay fixed
                    try:
                        src.get_nowait()
                        self.dropped += 1
                    except queue.Empty:
                        break
                data = src.get_nowait()
            else:
                try:
                    free = stream.write_available
                except Exception:
                    free = card_frames
                if free < card_frames - block_frames:
                    time.sleep(0.01)      # the card still has audio queued: wait for the source
                    continue
                data = silence            # the card is about to run dry: keep it fed, no glitch
                self.underruns += 1
                filling = True
            try:
                stream.write(data)        # blocks while the card's buffer is full: this paces the loop
            except Exception as ex:
                if not self.running:
                    break
                raise OutputError(f"{label} stopped: {ex} (unplugged?)") from None
            self.sent += len(data)
            if self.proc is not None and self.proc.poll() is not None and self.running:
                raise OutputError("the processing stage stopped unexpectedly")
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        self.stream = None
        return True

    def _read_processed(self, proc: subprocess.Popen, out: queue.Queue) -> None:
        block = self.engine.block_frames * self.engine.bpf
        while self.running:
            data = proc.stdout.read(block)
            if not data:
                return
            try:
                out.put_nowait(data)
            except queue.Full:
                try:
                    out.get_nowait()
                except queue.Empty:
                    pass
                out.put_nowait(data)

    # SRT / UDP / RTP: ffmpeg does the network side
    def _ffmpeg_session(self) -> bool:
        o = self.o
        kind = o["kind"]
        if kind == "srt":
            host = "0.0.0.0" if o["mode"] == "listener" else o["host"]
            target = ["-mode", o["mode"], "-latency", str(o["latency_ms"] * 1000)]
            if o["passphrase"]:
                target += ["-passphrase", o["passphrase"]]
            target += [f"srt://{host}:{o['port']}"]
            self._set("waiting" if o["mode"] == "listener" else "connecting",
                      f"for a receiver on port {o['port']}" if o["mode"] == "listener" else describe(o))
        elif kind == "udp":
            target = [f"udp://{o['host']}:{o['port']}?pkt_size=1316&ttl={o['ttl']}"]
            self._set("connecting", describe(o))
        else:
            sdp = self.sdp_path = self.engine.cfg.effective_spool_dir() / f"output-{o['id']}.sdp"
            self.sdp = ""
            target = ["-sdp_file", str(sdp), f"rtp://{o['host']}:{o['port']}?ttl={o['ttl']}"]
            self._set("connecting", describe(o))
        proc = self.proc = self._encoder(target, "info")
        threading.Thread(target=self._pump_pcm, args=(proc,), daemon=True).start()
        connected = False
        last_err = ""
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode(errors="replace").strip()
            if not self.running:
                break
            if line.startswith("Output #0"):
                connected = True
                if kind == "rtp":
                    try:
                        self.sdp = sdp.read_text(encoding="utf-8")
                    except OSError:
                        self.sdp = ""
                self._set("connected" if kind == "srt" else "sending", describe(o))
                continue
            low = line.lower()
            if "badsecret" in low or "incorrect passphrase" in low:
                last_err = "wrong SRT passphrase (or the other end has none / a different one)"
            elif "connection timed out" in low or "connection setup failure" in low:
                last_err = f"no answer from {o['host']}:{o['port']} (is the receiver listening, and the port open?)"
            elif "connection refused" in low or "rejected" in low:
                last_err = f"refused by {o['host']}:{o['port']}"
            elif "address already in use" in low:
                last_err = f"port {o['port']} is already in use on this machine"
            elif ("error" in low or "failed" in low or "invalid" in low) and not last_err:
                last_err = line[:200]
        proc.wait()
        if not self.running:
            return connected
        if connected:
            self._set("connecting", "connection lost; reconnecting")
            return True
        raise OutputError(last_err or f"ffmpeg exited ({proc.returncode})")

    # Icecast / Shoutcast: ffmpeg encodes to stdout, we speak the server protocol
    def _server_session(self) -> bool:
        o = self.o
        _, ctype = codec_args(o, self.engine.sr, self.engine.ch)
        self._set("connecting", describe(o))
        sock = self.sock = (_icecast_connect if o["kind"] == "icecast" else _shoutcast_connect)(o, ctype, self.engine)
        proc = self.proc = self._encoder(["pipe:1"], "error")
        threading.Thread(target=self._pump_pcm, args=(proc,), daemon=True).start()
        errs: list[str] = []
        threading.Thread(target=lambda: errs.extend(l.decode(errors="replace").strip()
                                                    for l in iter(proc.stderr.readline, b"")), daemon=True).start()
        self._set("connected", describe(o))
        self._title_sent = None
        sock.settimeout(20)
        next_title_check = 0.0
        out = proc.stdout
        while self.running:
            chunk = out.read1(16384) if hasattr(out, "read1") else out.read(4096)
            if not chunk:
                break
            try:
                sock.sendall(chunk)
            except (OSError, socket.timeout) as e:
                if not self.running:
                    break
                raise OutputError(f"connection to the server dropped ({type(e).__name__})") from None
            self.sent += len(chunk)
            if o["send_titles"] and time.monotonic() >= next_title_check:
                next_title_check = time.monotonic() + 2
                self._maybe_send_title()
        if self.running and proc.poll() not in (None, 0):
            raise OutputError("encoder failed: " + (errs[-1] if errs else f"exit {proc.returncode}"))
        return True

    def _maybe_send_title(self) -> None:
        if self.o["codec"] not in ("mp3", "aac"):
            return                        # Ogg streams carry their own tags; servers ignore updinfo for them
        cur = self.engine.tracks.current()
        title = f"{cur.artist} - {cur.title}" if cur else (self.o["stream_name"] or self.engine.cfg.station_name)
        if title == self._title_sent:
            return
        self._title_sent = title
        threading.Thread(target=_send_title, args=(self.o, title, self.engine), daemon=True).start()


# ---------------------------------------------------------------- server protocols
def _open(o: dict, port: int, timeout: float = 10) -> socket.socket:
    try:
        s = socket.create_connection((o["host"], port), timeout=timeout)
    except socket.gaierror:
        raise OutputError(f"server address {o['host']} not found") from None
    except ConnectionRefusedError:
        raise OutputError(f"nothing accepting connections on {o['host']}:{port}") from None
    except (socket.timeout, TimeoutError):
        raise OutputError(f"no answer from {o['host']}:{port}") from None
    except OSError as e:
        raise OutputError(f"can't connect to {o['host']}:{port}: {e.strerror or e}") from None
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if o.get("tls"):
        try:
            s = ssl.create_default_context().wrap_socket(s, server_hostname=o["host"])
        except (ssl.SSLError, OSError) as e:
            s.close()
            raise OutputError(f"TLS failed with {o['host']}: {e}") from None
    return s


def _read_head(s: socket.socket) -> tuple[int, str, bytes]:
    """Read a response head; returns (status, reason, any body bytes already received)."""
    buf = b""
    s.settimeout(10)
    try:
        while b"\r\n\r\n" not in buf and b"\n\n" not in buf and len(buf) < 16384:
            got = s.recv(4096)
            if not got:
                break
            buf += got
    except (socket.timeout, TimeoutError):
        pass
    head, _, body = buf.replace(b"\r\n", b"\n").partition(b"\n\n")
    first = head.split(b"\n", 1)[0].decode(errors="replace")
    m = re.match(r"\S+\s+(\d{3})\s*(.*)", first)
    if not m:
        return 0, first, body
    return int(m.group(1)), m.group(2), body


def _body_text(s: socket.socket, already: bytes) -> str:
    buf = already
    s.settimeout(1)
    try:
        while len(buf) < 4096:
            got = s.recv(4096)
            if not got:
                break
            buf += got
    except (socket.timeout, TimeoutError, OSError):
        pass
    text = re.sub(r"<[^>]+>", " ", buf.decode(errors="replace"))
    return re.sub(r"\s+", " ", text).strip()[:160]


def _icecast_headers(o: dict, ctype: str, engine: "Engine") -> list[str]:
    name = o["stream_name"] or engine.cfg.station_name
    h = [f"Content-Type: {ctype}", f"Ice-Public: {1 if o['public'] else 0}", f"Ice-Name: {name}",
         f"User-Agent: Airlog/{__version__}"]
    for key, hdr in (("description", "Ice-Description"), ("genre", "Ice-Genre"), ("url", "Ice-Url")):
        if o[key]:
            h.append(f"{hdr}: {o[key]}")
    if o["codec"] != "opus":
        h.append(f"Ice-Audio-Info: bitrate={o['bitrate_kbps']};channels={engine.ch};samplerate={engine.sr}")
    return [x.replace("\r", " ").replace("\n", " ") for x in h]


def _icecast_connect(o: dict, ctype: str, engine: "Engine") -> socket.socket:
    auth = base64.b64encode(f"{o['user'] or 'source'}:{o['password']}".encode()).decode()
    mount = urllib.parse.quote(o["mount"])
    for method in ("PUT", "SOURCE"):
        s = _open(o, o["port"])
        lines = [f"{method} {mount} HTTP/1.{1 if method == 'PUT' else 0}", f"Host: {o['host']}:{o['port']}",
                 f"Authorization: Basic {auth}", *_icecast_headers(o, ctype, engine)]
        if method == "PUT":
            lines.append("Expect: 100-continue")
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        code, reason, body = _read_head(s)
        if code in (100, 200):
            return s
        detail = _body_text(s, body)
        s.close()
        if method == "PUT" and code in (400, 405, 501, 0):
            continue                      # older Icecast: try the legacy SOURCE method
        if code == 401:
            raise OutputError("the server refused the user name or source password")
        if code == 403:
            raise OutputError(f"the server refused the stream: {detail}" if detail else
                              "the server refused the stream (mount already in use, wrong format, "
                              "or too many sources?)")
        if code == 404:
            raise OutputError(f"mount {o['mount']} not found on the server")
        raise OutputError(f"server answered {code} {reason} {detail}".strip())
    raise OutputError("the server doesn't speak the Icecast source protocol (is this the right port?)")


def _shoutcast_connect(o: dict, ctype: str, engine: "Engine") -> socket.socket:
    """Shoutcast v1: password on port+1, then icy- headers, then audio."""
    s = _open(o, o["port"] + 1)
    s.sendall(f"{o['password']}\r\n".encode())
    s.settimeout(10)
    buf = b""
    try:
        while b"\n" not in buf and len(buf) < 1024:
            got = s.recv(1024)
            if not got:
                break
            buf += got
    except (socket.timeout, TimeoutError):
        pass
    reply = buf.decode(errors="replace").strip()
    if not reply.upper().startswith("OK"):
        s.close()
        if "invalid" in reply.lower() or "password" in reply.lower():
            raise OutputError("the server refused the source password")
        raise OutputError(f"unexpected reply from the Shoutcast server: {reply[:80] or 'nothing'} "
                          f"(the source port is {o['port']} + 1 = {o['port'] + 1})")
    name = o["stream_name"] or engine.cfg.station_name
    h = [f"content-type:{ctype}", f"icy-name:{name}", f"icy-genre:{o['genre']}", f"icy-pub:{1 if o['public'] else 0}",
         f"icy-br:{o['bitrate_kbps']}", f"icy-url:{o['url']}"]
    s.sendall(("\r\n".join(x.replace("\r", " ").replace("\n", " ") for x in h) + "\r\n\r\n").encode())
    return s


def _send_title(o: dict, title: str, engine: "Engine") -> None:
    """Now-playing title from song identification: Icecast admin/metadata or Shoutcast admin.cgi."""
    q = urllib.parse.quote(title)
    if o["kind"] == "icecast":
        path = f"/admin/metadata?mount={urllib.parse.quote(o['mount'])}&mode=updinfo&charset=UTF-8&song={q}"
        auth = base64.b64encode(f"{o['user'] or 'source'}:{o['password']}".encode()).decode()
        headers = [f"Authorization: Basic {auth}", f"User-Agent: Airlog/{__version__}"]
    else:
        pw, _, sid = o["password"].partition(":#")
        path = f"/admin.cgi?pass={urllib.parse.quote(pw)}&mode=updinfo&song={q}" + (f"&sid={sid}" if sid else "")
        headers = ["User-Agent: Mozilla/5.0 (Airlog)"]          # Shoutcast 1.x insists on "Mozilla"
    try:
        s = _open(o, o["port"], timeout=5)
        s.sendall((f"GET {path} HTTP/1.0\r\nHost: {o['host']}:{o['port']}\r\n" + "\r\n".join(headers)
                   + "\r\n\r\n").encode())
        code, reason, _ = _read_head(s)
        s.close()
        if code and code >= 400:
            engine.log(f"Output \"{o['name']}\": song title not accepted ({code} {reason})")
    except (OutputError, OSError):
        pass


# ---------------------------------------------------------------- sound cards
def sounddevice_module():
    """The sounddevice (PortAudio) module, or an OutputError saying how to get it."""
    import sys
    try:
        import sounddevice
        return sounddevice
    except OSError:                       # the module is there but the PortAudio library isn't
        if sys.platform.startswith("linux"):
            raise OutputError("sound card output needs PortAudio: sudo apt install libportaudio2") from None
        raise OutputError("sound card output: the PortAudio library couldn't be loaded") from None
    except ImportError:
        raise OutputError("sound card output isn't available in this build (sounddevice not installed)") from None


def _hostapi_name(sd, dev: dict) -> str:
    try:
        return sd.query_hostapis(dev["hostapi"])["name"]
    except Exception:
        return ""


def list_output_devices() -> tuple[list[dict], str]:
    """([{value, label}], error). The value is 'name | host API', stable across reboots, unlike indexes."""
    try:
        sd = sounddevice_module()
        devs = sd.query_devices()
    except OutputError as e:
        return [], str(e)
    except Exception as e:  # noqa: BLE001
        return [], f"can't list sound cards: {e}"
    out = [{"value": "", "label": "System default output"}]
    for d in devs:
        if d.get("max_output_channels", 0) > 0:
            api = _hostapi_name(sd, d)
            out.append({"value": f"{d['name']} | {api}", "label": f"{d['name']} ({api}, {d['max_output_channels']} ch)"})
    return out, ""


def resolve_device(sd, spec: str):
    """(index or None for the default, label, extra_settings) for a saved 'name | host API'."""
    if not spec:
        return None, "the default sound card", None
    name, _, api = spec.partition(" | ")
    devs = [(i, d) for i, d in enumerate(sd.query_devices()) if d.get("max_output_channels", 0) > 0]
    exact = [i for i, d in devs if d["name"] == name and (not api or _hostapi_name(sd, d) == api)]
    loose = [i for i, d in devs if d["name"] == name] or [i for i, d in devs if name.lower() in d["name"].lower()]
    pick = (exact or loose or [None])[0]
    if pick is None:
        raise OutputError(f"sound card '{name}' not found (unplugged or renamed?)")
    extra = None
    if "WASAPI" in _hostapi_name(sd, sd.query_devices(pick)):
        try:
            extra = sd.WasapiSettings(auto_convert=True)   # let Windows convert the sample rate
        except Exception:
            extra = None
    return pick, f"sound card '{name}'", extra


# ---------------------------------------------------------------- the set
class OutputSet:
    def __init__(self, engine: "Engine"):
        try:
            specs = normalize_outputs(engine.cfg.outputs or [])
        except ValueError as e:
            engine.log(f"Outputs not started: {e}", error=True)
            specs = []
        self.items = [Output(engine, s) for s in specs]
        self.active = [o for o in self.items if o.o["enabled"]]

    def start(self) -> None:
        for o in self.active:
            o.start()

    def stop(self) -> None:
        for o in self.items:
            o.stop()

    def feed(self, pcm: bytes) -> None:
        for o in self.active:
            o.feed(pcm)

    def status(self) -> list[dict]:
        return [o.status() for o in self.items]
