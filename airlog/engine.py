"""Capture engine.

One ffmpeg process decodes the source (stream or sound card) to raw 16-bit PCM
on stdout. Python reads it in 100 ms blocks and fans each block out to:

  * the archive writer - an ffmpeg encoder per file, rolled at exact clock
    boundaries (the split is sample-accurate because we cut the PCM ourselves);
  * the spool - raw PCM segments on disk covering the last N minutes, which is
    what "grab" reads from, so audio is available seconds after it airs;
  * the peak history and level meters used by the UI.

Timeline: every captured frame has a wall-clock time  t0 + n / sample_rate.
If the source drops out, the gap is filled with silence on reconnect (up to
10 minutes) so archive files stay clock-aligned and grabs by time stay exact.
"""
from __future__ import annotations

import collections
import datetime as dt
import os
import re
import subprocess
import threading
import time
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import nowplaying, sources
from .config import Config

NO_WINDOW = sources.NO_WINDOW
BLOCKS_PER_SEC = 10
GAP_TOLERANCE_S = 1.5        # drift beyond this is treated as a gap / clock correction
GAP_FILL_MAX_S = 600         # gaps up to this long are filled with silence
WATCHDOG_S = 20              # no data for this long -> restart the source
SEGMENT_S = 60               # spool segment length


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def next_boundary(t: float, split_minutes: int) -> float:
    """Next local-time boundary strictly after t, e.g. the top of the next hour."""
    m = max(1, int(split_minutes))
    d = dt.datetime.fromtimestamp(t + 0.001)
    if m >= 60:
        floor = d.replace(minute=0, second=0, microsecond=0)
        nxt = floor + dt.timedelta(minutes=m)
    else:
        floor = d.replace(minute=(d.minute // m) * m, second=0, microsecond=0)
        nxt = floor + dt.timedelta(minutes=m)
    return nxt.timestamp()


def safe_name(s: str) -> str:
    return re.sub(r'[<>:"\\|?*\x00-\x1f]', "_", s).strip() or "Station"


def render_pattern(pattern: str, t: float, station: str) -> str:
    stamp = dt.datetime.fromtimestamp(t)
    return stamp.strftime(pattern.replace("{station}", safe_name(station).replace("%", "%%")))


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 2
    while True:
        q = p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not q.exists():
            return q
        i += 1


def codec_args(fmt: str, bitrate: int) -> list[str]:
    fmt = fmt.lower()
    if fmt == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", f"{bitrate}k"]
    if fmt == "flac":
        return ["-c:a", "flac"]
    if fmt == "ogg":
        return ["-c:a", "libvorbis", "-b:a", f"{bitrate}k"]
    if fmt == "wav":
        return ["-c:a", "pcm_s16le"]
    raise ValueError(f"Unsupported format {fmt!r}")


def fmt_clock(t: float) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%H:%M:%S")


# ----------------------------------------------------------------------------
# Spool: rolling raw-PCM buffer on disk
# ----------------------------------------------------------------------------
@dataclass
class Segment:
    start: float
    frames: int
    path: Path

    def end(self, sr: int) -> float:
        return self.start + self.frames / sr


class Spool:
    def __init__(self, folder: Path, sr: int, ch: int, retention_s: float):
        self.dir = folder
        self.sr, self.ch = sr, ch
        self.bpf = 2 * ch
        self.retention = retention_s
        self.lock = threading.Lock()
        self.segments: list[Segment] = []
        self._fh = None
        self._cur: Segment | None = None
        self.dir.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    # Segments survive an app restart, so you can still grab what aired before it.
    def _load_existing(self) -> None:
        marker = self.dir / "format.txt"
        fmt = f"{self.sr} {self.ch}"
        same = marker.exists() and marker.read_text().strip() == fmt
        for p in sorted(self.dir.glob("seg_*.pcm")):
            if not same:
                p.unlink(missing_ok=True)
                continue
            try:
                start = int(p.stem.split("_")[1]) / 1000.0
                frames = p.stat().st_size // self.bpf
            except (ValueError, OSError):
                continue
            if frames:
                self.segments.append(Segment(start, frames, p))
            else:
                p.unlink(missing_ok=True)
        marker.write_text(fmt)
        self.segments.sort(key=lambda s: s.start)
        self.prune(time.time())

    def write(self, pcm: bytes, start: float, new_segment: bool = False) -> None:
        n = len(pcm) // self.bpf
        cur = self._cur
        if (cur is None or new_segment or cur.frames >= SEGMENT_S * self.sr
                or abs(cur.end(self.sr) - start) > 0.05):
            self._open(start)
            cur = self._cur
        self._fh.write(pcm)
        self._fh.flush()
        with self.lock:
            cur.frames += n

    def _open(self, start: float) -> None:
        self.close()
        path = self.dir / f"seg_{int(round(start * 1000))}.pcm"
        path = unique_path(path)
        self._fh = open(path, "wb")
        seg = Segment(start, 0, path)
        with self.lock:
            self.segments.append(seg)
        self._cur = seg

    def close(self) -> None:
        if self._fh:
            self._fh.close()
        self._fh = None
        self._cur = None

    def prune(self, now: float) -> None:
        cutoff = now - self.retention
        with self.lock:
            keep = []
            for s in self.segments:
                if s.end(self.sr) < cutoff and s is not self._cur:
                    try:
                        s.path.unlink(missing_ok=True)
                    except OSError:      # Windows: a grab may still have it open
                        keep.append(s)
                else:
                    keep.append(s)
            self.segments = keep

    def earliest(self) -> float | None:
        with self.lock:
            return self.segments[0].start if self.segments else None

    def latest(self) -> float | None:
        with self.lock:
            return self.segments[-1].end(self.sr) if self.segments else None

    def frames_for(self, t1: float, t2: float) -> int:
        return max(0, int(round((t2 - t1) * self.sr)))

    def iter_range(self, t1: float, t2: float, chunk_frames: int = 48000):
        """Yield PCM for [t1, t2) in chunks; holes are filled with silence.
        Always yields exactly frames_for(t1, t2) frames."""
        sr, bpf = self.sr, self.bpf
        with self.lock:
            segs = [Segment(s.start, s.frames, s.path) for s in self.segments]
        total_needed = self.frames_for(t1, t2)
        written = 0

        def silence(frames: int):
            while frames > 0:
                k = min(frames, chunk_frames)
                yield bytes(k * bpf)
                frames -= k

        for s in segs:
            if written >= total_needed:
                break
            cursor = t1 + written / sr
            s_end = s.end(sr)
            if s_end <= cursor or s.start >= t2:
                continue
            a = max(cursor, s.start)
            b = min(t2, s_end)
            if b <= a:
                continue
            gap = min(int(round((a - t1) * sr)), total_needed) - written
            if gap > 0:
                yield from silence(gap)
                written += gap
            f_from = int(round((a - s.start) * sr))
            f_to = min(s.frames, f_from + total_needed - written)
            try:
                fh = open(s.path, "rb")
            except OSError:
                continue          # pruned while we were reading
            with fh:
                fh.seek(f_from * bpf)
                remaining = f_to - f_from
                while remaining > 0:
                    data = fh.read(min(remaining, chunk_frames) * bpf)
                    data = data[: len(data) - len(data) % bpf]
                    if not data:
                        break
                    yield data
                    got = len(data) // bpf
                    written += got
                    remaining -= got
        if written < total_needed:
            yield from silence(total_needed - written)

    def read_range(self, t1: float, t2: float, sink: Callable[[bytes], None]) -> int:
        n = 0
        for chunk in self.iter_range(t1, t2):
            sink(chunk)
            n += len(chunk) // self.bpf
        return n


# ----------------------------------------------------------------------------
# Archive writer: one ffmpeg encoder per file
# ----------------------------------------------------------------------------
class ArchiveFile:
    def __init__(self, engine: "Engine", start: float):
        cfg = engine.cfg
        self.engine = engine
        # Snap sub-frame float error so a file opened at 12:59:59.99998 is the 13:00 file.
        if abs(start - round(start)) < 0.05:
            start = float(round(start))
        self.start = start
        self.boundary = next_boundary(start, cfg.split_minutes)
        rel = render_pattern(cfg.archive_pattern, start, cfg.station_name) + "." + cfg.archive_format
        path = unique_path(Path(cfg.archive_dir) / rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.frames = 0
        title = f"{cfg.station_name} {dt.datetime.fromtimestamp(start):%Y-%m-%d %H:%M}"
        cmd = [cfg.ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
               "-f", "s16le", "-ar", str(cfg.sample_rate), "-ac", str(cfg.channels), "-i", "pipe:0",
               *codec_args(cfg.archive_format, cfg.archive_bitrate_kbps),
               "-metadata", f"title={title}", "-metadata", f"artist={cfg.station_name}",
               "-metadata", f"date={dt.datetime.fromtimestamp(start):%Y-%m-%d}",
               str(path)]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        self.broken = False
        engine.log(f"Archive: started {path.name}")

    def write(self, pcm: bytes) -> None:
        if self.broken:
            return
        try:
            self.proc.stdin.write(pcm)
            self.frames += len(pcm) // (2 * self.engine.cfg.channels)
        except (BrokenPipeError, OSError, ValueError):
            self.broken = True
            err = b""
            try:
                err = self.proc.stderr.read() or b""
            except Exception:
                pass
            self.engine.log(f"Archive encoder failed for {self.path.name}: "
                            f"{err.decode(errors='replace').strip()[-300:]}", error=True)

    def close(self) -> None:
        proc, name, engine = self.proc, self.path.name, self.engine
        secs = self.frames / engine.cfg.sample_rate

        def finish():
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
            if proc.returncode not in (0, None) and not self.broken:
                engine.log(f"Archive encoder exited {proc.returncode} for {name}", error=True)
            else:
                engine.log(f"Archive: closed {name} ({secs/60:.1f} min)")
            engine.archive_closed(self.path)

        threading.Thread(target=finish, daemon=True).start()


# ----------------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------------
class Engine:
    def __init__(self, cfg: Config, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.now = clock
        self.sr = cfg.sample_rate
        self.ch = cfg.channels
        self.bpf = 2 * self.ch
        self.block_frames = self.sr // BLOCKS_PER_SEC

        self.status = "stopped"           # stopped | starting | recording | reconnecting | error
        self.last_error = ""
        self.logs: collections.deque[str] = collections.deque(maxlen=500)
        self.on_log: Callable[[str, bool], None] | None = None
        self.recent_archive: collections.deque[Path] = collections.deque(maxlen=50)

        retention = max(5, cfg.buffer_minutes) * 60
        self.spool = Spool(cfg.effective_spool_dir(), self.sr, self.ch, retention)
        self.tracks = nowplaying.TrackLog(cfg.effective_spool_dir() / "tracks.json")
        self.recognizer: nowplaying.Recognizer | None = None

        # Peak history for the waveform: one byte (0-255) per 100 ms block.
        self.peak_lock = threading.Lock()
        self.peak_times = array("d")
        self.peak_vals = array("B")
        self.level = (0.0, 0.0)           # last block peak, linear 0..1, L/R
        self.silent_since: float | None = None

        self.t0: float | None = None
        self.total = 0
        self.archive: ArchiveFile | None = None
        self.files_started = 0

        self._running = False
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._last_data = 0.0
        self._pending_new_segment = False
        self._reconnected = False

        if self.spool.segments:
            threading.Thread(target=self._rebuild_peaks, name="peaks", daemon=True).start()

    def _rebuild_peaks(self) -> None:
        """Recompute waveform peaks for audio left in the spool by a previous run."""
        with self.spool.lock:
            segs = [Segment(s.start, s.frames, s.path) for s in self.spool.segments]
        times, vals = array("d"), array("B")
        step = self.block_frames * self.ch
        for s in segs:
            try:
                with open(s.path, "rb") as fh:
                    data = fh.read(s.frames * self.bpf)
            except OSError:
                continue
            a = array("h")
            a.frombytes(data[: len(data) - len(data) % 2])
            for i in range(0, len(a), step):
                blk = a[i:i + step]
                if not blk:
                    break
                pk = max(max(blk), -min(blk)) / 32768
                times.append(s.start + (i // self.ch) / self.sr)
                vals.append(min(255, int(pk * 255 + 0.5)))
        with self.peak_lock:
            if self.peak_times and times and times[-1] >= self.peak_times[0]:
                import bisect
                cut = bisect.bisect_left(times, self.peak_times[0])
                times, vals = times[:cut], vals[:cut]
            self.peak_times[:0] = times
            self.peak_vals[:0] = vals
        if times:
            self.log(f"Restored {len(times) / BLOCKS_PER_SEC / 60:.1f} min of buffer from previous session")

    # ------------------------------------------------------------------ logging
    def log(self, msg: str, error: bool = False) -> None:
        line = f"{dt.datetime.now():%H:%M:%S}  {msg}"
        self.logs.append(line)
        if error:
            self.last_error = msg
        if self.on_log:
            try:
                self.on_log(line, error)
            except Exception:
                pass

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.status = "starting"
        self._thread = threading.Thread(target=self._supervise, name="capture", daemon=True)
        self._thread.start()
        threading.Thread(target=self._watchdog, name="watchdog", daemon=True).start()
        threading.Thread(target=self._housekeeping, name="housekeeping", daemon=True).start()
        if self.cfg.identify_songs:
            self.recognizer = nowplaying.Recognizer(self)
            self.recognizer.start()

    def stop(self) -> None:
        self._running = False
        self.tracks.close(self.live_time() or time.time())
        p = self._proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        self._close_archive()
        self.spool.close()
        self.status = "stopped"

    @property
    def running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------ timeline
    def live_time(self) -> float | None:
        """Wall-clock time of the most recently captured audio."""
        if self.t0 is None:
            return self.spool.latest()
        return self.t0 + self.total / self.sr

    def buffer_start(self) -> float | None:
        return self.spool.earliest()

    def peak_buckets(self, t1: float, t2: float, n: int) -> list[int]:
        """Max peak (0-255) in each of n equal time buckets across [t1, t2)."""
        import bisect
        n = max(1, min(int(n), 8000))
        out = [0] * n
        step = (t2 - t1) / n
        with self.peak_lock:
            times, vals = self.peak_times, self.peak_vals
            i = bisect.bisect_left(times, t1)
            for k in range(n):
                end = t1 + (k + 1) * step
                j = bisect.bisect_left(times, end, i)
                if j > i:
                    out[k] = max(vals[i:j])
                elif step < 0.1 and i < len(times) and i > 0 and times[i] - times[i - 1] <= 0.15:
                    out[k] = vals[i - 1]        # zoomed in past block resolution
                i = j
        return out

    def peaks_between(self, t1: float, t2: float) -> tuple[array, array]:
        import bisect
        with self.peak_lock:
            i = bisect.bisect_left(self.peak_times, t1)
            j = bisect.bisect_right(self.peak_times, t2)
            return self.peak_times[i:j], self.peak_vals[i:j]

    # ------------------------------------------------------------------ capture loop
    def _capture_cmd(self) -> list[str]:
        return [self.cfg.ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "warning",
                *sources.input_args(self.cfg),
                "-vn", "-sn", "-dn", "-ac", str(self.ch), "-ar", str(self.sr),
                "-f", "s16le", "pipe:1"]

    def _supervise(self) -> None:
        backoff = 2.0
        while self._running:
            try:
                cmd = self._capture_cmd()
            except ValueError as e:
                self.status = "error"
                self.log(str(e), error=True)
                self._sleep(5)
                continue
            self.log("Source: " + sources.describe(self.cfg))
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                              stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
            except OSError as e:
                self.status = "error"
                self.log(f"Could not run ffmpeg ({self.cfg.ffmpeg_path}): {e}", error=True)
                self._sleep(10)
                continue
            threading.Thread(target=self._pump_stderr, args=(self._proc,), daemon=True).start()
            self._last_data = time.monotonic()
            got_any = self._read_loop(self._proc)
            rc = self._proc.poll()
            if rc is None:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            if not self._running:
                break
            self.status = "reconnecting"
            self.log(f"Source ended (ffmpeg exit {self._proc.returncode}); retrying in {backoff:.0f}s",
                     error=True)
            self._sleep(backoff)
            backoff = 2.0 if got_any else min(backoff * 2, 30.0)
        self.status = "stopped"

    def _sleep(self, s: float) -> None:
        end = time.monotonic() + s
        while self._running and time.monotonic() < end:
            time.sleep(0.2)

    def _pump_stderr(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode(errors="replace").strip()
            if line:
                self.log("ffmpeg: " + line[:300])

    def _read_loop(self, proc: subprocess.Popen) -> bool:
        need = self.block_frames * self.bpf
        got_any = False
        out = proc.stdout
        while self._running:
            data = out.read(need)
            if not data:
                break
            if len(data) % self.bpf:
                data = data[: len(data) - len(data) % self.bpf]
                if not data:
                    continue
            self._last_data = time.monotonic()
            if not got_any:
                got_any = True
                self._reconnected = self.t0 is not None
                if self.status != "recording":
                    self.log("Recording")
                self.status = "recording"
            self._on_block(data)
        return got_any

    def _watchdog(self) -> None:
        while self._running:
            time.sleep(1)
            p = self._proc
            if (p and p.poll() is None and self.status in ("recording", "starting", "reconnecting")
                    and time.monotonic() - self._last_data > WATCHDOG_S):
                self.log(f"No audio for {WATCHDOG_S}s; restarting source", error=True)
                self._last_data = time.monotonic()
                try:
                    p.kill()
                except Exception:
                    pass

    # ------------------------------------------------------------------ per-block processing
    def _on_block(self, pcm: bytes) -> None:
        n = len(pcm) // self.bpf
        now = self.now() - float(self.cfg.source_delay_s or 0)
        if self.t0 is None:
            self.t0 = now - n / self.sr
            self.total = 0
        end_clock = self.t0 + (self.total + n) / self.sr
        drift = now - end_clock
        tol = 0.05 if self._reconnected else GAP_TOLERANCE_S
        self._reconnected = False
        if drift > tol:
            if drift <= GAP_FILL_MAX_S:
                self.log(f"Gap of {drift:.1f}s filled with silence")
                frames = int(round(drift * self.sr))
                step = self.sr  # 1 s chunks
                while frames > 0:
                    k = min(step, frames)
                    self._emit(bytes(k * self.bpf), k)
                    frames -= k
            else:
                self.log(f"Gap of {drift/60:.1f} min - starting a new archive file")
                self.t0 += drift
                self._close_archive()
                self._pending_new_segment = True
        elif drift < -GAP_TOLERANCE_S:
            # Audio is arriving faster than real time (stream burst at connect, or a
            # fast sound-card clock): relabel so the newest audio lines up with now.
            self.t0 += drift
            self._pending_new_segment = True
        self._emit(pcm, n)

    def _emit(self, pcm: bytes, n: int) -> None:
        start = self.t0 + self.total / self.sr
        cfg = self.cfg
        if cfg.archive_enabled:
            offset = 0
            t = start
            while offset < n:
                if self.archive is None or self.archive.broken:
                    if self.archive is not None:
                        self._close_archive()
                    self.archive = ArchiveFile(self, t)
                    self.files_started += 1
                to_boundary = int(round((self.archive.boundary - t) * self.sr))
                if to_boundary <= 0:
                    self._close_archive()
                    continue
                k = min(n - offset, to_boundary)
                self.archive.write(pcm[offset * self.bpf:(offset + k) * self.bpf])
                offset += k
                t = start + offset / self.sr
                if k == to_boundary:
                    # Boundary reached exactly: the next frame opens the next file,
                    # named from the boundary time (e.g. 13-00).
                    self._close_archive()

        self.spool.write(pcm, start, new_segment=self._pending_new_segment)
        self._pending_new_segment = False
        self._measure(pcm, start)
        self.total += n

    def _close_archive(self) -> None:
        if self.archive is not None:
            self.archive.close()
            self.archive = None

    def archive_closed(self, path: Path) -> None:
        self.recent_archive.appendleft(path)

    def _measure(self, pcm: bytes, start: float) -> None:
        a = array("h")
        a.frombytes(pcm)
        step = self.block_frames * self.ch
        times, vals = [], []
        lvl = (0.0, 0.0)
        for i in range(0, len(a), step):
            blk = a[i:i + step]
            if self.ch == 2:
                l, r = blk[0::2], blk[1::2]
                pl = max(max(l), -min(l)) / 32768 if l else 0.0
                pr = max(max(r), -min(r)) / 32768 if r else 0.0
            else:
                pl = pr = (max(max(blk), -min(blk)) / 32768) if blk else 0.0
            lvl = (pl, pr)
            times.append(start + (i // self.ch) / self.sr)
            vals.append(min(255, int(max(pl, pr) * 255 + 0.5)))
        self.level = lvl
        with self.peak_lock:
            self.peak_times.extend(times)
            self.peak_vals.extend(vals)
            keep = int(self.spool.retention * BLOCKS_PER_SEC) + 600
            if len(self.peak_times) > keep + 3000:
                cut = len(self.peak_times) - keep
                del self.peak_times[:cut]
                del self.peak_vals[:cut]
        # silence detection
        thresh = 10 ** (self.cfg.silence_alert_db / 20)
        if max(lvl) < thresh:
            if self.silent_since is None:
                self.silent_since = start
        else:
            self.silent_since = None

    def silence_seconds(self) -> float:
        lt = self.live_time()
        if self.silent_since is None or lt is None:
            return 0.0
        return lt - self.silent_since

    # ------------------------------------------------------------------ housekeeping
    def _housekeeping(self) -> None:
        last_archive_sweep = 0.0
        while self._running:
            self.spool.prune(self.now())
            if self.cfg.archive_keep_days > 0 and time.time() - last_archive_sweep > 3600:
                last_archive_sweep = time.time()
                self._sweep_archive()
            self._sleep(30)

    def _sweep_archive(self) -> None:
        root = Path(self.cfg.archive_dir)
        if not root.exists():
            return
        cutoff = time.time() - self.cfg.archive_keep_days * 86400
        exts = {".mp3", ".flac", ".wav", ".ogg"}
        removed = 0
        for p in root.rglob("*"):
            try:
                if p.is_file() and p.suffix.lower() in exts and p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
        for d in sorted((d for d in root.rglob("*") if d.is_dir()), key=lambda d: len(d.parts), reverse=True):
            try:
                d.rmdir()   # only succeeds when empty
            except OSError:
                pass
        if removed:
            self.log(f"Archive cleanup: removed {removed} file(s) older than {self.cfg.archive_keep_days} days")

    # ------------------------------------------------------------------ clips
    def clamp_range(self, t1: float, t2: float) -> tuple[float, float]:
        if t2 <= t1:
            raise ValueError("End must be after start")
        earliest = self.buffer_start()
        latest = self.live_time()
        if earliest is None or latest is None:
            raise ValueError("Nothing recorded yet")
        if t2 < earliest or t1 > latest:
            raise ValueError(f"That range is outside the buffer ({fmt_clock(earliest)}–{fmt_clock(latest)})")
        t1, t2 = max(t1, earliest), min(t2, latest)
        if t2 - t1 < 0.01:
            raise ValueError("Nothing in that range yet")
        return t1, t2

    def wav_header(self, frames: int) -> bytes:
        import struct
        data = frames * self.bpf
        return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + data, b"WAVE", b"fmt ", 16, 1, self.ch,
                           self.sr, self.sr * self.bpf, self.bpf, 16, b"data", data)

    def iter_clip(self, t1: float, t2: float, fmt: str = "wav", bitrate: int | None = None):
        """Yield an encoded file for [t1, t2) as bytes chunks (for HTTP streaming)."""
        fmt = fmt.lower()
        if fmt == "wav":
            yield self.wav_header(self.spool.frames_for(t1, t2))
            yield from self.spool.iter_range(t1, t2)
            return
        cfg = self.cfg
        if fmt == "flac":
            # FLAC written to a pipe has no length in its header, so encode to a temp file first.
            import tempfile
            fd, tmpname = tempfile.mkstemp(suffix=".flac", prefix="airlog_")
            os.close(fd)
            tmp = Path(tmpname)
            try:
                self.grab(t1, t2, out_path=tmp, fmt="flac", quiet=True)
                with open(tmp, "rb") as fh:
                    while True:
                        data = fh.read(65536)
                        if not data:
                            break
                        yield data
            finally:
                tmp.unlink(missing_ok=True)
            return
        container = {"mp3": "mp3", "ogg": "ogg"}[fmt]
        cmd = [cfg.ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "error",
               "-f", "s16le", "-ar", str(self.sr), "-ac", str(self.ch), "-i", "pipe:0",
               *codec_args(fmt, bitrate or max(cfg.archive_bitrate_kbps, 192)), "-f", container, "pipe:1"]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             creationflags=NO_WINDOW)

        def feed():
            try:
                for chunk in self.spool.iter_range(t1, t2):
                    p.stdin.write(chunk)
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try:
                    p.stdin.close()
                except OSError:
                    pass
        threading.Thread(target=feed, daemon=True).start()
        try:
            while True:
                data = p.stdout.read(65536)
                if not data:
                    break
                yield data
        finally:
            if p.poll() is None:
                p.kill()
            p.wait()

    def clip_name(self, t1: float, t2: float, fmt: str) -> str:
        return render_pattern(self.cfg.grab_pattern, t1, self.cfg.station_name) + f"_{(t2 - t1):.0f}s.{fmt}"

    # ------------------------------------------------------------------ grabs
    def grab(self, t1: float, t2: float, out_path: Path | None = None, fmt: str | None = None,
             quiet: bool = False) -> Path:
        """Export [t1, t2) to a file. Blocking - call from a worker thread."""
        cfg = self.cfg
        earliest = self.buffer_start()
        if earliest is not None and t1 < earliest - 0.5:
            raise ValueError(f"Start {fmt_clock(t1)} is before the buffer begins ({fmt_clock(earliest)})")
        t1, t2 = self.clamp_range(t1, t2)
        fmt = (fmt or cfg.grab_format).lower()
        if out_path is None:
            out_path = unique_path(Path(cfg.grabs_dir) / self.clip_name(t1, t2, fmt))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(out_path.stem + ".partial" + out_path.suffix)

        if fmt == "wav":
            with wave.open(str(tmp), "wb") as w:
                w.setnchannels(self.ch)
                w.setsampwidth(2)
                w.setframerate(self.sr)
                self.spool.read_range(t1, t2, w.writeframesraw)
        else:
            cmd = [cfg.ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                   "-f", "s16le", "-ar", str(self.sr), "-ac", str(self.ch), "-i", "pipe:0",
                   *codec_args(fmt, max(cfg.archive_bitrate_kbps, 192)),
                   "-f", {"mp3": "mp3", "flac": "flac", "ogg": "ogg"}[fmt], str(tmp)]
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, creationflags=NO_WINDOW)
            try:
                self.spool.read_range(t1, t2, p.stdin.write)
            finally:
                p.stdin.close()
                err = p.stderr.read()
                p.wait()
            if p.returncode != 0:
                tmp.unlink(missing_ok=True)
                raise RuntimeError("Encoder failed: " + err.decode(errors="replace")[-300:])
        os.replace(tmp, out_path)
        if not quiet:
            self.log(f"Grab: {out_path.name} ({fmt_clock(t1)}-{fmt_clock(t2)}, {t2 - t1:.1f}s)")
        return out_path
