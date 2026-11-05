"""One audio source being captured: an ffmpeg process decoding it to PCM, kept running.

The engine runs one Capture for the main source and, if a backup source is set, a second one
alongside it (hot standby), so failing over is instant and the backup's last seconds can fill
the hole the main source left. Each Capture hands its PCM blocks to Engine._ingest().
"""
from __future__ import annotations

import collections
import subprocess
import threading
import time
from array import array
from typing import TYPE_CHECKING

from . import lifecycle, sources
from .config import Config

if TYPE_CHECKING:
    from .engine import Engine

NO_WINDOW = sources.NO_WINDOW
WATCHDOG_S = 20              # no data for this long -> restart the source
RING_S = 30                  # recent audio kept per source, for filling the gap at a switch


class Capture:
    def __init__(self, engine: "Engine", cfg: Config, role: str):
        self.engine = engine
        self.cfg = cfg
        self.role = role                      # "main" | "backup"
        self.prefix = "" if role == "main" else "Backup source: "
        self.status = "stopped"               # stopped | starting | recording | reconnecting | waiting | error
        self.delay = float(cfg.source_delay_s or 0)
        self.receiving = cfg.source_type == "receive"
        self.proc: subprocess.Popen | None = None
        self.push = None                      # receive.PushServer for an Icecast-style push input
        self.sender = ""
        self.sender_since = 0.0
        self.last_data = 0.0                  # monotonic time of the last PCM block
        self.ever_recorded = False
        self.silent_since: float | None = None  # monotonic; None while there's sound
        self.ring: collections.deque = collections.deque(maxlen=RING_S * engine.block_rate)
        self.running = False
        self.thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle
    def log(self, msg: str, error: bool = False) -> None:
        self.engine.log(self.prefix + msg, error=error)

    def describe(self) -> str:
        return sources.describe(self.cfg)

    def start(self) -> None:
        self.running = True
        self.status = "starting"
        self.thread = threading.Thread(target=self._supervise, name=f"capture-{self.role}", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.running = False
        if self.push is not None:
            self.push.close()
            self.push = None
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
        if self.thread:
            self.thread.join(timeout=5)
        self.status = "stopped"

    # ------------------------------------------------------------------ health (for failover)
    def fresh(self, within: float) -> bool:
        return self.status == "recording" and time.monotonic() - self.last_data < within

    def silent_for(self) -> float:
        return time.monotonic() - self.silent_since if self.silent_since is not None else 0.0

    def _track_level(self, pcm: bytes) -> None:
        a = array("h")
        a.frombytes(pcm)
        peak = (max(max(a), -min(a)) / 32768) if a else 0.0
        if peak < 10 ** (self.engine.cfg.silence_alert_db / 20):
            if self.silent_since is None:
                self.silent_since = time.monotonic()
        else:
            self.silent_since = None

    # ------------------------------------------------------------------ capture loop
    def _cmd(self) -> list[str]:
        e = self.engine
        return [self.cfg.ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "warning",
                *sources.input_args(self.cfg),
                "-vn", "-sn", "-dn", "-ac", str(e.ch), "-ar", str(e.sr), "-f", "s16le", "pipe:1"]

    def _sleep(self, s: float) -> None:
        end = time.monotonic() + s
        while self.running and time.monotonic() < end:
            time.sleep(0.2)

    def _supervise(self) -> None:
        backoff = 2.0
        announced_wait = False
        while self.running:
            try:
                cmd = self._cmd()
            except ValueError as e:
                self.status = "error"
                self.log(str(e), error=True)
                self._sleep(5)
                continue
            sender = None
            if self.receiving:
                if not announced_wait:
                    self.log("Waiting for a sender: " + self.describe())
                    announced_wait = True
                self.status = "waiting"
                if sources.is_push(self.cfg):
                    sender = self._wait_for_push()
                    if sender is None:
                        continue
                    hint = sender.format_hint
                    if hint:
                        i = cmd.index("pipe:0")
                        cmd[i - 1:i - 1] = ["-f", hint]
            else:
                self.log("Source: " + self.describe())
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             stdin=subprocess.PIPE if sender else subprocess.DEVNULL,
                                             creationflags=NO_WINDOW)
            except OSError as e:
                self.status = "error"
                self.log(f"Could not run ffmpeg ({self.cfg.ffmpeg_path}): {e}", error=True)
                if sender:
                    sender.close()
                self._sleep(10)
                continue
            threading.Thread(target=self._pump_stderr, args=(self.proc,), daemon=True).start()
            if sender:
                threading.Thread(target=self._feed_push, args=(sender, self.proc), daemon=True).start()
            self.last_data = time.monotonic()
            got_any = self._read_loop(self.proc)
            if self.proc.poll() is None:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            if sender:
                sender.close()
                if self.push:
                    self.push.busy.clear()
                self.sender = ""
            if not self.running or lifecycle.ended_by_shutdown():
                break
            if self.receiving:
                # A sender going away is normal for an input: wait quietly for it to come back.
                if got_any:
                    self.log("Sender stopped; waiting for it to come back")
                    announced_wait = True
                self.status = "waiting"
                self._sleep(1)
                continue
            self.status = "reconnecting"
            self.log(f"Source ended (ffmpeg exit {self.proc.returncode}); retrying in {backoff:.0f}s", error=True)
            self._sleep(backoff)
            backoff = 2.0 if got_any else min(backoff * 2, 30.0)
        self.status = "stopped"

    def _wait_for_push(self):
        from .receive import PushServer
        if self.push is None:
            try:
                self.push = PushServer(int(self.cfg.receive_port), self.cfg.receive_password, self.log)
            except OSError as e:
                self.status = "error"
                self.log(f"Stream input: {e}", error=True)
                self._sleep(10)
                return None
        while self.running:
            s = self.push.next_sender(timeout=0.5)
            if s is not None:
                self.push.busy.set()
                self.sender, self.sender_since = s.peer, time.time()
                self.push.current_peer = s.peer
                self.log(f"Stream input: receiving from {s.peer}" + (f" ({s.content_type})" if s.content_type else ""))
                return s
        return None

    def _feed_push(self, sender, proc: subprocess.Popen) -> None:
        try:
            for data in sender.read_body():
                if not self.running:
                    break
                proc.stdin.write(data)
                proc.stdin.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    def _pump_stderr(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode(errors="replace").strip()
            if line and not lifecycle.SHUTTING_DOWN.is_set():     # ffmpeg reporting its own stop: noise
                self.log("ffmpeg: " + line[:300])

    def _read_loop(self, proc: subprocess.Popen) -> bool:
        e = self.engine
        need = e.block_frames * e.bpf
        got_any = False
        out = proc.stdout
        while self.running:
            data = out.read(need)
            if not data:
                break
            if len(data) % e.bpf:
                data = data[: len(data) - len(data) % e.bpf]
                if not data:
                    continue
            self.last_data = time.monotonic()
            if not got_any:
                got_any = True
                self.ever_recorded = True
                if self.status != "recording":
                    self.log("Recording" if self.role == "main" else "Receiving audio (standing by)")
                self.status = "recording"
                e._source_started(self)
            self._track_level(data)
            e._ingest(self, data)
        return got_any

    def watchdog_tick(self) -> None:
        p = self.proc
        if (p and p.poll() is None and self.status in ("recording", "starting", "reconnecting")
                and time.monotonic() - self.last_data > WATCHDOG_S):
            self.log(f"No audio for {WATCHDOG_S}s; restarting source", error=True)
            self.last_data = time.monotonic()
            try:
                p.kill()
            except Exception:
                pass
