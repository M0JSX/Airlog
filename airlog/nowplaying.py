"""Song identification.

Every `identify_interval_s` seconds the last SAMPLE_S seconds of captured audio
are sent to Shazam (through the unofficial `shazamio` client, which uses
Shazam's private app API: it needs no account but can stop working without
notice). This works for any source, including a sound card with no metadata.

Shazam reports how far into the song the sample was, so a track's start is
estimated as  sample_start - offset, which puts its marker on the timeline close
to where the song really began.

Tracks are kept in  <spool dir>/tracks.json  so the list survives restarts.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Engine

SAMPLE_S = 12          # seconds of audio sent per check
MISSES_TO_END = 2      # consecutive checks without a match before the current song is closed
REJOIN_S = 150         # the same song matched again within this gap continues the old entry
KEEP = 1000            # most tracks ever kept in tracks.json (the buffer length usually limits it first)
SAVE_EVERY_S = 120     # how often "still playing" updates are written out
SHOW_FAILING_AFTER = 3 # consecutive failed checks before the page says Shazam can't be reached
TIMEOUT_S = 30         # longest a single check may take


@dataclass
class Track:
    start: float                 # timeline time the song is estimated to have started
    end: float | None            # None while it is still playing
    artist: str
    title: str
    key: str = ""                # Shazam track id
    art: str = ""                # cover art URL
    url: str = ""                # Shazam page
    last_seen: float = 0.0       # end of the last sample that matched


_FIELDS = {f.name for f in fields(Track)}


class TrackLog:
    def __init__(self, path: Path, retention_s: float | None = None):
        self.path = path
        # Songs that ended longer ago than this (the grab buffer's length) are dropped: they're no
        # longer in the buffer, so they can't be selected or grabbed.
        self.retention_s = retention_s
        self.lock = threading.Lock()
        self.tracks: list[Track] = []
        self.rev = 0                 # bumped on every change so clients know to refetch
        self._saved_at = 0.0
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.tracks = [Track(**{k: v for k, v in d.items() if k in _FIELDS}) for d in data]
        except (OSError, ValueError, TypeError):
            return
        last = self.tracks[-1] if self.tracks else None
        if last and last.end is None:
            last.end = max(last.start, last.last_seen)   # we weren't listening after this
        self._prune()

    def _prune(self) -> None:
        if self.retention_s:
            cutoff = time.time() - self.retention_s
            self.tracks = [t for t in self.tracks if t.end is None or t.end > cutoff]
        self.tracks = self.tracks[-KEEP:]

    def _save(self) -> None:
        self._prune()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps([asdict(t) for t in self.tracks]), encoding="utf-8")
            tmp.replace(self.path)
            self._saved_at = time.monotonic()
        except OSError:
            pass

    def _changed(self, save: bool = True) -> None:
        self.rev += 1
        if save or time.monotonic() - self._saved_at > SAVE_EVERY_S:
            self._save()

    def last(self) -> Track | None:
        with self.lock:
            return self.tracks[-1] if self.tracks else None

    def current(self) -> Track | None:
        t = self.last()
        return t if t and t.end is None else None

    def begin(self, track: Track) -> None:
        with self.lock:
            prev = self.tracks[-1] if self.tracks else None
            if prev:
                track.start = max(track.start, prev.start + 1)
                if prev.end is None or prev.end > track.start:
                    prev.end = track.start
            self.tracks.append(track)
            self._changed()

    def seen(self, track: Track, until: float) -> None:
        with self.lock:
            reopened = track.end is not None
            track.end = None
            track.last_seen = max(track.last_seen, until)
            self._changed(save=reopened)

    def close(self, end: float) -> None:
        with self.lock:
            t = self.tracks[-1] if self.tracks else None
            if t and t.end is None:
                t.end = max(t.start, end)
                self._changed()

    def recent(self, limit: int = 200, since: float | None = None) -> list[dict]:
        """Newest first. With since (the buffer's start), only songs still (partly) in the buffer."""
        with self.lock:
            keep = [t for t in self.tracks if since is None or t.end is None or t.end > since]
            return [asdict(t) for t in reversed(keep[-limit:])]


def make_shazam(Shazam):
    """A Shazam client that fails fast. shazamio's default retries a slow or rate-limited request up to
    20 times with waits of up to a minute, which only ever surfaced here as a bare timeout. One quick
    retry, then the real error. Returns (shazam, client we must close) - or the default if the
    installed shazamio predates the hook."""
    try:
        from aiohttp_retry import ExponentialRetry
        from shazamio import HTTPClient
        client = HTTPClient(retry_options=ExponentialRetry(attempts=2, start_timeout=1.0, max_timeout=3.0,
                                                           statuses={500, 502, 503, 504}))
        return Shazam(http_client=client), client
    except Exception:
        return Shazam(), None


def rate_limited(ex: Exception) -> bool:
    return type(ex).__name__ == "RateLimited" or "429" in str(ex)


def describe_failure(ex: Exception) -> str:
    if isinstance(ex, (TimeoutError, asyncio.TimeoutError)):
        return f"no answer from Shazam within {TIMEOUT_S} s"
    if rate_limited(ex):
        return "Shazam is rate-limiting this connection (HTTP 429); checking less often"
    text = str(ex).strip()
    return f"{type(ex).__name__}: {text}" if text else type(ex).__name__


class Recognizer:
    """Background thread: samples the live audio and keeps the TrackLog up to date."""

    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.log = engine.tracks
        self.interval = max(15, int(engine.cfg.identify_interval_s))
        self.misses = 0
        self.failures = 0
        self.state = "starting"      # starting | listening | unavailable | failing

    def start(self) -> None:
        threading.Thread(target=self._run, name="identify", daemon=True).start()

    def _run(self) -> None:
        e = self.engine
        try:
            from shazamio import Shazam
        except Exception as ex:      # not installed (e.g. a source checkout without it)
            self.state = "unavailable"
            e.log(f"Song identification unavailable: {ex}")
            return
        loop = asyncio.new_event_loop()
        shazam, client = make_shazam(Shazam)
        self.state = "listening"
        next_at = time.monotonic() + SAMPLE_S
        try:
            while e.running:
                if time.monotonic() < next_at:
                    time.sleep(0.5)
                    continue
                next_at = time.monotonic() + self.interval
                try:
                    delay = self._check(loop, shazam)
                except Exception as ex:
                    delay = self._failed(ex)
                next_at += delay
        finally:
            self.log.close(e.live_time() or time.time())
            if client is not None:
                try:
                    loop.run_until_complete(client.close())
                except Exception:
                    pass
            loop.close()

    def _check(self, loop, shazam) -> float:
        e = self.engine
        if e.status != "recording":
            return 0
        t2, t1 = e.live_time(), e.buffer_start()
        if t2 is None or t1 is None or t2 - t1 < SAMPLE_S:
            return 0
        t1 = t2 - SAMPLE_S
        if e.silence_seconds() >= SAMPLE_S:      # dead air: nothing to identify
            self._no_match(t1)
            return 0
        wav = b"".join(e.iter_clip(t1, t2, "wav"))
        res = loop.run_until_complete(asyncio.wait_for(shazam.recognize(wav), TIMEOUT_S))
        self._ok()
        self._result(res or {}, t1, t2)
        return 0

    def _ok(self) -> None:
        if self.state == "failing":
            self.engine.log("Song identification is working again")
        self.failures = 0
        self.state = "listening"

    def _failed(self, ex: Exception) -> float:
        """A check that didn't get an answer. One or two in a row are normal (a slow or busy Shazam):
        skip them quietly. From SHOW_FAILING_AFTER on, say so on the page and in the log, and back off."""
        self.failures += 1
        if self.failures < SHOW_FAILING_AFTER:
            return 0
        self.state = "failing"
        n = self.failures
        if n == SHOW_FAILING_AFTER or n == 10 or n % 50 == 0:
            self.engine.log(f"Song identification: {n} checks in a row failed; last: {describe_failure(ex)}"[:300])
        backoff = self.interval * (n - SHOW_FAILING_AFTER + 1)
        if rate_limited(ex):
            backoff = max(backoff, 120)          # asked to slow down: do
        return min(600, backoff)

    def _no_match(self, t1: float) -> None:
        self.misses += 1
        cur = self.log.current()
        if cur and self.misses >= MISSES_TO_END:
            self.log.close(max(cur.last_seen, t1))

    def _result(self, res: dict, t1: float, t2: float) -> None:
        tr = res.get("track")
        if not tr:
            self._no_match(t1)
            return
        self.misses = 0
        key = str(tr.get("key", ""))
        last = self.log.last()
        if last and last.key == key and (last.end is None or t1 - last.end < REJOIN_S):
            self.log.seen(last, t2)
            return
        try:
            offset = float((res.get("matches") or [{}])[0].get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0.0
        start = t1 - offset if 0 <= offset < 1800 else t1
        images = tr.get("images") or {}
        t = Track(start=start, end=None, artist=str(tr.get("subtitle", "")), title=str(tr.get("title", "")),
                  key=key, art=str(images.get("coverart", "")), url=str(tr.get("url", "")), last_seen=t2)
        self.log.begin(t)
        self.engine.log(f"Now playing: {t.artist} – {t.title}")
