"""Outputs stay connected while the source stalls: they get real-time silence, and the gap-fill
silence when the source comes back isn't sent to them again as a burst.

Reproduces a real failure: a recorded stream stalled (still connected, no data) for ~30 s, the
Shoutcast output had nothing to send, and the server dropped it ("Timeout waiting for stream data").

The test plays a stream sender into Airlog's stream input, pausing it for 6 s mid-stream, while
an output streams to a UDP receiver that measures every hole in what arrives.

    python tests/test_keepalive.py      (needs ffmpeg)
"""
import base64
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airlog.config import Config  # noqa: E402
from airlog.engine import Engine  # noqa: E402

KBPS = 128
PAUSE_AT, PAUSE_S, TOTAL_S = 8.0, 6.0, 22.0


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Receiver:
    """UDP sink: logs (time, bytes) for every datagram."""

    def __init__(self):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind(("127.0.0.1", 0))
        self.s.settimeout(0.1)
        self.port = self.s.getsockname()[1]
        self.got: list[tuple[float, int]] = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                self.got.append((time.monotonic(), len(self.s.recv(65536))))
            except socket.timeout:
                pass
            except OSError:
                return

    def holes(self, t0, t1):
        times = [t for t, _ in self.got if t0 <= t <= t1]
        if not times:
            return t1 - t0
        edges = [t0] + times + [t1]
        return max(b - a for a, b in zip(edges, edges[1:]))

    def bytes_between(self, t0, t1):
        return sum(n for t, n in self.got if t0 <= t < t1)


def sender(port, mp3: bytes, marks: dict):
    """An Icecast source pushing MP3 in real time, going quiet (still connected) for PAUSE_S."""
    c = socket.create_connection(("127.0.0.1", port))
    auth = base64.b64encode(b"source:pw").decode()
    c.sendall(f"PUT /live HTTP/1.1\r\nHost: x\r\nAuthorization: Basic {auth}\r\n"
              f"Content-Type: audio/mpeg\r\nIce-Name: test\r\n\r\n".encode())
    per_s = KBPS * 1000 // 8
    chunk = per_s // 10
    start = time.monotonic()
    sent_s = 0.0
    pos = 0
    paused = False
    while pos < len(mp3) and sent_s < TOTAL_S:
        if not paused and sent_s >= PAUSE_AT:
            marks["pause"] = time.monotonic()
            time.sleep(PAUSE_S)                       # stalled: connected, no data
            marks["resume"] = time.monotonic()
            start += PAUSE_S
            paused = True
        c.sendall(mp3[pos:pos + chunk])
        pos += chunk
        sent_s += 0.1
        time.sleep(max(0.0, start + sent_s - time.monotonic()))
    marks["end"] = time.monotonic()
    time.sleep(1)
    c.close()


mp3 = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                      "-t", str(TOTAL_S + 5), "-c:a", "libmp3lame", "-b:a", f"{KBPS}k", "-f", "mp3", "-"],
                     capture_output=True, check=True).stdout
rx = Receiver()
rport = free_port()
d = Path(tempfile.mkdtemp(prefix="airlog_keepalive_"))
cfg = Config(station_name="KA", source_type="receive", receive_protocol="icecast", receive_port=rport,
             receive_password="pw", archive_dir=str(d / "arch"), grabs_dir=str(d / "grabs"),
             spool_dir=str(d / "spool"), identify_songs=False, announce=False,
             outputs=[{"id": "o1", "name": "UDP", "kind": "udp", "host": "127.0.0.1", "port": rx.port,
                       "codec": "mp2", "bitrate_kbps": 192}])
from airlog import outputs  # noqa: E402
cfg.outputs = outputs.normalize_outputs(cfg.outputs)
e = Engine(cfg)
e.start()
time.sleep(1.5)
marks: dict = {}
t = threading.Thread(target=sender, args=(rport, mp3, marks), daemon=True)
t.start()
t.join(TOTAL_S + PAUSE_S + 15)
time.sleep(2)
logs = "\n".join(e.logs)
e.stop()

pause, resume = marks["pause"], marks["resume"]
hole = rx.holes(pause + 0.5, resume)
print(f"-- source stalled {PAUSE_S:.0f}s: longest hole in the output during the stall {hole:.2f}s")
assert hole < 1.6, f"the output went quiet for {hole:.1f}s while the source stalled:\n{logs}"
assert "outputs are sending silence to stay connected" in logs, logs
assert "outputs resume after" in logs, logs

steady = rx.bytes_between(pause - 4, pause - 1) / 3
after = max(rx.bytes_between(resume + k * 0.5, resume + k * 0.5 + 1) for k in range(6))
print(f"   steady {steady / 1000:.1f} kB/s; busiest second after the source came back {after / 1000:.1f} kB/s")
assert "filled with silence" in logs, "expected the archive gap to be filled"
assert after < steady * 2.2, f"a burst reached the output after the stall ({after / steady:.1f}x normal)"
print("keepalive OK")
