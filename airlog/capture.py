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
CARD_NODATA_S = 5            # a sound card that's open but sends nothing for this long is given up
QUIET_AFTER = 3              # failed attempts in a row before the log goes quiet...
QUIET_SUMMARY_S = 600        # ...apart from a summary this often, until it works again
NOT_CAUSES = ("Source ended", "failed ", "still not working")   # consequences, not the error to report
RING_S = 30                  # recent audio kept per source, for filling the gap at a switch


class Capture:
    def __init__(self, engine: "Engine", cfg: Config, role: str):
        self.engine = engine
        self.cfg = cfg
        self.role = role                      # "main" | "backup" | "alt"
        self.prefix = {"main": "", "backup": "Backup source: ", "alt": "Alternative input: "}.get(role, "")
        self.playing = ""                     # "files" source: the file being played
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
        self.feeder: threading.Thread | None = None   # sound card / files -> ffmpeg, for this attempt
        self.fails = 0                        # attempts in a row that produced no audio
        self.quiet = False                    # repeated failures: hold messages back (see log())
        self.held = 0
        self.last_error = ""
        self.summary_at = 0.0

    # ------------------------------------------------------------------ lifecycle
    def log(self, msg: str, error: bool = False, force: bool = False) -> None:
        """While a source keeps failing the same way, every attempt would log the same few lines (every
        30 s, for hours). After QUIET_AFTER failures in a row they're held back, with a summary every
        QUIET_SUMMARY_S, until audio arrives again."""
        if self.quiet and not force:
            self.held += 1
            if error and not msg.startswith(NOT_CAUSES):
                self.last_error = msg
            if time.monotonic() >= self.summary_at:
                self.summary_at = time.monotonic() + QUIET_SUMMARY_S
                self.engine.log(self.prefix + f"still not working: {self.fails} attempts so far, retrying every "
                                f"{int(self._backoff)}s (last error: {self.last_error or 'none recorded'})", error=True)
            return
        if error and not msg.startswith(NOT_CAUSES):
            self.last_error = msg
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
        self._wait_feeder()                       # the card closed before we say we've stopped
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

    _backoff = 2.0

    def _wait_feeder(self) -> None:
        """The previous attempt's sound card reader must have let go of the card before it's opened
        again (and before the process exits: PortAudio still running at exit can crash it)."""
        f = self.feeder
        if f is not None and f.is_alive():
            f.join(timeout=CARD_NODATA_S + 3)
            if f.is_alive():
                self.log("the sound card reader hasn't stopped; the card may stay busy", error=True)
        self.feeder = None

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
            feed = sources.feeder(self.cfg)       # audio Airlog produces itself: a sound card, or files
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             stdin=subprocess.PIPE if sender or feed else subprocess.DEVNULL,
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
            if feed:
                target = self._feed_card if feed == "card" else self._feed_files
                self.feeder = threading.Thread(target=target, args=(self.proc,), daemon=True,
                                               name=f"feed-{self.role}")
                self.feeder.start()
            self.last_data = time.monotonic()
            got_any = self._read_loop(self.proc)
            if self.proc.poll() is None:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self._wait_feeder()
            if got_any:
                self.fails = 0
            elif not self.receiving:
                self.fails += 1
                if self.fails == QUIET_AFTER and not self.quiet:
                    self.log(f"failed {self.fails} times in a row; logging this only every "
                             f"{QUIET_SUMMARY_S // 60} minutes until it works", error=True)
                    self.quiet, self.held = True, 0
                    self.summary_at = time.monotonic() + QUIET_SUMMARY_S
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
            self._backoff = backoff
            self._sleep(backoff)
            backoff = 2.0 if got_any else min(backoff * 2, 30.0)
        self._wait_feeder()
        self.status = "stopped"

    def _wait_for_push(self):
        from .receive import PushServer
        if not self.running:                     # stopped meanwhile: don't open a listener nobody closes
            return None
        if self.push is None:
            try:
                self.push = PushServer(int(self.cfg.receive_port), self.cfg.receive_password, self.log)
            except OSError as e:
                self.status = "error"
                self.log(f"Stream input: {e}", error=True)
                self._sleep(10)
                return None
            if not self.running:                 # stop() ran while it was being opened
                self.push.close()
                self.push = None
                return None
        while self.running:
            push = self.push
            if push is None:                     # stop() closed it
                return None
            s = push.next_sender(timeout=0.5)
            if s is not None:
                push.busy.set()
                self.sender, self.sender_since = s.peer, time.time()
                push.current_peer = s.peer
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

    def _feed_card(self, proc: subprocess.Popen) -> None:
        """Sound card -> ffmpeg's stdin through PortAudio (Linux ALSA; see sources.via_portaudio).
        If the card can't be opened or stops, ffmpeg sees the end of its input and the supervisor
        retries as for any source that ends."""
        cfg = self.cfg
        sr, ch = int(cfg.sample_rate), int(cfg.channels)
        block = sr // 10                                      # 100 ms reads
        try:
            from .outputs import OutputError, sounddevice_module
            try:
                sd = sounddevice_module()
            except OutputError as e:
                raise ValueError(str(e)) from None
            dev, label = sources.portaudio_input(sd, cfg.device_name)
            stream = sd.RawInputStream(samplerate=sr, channels=ch, dtype="int16", device=dev,
                                       latency="high", blocksize=block)
            try:
                stream.start()
                # Only read what's there: a blocking read on a card that opened but sends nothing never
                # returns, and that thread would hold the card (and PortAudio) for good.
                last = time.monotonic()
                while self.running and proc.poll() is None:
                    if stream.read_available >= block:
                        data, _overflowed = stream.read(block)
                        proc.stdin.write(bytes(data))
                        last = time.monotonic()
                    elif time.monotonic() - last > CARD_NODATA_S:
                        raise OSError(f"{label} opened, but no audio came from it for {CARD_NODATA_S}s")
                    else:
                        time.sleep(0.02)
            finally:
                for step in (stream.abort, stream.close):
                    try:
                        step()
                    except Exception:
                        pass
        except (BrokenPipeError, OSError) as e:
            if self.running and proc.poll() is None and not lifecycle.SHUTTING_DOWN.is_set():
                self.log(f"Sound card: {e}", error=True)
        except Exception as e:  # noqa: BLE001 - PortAudioError, not found, bad rate...
            if self.running and not lifecycle.SHUTTING_DOWN.is_set():
                self.log(f"Sound card: {e}", error=True)
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    def _feed_files(self, proc: subprocess.Popen) -> None:
        """A folder of audio files -> ffmpeg's stdin, in a loop, in real time. Each file is decoded on
        its own, so the folder can mix formats and sample rates. The folder is re-read on each pass,
        so files added or removed take effect next time round."""
        cfg = self.cfg
        sr, ch = int(cfg.sample_rate), int(cfg.channels)
        bpf = 2 * ch
        block = sr // 10 * bpf                                # 100 ms
        start = time.monotonic()
        sent = 0                                              # frames, for real-time pacing
        warned = False
        try:
            while self.running and proc.poll() is None:
                files = sources.audio_files(cfg.files_dir)
                if not files:
                    if not warned:
                        self.log(f"no audio files in {cfg.files_dir or '(no folder chosen)'}", error=True)
                        warned = True
                    return                                    # ends ffmpeg's input: retried with back-off
                warned = False
                pass_any = False
                for f in files:
                    if not self.running or proc.poll() is not None:
                        return
                    dec = subprocess.Popen([cfg.ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-i", str(f),
                                            "-vn", "-ac", str(ch), "-ar", str(sr), "-f", "s16le", "pipe:1"],
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
                    self.playing = f.stem
                    got_any = False
                    try:
                        while self.running and proc.poll() is None:
                            data = dec.stdout.read(block)
                            if not data:
                                break
                            got_any = True
                            proc.stdin.write(data)
                            sent += len(data) // bpf
                            ahead = start + sent / sr - time.monotonic()
                            if ahead > 0.2:                   # stay ~0.2 s ahead of real time
                                time.sleep(ahead - 0.2)
                            elif ahead < -2:                  # fell behind (a stall): don't race to catch up
                                start = time.monotonic() - sent / sr
                    finally:
                        if dec.poll() is None:
                            dec.kill()
                        dec.wait()
                    pass_any = pass_any or got_any
                    if not got_any:
                        self.log(f"couldn't play {f.name} (not audio, or damaged); skipping", error=True)
                if not pass_any:
                    return                                    # nothing playable: retried with back-off
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            self.playing = ""
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
                if self.quiet:
                    self.quiet = False
                    self.log(f"working again after {self.fails} failed attempts", force=True)
                self.fails = 0
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
