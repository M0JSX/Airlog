"""Backup source and automatic failover, end to end with real ffmpeg.

The main source is a UDP input fed by a separate ffmpeg "sender" this test starts and stops; the
backup is a tone at a different level, so the recording itself shows which source was on air.

    python tests/test_failover.py        (~1.5 minutes)
"""
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airlog.config import Config  # noqa: E402
from airlog.engine import Engine  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="airlog_failover_"))
# Mono test tones are panned to stereo at -3 dB: main (sine at 0.5) ~90/255, backup (1/8) ~23/255.
MAIN_PEAK = 90
BACKUP_PEAK = 23


def free_udp() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Sender:
    """ffmpeg sending a tone (or silence) to the main source's UDP port."""

    def __init__(self, port: int):
        self.port, self.p = port, None

    def start(self, silent=False):
        self.stop()
        src = "anullsrc=r=48000:cl=stereo" if silent else "sine=frequency=440:sample_rate=48000,volume=4"
        self.p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", src,
                                   "-c:a", "mp2", "-b:a", "256k", "-f", "mpegts",
                                   f"udp://127.0.0.1:{self.port}?pkt_size=1316"],
                                  stdin=subprocess.DEVNULL)

    def stop(self):
        if self.p and self.p.poll() is None:
            self.p.kill()
            self.p.wait()
        self.p = None


def make(name, port, **kw) -> Engine:
    d = TMP / name
    cfg = Config(station_name=name, source_type="receive", receive_protocol="udp", receive_port=port,
                 archive_dir=str(d / "arch"), grabs_dir=str(d / "grabs"), spool_dir=str(d / "spool"),
                 buffer_minutes=5, identify_songs=False, web_port=1,
                 backup_enabled=True, backup_source_type="custom",
                 backup_custom_input_args="-re -f lavfi -i sine=frequency=880:sample_rate=48000",
                 failover_after_s=3, failover_silence_s=4, failover_return_s=5, **kw)
    e = Engine(cfg)
    e.on_log = lambda line, err: print("   ", line) if ("BACKUP" in line or "main source" in line) else None
    return e


def wait(pred, secs) -> bool:
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return pred()


def level(e: Engine, secs=1.5) -> int:
    lt = e.live_time()
    return max(e.peak_buckets(lt - secs, lt, 15)) if lt else 0


def longest_silence(e: Engine, t1: float, t2: float) -> float:
    """Longest run of silent 100 ms blocks in [t1, t2), in seconds."""
    times, vals = e.peaks_between(t1, t2)
    run = best = 0
    for v in vals:
        run = run + 1 if v < 5 else 0
        best = max(best, run)
    return best / 10


port = free_udp()
snd = Sender(port)
snd.start()
e = make("A", port)
e.start()
try:
    print("-- main on air")
    assert wait(lambda: e.status == "recording" and level(e) > 70, 15), (e.status, level(e))
    assert e.active is e.main and wait(lambda: e.backup.status == "recording", 10)
    assert abs(level(e) - MAIN_PEAK) < 15, level(e)
    print(f"   OK (level {level(e)}; backup standing by: {e.backup.status})")

    print("-- main drops out -> backup within failover_after_s, and no hole in the recording")
    t_drop = e.live_time()
    snd.stop()
    assert wait(lambda: e.active is e.backup, 8), "no failover"
    assert e.switches[-1]["to"] == "backup" and "no audio" in e.switches[-1]["reason"], e.switches[-1]
    assert wait(lambda: abs(level(e) - BACKUP_PEAK) < 8, 5), level(e)
    time.sleep(2)
    hole = longest_silence(e, t_drop - 1, e.live_time())
    print(f"   OK ({e.switches[-1]['reason']}; longest silence around the switch {hole:.1f}s)")
    assert hole <= 1.0, f"{hole:.1f}s of silence at the switch"

    print("-- main back briefly (shorter than failover_return_s) -> stays on backup")
    snd.start()
    time.sleep(2.5)
    snd.stop()
    time.sleep(4)
    assert e.active is e.backup and len(e.switches) == 1, list(e.switches)
    print("   OK (no flapping)")

    print("-- main back for good -> returns after failover_return_s")
    snd.start()
    t_back = time.monotonic()
    assert wait(lambda: e.active is e.main, 15), "never switched back"
    waited = time.monotonic() - t_back
    assert waited >= 4.5, f"switched back after only {waited:.1f}s"
    assert wait(lambda: abs(level(e) - MAIN_PEAK) < 15, 5), level(e)
    print(f"   OK (back on main after {waited:.1f}s: {e.switches[-1]['reason']})")

    print("-- main goes silent (dead air) -> backup after failover_silence_s")
    snd.start(silent=True)
    assert wait(lambda: e.active is e.backup, 12), "no failover on silence"
    assert "silent" in e.switches[-1]["reason"], e.switches[-1]
    print(f"   OK ({e.switches[-1]['reason']})")
    snd.start()
    assert wait(lambda: e.active is e.main, 15)
    print("   OK (back when the sound returned)")

    st = e.failover_status()
    assert st["active"] == "main" and len(st["switches"]) == 4, st
    assert st["backup"]["status"] == "recording"
finally:
    e.stop()
    snd.stop()

print("-- main never connects at start -> backup")
port2 = free_udp()
e2 = make("B", port2)
e2.start()
try:
    assert wait(lambda: e2.active is e2.backup, 20), (e2.main.status, e2.backup.status)
    assert e2.status == "recording"
    print(f"   OK ({e2.switches[-1]['reason']})")
finally:
    e2.stop()

print("-- no backup configured: unchanged behaviour")
e3 = Engine(Config(station_name="C", source_type="custom",
                   custom_input_args="-re -f lavfi -i sine=frequency=440:sample_rate=48000",
                   archive_dir=str(TMP / "C/arch"), grabs_dir=str(TMP / "C/g"), spool_dir=str(TMP / "C/s"),
                   identify_songs=False))
e3.start()
assert wait(lambda: e3.status == "recording", 10) and e3.backup is None and e3.failover_status() is None
e3.stop()
print("   OK")

shutil.rmtree(TMP, ignore_errors=True)
print("failover OK")
