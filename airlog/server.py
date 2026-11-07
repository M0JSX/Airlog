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

from . import __version__, altfeed, discovery, outputs, sources
from . import lifecycle
from .config import ALT_MODES, Config
from .engine import Engine

WEB_DIR = Path(__file__).with_name("web")
AUDIO_EXT = {".mp3", ".flac", ".wav", ".ogg"}

# Settings the browser may edit (everything else stays file-only). ffmpeg_path is deliberately
# file-only: whoever can set it can run any program on the recorder.
EDITABLE = [
    "files_dir",
    "alt_enabled", "alt_mode", "alt_title", "alt_schedule", "alt_source_type", "alt_stream_url",
    "alt_device_backend", "alt_device_name", "alt_custom_input_args", "alt_source_delay_s",
    "alt_receive_protocol", "alt_receive_port", "alt_receive_latency_ms", "alt_receive_passphrase",
    "alt_receive_password", "alt_receive_group", "alt_receive_sdp", "alt_files_dir",
    "station_name", "source_type", "stream_url", "device_backend", "device_name", "custom_input_args",
    "source_delay_s", "sample_rate", "channels",
    "archive_enabled", "archive_dir", "archive_pattern", "archive_format", "archive_bitrate_kbps",
    "split_minutes", "archive_keep_days",
    "buffer_minutes", "spool_dir", "grabs_dir", "grab_format", "grab_pattern", "grab_preroll_s",
    "silence_alert_db", "silence_alert_s", "autostart", "web_password",
    "identify_songs", "identify_interval_s", "announce", "trusted_lan", "switch_password", "switch_port", "switch_note",
    "receive_protocol", "receive_port", "receive_latency_ms", "receive_passphrase", "receive_password",
    "receive_group", "receive_sdp", "outputs",
    "backup_enabled", "backup_source_type", "backup_stream_url", "backup_device_backend", "backup_device_name",
    "backup_custom_input_args", "backup_source_delay_s", "backup_receive_protocol", "backup_receive_port",
    "backup_receive_latency_ms", "backup_receive_passphrase", "backup_receive_password", "backup_receive_group",
    "backup_receive_sdp", "failover_after_s", "failover_silence_s", "failover_return_s",
]
SECRET_FIELDS = ("web_password", "switch_password", "receive_password", "receive_passphrase",
                 "backup_receive_password", "backup_receive_passphrase",
                 "alt_receive_password", "alt_receive_passphrase")   # never sent back to browsers

SESSION_COOKIE = "airlog_session"
SESSION_DAYS = 30
LOGIN_TOKEN_S = 60

# The /switch page: only switches the internet outputs between the programme and the alternative input,
# with its own password, so it can be given to commentators and reached from outside.
SWITCH_COOKIE = "airlog_switch"
SWITCH_PATHS = ("/switch", "/api/switch", "/api/switch/login", "/api/switch/logout")
SWITCH_MIN_PASSWORD = 8
SWITCH_MAX_FAILS = 5                 # wrong passwords from one address...
SWITCH_FAIL_WINDOW_S = 15 * 60       # ...within this long lock that address out for the rest of it


class Holder:
    """Owns the engine so it can be swapped when settings change."""

    def __init__(self, cfg: Config, cfg_path: Path):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.lock = threading.Lock()
        self.engine = self._new_engine()
        self.started = time.time()
        self.advertiser: discovery.Advertiser | None = None
        self.switch_fails: dict[str, list[float]] = {}     # switch page: address -> wrong-password times
        self.switch_listener: SwitchListener | None = None

    def serve_switch_page(self) -> None:
        """Real servers only: the switch page's own port, if one is set (see SwitchListener)."""
        self.switch_listener = SwitchListener(self)
        self.switch_listener.apply()

    def apply_switch_port(self) -> None:
        if self.switch_listener is not None:
            self.switch_listener.apply()

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


