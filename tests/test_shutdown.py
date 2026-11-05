"""Stopping the service: no false "source dropped", no retry, no stub archive file, no tracebacks.

Runs the real service (python -m airlog --service) and stops it the two ways it gets stopped:
  * SIGTERM to the whole process group (systemd without KillMode=mixed, or Ctrl+C in a terminal):
    the ffmpeg children die at the same moment as Airlog itself;
  * SIGTERM to Airlog only (systemd with KillMode=mixed, launchd).
And checks a genuine dropout while running is still noticed and recovered.

    python tests/test_shutdown.py          (Linux/macOS; needs flask + waitress + ffmpeg)
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from airlog import unixservice  # noqa: E402

if sys.platform.startswith("win"):
    print("shutdown: skipped on Windows")
    sys.exit(0)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(name):
    d = Path(tempfile.mkdtemp(prefix=f"airlog_sd_{name}_"))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        udp = s.getsockname()[1]
    cfg = {"station_name": "SD", "source_type": "custom",
           "custom_input_args": "-re -f lavfi -i sine=frequency=440:sample_rate=48000",
           "archive_dir": str(d / "arch"), "grabs_dir": str(d / "grabs"), "spool_dir": str(d / "spool"),
           "web_port": free_port(), "identify_songs": False, "announce": False,
           "outputs": [{"id": "o1", "name": "UDP", "kind": "udp", "host": "127.0.0.1", "port": udp, "codec": "mp2"}]}
    (d / "config.json").write_text(json.dumps(cfg))
    log = open(d / "log.txt", "w")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])),
               PYTHONUNBUFFERED="1")
    p = subprocess.Popen([sys.executable, "-m", "airlog", "--service", "--config", str(d / "config.json")],
                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                         start_new_session=True)              # its own process group, like a service
    deadline = time.time() + 20
    while time.time() < deadline and "sending" not in (d / "log.txt").read_text():
        time.sleep(0.2)
    assert "Recording" in (d / "log.txt").read_text(), (d / "log.txt").read_text()
    time.sleep(3)
    return p, d


def finish(p, d, how):
    try:
        p.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        raise AssertionError(f"{how}: the service didn't exit")
    text = (d / "log.txt").read_text()
    after = text.split("Service stopping", 1)[1] if "Service stopping" in text else ""
    files = sorted((d / "arch").rglob("*.mp3"))
    print(f"   exit {p.returncode}; {len(files)} archive file(s); log after 'Service stopping': "
          f"{[line for line in after.splitlines() if line.strip()] or 'nothing'}")
    assert "Service stopping" in text, text
    for bad in ("Source ended", "retrying", "reconnecting", "connection lost", "Traceback", "Bad file descriptor",
                "Archive: started"):
        assert bad not in after, f"{how}: '{bad}' while stopping:\n{after}"
    assert len(files) == 1, f"{how}: {[f.name for f in files]} (a stub file was started while stopping)"
    errs = subprocess.run(["ffmpeg", "-v", "error", "-i", str(files[0]), "-f", "null", "-"],
                          capture_output=True, text=True).stderr.strip()
    assert not errs, f"{how}: archive file doesn't decode cleanly: {errs[:300]}"


print("-- stop by SIGTERM to the whole process group (ffmpeg dies with Airlog)")
p, d = start("group")
os.killpg(p.pid, signal.SIGTERM)
finish(p, d, "group SIGTERM")

print("-- stop by SIGTERM to Airlog only (systemd KillMode=mixed, launchd)")
p, d = start("main")
os.kill(p.pid, signal.SIGTERM)
finish(p, d, "SIGTERM")

print("-- a real dropout while running is still noticed and recovered")
p, d = start("dropout")
kids = subprocess.run(["pgrep", "-P", str(p.pid), "-f", "lavfi"], capture_output=True, text=True).stdout.split()
assert kids, "couldn't find the capture ffmpeg"
os.kill(int(kids[0]), signal.SIGKILL)
deadline = time.time() + 15
while time.time() < deadline and (d / "log.txt").read_text().count("Recording") < 2:
    time.sleep(0.2)
text = (d / "log.txt").read_text()
assert "Source ended" in text and text.count("Recording") >= 2, text
print("   OK (source ended, retried, recording again)")
os.kill(p.pid, signal.SIGTERM)
p.wait(timeout=20)

assert "KillMode=mixed" in unixservice._unit_text("nobody", ["/opt/airlog/Airlog"])
print("shutdown OK")
