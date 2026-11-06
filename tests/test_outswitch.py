"""Switching single outputs on and off from the main page, while recording carries on.

    python tests/test_outswitch.py      (needs flask + ffmpeg)
"""
import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airlog.config import Config  # noqa: E402
from airlog.server import Holder, create_app  # noqa: E402


class Sink:
    """A UDP receiver counting what an output sends."""

    def __init__(self):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind(("127.0.0.1", 0))
        self.s.settimeout(0.2)
        self.port = self.s.getsockname()[1]
        self.bytes = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                self.bytes += len(self.s.recv(65536))
            except socket.timeout:
                pass
            except OSError:
                return


def wait(pred, secs):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


a, b = Sink(), Sink()
tmp = Path(tempfile.mkdtemp(prefix="airlog_outsw_"))
cfg = Config(station_name="SW", source_type="custom",
             custom_input_args="-re -f lavfi -i sine=frequency=440:sample_rate=48000",
             archive_dir=str(tmp / "arch"), grabs_dir=str(tmp / "grabs"), spool_dir=str(tmp / "spool"),
             autostart=False, identify_songs=False, announce=False,
             outputs=[{"id": "a", "name": "Out A", "kind": "udp", "host": "127.0.0.1", "port": a.port, "codec": "mp2"},
                      {"id": "b", "name": "Out B", "kind": "udp", "host": "127.0.0.1", "port": b.port, "codec": "mp2"},
                      {"id": "c", "name": "Not set up", "kind": "icecast", "enabled": False, "host": "", "port": 8000}])
cfg_path = tmp / "config.json"
cfg.save(cfg_path)
holder = Holder(Config.load(cfg_path), cfg_path)
c = create_app(holder).test_client()
e = holder.engine
e.start()
assert wait(lambda: a.bytes > 10_000 and b.bytes > 10_000, 15), (a.bytes, b.bytes, holder.engine.outputs.status())
engine_before = holder.engine


def state(oid):
    return next(o for o in c.get("/api/status").get_json()["outputs"] if o["id"] == oid)


def switch(oid, on, **kw):
    return c.post(f"/api/outputs/{oid}", json={"enabled": on}, **kw)


print("-- switch A off: A stops, B and the recording carry on")
r = switch("a", False)
assert r.status_code == 200, r.get_json()
assert state("a")["state"] == "off" and state("a")["enabled"] is False
time.sleep(0.5)
a0, b0 = a.bytes, b.bytes
time.sleep(2)
assert a.bytes == a0, "A still sending after being switched off"
assert b.bytes > b0 + 5_000, "B stopped too"
assert holder.engine is engine_before and e.running and e.status == "recording", "the recorder restarted"
assert json.loads(cfg_path.read_text())["outputs"][0]["enabled"] is False, "not saved"
print("   OK")

print("-- switch A back on")
assert switch("a", True).status_code == 200
assert wait(lambda: a.bytes > a0 + 5_000, 10), state("a")
assert state("a")["state"] == "sending"
assert json.loads(cfg_path.read_text())["outputs"][0]["enabled"] is True
print("   OK")

print("-- off and straight back on: one sender, not two")
switch("a", False)
switch("a", True)
assert wait(lambda: state("a")["state"] == "sending", 10)
time.sleep(1)
names = [t.name for t in threading.enumerate()]
assert names.count("output-a") == 1, names
print("   OK")

print("-- refused: an output that isn't set up, unknown ids, bad bodies, and other machines")
r = switch("c", True)
assert r.status_code == 400 and "address" in r.get_json()["error"], r.get_json()
assert json.loads(cfg_path.read_text())["outputs"][2]["enabled"] is False
assert switch("nope", True).status_code == 404
assert c.post("/api/outputs/a", json={"enabled": "yes"}).status_code == 400
r = switch("b", False, environ_base={"REMOTE_ADDR": "192.168.1.50"})
assert r.status_code == 403, r.status_code
assert state("b")["state"] == "sending"
print("   OK")

e.stop()
assert "Recording" in "\n".join(e.logs) and sum("Recording" == line.split("  ", 1)[-1] for line in e.logs) == 1, \
    [x for x in e.logs if "Recording" in x]
print("outswitch OK")