class SwitchListener:
    """The switch page on a port of its own, serving nothing else, so only that port need be opened to
    the internet. On while a switch password and port are set; follows settings changes without a
    restart."""

    def __init__(self, holder: "Holder"):
        self.holder = holder
        self.srv = None
        self.thread: threading.Thread | None = None
        self.port = 0
        self.error = ""

    def wanted(self) -> int:
        c = self.holder.cfg
        return int(c.switch_port or 0) if c.switch_password else 0

    def apply(self) -> None:
        want = self.wanted()
        if want == self.port and (want == 0 or self.srv is not None):
            return
        self.stop()
        self.error = ""
        if not want:
            return
        log = self.holder.engine.log
        try:
            from waitress import create_server
            srv = create_server(create_switch_app(self.holder), host=self.holder.cfg.web_host, port=want,
                                threads=4, channel_timeout=60, connection_limit=50)
        except OSError as ex:
            self.port, self.error = want, f"can't listen on port {want} ({ex.strerror or ex})"
            log(f"Switch page: {self.error}", error=True)
            return
        self.srv, self.port = srv, want
        self.thread = threading.Thread(target=self._run, args=(srv,), daemon=True, name=f"switch-{want}")
        self.thread.start()
        log(f"Switch page: listening on port {want} (it serves only the switch page)")

    @staticmethod
    def _run(srv) -> None:
        try:
            srv.run()
        except Exception:                        # closed under it while stopping
            pass

    def stop(self) -> None:
        srv, self.srv, port, self.port = self.srv, None, self.port, 0
        if srv is None:
            return
        trig = getattr(srv, "trigger", None)
        if trig is not None and hasattr(trig, "pull_trigger"):
            # waitress: close its sockets from inside its own loop (closing them from here, under a
            # running select(), is what logs "Bad file descriptor"); the loop ends once nothing's left.
            smap = getattr(srv, "_map", {})

            def close_all():
                for obj in list(smap.values()):
                    try:
                        obj.close()
                    except Exception:
                        pass
            trig.pull_trigger(close_all)
        else:
            srv.close()
        if self.thread is not None:
            self.thread.join(5)
        td = getattr(srv, "task_dispatcher", None)
        if td is not None:
            try:
                td.shutdown(cancel_pending=True, timeout=2)
            except Exception:
                pass
        self.holder.engine.log(f"Switch page: stopped listening on port {port}")


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


def session_key(pw: str, label: bytes = b"airlog-session-v1") -> bytes:
    # Changing the password invalidates every session cookie.
    return hmac.new(pw.encode(), label, hashlib.sha256).digest()

def make_session(pw: str, label: bytes = b"airlog-session-v1") -> str:
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    return exp + "." + hmac.new(session_key(pw, label), exp.encode(), hashlib.sha256).hexdigest()

def valid_session(pw: str, value: str, label: bytes = b"airlog-session-v1") -> bool:
    exp, _, sig = (value or "").partition(".")
    if not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(sig, hmac.new(session_key(pw, label), exp.encode(), hashlib.sha256).hexdigest())


def _err(msg: str, code: int = 400):
    return jsonify(error=msg), code


