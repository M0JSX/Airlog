"""Encoder outputs and the receive input, end to end with real ffmpeg.

Airlog A records a test tone and sends it out; Airlog B receives it and must end up recording audio.
Also checks the Icecast/Shoutcast protocol handling against small fake servers.

    python tests/test_streaming.py          (needs ffmpeg with SRT on PATH; ~1 minute)
"""
import base64
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airlog import outputs  # noqa: E402
from airlog.config import Config  # noqa: E402
from airlog.engine import Engine  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="airlog_stream_"))
TONE = "-re -f lavfi -i sine=frequency=440:sample_rate=48000"


def free_port(kind=socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def engine(name: str, **kw) -> Engine:
    d = TMP / name
    cfg = Config(station_name=name, archive_dir=str(d / "arch"), grabs_dir=str(d / "grabs"),
                 spool_dir=str(d / "spool"), buffer_minutes=5, identify_songs=False, web_port=1, **kw)
    e = Engine(cfg)
    e.on_log = lambda line, err: print(f"   [{name}] {line}") if err or "Output" in line or "input" in line \
        or "Waiting" in line or "Sender" in line else None
    return e


def wait(pred, secs=20.0) -> bool:
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.25)
    return pred()


def audible(e: Engine) -> bool:
    """B is recording and the last 2 s contain the tone (not silence)."""
    lt = e.live_time()
    if e.status != "recording" or lt is None:
        return False
    return max(e.peak_buckets(lt - 2, lt, 20)) > 15     # the test sine peaks at ~32 of 255


def assert_(cond, msg):
    if not cond:
        raise AssertionError(msg)


def link(label, out: dict, recv: dict, check_detail=None):
    print(f"-- {label}")
    b = engine("B_" + label.split()[0], source_type="receive", **recv)
    b.start()
    assert wait(lambda: b.status == "waiting", 10), f"{label}: B never started waiting ({b.status})"
    a = engine("A_" + label.split()[0], source_type="custom", custom_input_args=TONE,
               outputs=outputs.normalize_outputs([out]))
    a.start()
    try:
        ok = wait(lambda: audible(b), 25)
        st = a.outputs.status()[0]
        assert ok, f"{label}: B isn't recording the tone (B {b.status}; A output {st['state']}: {st['detail']})"
        assert st["state"] in ("connected", "sending"), st
        if check_detail:
            check_detail(a, b)
        # The sender going away puts B back to waiting, without errors.
        a.stop()
        assert wait(lambda: b.status == "waiting", 30), f"{label}: B didn't go back to waiting ({b.status})"
        print(f"   OK ({st['state']}, B recorded {b.live_time() - b.buffer_start():.1f}s)")
    finally:
        a.stop()
        b.stop()


# ---------------------------------------------------------------- Airlog A -> Airlog B
p = free_port(socket.SOCK_DGRAM)
link("srt with passphrase",
     {"kind": "srt", "host": "127.0.0.1", "port": p, "codec": "aac", "bitrate_kbps": 192,
      "passphrase": "correct horse battery"},
     {"receive_protocol": "srt", "receive_port": p, "receive_passphrase": "correct horse battery"})

p = free_port(socket.SOCK_DGRAM)
link("srt pcm", {"kind": "srt", "host": "127.0.0.1", "port": p, "codec": "pcm"},
     {"receive_protocol": "srt", "receive_port": p})

p = free_port(socket.SOCK_DGRAM)
link("udp mp2", {"kind": "udp", "host": "127.0.0.1", "port": p, "codec": "mp2", "bitrate_kbps": 256},
     {"receive_protocol": "udp", "receive_port": p})

p = free_port(socket.SOCK_DGRAM) & ~1
link("rtp mp2 (static payload type, no SDP)", {"kind": "rtp", "host": "127.0.0.1", "port": p, "codec": "mp2"},
     {"receive_protocol": "rtp", "receive_port": p})

# RTP L16 needs the SDP: B is configured with the one A writes.
p = free_port(socket.SOCK_DGRAM) & ~1
print("-- rtp pcm (L16 + SDP)")
a = engine("A_rtpsdp", source_type="custom", custom_input_args=TONE,
           outputs=outputs.normalize_outputs([{"kind": "rtp", "host": "127.0.0.1", "port": p, "codec": "pcm"}]))
a.start()
assert wait(lambda: "L16/48000" in a.outputs.status()[0]["sdp"], 15), a.outputs.status()
sdp = a.outputs.status()[0]["sdp"]
b = engine("B_rtpsdp", source_type="receive", receive_protocol="rtp", receive_port=p, receive_sdp=sdp)
b.start()
assert wait(lambda: audible(b), 25), f"rtp L16: B {b.status}"
print("   OK (SDP: " + sdp.splitlines()[-1] + ")")
a.stop(); b.stop()

