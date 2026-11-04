"""Who may change settings / control recording, and which files the web UI will serve.

    python tests/test_access.py      (needs flask)
"""
import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airlog.config import Config  # noqa: E402
from airlog.server import Holder, create_app  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="airlog_access_"))
(tmp / "grabs").mkdir()
(tmp / "grabs" / "clip.wav").write_bytes(b"RIFF")
(tmp / "grabs" / "notes.txt").write_text("secret")
cfg = Config(station_name="T", archive_dir=str(tmp), grabs_dir=str(tmp / "grabs"), spool_dir=str(tmp / "spool"),
             autostart=False, identify_songs=False)
holder = Holder(cfg, tmp / "config.json")
c = create_app(holder).test_client()

REMOTE = {"REMOTE_ADDR": "192.168.1.50"}
LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
PROXIED = {"REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_FOR": "203.0.113.9"}


def post_cfg(env, body, auth=None):
    h = {"Authorization": "Basic " + base64.b64encode(f"x:{auth}".encode()).decode()} if auth else {}
    return c.post("/api/config", json=body, environ_base=env, headers=h).status_code


# No password: the LAN can grab and listen but not reconfigure or stop the recorder.
assert c.get("/api/status", environ_base=REMOTE).get_json()["can_edit"] is False
assert c.get("/api/config", environ_base=REMOTE).get_json()["can_edit"] is False
assert post_cfg(REMOTE, {"station_name": "Hacked"}) == 403
assert c.post("/api/record", json={"action": "stop"}, environ_base=REMOTE).status_code == 403
assert post_cfg(PROXIED, {"station_name": "Hacked"}) == 403, "reverse proxy must not count as local"
assert holder.cfg.station_name == "T"
assert c.get("/files/grabs/clip.wav", environ_base=REMOTE).status_code == 200

# "Trusted network" with no password: the LAN may administer too.
holder.cfg.trusted_lan = True
assert c.get("/api/status", environ_base=REMOTE).get_json()["can_edit"] is True
assert post_cfg(REMOTE, {"station_name": "Trusted"}) == 200 and holder.cfg.station_name == "Trusted"
assert post_cfg(REMOTE, {"trusted_lan": False}) == 200
assert post_cfg(REMOTE, {"station_name": "Hacked"}) == 403

# ffmpeg_path is never accepted from the browser, even locally.
assert post_cfg(LOCAL, {"ffmpeg_path": "/bin/sh", "station_name": "Local"}) == 200
assert holder.cfg.ffmpeg_path == "ffmpeg" and holder.cfg.station_name == "Local"
assert "ffmpeg_path" not in c.get("/api/config", environ_base=LOCAL).get_json()["config"]

# Only audio files are served or deleted, wherever the folders point.
assert c.get("/files/grabs/notes.txt", environ_base=LOCAL).status_code == 404
assert c.delete("/api/grabs/notes.txt", environ_base=LOCAL).status_code == 404
assert c.get("/files/archive/config.json", environ_base=LOCAL).status_code == 404
assert c.get("/files/archive/grabs/../../etc/passwd", environ_base=LOCAL).status_code == 404
assert (tmp / "grabs" / "notes.txt").exists()

# With a password, signed-in users anywhere may administer; others get 401.
assert post_cfg(LOCAL, {"web_password": "pw"}) == 200
assert holder.cfg.web_password == "pw"
assert post_cfg(REMOTE, {"station_name": "X"}) == 401
assert post_cfg(REMOTE, {"station_name": "Remote"}, auth="pw") == 200
assert holder.cfg.station_name == "Remote"

# Outputs saved by an older version (no processing fields): Settings must come back complete and
# save again unchanged, rather than failing on fields the older version didn't have.
holder.cfg.web_password = ""
holder.cfg.outputs = [{"id": "o1", "name": "Old", "kind": "udp", "host": "127.0.0.1", "port": 5004,
                       "codec": "mp2", "bitrate_kbps": 256}]
got = c.get("/api/config", environ_base=LOCAL).get_json()["config"]["outputs"][0]
assert got["processing"] == "off" and got["ceiling_db"] == -1.0 and got["mb_bass_db"] == 0.0, got
assert post_cfg(LOCAL, {"outputs": [got]}) == 200
blank = dict(got, processing="mb_punchy", ceiling_db="", mb_bass_db="")
assert post_cfg(LOCAL, {"outputs": [blank]}) == 200
assert holder.cfg.outputs[0]["processing"] == "mb_punchy" and holder.cfg.outputs[0]["ceiling_db"] == -1.0

holder.engine.stop()
print("access OK")