def add_switch_routes(app: Flask, holder: "Holder", fallback=lambda: False) -> None:
    """The switch page's routes. On the main port, fallback is the main page's can_admin (so the
    recorder itself, or a trusted network, can use it without a password); on the switch page's own
    port there's no fallback: its password, or a main-page sign-in, only."""
    err = _err

    def switch_allowed() -> bool:
        """The switch page's own sign-in, or anyone allowed to use the main page's switch."""
        cfg = holder.cfg
        if cfg.switch_password and valid_session(cfg.switch_password, request.cookies.get(SWITCH_COOKIE, ""),
                                                 b"airlog-switch-v1"):
            return True
        if cfg.web_password:
            return valid_session(cfg.web_password, request.cookies.get(SESSION_COOKIE, ""))
        return fallback()

    def switch_view() -> dict:
        cfg = holder.cfg
        return {"station": cfg.station_name, "signed_in": True, "alt": holder.engine.alt_status(),
                "note": cfg.switch_note}

    def apply_alt_state(state: str, who: str):
        """Shared by the main page's button and the switch page. Returns an error response or None."""
        cfg = holder.cfg
        if not cfg.alt_enabled:
            return err("The alternative input isn't enabled (Settings)")
        state = "" if state == "auto" else state
        if state not in altfeed.OVERRIDES:
            return err("state must be alternative, programme or auto")
        if state == "" and cfg.alt_mode != "schedule":
            return err("'Follow the schedule' needs schedule mode")
        if cfg.alt_mode == "permanent":
            return err("In permanent mode the switched outputs always carry the alternative")
        with holder.lock:
            if who:
                holder.engine.log(f"{who}: internet outputs set to "
                                  + {"alternative": "the alternative input", "programme": "the programme",
                                     "": "follow the schedule"}[state])
            holder.engine.set_alt_override(state)
            cfg.save(holder.cfg_path)
        return None

    app.extensions["airlog_apply_alt"] = apply_alt_state

    @app.get("/switch")
    def switch_page():
        resp = send_from_directory(WEB_DIR, "switch.html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/api/switch")
    def switch_status():
        if not switch_allowed():
            return jsonify(signed_in=False, available=bool(holder.cfg.switch_password)), 401
        return jsonify(switch_view())

    @app.post("/api/switch/login")
    def switch_login():
        cfg = holder.cfg
        ip = request.remote_addr or "?"
        if not cfg.switch_password:
            return err("The switch page isn't turned on. Set a switch page password in Airlog's settings", 403)
        now = time.monotonic()
        fails = [t for t in holder.switch_fails.get(ip, []) if now - t < SWITCH_FAIL_WINDOW_S]
        holder.switch_fails[ip] = fails
        if len(fails) >= SWITCH_MAX_FAILS:
            mins = max(1, round((SWITCH_FAIL_WINDOW_S - (now - fails[0])) / 60))
            return err(f"Too many wrong passwords. Try again in {mins} minute{'s' if mins > 1 else ''}", 429)
        given = str((request.get_json(silent=True) or {}).get("password", ""))
        if not hmac.compare_digest(given.encode(), cfg.switch_password.encode()):
            fails.append(now)
            if len(fails) >= SWITCH_MAX_FAILS:
                holder.engine.log(f"Switch page: {len(fails)} wrong passwords from {ip}; "
                                  f"locked out for {SWITCH_FAIL_WINDOW_S // 60} minutes", error=True)
            time.sleep(1)                        # slow down guessing
            return err("Wrong password", 403)
        holder.switch_fails.pop(ip, None)
        resp = jsonify(switch_view())
        resp.set_cookie(SWITCH_COOKIE, make_session(cfg.switch_password, b"airlog-switch-v1"),
                        max_age=SESSION_DAYS * 86400, httponly=True, samesite="Strict", path="/",
                        secure=request.is_secure)
        return resp

    @app.post("/api/switch/logout")
    def switch_logout():
        resp = jsonify(ok=True)
        resp.delete_cookie(SWITCH_COOKIE, path="/")
        return resp

    @app.post("/api/switch")
    def switch_set():
        if not switch_allowed():
            return err("Signed out: enter the switch page password again", 401)
        bad = apply_alt_state(str((request.get_json(silent=True) or {}).get("state", "")),
                              f"Switch page ({request.remote_addr})")
        return bad or jsonify(switch_view())


def create_switch_app(holder: Holder) -> Flask:
    """The switch page's own port: the page, its API and the icon. Nothing else answers."""
    app = Flask(__name__, static_folder=None)
    add_switch_routes(app, holder)

    @app.get("/")
    def root():
        return Response(status=302, headers={"Location": "/switch"})

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(Path(__file__).with_name("assets"), "icon.png", mimetype="image/png")

    @app.after_request
    def headers(resp):
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp
    return app