p = free_port()
link("icecast push",
     {"kind": "icecast", "host": "127.0.0.1", "port": p, "mount": "/studio", "password": "hackme", "codec": "mp3"},
     {"receive_protocol": "icecast", "receive_port": p, "receive_password": "hackme"},
     check_detail=lambda a, b: (
         assert_(b.sender == "127.0.0.1", f"sender {b.sender!r}"),
         assert_(a.outputs.status()[0]["sent"] > 10_000, "nothing sent"))[-1])

p = free_port()
link("icecast push ogg/opus",
     {"kind": "icecast", "host": "127.0.0.1", "port": p, "password": "hackme", "codec": "opus", "bitrate_kbps": 96},
     {"receive_protocol": "icecast", "receive_port": p, "receive_password": "hackme"})



# ---------------------------------------------------------------- wrong credentials, clear messages
print("-- wrong passwords give clear errors")
p = free_port()
b = engine("B_wrongpw", source_type="receive", receive_protocol="icecast", receive_port=p, receive_password="right")
b.start()
assert wait(lambda: b.status == "waiting", 10)
a = engine("A_wrongpw", source_type="custom", custom_input_args=TONE, outputs=outputs.normalize_outputs(
    [{"kind": "icecast", "host": "127.0.0.1", "port": p, "password": "wrong"}]))
a.start()
assert wait(lambda: a.outputs.status()[0]["state"] == "error", 15)
d = a.outputs.status()[0]["detail"]
assert "password" in d, d
assert b.status == "waiting"
print("   icecast:", d)
a.stop(); b.stop()

p = free_port(socket.SOCK_DGRAM)
b = engine("B_srtpw", source_type="receive", receive_protocol="srt", receive_port=p, receive_passphrase="right passphrase")
b.start()
a = engine("A_srtpw", source_type="custom", custom_input_args=TONE, outputs=outputs.normalize_outputs(
    [{"kind": "srt", "host": "127.0.0.1", "port": p, "passphrase": "wrong passphrase"}]))
a.start()
assert wait(lambda: a.outputs.status()[0]["state"] == "error", 20), a.outputs.status()
d = a.outputs.status()[0]["detail"]
assert "passphrase" in d, d
print("   srt:", d)
a.stop(); b.stop()


# ---------------------------------------------------------------- fake servers
class FakeServer:
    """Accepts connections and answers with a script; records what it was sent."""

    def __init__(self, handler):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.handler = handler
        self.requests: list[bytes] = []
        self.audio = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.handler, args=(self, c), daemon=True).start()

    def head(self, c):
        buf = b""
        while b"\r\n\r\n" not in buf:
            got = c.recv(4096)
            if not got:
                break
            buf += got
        self.requests.append(buf)
        return buf

    def drain(self, c):
        c.settimeout(5)
        try:
            while True:
                got = c.recv(65536)
                if not got:
                    return
                self.audio += len(got)
        except OSError:
            return

    def close(self):
        self.srv.close()


def old_icecast(srv, c):
    """Icecast < 2.4: PUT is 'not implemented', SOURCE works; metadata updates accepted."""
    h = srv.head(c)
    if h.startswith(b"PUT"):
        c.sendall(b"HTTP/1.0 501 Not Implemented\r\n\r\n"); c.close(); return
    if h.startswith(b"GET /admin/metadata"):
        c.sendall(b"HTTP/1.0 200 OK\r\n\r\n<ok/>"); c.close(); return
    c.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
    srv.drain(c)


def mount_in_use(srv, c):
    srv.head(c)
    c.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/html\r\n\r\n<html><b>Mountpoint in use</b></html>")
    c.close()


print("-- icecast: PUT refused -> SOURCE fallback, song titles")
srv = FakeServer(old_icecast)
a = engine("A_oldice", source_type="custom", custom_input_args=TONE, outputs=outputs.normalize_outputs(
    [{"kind": "icecast", "host": "127.0.0.1", "port": srv.port, "mount": "live", "password": "pw",
      "stream_name": "Test FM", "genre": "Various"}]))
from airlog.nowplaying import Track  # noqa: E402
a.start()
assert wait(lambda: srv.audio > 20_000, 15), (srv.audio, a.outputs.status())
src = next(r for r in srv.requests if r.startswith(b"SOURCE"))
assert b"SOURCE /live HTTP/1.0" in src and b"Ice-Name: Test FM" in src and b"Ice-Genre: Various" in src, src
assert b"Authorization: Basic " + base64.b64encode(b"source:pw") in src
assert wait(lambda: any(b"song=Test%20FM" in r for r in srv.requests), 10), "station name not sent as title"
a.tracks.begin(Track(start=time.time(), end=None, artist="Kraftwerk", title="Radioactivity", key="1"))
assert wait(lambda: any(b"song=Kraftwerk%20-%20Radioactivity" in r for r in srv.requests), 10), "title not sent"
print(f"   OK ({srv.audio // 1024} KB of MP3, titles sent)")
a.stop(); srv.close()

