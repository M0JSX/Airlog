"""Track-change logic of the song identifier, with canned Shazam answers (no network).
Run:  python tests/test_nowplaying.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from airlog.config import Config                       # noqa: E402
from airlog.nowplaying import Recognizer, TrackLog     # noqa: E402


class FakeEngine:
    def __init__(self, folder: Path):
        self.cfg = Config(identify_interval_s=30)
        self.tracks = TrackLog(folder / "tracks.json")
        self.lines = []

    def log(self, msg, error=False):
        self.lines.append(msg)


def hit(key, title, offset):
    return {"track": {"key": key, "title": title, "subtitle": "Artist " + key, "url": "u", "images": {"coverart": "a"}},
            "matches": [{"offset": offset}]}


def main() -> None:
    folder = Path(tempfile.mkdtemp(prefix="airlog_np_"))
    e = FakeEngine(folder)
    r = Recognizer(e)
    T = 1_000_000.0

    r._result(hit("1", "One", 40.0), T, T + 12)            # sample 40 s into song 1
    cur = e.tracks.current()
    assert cur and cur.title == "One" and abs(cur.start - (T - 40)) < 1e-6, cur
    r._result(hit("1", "One", 70.0), T + 30, T + 42)       # same song again: no new entry
    assert len(e.tracks.tracks) == 1 and cur.last_seen == T + 42

    r._result(hit("2", "Two", 5.0), T + 60, T + 72)        # song 2 started 5 s before the sample
    one, two = e.tracks.tracks
    assert one.end == two.start == T + 55, (one, two)
    assert e.lines[-1] == "Now playing: Artist 2 – Two"

    r._result({}, T + 90, T + 102)                         # one miss: still playing
    assert e.tracks.current() is two
    r._result({}, T + 120, T + 132)                        # second miss: closed
    assert e.tracks.current() is None and two.end == T + 120

    r._result(hit("2", "Two", 70.0), T + 150, T + 162)     # back within REJOIN_S: same entry reopens
    assert len(e.tracks.tracks) == 2 and e.tracks.current() is two

    r._result(hit("3", "Three", 9999.0), T + 180, T + 192) # implausible offset: start at the sample
    assert e.tracks.current().start == T + 180

    # Survives a restart; the song playing at shutdown is closed at its last sighting.
    again = TrackLog(folder / "tracks.json")
    assert [t.title for t in again.tracks] == ["One", "Two", "Three"]
    assert again.current() is None and again.tracks[-1].end == T + 192

    # The list matches the grab buffer: songs that ended before it are dropped (stored and listed),
    # a song still playing is always kept.
    import time
    from airlog.nowplaying import Track
    now = time.time()
    buf = TrackLog(folder / "buf.json", retention_s=600)                     # a 10-minute buffer
    for i, (start, end) in enumerate([(now - 1500, now - 1300), (now - 700, now - 550), (now - 300, now - 60),
                                      (now - 60, None)]):
        buf.begin(Track(start=start, end=end, artist="A", title=f"T{i}", key=str(i), last_seen=end or now))
    assert [t.title for t in buf.tracks] == ["T1", "T2", "T3"], [t.title for t in buf.tracks]   # T0 left the buffer
    assert [t["title"] for t in buf.recent(since=now - 500)] == ["T3", "T2"]                   # newest first; T1 out
    reloaded = TrackLog(folder / "buf.json", retention_s=600)
    assert "T0" not in [t.title for t in reloaded.tracks]
    old = TrackLog(folder / "buf.json", retention_s=60)                      # buffer shortened in Settings
    assert [t.title for t in old.tracks] == ["T3"], [t.title for t in old.tracks]

    # Failure handling: one or two missed checks are skipped quietly; from the third the page says
    # Shazam can't be reached, the log says why (once), and checks back off. Recovery is logged once.
    import asyncio
    from airlog import nowplaying
    f = FakeEngine(folder)
    r = Recognizer(f)
    r.state = "listening"
    assert r._failed(asyncio.TimeoutError()) == 0 and r._failed(asyncio.TimeoutError()) == 0
    assert r.state == "listening" and f.lines == [], f.lines
    d3 = r._failed(asyncio.TimeoutError())
    assert r.state == "failing" and len(f.lines) == 1 and "3 checks in a row" in f.lines[0], f.lines
    assert "no answer from Shazam within 30 s" in f.lines[0], f.lines[0]
    assert d3 == 30 and r._failed(asyncio.TimeoutError()) == 60          # backs off
    assert len(f.lines) == 1                                             # not logged every time
    r._ok()
    assert r.state == "listening" and r.failures == 0 and f.lines[-1] == "Song identification is working again"
    r._failed(asyncio.TimeoutError())
    r._ok()
    assert f.lines.count("Song identification is working again") == 1    # a single blip isn't news

    class RateLimited(Exception):
        pass
    for _ in range(2):
        r._failed(RateLimited("429"))
    assert r._failed(RateLimited("Too many requests (429)")) >= 120 and "rate-limiting" in f.lines[-1], f.lines[-1]
    assert nowplaying.describe_failure(ValueError("")) == "ValueError"

    class FakeShazam:
        def __init__(self, http_client=None):
            self.http_client = http_client
    shz, client = nowplaying.make_shazam(FakeShazam)
    try:
        import shazamio  # noqa: F401
        assert client is not None and shz.http_client is client and client.retry_options.attempts == 2
    except ImportError:
        assert client is None                                            # falls back to shazamio's default
    print("nowplaying OK")


if __name__ == "__main__":
    main()
