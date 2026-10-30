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
    print("nowplaying OK")


if __name__ == "__main__":
    main()
