"""Listening live, and previewing a selection that has no Out yet: /api/listen keeps playing past
where the recording was when it started, following it as it grows, with nothing repeated or skipped.

    python tests/test_listen.py      (needs flask + ffmpeg)
"""
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airlog.config import Config  # noqa: E402
from airlog.server import Holder, create_app  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="airlog_listen_"))
cfg = Config(station_name="L", source_type="custom",
             custom_input_args="-re -f lavfi -i sine=frequency=440:sample_rate=48000",
             archive_dir=str(tmp / "arch"), grabs_dir=str(tmp / "grabs"), spool_dir=str(tmp / "spool"),
             autostart=False, identify_songs=False, announce=False)
holder = Holder(cfg, tmp / "config.json")
c = create_app(holder).test_client()
e = holder.engine

assert c.get("/api/listen").status_code == 416, "nothing recorded yet"
e.start()
deadline = time.time() + 20
while time.time() < deadline and (e.status != "recording" or (e.live_time() or 0) - (e.buffer_start() or 1e18) < 5):
    time.sleep(0.2)
assert e.status == "recording", list(e.logs)


def decoded_seconds(mp3: bytes) -> float:
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "mp3", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "8000",
                        "pipe:1"], input=mp3, capture_output=True)
    return len(r.stdout) / 2 / 8000


def collect(url: str, secs: float) -> tuple[bytes, float]:
    """Read a /api/listen stream for secs of wall time; returns (mp3, live time when it started)."""
    live_then = e.live_time()
    resp = c.get(url, buffered=False)
    assert resp.status_code == 200 and resp.mimetype == "audio/mpeg", resp.status_code
    got, stop = bytearray(), time.time() + secs
    it = iter(resp.response)

    def reader():
        for chunk in it:
            got.extend(chunk)
            if time.time() > stop:
                break
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(secs + 5)
    resp.close()
    return bytes(got), live_then


# Preview from an In point 3 s back, no Out: it catches up, then keeps going past where live was.
lt = e.live_time()
mp3, live_then = collect(f"/api/listen?from={lt - 3:.3f}", 6)
heard = decoded_seconds(mp3)
print(f"-- from In (3 s back): heard {heard:.1f}s in 6 s of wall time")
assert heard > 3 + 4, f"stopped at the old live point ({heard:.1f}s)"
assert heard < 3 + 6 + 1.5, f"more audio than exists: something repeated ({heard:.1f}s)"

# Listen live: starts just behind live and stays real time.
mp3, _ = collect("/api/listen", 5)
heard = decoded_seconds(mp3)
print(f"-- live: heard {heard:.1f}s in 5 s of wall time")
assert 3.5 < heard < 6.5, heard

# A From older than the buffer is clamped to the buffer's start rather than refused.
r = c.get("/api/listen?from=0", buffered=False)
assert r.status_code == 200 and float(r.headers["X-Airlog-From"]) >= e.buffer_start() - 0.01
r.close()

# Stopping recording ends the stream instead of hanging the listener.
resp = c.get("/api/listen", buffered=False)
it = iter(resp.response)
next(it)
threading.Timer(1, e.stop).start()
t0 = time.time()
for _ in it:
    if time.time() - t0 > 15:
        raise AssertionError("listen stream didn't end when recording stopped")
print(f"-- stream ended {time.time() - t0:.1f}s after it started, recording stopped at 1 s")
assert c.get("/api/listen").status_code == 416
print("listen OK")
