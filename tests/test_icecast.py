"""Airlog's Icecast output against a real Icecast 2 server (Debian's icecast2 package).

The other streaming tests use a small fake server; this one runs the real thing and listens to what
it serves, like a listener would:
  * MP3 320, AAC 320, Ogg Vorbis and Opus mounts connect and are listed with the stream name/genre;
  * each mount decodes as the right codec, with the test tone audible;
  * song titles reach the server's "now playing" (MP3 and AAC);
  * a wrong password is reported, not retried silently;
  * the server restarting: Airlog reconnects by itself;
  * the alternative input: a switched mount carries it (with its title); a programme mount doesn't.

    python tests/test_icecast.py      (needs icecast2 and ffmpeg; skipped without icecast2)
"""
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ICECAST = shutil.which("icecast2") or shutil.which("icecast")
if not ICECAST:
    print("icecast: no icecast2 installed, skipped")
    sys.exit(0)

from airlog import outputs  # noqa: E402
from airlog.config import Config  # noqa: E402
from airlog.engine import Engine  # noqa: E402
from airlog.nowplaying import Track  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="airlog_icecast_"))
os.chmod(TMP, 0o777)
SRC_PW, ADMIN_PW = "src-pass-123", "adm-pass-456"
PROG = "-re -f lavfi -i sine=frequency=440:sample_rate=48000,volume=4"       # programme: 440 Hz
ALT = "-re -f lavfi -i sine=frequency=1000:sample_rate=48000,volume=2"       # alternative: 1 kHz


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(pred, secs=20.0):
    end = time.time() + secs
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return pred()


PORT = free_port()


def share(name):
    for d in ("/usr/share/icecast2", "/usr/share/icecast", "/usr/local/share/icecast"):
        if (Path(d) / name).is_dir():
            return f"{d}/{name}"
    return f"/usr/share/icecast2/{name}"


def owner():
    """Icecast won't run as root: have it drop to its own user (Debian: icecast2), or nobody."""
    if os.geteuid() != 0:
        return ""
    for user in ("icecast2", "icecast", "nobody"):
        try:
            pw = pwd.getpwnam(user)
        except KeyError:
            continue
        import grp
        group = grp.getgrgid(pw.pw_gid).gr_name
        return f"<changeowner><user>{user}</user><group>{group}</group></changeowner>"
    return ""


(TMP / "log").mkdir()
os.chmod(TMP / "log", 0o777)
(TMP / "icecast.xml").write_text(f"""<icecast>
  <location>Airlog test</location><admin>test@example.org</admin>
  <limits><clients>50</clients><sources>10</sources><queue-size>524288</queue-size>
    <client-timeout>30</client-timeout><header-timeout>15</header-timeout><source-timeout>10</source-timeout>
    <burst-on-connect>1</burst-on-connect><burst-size>65535</burst-size></limits>
  <authentication><source-password>{SRC_PW}</source-password>
    <admin-user>admin</admin-user><admin-password>{ADMIN_PW}</admin-password></authentication>
  <hostname>127.0.0.1</hostname>
  <listen-socket><port>{PORT}</port><bind-address>127.0.0.1</bind-address></listen-socket>
  <fileserve>1</fileserve>
  <paths><logdir>{TMP}/log</logdir><webroot>{share("web")}</webroot><adminroot>{share("admin")}</adminroot></paths>
  <logging><accesslog>access.log</accesslog><errorlog>error.log</errorlog><loglevel>3</loglevel></logging>
  <security><chroot>0</chroot>{owner()}</security>
</icecast>
""")
server = None