print("-- icecast: mount in use")
srv = FakeServer(mount_in_use)
a = engine("A_inuse", source_type="custom", custom_input_args=TONE, outputs=outputs.normalize_outputs(
    [{"kind": "icecast", "host": "127.0.0.1", "port": srv.port, "password": "pw"}]))
a.start()
assert wait(lambda: a.outputs.status()[0]["state"] == "error", 10)
d = a.outputs.status()[0]["detail"]
assert "Mountpoint in use" in d, d
print("   OK:", d)
a.stop(); srv.close()


def shoutcast_v1(srv, c):
    data = c.recv(1024)
    srv.requests.append(data)
    if data.startswith(b"GET /admin.cgi"):
        c.sendall(b"HTTP/1.0 200 OK\r\n\r\n"); c.close(); return
    if data.strip() != b"sc-pass":
        c.sendall(b"invalid password\r\n"); c.close(); return
    c.sendall(b"OK2\r\nicy-caps:11\r\n\r\n")
    srv.requests.append(srv.head(c))
    srv.drain(c)


print("-- shoutcast v1")
data_srv = FakeServer(shoutcast_v1)              # the source port is base port + 1
base = data_srv.port - 1
try:
    meta_srv = FakeServer(shoutcast_v1)
    meta_srv.srv.close()
    ms = socket.socket(); ms.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); ms.bind(("127.0.0.1", base))
    ms.close()
    have_base = True
except OSError:
    have_base = False
a = engine("A_shout", source_type="custom", custom_input_args=TONE, outputs=outputs.normalize_outputs(
    [{"kind": "shoutcast", "host": "127.0.0.1", "port": base, "password": "sc-pass", "codec": "mp3",
      "send_titles": False}]))
a.start()
assert wait(lambda: data_srv.audio > 20_000, 15), (data_srv.audio, a.outputs.status())
assert any(b"icy-name:A_shout" in r for r in data_srv.requests), data_srv.requests
print(f"   OK ({data_srv.audio // 1024} KB sent)")
a.stop()
a = engine("A_shoutpw", source_type="custom", custom_input_args=TONE, outputs=outputs.normalize_outputs(
    [{"kind": "shoutcast", "host": "127.0.0.1", "port": base, "password": "nope"}]))
a.start()
assert wait(lambda: a.outputs.status()[0]["state"] == "error", 10)
assert "password" in a.outputs.status()[0]["detail"]
a.stop(); data_srv.close()

# ---------------------------------------------------------------- an output that can't keep up
print("-- a stalled output never holds up recording")
stall = socket.socket(); stall.bind(("127.0.0.1", 0)); stall.listen(1)   # accepts, never reads
sp = stall.getsockname()[1]


def accept_and_ignore():
    c, _ = stall.accept()
    c.recv(4096)
    c.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
    time.sleep(60)


threading.Thread(target=accept_and_ignore, daemon=True).start()
a = engine("A_stall", source_type="custom", custom_input_args=TONE, outputs=outputs.normalize_outputs(
    [{"kind": "icecast", "host": "127.0.0.1", "port": sp, "password": "pw", "codec": "aac", "bitrate_kbps": 512}]))
a.start()
assert wait(lambda: a.status == "recording", 10)
t0 = a.live_time()
time.sleep(12)
rec = a.live_time() - t0
assert rec > 10.5, f"recording fell behind: {rec:.1f}s in 12s"
print(f"   OK (recorded {rec:.1f}s in 12s; output dropped {a.outputs.status()[0]['dropped']} blocks)")
a.stop(); stall.close()

# ---------------------------------------------------------------- settings validation and masking
print("-- settings")
o = outputs.normalize_outputs([{"kind": "icecast", "host": "h", "password": "secret"}])
assert o[0]["id"] and o[0]["mount"] == "/live" and o[0]["name"] == "Icecast 1"
masked = outputs.mask_outputs(o)
assert masked[0]["password"] == outputs.MASK
again = outputs.normalize_outputs(masked, previous=o)
assert again[0]["password"] == "secret"
for bad, msg in (([{"kind": "icecast", "host": "h"}], "password"),
                 ([{"kind": "srt", "host": "h", "passphrase": "short"}], "10"),
                 ([{"kind": "udp", "host": "http://x/"}], "address"),
                 ([{"kind": "nope", "host": "h"}], "type")):
    try:
        outputs.normalize_outputs(bad)
        raise AssertionError(f"accepted {bad}")
    except ValueError as e:
        assert msg in str(e), (msg, e)
assert outputs.normalize_outputs([{"kind": "srt", "mode": "listener"}])[0]["host"] == ""
print("   OK")

shutil.rmtree(TMP, ignore_errors=True)
print("streaming OK")
