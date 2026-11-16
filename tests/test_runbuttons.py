"""Separate Start/Stop Recording and Start/Stop Streaming buttons.

Real engine, a UDP output to a local socket (to see whether audio is going out) and the archive folder
(to see whether it's recording). Each switches live while the other carries on; both off stops the
recorder; the choice survives a restart; turning the option off puts streaming back on.

    python tests/test_runbuttons.py      (needs flask + ffmpeg)
"""
import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airlog import outputs  # noqa: E402
from airlog.config import Config  # noqa: E402
from airlog.server import Holder, create_app  # noqa: E402

got = []                                   # arrival times of UDP packets from the output
sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sink.bind(("127.0.0.1", 0))
threading.Thread(target=lambda: [got.append(time.monotonic()) for _ in iter(lambda: sink.recv(65536), b"")],
                 daemon=True).start()


def streaming(window=1.5):
    time.sleep(window)
    return any(t > time.monotonic() - 1.0 for t in got)


def wait(pred, secs=15):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return pred()


tmp = Path(tempfile.mkdtemp(prefix="airlog_run_"))
arch = tmp / "arch"
cfg = Config(station_name="Run", source_type="custom",
             custom_input_args="-re -f lavfi -i sine=frequency=440:sample_rate=48000",
             archive_dir=str(arch), archive_pattern="{station}_%H-%M-%S", grabs_dir=str(tmp / "g"),
             spool_dir=str(tmp / "s"), buffer_minutes=5, identify_songs=False, announce=False, split_controls=True,
             outputs=outputs.normalize_outputs([{"id": "u", "name": "UDP", "kind": "udp", "host": "127.0.0.1",
                                                 "port": sink.getsockname()[1], "codec": "mp2"}]))
cfg.save(tmp / "config.json")
holder = Holder(Config.load(tmp / "config.json"), tmp / "config.json")
cl = create_app(holder).test_client()
cl.environ_base["REMOTE_ADDR"] = "127.0.0.1"


def run(what, on):
    r = cl.post("/api/run", json={"what": what, "on": on})
    assert r.status_code == 200, r.get_json()


def files():
    return sorted(arch.rglob("*.mp3")) if arch.exists() else []


def recording(window=1.5):
    """The recorder is writing archive audio now (the encoder buffers, so file sizes lag)."""
    a = holder.engine.archive
    n = a.frames if a else 0
    time.sleep(window)
    b = holder.engine.archive
    return b is not None and (b is not a or b.frames > n)


def status():
    return cl.get("/api/status").get_json()


holder.engine.start()
assert wait(lambda: holder.engine.status == "recording" and files(), 20), list(holder.engine.logs)
assert streaming() and recording()
st = status()
assert st["split_controls"] and st["streaming"] and st["archive_enabled"]
print("-- both on: recording and streaming")

run("streaming", False)
time.sleep(1)
assert not streaming(), "still sending after Stop Streaming"
assert recording() and holder.engine.running, "recording should carry on"
assert status()["outputs"][0]["state"] == "off" and status()["streaming"] is False
assert json.loads((tmp / "config.json").read_text())["streaming_enabled"] is False
print("-- Stop Streaming: outputs stop, recording carries on")

first = files()[-1]
run("recording", False)
assert wait(lambda: not holder.engine.running, 10), "both off should stop the recorder"
assert holder.engine.archive is None and status()["status"] == "stopped"
print("-- Stop Recording too: the recorder stops")

run("streaming", True)
assert wait(lambda: holder.engine.running and holder.engine.status == "recording", 20)
assert wait(lambda: streaming(0.5), 15), "Start Streaming didn't send"
assert files() == [first], f"streaming only shouldn't record: {files()}"
print("-- Start Streaming alone: sending, no archive file")

time.sleep(1.1)                                   # a different second, so the new file gets its own name
run("recording", True)
assert wait(lambda: len(files()) == 2, 10), files()
assert recording() and streaming()
print("-- Start Recording: a new file from now, streaming carried on:", files()[-1].name)

print("-- a restart comes back the same way")
run("streaming", False)
holder.restart(True)
assert wait(lambda: holder.engine.status == "recording", 20)
time.sleep(1)
assert not streaming() and holder.engine.cfg.archive_enabled
run("recording", False)
assert wait(lambda: not holder.engine.running, 10)
holder.restart(True)                              # autostart with both off: stays stopped
time.sleep(2)
assert not holder.engine.running, "both off: shouldn't start on its own"

print("-- turning the option off puts streaming back on; the API refuses then")
assert cl.post("/api/config", json={"split_controls": False, "archive_enabled": True}).status_code == 200
assert wait(lambda: holder.engine.running and holder.engine.status == "recording", 25), list(holder.engine.logs)[-5:]
assert holder.cfg.streaming_enabled is True and wait(lambda: streaming(0.5), 15)
r = cl.post("/api/run", json={"what": "streaming", "on": False})
assert r.status_code == 400 and "Separate" in r.get_json()["error"]
remote = create_app(holder).test_client()
remote.environ_base["REMOTE_ADDR"] = "192.168.1.9"
assert remote.post("/api/run", json={"what": "recording", "on": False}).status_code == 403
assert cl.post("/api/run", json={"what": "both", "on": True}).status_code == 400
names = [f.name for f in files()]
assert not any(n.endswith("_2.mp3") for n in names), "a stub file: " + str(names) + "\n" + "\n".join(l for l in holder.engine.logs if any(w in l for w in ("Archive", "Gap", "Recording", "Streaming", "faster", "Source:")))
print("   files:", names)
from _finish import finish  # noqa: E402
finish(holder, "run buttons OK")
