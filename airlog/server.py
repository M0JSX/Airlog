"""Airlog server: headless recorder with a browser UI.

    python3 -m airlog.server [--config /path/config.json] [--host 0.0.0.0] [--port 8080]

Runs the capture engine (same as the desktop app) and serves a web UI on the
LAN. Clients see the live waveform, select audio and download it straight to
their own machine for editing, or save grabs on the server.
"""
from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import hashlib
import hmac
import secrets
import logging
import os
import re
import threading
import time
from dataclasses import asdict
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory, stream_with_context

from . import __version__, discovery, outputs, sources
from . import lifecycle
from .config import Config
from .engine import Engine

WEB_DIR = Path(__file__).with_name("web")
AUDIO_EXT = {".mp3", ".flac", ".wav", ".ogg"}

# Settings the browser may edit (everything else stays file-only). ffmpeg_path is deliberately
# file-only: whoever can set it can run any program on the recorder.
EDITABLE = [
    "station_name", "source_type", "stream_url", "device_backend", "device_name", "custom_input_args",
    "source_delay_s", "sample_rate", "channels",
    "archive_enabled", "archive_dir", "archive_pattern", "archive_format", "archive_bitrate_kbps",
    "split_minutes", "archive_keep_days",
    "buffer_minutes", "spool_dir", "grabs_dir", "grab_format", "grab_pattern", "grab_preroll_s",
    "silence_alert_db", "silence_alert_s", "autostart", "web_password",
    "identify_songs", "identify_interval_s", "announce", "trusted_lan",
    "receive_protocol", "receive_port", "receive_latency_ms", "receive_passphrase", "receive_password",
    "receive_group", "receive_sdp", "outputs",
    "backup_enabled", "backup_source_type", "backup_stream_url", "backup_device_backend", "backup_device_name",
    "backup_custom_input_args", "backup_source_delay_s", "backup_receive_protocol", "backup_receive_port",
    "backup_receive_latency_ms", "backup_receive_passphrase", "backup_receive_password", "backup_receive_group",
    "backup_receive_sdp", "failover_after_s", "failover_silence_s", "failover_return_s",
]
SECRET_FIELDS = ("web_password", "receive_password", "receive_passphrase",
                 "backup_receive_password", "backup_receive_passphrase")   # never sent back to browsers

SESSION_COOKIE = "airlog_session"
SESSION_DAYS = 30
LOGIN_TOKEN_S = 60


class Holder:
    """Owns the engine so it can be swapped when settings change."""

    def __init__(self, cfg: Config, cfg_path: Path):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.lock = threading.Lock()
        self.engine = self._new_engine()
        self.started = time.time()
        self.advertiser: discovery.Advertiser | None = None

    def start_announcing(self) -> None:
        """mDNS announcement for Airlog Viewer (only for a real server, not tests/self-test)."""
        self.advertiser = discovery.Advertiser(log=lambda m: self.engine.log(m))   # whichever engine is current
        self.announce()

    def announce(self) -> None:
        if self.advertiser is not None:
            c = self.cfg
            self.advertiser.update(c.station_name, c.web_port, __version__, bool(c.web_password), c.announce)

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
        self.announce()             # station name / password / on-off may have changed


def tz_offset(t: float) -> float:
    off = dt.datetime.fromtimestamp(t).astimezone().utcoffset()
    return off.total_seconds() if off else 0.0


ADMIN_REFUSED = ("Changing settings or starting/stopping the recorder from another machine needs a web "
                 "password, or \"Trusted network\" turned on. Change that in Settings on the recorder itself "
                 "(http://localhost:<port>/).")


