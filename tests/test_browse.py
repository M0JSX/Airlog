"""The folder browser in Settings: lists folders and audio files on the recorder, makes new folders,
and is only open to whoever may change settings.

    python tests/test_browse.py      (needs flask)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airlog.config import Config  # noqa: E402
from airlog.server import Holder, create_app  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="airlog_browse_"))
music = tmp / "Match day"
(music / "Old jingles").mkdir(parents=True)
(music / ".hidden").mkdir()
for n in ("02 Ident.WAV", "01 Back after the match.mp3", "notes.txt", ".DS_Store"):
    (music / n).write_bytes(b"x")
cfg = Config(archive_dir=str(tmp / "a"), grabs_dir=str(tmp / "g"), spool_dir=str(tmp / "s"),
             identify_songs=False, announce=False)
cfg.save(tmp / "config.json")
holder = Holder(Config.load(tmp / "config.json"), tmp / "config.json")
app = create_app(holder)
local = app.test_client()
local.environ_base["REMOTE_ADDR"] = "127.0.0.1"
remote = app.test_client()
remote.environ_base["REMOTE_ADDR"] = "192.168.1.50"

print("-- a folder: sub-folders and audio files, hidden ones left out")
r = local.get("/api/browse", query_string={"path": str(music)}).get_json()
assert r["path"] == str(music.resolve()) and r["parent"] == str(tmp.resolve()), r
assert r["dirs"] == ["Old jingles"], r["dirs"]
assert r["audio"] == ["01 Back after the match.mp3", "02 Ident.WAV"] and r["audio_count"] == 2, r
assert r["other_count"] == 1 and r["error"] == ""
assert any(x["label"] == "Home" for x in r["roots"])

print("-- blank starts at home; a folder that doesn't exist yet starts at the nearest one that does")
assert local.get("/api/browse").get_json()["path"] == str(Path.home().resolve())
r = local.get("/api/browse", query_string={"path": str(music / "not yet" / "made")}).get_json()
assert r["path"] == str(music.resolve()), r["path"]
r = local.get("/api/browse", query_string={"path": "/"}).get_json()
assert r["parent"] is None

print("-- new folders")
r = local.post("/api/browse/mkdir", json={"path": str(music), "name": "Cup run"})
assert r.status_code == 200 and Path(r.get_json()["path"]).is_dir()
for name, why in (("Cup run", "already a folder"), ("../escape", "can't be blank or contain"),
                  ("", "can't be blank"), ("a:b", "contain")):
    r = local.post("/api/browse/mkdir", json={"path": str(music), "name": name})
    assert r.status_code == 400 and why in r.get_json()["error"], (name, r.get_json())
assert not (tmp / "escape").exists()
assert local.post("/api/browse/mkdir", json={"path": str(tmp / "nope"), "name": "x"}).status_code == 400

print("-- only for whoever may change settings")
assert remote.get("/api/browse").status_code == 403
assert remote.post("/api/browse/mkdir", json={"path": str(music), "name": "x"}).status_code == 403
holder.engine.stop()
print("browse OK")
