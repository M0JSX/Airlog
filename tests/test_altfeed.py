"""The alternative input for internet outputs (e.g. football commentary that can't go online).

Programme: a tone at 50% level. Alternative input: a tone at 12.5%. Every block each output receives
is measured, so the test checks what actually went out, not what the settings say:
  * a "switch" output carries the alternative while it's on; a "programme" output and the recording
    never do;
  * not one block of programme reaches a switched output after the switch, nor when the alternative
    input fails (silence instead), nor after a restart mid-match;
  * manual, schedule (with override) and permanent modes; a looped folder of mixed-format files;
    titles; the /api/alt endpoint and settings validation.

    python tests/test_altfeed.py      (needs flask + ffmpeg)
"""
import datetime as dt
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airlog import altfeed, outputs  # noqa: E402
from airlog.config import Config  # noqa: E402
from airlog.engine import Engine  # noqa: E402

PROG = '-re -f lavfi -i "sine=frequency=440:sample_rate=48000,volume=4"'      # peak ~0.50
ALT = '-re -f lavfi -i "sine=frequency=1000:sample_rate=48000,volume=1"'      # peak ~0.125


def peak(pcm: bytes) -> float:
    a = array("h")
    a.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    return max((abs(x) for x in a), default=0) / 32768


def kind(p: float) -> str:
    return "prog" if p > 0.3 else "alt" if p > 0.05 else "silence"


def wait(pred, secs):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


def sink():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    threading.Thread(target=lambda: [s.recv(65536) for _ in iter(int, 1)], daemon=True).start()
    return s.getsockname()[1]


def make_cfg(name, **kw):
    d = Path(tempfile.mkdtemp(prefix=f"airlog_alt_{name}_"))
    base = dict(station_name="HRR", source_type="custom", custom_input_args=PROG,
                archive_dir=str(d / "arch"), grabs_dir=str(d / "grabs"), spool_dir=str(d / "spool"),
                identify_songs=False, announce=False, alt_enabled=True, alt_source_type="custom",
                alt_custom_input_args=ALT,
                outputs=[{"id": "stl", "name": "STL", "kind": "udp", "host": "127.0.0.1", "port": sink(),
                          "codec": "mp2", "feed": "programme"},
                         {"id": "web", "name": "Web", "kind": "udp", "host": "127.0.0.1", "port": sink(),
                          "codec": "mp2", "feed": "switch"}])
    base.update(kw)
    cfg = Config(**base)
    cfg.outputs = outputs.normalize_outputs(cfg.outputs)
    return cfg


class Spy:
    """Records (time, kind) of every block each output is fed."""

    def __init__(self, e: Engine):
        self.got = {"stl": [], "web": []}
        for o in e.outputs.items:
            orig = o.feed

            def fed(pcm, oid=o.o["id"], orig=orig):
                self.got[oid].append((time.monotonic(), kind(peak(pcm))))
                orig(pcm)
            o.feed = fed

    def kinds(self, oid, t0, t1=None):
        t1 = t1 or time.monotonic()
        return [k for t, k in self.got[oid] if t0 <= t <= t1]


def recorded_kind(e: Engine) -> str:
    lt = e.live_time()
    return kind(peak(b"".join(e.spool.iter_range(lt - 1, lt))))


# ---------------------------------------------------------------- schedule logic
print("-- schedule: days, dates, past midnight, overrides, modes")
sched = altfeed.normalize_schedule([
    {"label": "Reading v Bolton", "days": "sat", "start": "14:55", "end": "17:10"},
    {"label": "Cup replay", "date": "2026-11-11", "start": "19:40", "end": "22:00"},
    {"label": "Overnight", "days": "Fri", "start": "23:00", "end": "01:00"}])