def create_app(holder: Holder) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False

    # ---------------------------------------------------------------- auth
    login_tokens: dict[str, float] = {}          # one-time token -> expiry (monotonic)

    @app.before_request
    def check_auth():
        pw = holder.cfg.web_password
        if not pw:
            return None
        if request.path == "/login" or (request.path == "/api/login" and request.method == "POST"):
            return None
        if request.path in SWITCH_PATHS:
            return None                          # the switch page checks its own password
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

    add_switch_routes(app, holder, fallback=can_admin)

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
            alt=e.alt_status(),
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
            if k == "alt_schedule":
                try:
                    new[k] = altfeed.normalize_schedule(v)
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
        if trial.switch_password:
            if len(trial.switch_password) < SWITCH_MIN_PASSWORD:
                return err(f"The switch page password needs at least {SWITCH_MIN_PASSWORD} characters: "
                           "it can be tried from the internet")
            if trial.switch_password == trial.web_password:
                return err("The switch page password must be different from the web password")
        if len(trial.switch_note) > 600:
            return err("The switch page note is too long (600 characters at most)")
        sp = int(trial.switch_port or 0)
        if sp:
            if not 1024 <= sp <= 65535:
                return err("The switch page port must be 1024-65535, or 0 for none")
            if sp == int(trial.web_port):
                return err(f"The switch page port can't be the main page's port ({sp})")
            taken = [("main source", trial.source_type, trial.receive_port)]
            if trial.backup_enabled:
                taken.append(("backup source", trial.backup_source_type, trial.backup_receive_port))
            if trial.alt_enabled:
                taken.append(("alternative input", trial.alt_source_type, trial.alt_receive_port))
            for name, t, port in taken:
                if t == "receive" and int(port) == sp:
                    return err(f"The switch page port is the {name}'s receive port ({sp})")
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
        if trial.alt_enabled:
            if trial.alt_mode not in ALT_MODES:
                return err("Alternative input: unknown switching mode")
            a = trial.alt_config()
            try:
                if a.source_type == "receive":
                    sources.receive_args(a)
                elif a.source_type == "stream" and not a.stream_url.strip():
                    raise ValueError("enter the stream URL")
                elif a.source_type == "custom" and "-i" not in a.custom_input_args:
                    raise ValueError("custom input arguments must contain -i")
                elif a.source_type == "files" and not a.files_dir.strip():
                    raise ValueError("choose the folder of audio files")
            except ValueError as ex:
                return err(f"Alternative input: {ex}")
            ports = [("main", trial.source_type, trial.receive_port), ("alternative", a.source_type, a.receive_port)]
            if trial.backup_enabled:
                ports.append(("backup", trial.backup_source_type, trial.backup_receive_port))
            used = [(n, int(p)) for n, t, p in ports if t == "receive"]
            for i, (n1, p1) in enumerate(used):
                for n2, p2 in used[i + 1:]:
                    if p1 == p2:
                        return err(f"The {n1} and {n2} inputs can't receive on the same port ({p1})")
            if trial.alt_mode == "schedule" and not trial.alt_schedule:
                return err("Alternative input: add at least one scheduled time, or use the manual button")
        for k, v in new.items():
            setattr(cfg, k, v)
        cfg.save(holder.cfg_path)
        was = holder.engine.running

        def restart():
            holder.restart(was or cfg.autostart)
            holder.apply_switch_port()
        threading.Thread(target=restart, daemon=True).start()
        return jsonify(ok=True)

    @app.post("/api/alt")
    def alt_switch():
        """The main page's internet switch: {"state": "alternative" | "programme" | "auto"}. Saved,
        so a restart keeps it; applied at once, without restarting the recorder."""
        if not can_admin():
            return err(ADMIN_REFUSED, 403)
        bad = app.extensions["airlog_apply_alt"](str((request.get_json(silent=True) or {}).get("state", "")), "")
        return bad or jsonify(ok=True, alt=holder.engine.alt_status())

    # ---------------------------------------------------------------- folder browser (settings)
    def browse_roots() -> list[dict]:
        """Places to start: home, and the drives (Windows) or mount points (Linux/macOS)."""
        roots = [{"label": "Home", "path": str(Path.home())}]
        if os.name == "nt":
            import string
            for letter in string.ascii_uppercase:
                d = f"{letter}:\\"
                if os.path.exists(d):
                    roots.append({"label": f"{letter}:", "path": d})
        else:
            roots.append({"label": "/", "path": "/"})
            for base in ("/Volumes", "/media", "/mnt", "/run/media"):
                b = Path(base)
                try:
                    kids = [k for k in b.iterdir() if k.is_dir() and not k.name.startswith(".")] if b.is_dir() else []
                except OSError:
                    kids = []
                for k in sorted(kids)[:20]:
                    # /media/<user>/<disk> on Ubuntu: offer the disks, not the user folder
                    sub = []
                    if base in ("/media", "/run/media") and k.name == os.environ.get("USER", ""):
                        try:
                            sub = sorted(x for x in k.iterdir() if x.is_dir())
                        except OSError:
                            pass
                    for x in sub or [k]:
                        roots.append({"label": x.name, "path": str(x)})
        return roots

    SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", "lost+found"}

    @app.get("/api/browse")
    def browse():
        """Folders on the recorder, for picking a folder in Settings: sub-folders, plus the audio files
        in it (so a folder for the alternative input can be checked before choosing it)."""
        if not can_admin():
            return err(ADMIN_REFUSED, 403)
        raw = (request.args.get("path") or "").strip()
        p = Path(os.path.expanduser(raw)) if raw else Path.home()
        if not p.is_absolute():
            p = Path.home() / p
        # A folder that doesn't exist yet (typed in, or not created): start from the nearest one that does.
        while not p.is_dir() and p.parent != p:
            p = p.parent
        p = p.resolve()
        dirs, audio, others, error = [], [], 0, ""
        try:
            for e in os.scandir(p):
                name = e.name
                try:
                    if e.is_dir():
                        if not name.startswith(".") and name not in SKIP_DIRS:
                            dirs.append(name)
                    elif e.is_file():
                        if Path(name).suffix.lower() in sources.AUDIO_EXT:
                            audio.append(name)
                        elif not name.startswith("."):
                            others += 1
                except OSError:
                    continue
        except PermissionError:
            error = "Airlog isn't allowed to read this folder"
        except OSError as ex:
            error = f"Can't read this folder ({ex.strerror or ex})"
        key = str.casefold
        dirs.sort(key=key)
        audio.sort(key=key)
        parent = str(p.parent) if p.parent != p else None
        return jsonify(path=str(p), parent=parent, sep=os.sep, dirs=dirs[:2000], audio=audio[:500],
                       audio_count=len(audio), other_count=others, truncated=len(dirs) > 2000,
                       error=error, roots=browse_roots())

    @app.post("/api/browse/mkdir")
    def browse_mkdir():
        if not can_admin():
            return err(ADMIN_REFUSED, 403)
        data = request.get_json(silent=True) or {}
        base = Path(str(data.get("path", "")))
        name = str(data.get("name", "")).strip()
        if not base.is_absolute() or not base.is_dir():
            return err("That folder doesn't exist")
        if not name or name in (".", "..") or any(c in name for c in '/\\:*?"<>|') or len(name) > 120:
            return err("Folder names can't be blank or contain / \\ : * ? \" < > |")
        new = base / name
        try:
            new.mkdir()
        except FileExistsError:
            return err(f"There's already a folder called {name}")
        except OSError as ex:
            return err(f"Couldn't create it ({ex.strerror or ex})")
        return jsonify(ok=True, path=str(new))

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
    holder.serve_switch_page()
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
        if holder.switch_listener:
            holder.switch_listener.stop()
        holder.engine.stop()


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
