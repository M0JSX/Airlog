"""The /switch page: commentators switch the internet outputs with their own password, from anywhere,
and can do nothing else.

    python tests/test_switchpage.py      (needs flask)
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airlog import outputs  # noqa: E402
from airlog.config import Config  # noqa: E402
from airlog.server import SWITCH_MAX_FAILS, Holder, create_app  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="airlog_switch_"))
cfg = Config(station_name="Hospital Radio Reading", archive_dir=str(tmp / "a"), grabs_dir=str(tmp / "g"),
             spool_dir=str(tmp / "s"), identify_songs=False, announce=False, alt_enabled=True,
             alt_source_type="files", alt_files_dir=str(tmp),
             outputs=outputs.normalize_outputs([{"id": "web", "name": "Web", "kind": "udp", "host": "127.0.0.1",
                                                 "port": 5990, "codec": "mp2", "feed": "switch"}]))
cfg.save(tmp / "config.json")
holder = Holder(Config.load(tmp / "config.json"), tmp / "config.json")
app = create_app(holder)


def client(ip):
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = ip
    return c


def saved():
    return json.loads((tmp / "config.json").read_text())


local = client("127.0.0.1")
phone = client("81.2.69.160")                  # a commentator's phone, on the internet

print("-- off until a switch password is set")
r = phone.get("/api/switch")
assert r.status_code == 401 and r.get_json() == {"signed_in": False, "available": False}, r.get_json()
assert phone.post("/api/switch/login", json={"password": "anything"}).status_code == 403
assert phone.get("/switch").status_code == 200 and b"Internet switch" in phone.get("/switch").data

print("-- settings: length, different from the web password, never sent back")
for pw, why in (("short", "at least 8"), ("same-as-web", "different from the web password")):
    r = local.post("/api/config", json={"switch_password": pw, "web_password": "same-as-web"})
    assert r.status_code == 400 and why in r.get_json()["error"], r.get_json()
r = local.post("/api/config", json={"switch_password": "kick-off-1530", "web_password": "studio-admin-99"})
assert r.status_code == 200, r.get_json()
time.sleep(0.5)                                # settings restart the recorder in the background
admin = client("192.168.1.20")
admin.post("/api/login", json={"password": "studio-admin-99"})
tok = admin.post("/api/login", json={"password": "studio-admin-99"}).get_json()["token"]
admin.get(f"/login?t={tok}")                   # main-page session cookie
assert admin.get("/api/config").get_json()["config"]["switch_password"] == "********"
assert saved()["switch_password"] == "kick-off-1530"

print("-- wrong passwords lock that address out; others are unaffected")
r = phone.get("/api/switch")
assert r.status_code == 401 and r.get_json()["available"] is True
t = time.time()
for i in range(SWITCH_MAX_FAILS):
    assert phone.post("/api/switch/login", json={"password": f"guess{i}"}).status_code == 403
r = phone.post("/api/switch/login", json={"password": "kick-off-1530"})
assert r.status_code == 429 and "Too many wrong passwords" in r.get_json()["error"], r.get_json()
print(f"   {SWITCH_MAX_FAILS} wrong guesses took {time.time() - t:.1f}s, then locked out (even the right one)")
assert "wrong passwords from 81.2.69.160" in "\n".join(holder.engine.logs)

print("-- signed in: switch, and nothing else")
comm = client("81.2.69.161")
r = comm.post("/api/switch/login", json={"password": "kick-off-1530"})
assert r.status_code == 200 and r.get_json()["station"] == "Hospital Radio Reading", r.get_json()
assert r.get_json()["alt"]["on"] is False
assert "HttpOnly" in r.headers["Set-Cookie"] and "SameSite=Strict" in r.headers["Set-Cookie"]
r = comm.post("/api/switch", json={"state": "alternative"})
assert r.status_code == 200 and r.get_json()["alt"]["on"] is True, r.get_json()
assert saved()["alt_override"] == "alternative"
assert "Switch page (81.2.69.161): internet outputs set to the alternative input" in "\n".join(holder.engine.logs)
assert holder.engine.outputs.status()[0]["on_alt"] is True
for path in ("/", "/api/status", "/api/config", "/api/log"):
    assert comm.get(path).status_code == 401, path
assert comm.post("/api/alt", json={"state": "programme"}).status_code == 401
assert comm.post("/api/config", json={"switch_password": ""}).status_code == 401
assert comm.post("/api/record", json={"action": "stop"}).status_code == 401
assert comm.post("/api/switch", json={"state": "auto"}).status_code == 400        # manual mode
r = comm.post("/api/switch", json={"state": "programme"})
assert r.status_code == 200 and r.get_json()["alt"]["on"] is False

print("-- the main page's sign-in works on the switch page too")
r = admin.post("/api/switch", json={"state": "alternative"})
assert r.status_code == 200 and r.get_json()["alt"]["on"] is True
assert client("81.2.69.162").post("/api/switch", json={"state": "programme"}).status_code == 401

print("-- changing the switch password signs everyone out of it")
assert admin.post("/api/config", json={"switch_password": "new-season-2027"}).status_code == 200
time.sleep(0.5)
assert comm.get("/api/switch").status_code == 401
assert comm.post("/api/switch", json={"state": "programme"}).status_code == 401
assert saved()["alt_override"] == "alternative"            # unchanged by the refused request

print("-- sign out; alternative input turned off")
comm.post("/api/switch/login", json={"password": "new-season-2027"})
assert comm.get("/api/switch").status_code == 200
comm.post("/api/switch/logout")
assert comm.get("/api/switch").status_code == 401
assert admin.post("/api/config", json={"alt_enabled": False}).status_code == 200
time.sleep(0.5)
comm.post("/api/switch/login", json={"password": "new-season-2027"})
r = comm.get("/api/switch")
assert r.status_code == 200 and r.get_json()["alt"] is None
assert comm.post("/api/switch", json={"state": "alternative"}).status_code == 400
holder.engine.stop()
print("switch page OK")