def start_server():
    global server
    server = subprocess.Popen([ICECAST, "-c", str(TMP / "icecast.xml")], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
    if not wait(lambda: socket.create_connection(("127.0.0.1", PORT), timeout=1).close() or True, 15):
        print(server.stdout.read().decode(errors="replace") if server.poll() is not None else "")
        error_log()
        raise SystemExit("icecast didn't start")


def stop_server():
    server.terminate()
    server.wait(10)


def error_log():
    p = TMP / "log" / "error.log"
    if p.exists():
        print("   --- icecast error.log (last lines) ---")
        print("\n".join(p.read_text(errors="replace").splitlines()[-25:]))


def sources() -> dict:
    """Mount -> its entry in Icecast's status-json.xsl."""
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/status-json.xsl", timeout=5) as r:
        st = json.loads(r.read().decode("utf-8", "replace"))["icestats"]
    src = st.get("source") or []
    if isinstance(src, dict):
        src = [src]
    return {"/" + s["listenurl"].rsplit("/", 1)[-1]: s for s in src}


def listen(mount: str, secs=4) -> tuple[str, float, float]:
    """(codec, mean volume dB, dominant frequency Hz) of what a listener hears on that mount."""
    url = f"http://127.0.0.1:{PORT}{mount}"
    codec = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=codec_name", "-of", "csv=p=0", "-rw_timeout", "8000000", url],
                           capture_output=True, text=True, timeout=30).stdout.strip()
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-rw_timeout", "8000000", "-i", url, "-t", str(secs),
                          "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True, timeout=40).stdout
    import array
    import math
    a = array.array("h")
    a.frombytes(pcm[len(pcm) // 2 - (len(pcm) // 2) % 2:])     # the second half: past any start-up
    if not a:
        return codec, -120.0, 0.0
    rms = math.sqrt(sum(x * x for x in a) / len(a))
    db = 20 * math.log10(rms / 32768) if rms else -120.0
    crossings = sum(1 for i in range(1, len(a)) if (a[i - 1] < 0) != (a[i] < 0))
    return codec, db, crossings / 2 / (len(a) / 8000)


def engine(name, outs, **kw):
    d = TMP / name
    cfg = Config(station_name="Airlog Icecast Test", archive_dir=str(d / "arch"), grabs_dir=str(d / "grabs"),
                 spool_dir=str(d / "spool"), buffer_minutes=5, identify_songs=False, announce=False,
                 source_type="custom", custom_input_args=PROG, **kw)
    cfg.outputs = outputs.normalize_outputs(outs)
    e = Engine(cfg)
    e.on_log = lambda line, err: print(f"   [{name}] {line}") if err or "Output" in line else None
    return e


def out(mount, codec, kbps, **kw):
    return {"id": mount.strip("/"), "name": f"Icecast {mount}", "kind": "icecast", "host": "127.0.0.1",
            "port": PORT, "mount": mount, "password": SRC_PW, "codec": codec, "bitrate_kbps": kbps, **kw}


def states(e):
    return {o["id"]: o["state"] for o in e.outputs.status()}


start_server()
ver = subprocess.run([ICECAST, "-v"], capture_output=True, text=True).stdout.strip()
print(f"-- {ver or 'icecast'} on port {PORT}")
ok = False
try:
    print("-- four mounts: MP3 320, AAC 320, Ogg Vorbis 128, Opus 96")
    e = engine("A", [out("/mp3", "mp3", 320, stream_name="Airlog Test MP3", genre="Hospital radio",
                         description="Airlog test stream", url="https://example.org"),
                     out("/aac", "aac", 320), out("/ogg", "ogg", 128), out("/opus", "opus", 96)])
    e.start()
    want = {"/mp3", "/aac", "/ogg", "/opus"}
    if not wait(lambda: want <= set(sources()), 30):
        print("   outputs:", states(e), "\n   server has:", list(sources()))
        error_log()
        raise AssertionError("not every mount appeared on the server")
    s = sources()
    print("   on the server:", ", ".join(f"{m} ({s[m].get('server_type', '?')})" for m in sorted(s)))
    assert s["/mp3"].get("server_name") == "Airlog Test MP3", s["/mp3"]
    assert s["/mp3"].get("genre") == "Hospital radio", s["/mp3"]
    assert s["/mp3"].get("server_description") == "Airlog test stream", s["/mp3"]

    print("-- listening to each mount")
    expect = {"/mp3": "mp3", "/aac": "aac", "/ogg": "vorbis", "/opus": "opus"}
    for mount, codec in expect.items():
        got, db, hz = listen(mount)
        print(f"   {mount}: {got}, {db:.1f} dBFS, ~{hz:.0f} Hz")
        assert got == codec, (mount, got)
        assert db > -30 and 380 < hz < 500, (mount, db, hz)             # the 440 Hz programme tone

    print("-- song titles")
    e.tracks.begin(Track(start=time.time(), end=None, artist="Kraftwerk", title="Radioactivity", key="k1"))
    assert wait(lambda: sources()["/mp3"].get("title") == "Kraftwerk - Radioactivity"
                and sources()["/aac"].get("title") == "Kraftwerk - Radioactivity", 20), \
        {m: v.get("title") for m, v in sources().items()}
    print("   /mp3 and /aac:", sources()["/mp3"]["title"])

    print("-- wrong password: reported")
    bad = engine("B", [out("/bad", "mp3", 128, password="not-the-password")])
    bad.start()
    assert wait(lambda: bad.outputs.status()[0]["state"] == "error", 20), bad.outputs.status()[0]
    print("  ", bad.outputs.status()[0]["detail"])
    assert "/bad" not in sources()
    bad.stop()

    print("-- the server restarts: Airlog reconnects")
    stop_server()
    assert wait(lambda: any(st != "connected" and st != "sending" for st in states(e).values()), 20), states(e)
    print("   while it was down:", states(e))
    start_server()
    t = time.time()
    if not wait(lambda: want <= set(sources()), 90):
        print("   outputs:", states(e))
        error_log()
        raise AssertionError("didn't reconnect after the server came back")
    print(f"   all four mounts back {time.time() - t:.0f}s after the server")
    got, db, hz = listen("/mp3")
    assert db > -30 and 380 < hz < 500, (db, hz)
    e.stop()

    print("-- alternative input: a switched mount carries it, a programme mount doesn't")
    e = engine("C", [out("/web", "mp3", 128, feed="switch"), out("/stl", "mp3", 128, feed="programme")],
               alt_enabled=True, alt_mode="manual", alt_source_type="custom", alt_custom_input_args=ALT,
               alt_title="Normal service resumes after the match")
    e.start()
    assert wait(lambda: {"/web", "/stl"} <= set(sources()) and e.alt.status == "recording", 30), states(e)
    e.set_alt_override("alternative")
    assert wait(lambda: sources()["/web"].get("title") == "Normal service resumes after the match", 20), \
        sources()["/web"]
    time.sleep(3)                                     # let a listener's burst move past the switch
    _, db, web_hz = listen("/web")
    _, _, stl_hz = listen("/stl")
    print(f"   /web: ~{web_hz:.0f} Hz, title {sources()['/web']['title']!r}; /stl: ~{stl_hz:.0f} Hz")
    assert 900 < web_hz < 1100 and db > -40, (web_hz, db)                 # the 1 kHz alternative
    assert 380 < stl_hz < 500, stl_hz                                     # still the programme
    e.set_alt_override("programme")
    time.sleep(3)
    _, _, web_hz = listen("/web")
    print(f"   back to the programme: /web ~{web_hz:.0f} Hz")
    assert 380 < web_hz < 500, web_hz
    e.stop()
    ok = True
finally:
    if not ok:
        error_log()
    stop_server()
print("icecast OK")