assert sched[0]["days"] == "Sat" and sched[1]["date"] == "2026-11-11"
c = Config(alt_enabled=True, alt_mode="schedule", alt_schedule=sched)
at = lambda s: dt.datetime.fromisoformat(s)  # noqa: E731
assert altfeed.decide(c, at("2026-11-07T15:00")) == (True, "schedule: Reading v Bolton")       # Saturday
assert altfeed.decide(c, at("2026-11-07T17:10"))[0] is False                                   # end is exclusive
assert altfeed.decide(c, at("2026-11-08T15:00"))[0] is False                                   # Sunday
assert altfeed.decide(c, at("2026-11-11T20:00"))[0] is True                                    # the date
assert altfeed.decide(c, at("2026-11-07T00:30"))[0] is True     # Friday 23:00 -> Saturday 01:00
assert altfeed.decide(c, at("2026-11-07T01:00"))[0] is False
c.alt_override = "programme"
assert altfeed.decide(c, at("2026-11-07T15:00")) == (False, "override")
c.alt_override = "alternative"
assert altfeed.decide(c, at("2026-11-08T15:00")) == (True, "override")
c.alt_mode, c.alt_override = "manual", ""
assert altfeed.decide(c)[0] is False
c.alt_override = "alternative"
assert altfeed.decide(c)[0] is True
c.alt_mode = "permanent"
c.alt_override = "programme"
assert altfeed.decide(c)[0] is True
c.alt_enabled = False
assert altfeed.decide(c)[0] is False
for bad, why in (([{"days": "Sat", "start": "25:00", "end": "17:00"}], "a time"),
                 ([{"days": "Funday", "start": "15:00", "end": "17:00"}], "isn't a day"),
                 ([{"days": "", "start": "15:00", "end": "17:00"}], "at least one day"),
                 ([{"date": "14/11/2026", "start": "15:00", "end": "17:00"}], "date must be"),
                 ([{"days": "Sat", "start": "15:00", "end": "15:00"}], "the same")):
    try:
        altfeed.normalize_schedule(bad)
        raise AssertionError(f"accepted {bad}")
    except ValueError as ex:
        assert why in str(ex), ex
d = altfeed.describe(Config(alt_enabled=True, alt_mode="schedule", alt_schedule=sched), at("2026-11-07T15:00"))
assert d["until"]["label"] == "Reading v Bolton" and d["until"]["at"].endswith("17:10"), d
assert d["next"]["label"] == "Cup replay" and d["next"]["start"] == "2026-11-11T19:40", d   # Wed, before Fri
print("   OK")

# ---------------------------------------------------------------- manual switching, with real audio
print("-- manual: the switched output carries the alternative; the STL and the recording never do")
e = Engine(make_cfg("manual"))
spy = Spy(e)
e.start()
assert wait(lambda: e.status == "recording" and e.alt.status == "recording", 20), list(e.logs)
time.sleep(2)
t = time.monotonic()
time.sleep(1.5)
assert set(spy.kinds("web", t)) == {"prog"} and set(spy.kinds("stl", t)) == {"prog"}, spy.kinds("web", t)

e.set_alt_override("alternative")
t_switch = time.monotonic()
time.sleep(2.5)
web, stl = spy.kinds("web", t_switch), spy.kinds("stl", t_switch)
print(f"   after switching: web {sorted(set(web))}, stl {sorted(set(stl))}, recording {recorded_kind(e)}")
assert "prog" not in web, f"programme reached the switched output after the switch: {web}"
assert web.count("alt") >= 15 and set(stl) == {"prog"} and recorded_kind(e) == "prog"
assert e.outputs.status()[1]["on_alt"] and not e.outputs.status()[0]["on_alt"]
assert "now carrying the ALTERNATIVE input (manual)" in "\n".join(e.logs)

print("-- the alternative input fails while switched: silence on the switched output, never the programme")
kid = subprocess.run(["pgrep", "-f", "frequency=1000"], capture_output=True, text=True).stdout.split()
assert kid, "couldn't find the alternative input's ffmpeg"
t = time.monotonic()
os.kill(int(kid[0]), signal.SIGSTOP)                  # stalled, like a stream that stops delivering
time.sleep(6)                                         # silence after 1 s; the log line after 3 s more
web = spy.kinds("web", t)
os.kill(int(kid[0]), signal.SIGCONT)
print(f"   while it was stalled: web got {web.count('silence')} silent blocks, {web.count('prog')} programme")
assert "prog" not in web and web.count("silence") >= 15, web
assert "No audio from the alternative input: internet outputs are sending silence" in "\n".join(e.logs)
assert set(spy.kinds("stl", t)) == {"prog"}

print("-- back to the programme")
e.set_alt_override("programme")
t = time.monotonic()
time.sleep(1.5)
assert set(spy.kinds("web", t + 0.2)) == {"prog"}, spy.kinds("web", t)
e.stop()
print("   OK")

print("-- restart mid-match: the switched output never gets a moment of the programme")
e = Engine(make_cfg("restart", alt_override="alternative"))
spy = Spy(e)
e.start()
assert wait(lambda: e.alt.status == "recording" and e.status == "recording", 20)
time.sleep(3)
web = spy.kinds("web", 0)
print(f"   from start-up: {web.count('silence')} silent blocks (while the inputs started), "
      f"{web.count('alt')} alternative, {web.count('prog')} programme")
assert "prog" not in web and "alt" in web and "prog" in spy.kinds("stl", 0)
e.stop()

