"""The Simple view's setting and what it relies on from /api/status.

    python tests/test_simpleview.py      (needs flask)
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airlog.config import Config  # noqa: E402
from airlog.server import Holder, create_app  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="airlog_simple_"))
Config(archive_dir=str(tmp / "a"), grabs_dir=str(tmp / "g"), spool_dir=str(tmp / "s"),
       identify_songs=False, announce=False).save(tmp / "config.json")
holder = Holder(Config.load(tmp / "config.json"), tmp / "config.json")
cl = create_app(holder).test_client()
cl.environ_base["REMOTE_ADDR"] = "127.0.0.1"

st = cl.get("/api/status").get_json()
assert st["default_view"] == "full", st["default_view"]                 # nothing changes unless chosen
for k in ("status", "archive_enabled", "outputs", "failover", "alt", "silence_s", "silence_alert_s", "level",
          "source", "last_error", "server_time", "archive"):
    assert k in st, k                                                    # what the verdict and tiles read
r = cl.post("/api/config", json={"default_view": "sideways"})
assert r.status_code == 400 and "layout" in r.get_json()["error"], r.get_json()
assert cl.post("/api/config", json={"default_view": "simple", "archive_enabled": False}).status_code == 200
assert json.loads((tmp / "config.json").read_text())["default_view"] == "simple"
st = cl.get("/api/status").get_json()
assert st["default_view"] == "simple" and st["archive_enabled"] is False
remote = create_app(holder).test_client()
remote.environ_base["REMOTE_ADDR"] = "192.168.1.40"
assert remote.post("/api/config", json={"default_view": "full"}).status_code == 403   # a setting like any other
page = cl.get("/").get_data(as_text=True)
assert 'id="simple"' in page and 'id="viewbtn"' in page and "default_view" in page
from _finish import finish  # noqa: E402
finish(holder, "simple view OK")