def is_local_request() -> bool:
    """A browser on the recorder itself, not via a reverse proxy (which would look local too)."""
    if request.headers.get("X-Forwarded-For") or request.headers.get("Forwarded") or request.headers.get("X-Real-IP"):
        return False
    return request.remote_addr in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


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
    login_tokens: dict[str, float] = {}          # one-time token -> expiry (monotonic)

    def session_key(pw: str) -> bytes:
        # Changing the password invalidates every session cookie.
        return hmac.new(pw.encode(), b"airlog-session-v1", hashlib.sha256).digest()

    def make_session(pw: str) -> str:
        exp = str(int(time.time()) + SESSION_DAYS * 86400)
        return exp + "." + hmac.new(session_key(pw), exp.encode(), hashlib.sha256).hexdigest()

    def valid_session(pw: str, value: str) -> bool:
        exp, _, sig = (value or "").partition(".")
        if not exp.isdigit() or int(exp) < time.time():
            return False
        return hmac.compare_digest(sig, hmac.new(session_key(pw), exp.encode(), hashlib.sha256).hexdigest())

    @app.before_request
    def check_auth():
        pw = holder.cfg.web_password
        if not pw:
            return None
        if request.path == "/login" or (request.path == "/api/login" and request.method == "POST"):
            return None
        if valid_session(pw, request.cookies.get(SESSION_COOKIE, "")):
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

    def can_admin() -> bool:
        """Settings and record control: anyone who passed the password check; with no password set,
        a browser on the recorder itself, or anyone if "trusted network" is on. Grabbing and listening
        are always open to whoever can reach the UI."""
        return bool(holder.cfg.web_password) or holder.cfg.trusted_lan or is_local_request()

    def audio_file(root: Path, rel: str) -> Path:
        """Only audio files are served or deleted, whatever the folder settings point at."""
        p = safe_child(root, rel)
        if p.suffix.lower() not in AUDIO_EXT or not p.is_file():
            abort(404)
        return p

    def ftime(name: str) -> float:
        v = request.values.get(name)
        if v is None:
            abort(err(f"missing {name}"))
        try:
            return float(v)
        except ValueError:
            abort(err(f"bad {name}"))

    # ---------------------------------------------------------------- one-time login (Airlog Viewer)
    @app.post("/api/login")
    def api_login():
        """Swap the password for a single-use link token, so the viewer can open the UI signed in
        without putting the password in a URL."""
        pw = holder.cfg.web_password
        if not pw:
            return jsonify(token=None, password_required=False)
        given = str((request.get_json(silent=True) or {}).get("password", ""))
        if not hmac.compare_digest(given.encode(), pw.encode()):
            time.sleep(1)                        # slow down guessing
            return err("wrong password", 403)
        now = time.monotonic()
        for t, exp in list(login_tokens.items()):
            if exp < now:
                login_tokens.pop(t, None)
        token = secrets.token_urlsafe(24)
        login_tokens[token] = now + LOGIN_TOKEN_S
        return jsonify(token=token, password_required=True)

    @app.get("/login")
    def login_link():
        pw = holder.cfg.web_password
        exp = login_tokens.pop(request.args.get("t", ""), 0)
        if pw and exp < time.monotonic():
            return Response("<!doctype html><meta charset=utf-8><title>Airlog</title>"
                            "<p style='font:15px system-ui;margin:2em'>This sign-in link has expired or was "
                            "already used. Open the recorder again from Airlog Viewer, or "
                            "<a href='/'>sign in with the password</a>.", 403, mimetype="text/html")
        resp = Response(status=303, headers={"Location": "/", "Cache-Control": "no-store"})
        if pw:
            resp.set_cookie(SESSION_COOKIE, make_session(pw), max_age=SESSION_DAYS * 86400,
                            httponly=True, samesite="Strict", path="/")
        return resp

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
            can_edit=can_admin(),
            outputs=e.outputs.status(),
            failover=e.failover_status(),
            receive={"protocol": cfg.receive_protocol, "port": cfg.receive_port, "sender": e.sender,
                     "since": e.sender_since} if cfg.source_type == "receive" else None,
        )

    @app.get("/api/tracks")
    def tracks():
        e = holder.engine
        return jsonify(tracks=e.tracks.recent(int(request.args.get("limit", 200)), since=e.buffer_start()))

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

    @app.get("/api/listen")
    def listen():
        """MP3 from `from` (default: just behind live) onwards, following the recording as it grows."""
        e = holder.engine
        lo, hi = e.buffer_start(), e.live_time()
        if not e.running or lo is None or hi is None:
            return err("Nothing is being recorded", 416)
        t1 = request.args.get("from", type=float)
        t1 = hi - 0.5 if t1 is None else min(max(t1, lo), hi)
        kbps = min(max(request.args.get("kbps", 128, type=int), 32), 320)
        return Response(stream_with_context(e.iter_follow(t1, kbps)), mimetype="audio/mpeg",
                        headers={"Cache-Control": "no-store", "X-Airlog-From": f"{t1:.3f}"},
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
        p = audio_file(Path(holder.cfg.grabs_dir), rel)
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
        p = audio_file(Path(holder.cfg.grabs_dir), rel)
        return send_file(p, as_attachment=bool(request.args.get("download")), download_name=p.name,
                         conditional=True)

    @app.get("/files/archive/<path:rel>")
    def archive_file(rel):
        p = audio_file(Path(holder.cfg.archive_dir), rel)
        return send_file(p, as_attachment=bool(request.args.get("download")), download_name=p.name,
                         conditional=True)

    # ---------------------------------------------------------------- control & settings
    @app.post("/api/record")
    def record():
        if not can_admin():
            return err(ADMIN_REFUSED, 403)
        action = (request.get_json(silent=True) or {}).get("action")
        e = holder.engine
        if action == "start":
            e.start()
        elif action == "stop":
            threading.Thread(target=e.stop, daemon=True).start()
        else:
            return err("action must be start or stop")
        return jsonify(ok=True)

    @app.post("/api/outputs/<oid>")
    def output_switch(oid: str):
        """Turn one output on or off from the main page: saved to the config, applied straight away,
        and the recording isn't restarted."""
        if not can_admin():
            return err(ADMIN_REFUSED, 403)
        body = request.get_json(silent=True) or {}
        if not isinstance(body.get("enabled"), bool):
            return err("enabled must be true or false")
        on = body["enabled"]
        with holder.lock:                            # not while Settings is restarting the recorder
            cfg = holder.cfg
            specs = copy.deepcopy(cfg.outputs or [])
            spec = next((o for o in specs if o.get("id") == oid), None)
            if spec is None:
                return err("no such output", 404)
            spec["enabled"] = on
            try:
                specs = outputs.normalize_outputs(specs, previous=cfg.outputs)
            except ValueError as ex:                 # e.g. switching on an output that has no address yet
                return err(str(ex))
            cfg.outputs = specs
            cfg.save(holder.cfg_path)
            try:
                holder.engine.outputs.set_enabled(oid, on)
            except KeyError:                         # the running set predates it: next restart picks it up
                pass
        return jsonify(ok=True, enabled=on)

    @app.get("/api/config")
    def get_config():
        d = asdict(holder.cfg)
        out = {k: d[k] for k in EDITABLE}
        for k in SECRET_FIELDS:
            out[k] = "********" if d[k] else ""
        out["outputs"] = outputs.mask_outputs(d["outputs"])
        kinds = {k: {"label": outputs.KIND_LABELS[k], "codecs": outputs.CODECS[k], "port": outputs.DEFAULT_PORT[k]}
                 for k in outputs.KINDS}
        return jsonify(config=out, can_edit=can_admin(), output_kinds=kinds, bitrates=outputs.BITRATES, backends=sources.BACKENDS, default_backend=sources.default_backend(),
                       ffmpeg=sources.ffmpeg_version(holder.cfg.ffmpeg_path))

    @app.post("/api/config")
    def set_config():
        if not can_admin():
            return err(ADMIN_REFUSED, 403)
        data = request.get_json(silent=True) or {}
        cfg = holder.cfg
        new = {}
        for k, v in data.items():
            if k not in EDITABLE:
                continue
            if k in SECRET_FIELDS and v == "********":
                continue
            if k == "outputs":
                try:
                    new[k] = outputs.normalize_outputs(v, previous=cfg.outputs)
                except ValueError as ex:
                    return err(str(ex))
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
            new[k] = v
        # Check the combination before applying anything, so a bad save changes nothing.
        trial = copy.copy(cfg)
        for k, v in new.items():
            setattr(trial, k, v)
        if trial.source_type == "receive":
            try:
                sources.receive_args(trial)
            except ValueError as ex:
                return err(str(ex))
        if trial.backup_enabled:
            b = trial.backup_config()
            try:
                if b.source_type == "receive":
                    sources.receive_args(b)
                elif b.source_type == "stream" and not b.stream_url.strip():
                    raise ValueError("enter the stream URL")
                elif b.source_type == "custom" and "-i" not in b.custom_input_args:
                    raise ValueError("custom input arguments must contain -i")
            except ValueError as ex:
                return err(f"Backup source: {ex}")
            if (trial.source_type == "receive" and b.source_type == "receive"
                    and int(trial.receive_port) == int(b.receive_port)):
                return err("The main and backup sources can't receive on the same port")
        for k, v in new.items():
            setattr(cfg, k, v)
        cfg.save(holder.cfg_path)
        was = holder.engine.running
        threading.Thread(target=holder.restart, args=(was or cfg.autostart,), daemon=True).start()
        return jsonify(ok=True)

    @app.get("/api/output_devices")
    def output_devices():
        devs, error = outputs.list_output_devices()
        return jsonify(devices=devs, error=error)

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
    import signal

    def on_signal(*_):
        lifecycle.begin_shutdown()
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)
    holder = Holder(cfg, cfg_path)
    if cfg.autostart:
        holder.engine.start()
    holder.start_announcing()
    app = create_app(holder)
    print(f"Airlog {__version__} — open http://{host if host != '0.0.0.0' else '<this-machine>'}:{port}/", flush=True)
    try:
        from waitress import serve        # preferred if installed
        serve(app, host=host, port=port, threads=12, channel_timeout=600)
    except ImportError:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        lifecycle.begin_shutdown()
        if holder.advertiser:
            holder.advertiser.close()
        holder.engine.stop()


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