print("-- schedule mode, with the button as an override; permanent mode")
now = dt.datetime.now()
in_window = [{"label": "Match", "days": altfeed.DAYS[now.weekday()],
              "start": (now - dt.timedelta(minutes=5)).strftime("%H:%M"),
              "end": (now + dt.timedelta(minutes=30)).strftime("%H:%M")}]
e = Engine(make_cfg("sched", alt_mode="schedule", alt_schedule=altfeed.normalize_schedule(in_window)))
spy = Spy(e)
e.start()
assert wait(lambda: e.alt_on and e.alt.status == "recording", 20)
assert e.alt_why == "schedule: Match"
e.set_alt_override("programme")
assert not e.alt_on
e.set_alt_override("")
assert e.alt_on
st = e.alt_status()
assert st["until"]["label"] == "Match" and st["input"]["status"] == "recording", st
e.stop()
e = Engine(make_cfg("perm", alt_mode="permanent"))
e.start()
assert e.alt_on and e.alt_why == "permanent"
e.stop()
print("   OK")

# ---------------------------------------------------------------- files
print("-- a looped folder of mixed-format files, and titles")
fdir = Path(tempfile.mkdtemp(prefix="airlog_altfiles_"))
for name, fmt, rate in (("01 Back after the match", "mp3", 44100), ("02 Station ident", "wav", 22050)):
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=700:sample_rate={rate}",
                    "-t", "2", "-ac", "1", f"{fdir}/{name}.{fmt}"], check=True)
(fdir / "notes.txt").write_text("not audio")
e = Engine(make_cfg("files", alt_source_type="files", alt_files_dir=str(fdir), alt_override="alternative"))
spy = Spy(e)
e.start()
assert wait(lambda: e.alt.status == "recording", 20), list(e.logs)
seen = set()
end = time.time() + 7
while time.time() < end:
    if e.alt.playing:
        seen.add(e.alt.playing)
    time.sleep(0.1)
web = spy.kinds("web", 0)
print(f"   played: {sorted(seen)}; web got {web.count('alt')} alternative blocks, {web.count('prog')} programme")
assert seen == {"01 Back after the match", "02 Station ident"}, seen          # both, and it looped
assert web.count("alt") > 40 and "prog" not in web
assert "files in" in e.alt.describe() and "(2 files)" in e.alt.describe()
web_out = next(o for o in e.outputs.items if o.o["id"] == "web")
stl_out = next(o for o in e.outputs.items if o.o["id"] == "stl")
assert web_out.title_now() in seen and stl_out.title_now() == "HRR"
e.cfg.alt_title = "Commentary on our hospital service only"
assert web_out.title_now() == "Commentary on our hospital service only"
e.stop()
print("   OK")

# ---------------------------------------------------------------- API and settings
print("-- /api/alt and settings validation")
from airlog.server import Holder, create_app  # noqa: E402
cfg = make_cfg("api")
tmp = Path(tempfile.mkdtemp(prefix="airlog_altapi_"))
cfg.save(tmp / "config.json")
holder = Holder(Config.load(tmp / "config.json"), tmp / "config.json")
cl = create_app(holder).test_client()
r = cl.post("/api/alt", json={"state": "alternative"})
assert r.status_code == 200 and r.get_json()["alt"]["on"], r.get_json()
assert json.loads((tmp / "config.json").read_text())["alt_override"] == "alternative"
assert cl.get("/api/status").get_json()["alt"]["on"] is True
assert cl.post("/api/alt", json={"state": "auto"}).status_code == 400            # manual mode: no schedule
assert cl.post("/api/alt", json={"state": "sideways"}).status_code == 400
assert cl.post("/api/alt", json={"state": "programme"},
               environ_base={"REMOTE_ADDR": "192.168.1.50"}).status_code == 403
for change, why in (({"alt_schedule": [{"days": "Sat", "start": "3pm", "end": "5pm"}]}, "time like"),
                    ({"alt_mode": "schedule", "alt_schedule": []}, "at least one scheduled"),
                    ({"alt_source_type": "files", "alt_files_dir": ""}, "folder of audio files"),
                    ({"alt_source_type": "receive", "alt_receive_protocol": "udp", "alt_receive_port": 9000,
                      "source_type": "receive", "receive_protocol": "udp", "receive_port": 9000}, "same port")):
    r = cl.post("/api/config", json=change)
    assert r.status_code == 400 and why in r.get_json()["error"], (change, r.get_json())
r = cl.post("/api/config", json={"alt_mode": "schedule", "alt_schedule": in_window})
assert r.status_code == 200, r.get_json()
saved = json.loads((tmp / "config.json").read_text())
assert saved["alt_schedule"][0]["label"] == "Match" and saved["alt_override"] == "alternative"
holder.engine.stop()
print("altfeed OK")
