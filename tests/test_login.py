"""One-time sign-in links used by Airlog Viewer, and the session cookie they set.

    python tests/test_login.py      (needs flask)
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airlog import server  # noqa: E402
from airlog.config import Config  # noqa: E402
from airlog.server import Holder, create_app  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="airlog_login_"))
cfg = Config(station_name="T", archive_dir=str(tmp), grabs_dir=str(tmp / "g"), spool_dir=str(tmp / "s"),
             autostart=False, identify_songs=False, web_password="s3cret")
holder = Holder(cfg, tmp / "config.json")
app = create_app(holder)
REMOTE = {"REMOTE_ADDR": "192.168.1.50"}


def client():
    return app.test_client()


c = client()
assert c.get("/api/status", environ_base=REMOTE).status_code == 401

# Wrong password: no token (and a deliberate delay).
t0 = time.time()
r = c.post("/api/login", json={"password": "nope"}, environ_base=REMOTE)
assert r.status_code == 403 and time.time() - t0 >= 0.9

# Right password -> single-use token -> cookie -> signed in, including admin rights.
tok = c.post("/api/login", json={"password": "s3cret"}, environ_base=REMOTE).get_json()["token"]
r = c.get(f"/login?t={tok}", environ_base=REMOTE)
assert r.status_code == 303 and r.headers["Location"] == "/"
sc = r.headers["Set-Cookie"]
assert "airlog_session=" in sc and "HttpOnly" in sc and "SameSite=Strict" in sc, sc
st = c.get("/api/status", environ_base=REMOTE)
assert st.status_code == 200 and st.get_json()["can_edit"] is True
assert c.post("/api/config", json={"station_name": "Viewer"}, environ_base=REMOTE).status_code == 200
assert holder.cfg.station_name == "Viewer"

# The token is single use, and a fresh browser without the cookie is still locked out.
c2 = client()
assert c2.get(f"/login?t={tok}", environ_base=REMOTE).status_code == 403
assert c2.get("/api/status", environ_base=REMOTE).status_code == 401
assert c2.get("/login?t=made-up", environ_base=REMOTE).status_code == 403

# Expired tokens are refused.
tok2 = c2.post("/api/login", json={"password": "s3cret"}, environ_base=REMOTE).get_json()["token"]
server.LOGIN_TOKEN_S, saved = -1, server.LOGIN_TOKEN_S
tok3 = c2.post("/api/login", json={"password": "s3cret"}, environ_base=REMOTE).get_json()["token"]
server.LOGIN_TOKEN_S = saved
assert c2.get(f"/login?t={tok3}", environ_base=REMOTE).status_code == 403
assert c2.get(f"/login?t={tok2}", environ_base=REMOTE).status_code == 303

# A forged or tampered cookie doesn't work.
c3 = client()
c3.set_cookie("airlog_session", f"{int(time.time()) + 999}.deadbeef")
assert c3.get("/api/status", environ_base=REMOTE).status_code == 401

# Changing the password signs every session out.
holder.cfg.web_password = "changed"
assert c.get("/api/status", environ_base=REMOTE).status_code == 401

# No password set: the viewer is told so and the link just redirects (no cookie).
holder.cfg.web_password = ""
r = client().post("/api/login", json={}, environ_base=REMOTE).get_json()
assert r == {"token": None, "password_required": False}, r
assert "Set-Cookie" not in client().get("/login", environ_base=REMOTE).headers

holder.engine.stop()
print("login OK")
