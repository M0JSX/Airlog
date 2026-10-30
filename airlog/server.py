"""Airlog server: headless recorder with a browser UI.

    python3 -m airlog.server [--config /path/config.json] [--host 0.0.0.0] [--port 8080]

Runs the capture engine (same as the desktop app) and serves a web UI on the
LAN. Clients see the live waveform, select audio and download it straight to
their own machine for editing, or save grabs on the server.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hmac
import logging
import os
import re
import threading
import time
from dataclasses import asdict
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory, stream_with_context

from . import __version__, sources
from .config import Config
from .engine import Engine

WEB_DIR = Path(__file__).with_name("web")
AUDIO_EXT = {".mp3", ".flac", ".wav", ".ogg"}

# Settings the browser may edit (everything else stays file-only).
EDITABLE = [
    "station_name", "source_type", "stream_url", "device_backend", "device_name", "custom_input_args",
    "ffmpeg_path", "source_delay_s", "sample_rate", "channels",
    "archive_enabled", "archive_dir", "archive_pattern", "archive_format", "archive_bitrate_kbps",
    "split_minutes", "archive_keep_days",
    "buffer_minutes", "spool_dir", "grabs_dir", "grab_format", "grab_pattern", "grab_preroll_s",
    "silence_alert_db", "silence_alert_s", "autostart", "web_password",
    "identify_songs", "identify_interval_s",
]


class Holder:
    """Owns the engine so it can be swapped when settings change."""

    def __init__(self, cfg: Config, cfg_path: Path):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.lock = threading.Lock()
        self.engine = self._new_engine()
        self.started = time.time()

    def _new_engine(self) -> Engine:
        e = Engine(self.cfg)
        e.on_log = lambda line, error: print(("ERROR " if error else "") + line, flush=True)  # -> journalctl
        return e

    def restart(self, start: bool) -> None:
        with self.lock:
            old = self.engine
            old.stop()
            self.engine = self._new_engine()
            for line in old.logs:          # keep the log continuous across restarts
                self.engine.logs.append(line)
            if start:
                self.engine.start()


def tz_offset(t: float) -> float:
    off = dt.datetime.fromtimestamp(t).astimezone().utcoffset()
    return off.total_seconds() if off else 0.0


def safe_child(root: Path, rel: str) -> Path:
    root = root.resolve()
    p = (root / rel).resolve()
    if root not in p.parents and p != root:
        abort(404)
    return p


def create_app(holder: Holder) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False

    # ---------------------------------------------------------------- auth
    @app.before_request
    def check_auth():
        pw = holder.cfg.web_password
        if not pw:
            return None
        hdr = request.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(hdr[6:]).decode().partition(":")
            except Exception:
                given = ""
            if hmac.compare_digest(given, pw):
                return None
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Airlog"'})

    def err(msg: str, code: int = 400):
        return jsonify(error=msg), code

    def ftime(name: str) -> float:
        v = request.values.get(name)
        if v is None:
            abort(err(f"missing {name}"))
        try:
            return float(v)
        except ValueError:
            abort(err(f"bad {name}"))

    # ---------------------------------------------------------------- pages
    @app.get("/")
    def index():
        resp = send_from_directory(WEB_DIR, "index.html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(Path(__file__).with_name("assets"), "icon.png", mimetype="image/png")

    # ---------------------------------------------------------------- status
    @app.get("/api/status")
    def status():
        e, cfg = holder.engine, holder.cfg
        now = time.time()
        arch = None
        if e.archive is not None:
            arch = {"name": e.archive.path.name, "start": e.archive.start, "boundary": e.archive.boundary}
        return jsonify(
            version=__version__,
            station=cfg.station_name,
            status=e.status,
            running=e.running,
            source=sources.describe(cfg),
            source_type=cfg.source_type,
            last_error=e.last_error,
            server_time=now,
            tz_offset=tz_offset(now),
            live=e.live_time(),
            buffer_start=e.buffer_start(),
            buffer_minutes=cfg.buffer_minutes,
            level=list(e.level) if e.status == "recording" else [0, 0],
            silence_s=e.silence_seconds() if e.status == "recording" else 0,
            silence_alert_s=cfg.silence_alert_s,
            archive=arch,
            archive_enabled=cfg.archive_enabled,
            split_minutes=cfg.split_minutes,
            sample_rate=cfg.sample_rate,
            channels=cfg.channels,
            grab_format=cfg.grab_format,
            grab_preroll_s=cfg.grab_preroll_s,
            identify=cfg.identify_songs,
            identify_state=e.recognizer.state if e.recognizer else "off",
            now_playing=asdict(cur) if (cur := e.tracks.current()) else None,
            tracks_rev=e.tracks.rev,
        )

    @app.get("/api/tracks")
    def tracks():
        return jsonify(tracks=holder.engine.tracks.recent(int(request.args.get("limit", 200))))

    @app.get("/api/peaks")
    def peaks():
        t1, t2 = ftime("from"), ftime("to")
        n = int(request.args.get("n", 1000))
        if t2 <= t1:
            return err("bad range")
        e = holder.engine
        return jsonify(**{"from": t1, "to": t2, "peaks": e.peak_buckets(t1, t2, n),
                          "live": e.live_time(), "buffer_start": e.buffer_start()})

    @app.get("/api/log")
    def log():
        return jsonify(lines=list(holder.engine.logs)[-300:])

    # ---------------------------------------------------------------- audio out
    @app.get("/api/clip")
    def clip():
        """Stream [from, to) as wav/mp3/flac. download=1 sets an attachment filename."""
        e = holder.engine
        fmt = request.args.get("fmt", "wav").lower()
        if fmt not in ("wav", "mp3", "flac", "ogg"):
            return err("format must be wav, mp3, flac or ogg")
        try:
            t1, t2 = e.clamp_range(ftime("from"), ftime("to"))
        except ValueError as ex:
            return err(str(ex), 416)
        bitrate = request.args.get("kbps", type=int)
        mime = {"wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg"}[fmt]
        headers = {"Cache-Control": "no-store"}
        if fmt == "wav":
            headers["Content-Length"] = str(44 + e.spool.frames_for(t1, t2) * e.bpf)
        if request.args.get("download"):
            name = request.args.get("name") or e.clip_name(t1, t2, fmt)
            name = re.sub(r'[\\/:*?"<>|\r\n]', "_", name)
            if not name.lower().endswith("." + fmt):
                name += "." + fmt
            headers["Content-Disposition"] = f'attachment; filename="{name}"'
        return Response(stream_with_context(e.iter_clip(t1, t2, fmt, bitrate)), mimetype=mime, headers=headers,
                        direct_passthrough=True)

    @app.post("/api/grab")
    def grab():
        """Save a clip into the server's grabs folder."""
        data = request.get_json(silent=True) or {}
        try:
            t1, t2 = float(data["from"]), float(data["to"])
        except (KeyError, TypeError, ValueError):
            return err("from/to required")
        fmt = (data.get("fmt") or holder.cfg.grab_format).lower()
        try:
            path = holder.engine.grab(t1, t2, fmt=fmt)
        except (ValueError, RuntimeError) as ex:
            return err(str(ex))
        rel = path.relative_to(Path(holder.cfg.grabs_dir)).as_posix()
        return jsonify(name=path.name, url=f"/files/grabs/{rel}", seconds=t2 - t1)

    # ---------------------------------------------------------------- file lists
    def list_audio(root: Path, limit: int, under: str = "") -> list[dict]:
        base = safe_child(root, under) if under else root
        if not base.exists():
            return []
        items = []
        for p in base.rglob("*"):
            if p.is_file() and p.suffix.lower() in AUDIO_EXT and ".partial." not in p.name:
                try:
                    st = p.stat()
                except OSError:
                    continue
                items.append((st.st_mtime, p, st.st_size))
        items.sort(key=lambda x: x[0], reverse=True)
        out = []
        for mtime, p, size in items[:limit]:
            out.append({"name": p.name, "path": p.relative_to(root).as_posix(), "size": size, "mtime": mtime})
        return out

    @app.get("/api/grabs")
    def grabs():
        return jsonify(files=list_audio(Path(holder.cfg.grabs_dir), 200))

    @app.delete("/api/grabs/<path:rel>")
    def delete_grab(rel):
        p = safe_child(Path(holder.cfg.grabs_dir), rel)
        if not p.is_file():
            return err("not found", 404)
        p.unlink()
        return jsonify(ok=True)

    @app.get("/api/archive")
    def archive():
        root = Path(holder.cfg.archive_dir)
        writing = holder.engine.archive.path.resolve() if holder.engine.archive else None
        files = list_audio(root, 500)
        day = request.args.get("date")
        for f in files:
            f["writing"] = writing is not None and (root / f["path"]).resolve() == writing
        if day:
            files = [f for f in files if day in f["path"]]
        days = sorted({m.group(0) for f in list_audio(root, 5000)
                       for m in [re.search(r"\d{4}-\d{2}-\d{2}", f["path"])] if m}, reverse=True)
        return jsonify(files=files, days=days)

    @app.get("/files/grabs/<path:rel>")
    def grab_file(rel):
        p = safe_child(Path(holder.cfg.grabs_dir), rel)
        if not p.is_file():
            abort(404)
        return send_file(p, as_attachment=bool(request.args.get("download")), download_name=p.name,
                         conditional=True)

    @app.get("/files/archive/<path:rel>")
    def archive_file(rel):
        p = safe_child(Path(holder.cfg.archive_dir), rel)
        if not p.is_file():
            abort(404)
        return send_file(p, as_attachment=bool(request.args.get("download")), download_name=p.name,
                         conditional=True)

    # ---------------------------------------------------------------- control & settings
    @app.post("/api/record")
    def record():
        action = (request.get_json(silent=True) or {}).get("action")
        e = holder.engine
        if action == "start":
            e.start()
        elif action == "stop":
            threading.Thread(target=e.stop, daemon=True).start()
        else:
            return err("action must be start or stop")
        return jsonify(ok=True)

    @app.get("/api/config")
    def get_config():
        d = asdict(holder.cfg)
        out = {k: d[k] for k in EDITABLE}
        out["web_password"] = "********" if holder.cfg.web_password else ""
        return jsonify(config=out, backends=sources.BACKENDS, default_backend=sources.default_backend(),
                       ffmpeg=sources.ffmpeg_version(holder.cfg.ffmpeg_path))

    @app.post("/api/config")
    def set_config():
        data = request.get_json(silent=True) or {}
        cfg = holder.cfg
        for k, v in data.items():
            if k not in EDITABLE:
                continue
            if k == "web_password" and v == "********":
                continue
            cur = getattr(cfg, k)
            try:
                if isinstance(cur, bool):
                    v = bool(v)
                elif isinstance(cur, int):
                    v = int(float(v))
                elif isinstance(cur, float):
                    v = float(v)
                else:
                    v = str(v).strip()
            except (TypeError, ValueError):
                return err(f"bad value for {k}")
            setattr(cfg, k, v)
        cfg.save(holder.cfg_path)
        was = holder.engine.running
        threading.Thread(target=holder.restart, args=(was or cfg.autostart,), daemon=True).start()
        return jsonify(ok=True)

    @app.get("/api/devices")
    def devices():
        backend = request.args.get("backend") or sources.default_backend()
        return jsonify(devices=[{"value": v, "label": l} for v, l in sources.list_devices(backend, holder.cfg.ffmpeg_path)])

    return app


def main() -> None:
    ap = argparse.ArgumentParser(prog="airlog-server", description="Airlog headless recorder with web UI")
    ap.add_argument("--config", type=Path, default=None, help="config file (default: per-user config)")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    sources.HEADLESS = True
    cfg_path = args.config or Config.path()
    cfg = Config.load(cfg_path)
    if not cfg_path.exists():
        cfg.save(cfg_path)
    host = args.host or cfg.web_host
    port = args.port or cfg.web_port

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    holder = Holder(cfg, cfg_path)
    if cfg.autostart:
        holder.engine.start()
    app = create_app(holder)
    print(f"Airlog {__version__} — open http://{host if host != '0.0.0.0' else '<this-pi>'}:{port}/", flush=True)
    try:
        from waitress import serve        # preferred if installed
        serve(app, host=host, port=port, threads=12, channel_timeout=600)
    except ImportError:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    finally:
        holder.engine.stop()


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
