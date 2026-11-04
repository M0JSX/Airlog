"""Output processing (compressor + limiter), measured on real audio.

1. Each preset is run over a test signal with quiet and loud passages and near-full-scale peaks;
   checks the level difference shrinks preset by preset and peaks stay under the ceiling.
2. End to end: Airlog A sends a loud tone through a UDP output limited to -12 dBFS; Airlog B
   receives it and must measure about -12 dBFS, while A's own recording stays unprocessed.

    python tests/test_processing.py
"""
import math
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airlog import outputs  # noqa: E402
from airlog.config import Config  # noqa: E402
from airlog.engine import Engine  # noqa: E402

SR = 48000
# 4 s cycle: 2 s quiet (-30 dBFS) then 2 s loud (-6 dBFS), with a 10 ms near-full-scale burst every second.
SIGNAL = ("aevalsrc='if(lt(mod(t,4),2),0.0316,0.5)*sin(2*PI*440*t)"
          "+if(lt(mod(t,1),0.01),0.45,0)*sin(2*PI*3000*t)':s=48000:d=16")


def run(af: str | None) -> array:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", SIGNAL]
    if af:
        cmd += ["-af", af]
    cmd += ["-ac", "1", "-f", "s16le", "-"]
    a = array("h")
    a.frombytes(subprocess.run(cmd, capture_output=True, check=True).stdout)
    return a


def db(x: float) -> float:
    return 20 * math.log10(max(x, 1e-9))


def rms(a: array, t1: float, t2: float) -> float:
    seg = a[int(t1 * SR):int(t2 * SR)]
    return db(math.sqrt(sum(v * v for v in seg) / len(seg)) / 32768)


def measure(a: array) -> tuple[float, float, float]:
    """(quiet dB, loud dB, sample peak dB) over the last two cycles, avoiding bursts and edges."""
    quiet = sum(rms(a, c + 1.1, c + 1.9) for c in (8, 12)) / 2
    loud = sum(rms(a, c + 3.1, c + 3.9) for c in (8, 12)) / 2
    peak = db(max(abs(v) for v in a[SR * 4:]) / 32768)
    return quiet, loud, peak


print("-- presets on a test signal (quiet -30 dBFS / loud -6 dBFS / peaks near 0 dBFS)")
print(f"   {'preset':10} {'quiet':>7} {'loud':>7} {'range':>7} {'peak':>7}")
ranges, louds, quiets = {}, {}, {}
for mode in outputs.PROCESSING:
    if mode == "custom":
        continue
    o = outputs.normalize_outputs([{"kind": "udp", "host": "h", "processing": mode, "ceiling_db": -1}])[0]
    q, l, pk = measure(run(outputs.process_filter(o)))
    ranges[mode], louds[mode], quiets[mode] = l - q, l, q
    print(f"   {mode:10} {q:7.1f} {l:7.1f} {l - q:7.1f} {pk:7.1f}")
    if mode != "off":
        assert pk <= -1.0 + 0.3, f"{mode}: peak {pk:.2f} dBFS over the -1 dBFS ceiling"
assert ranges["off"] > 23, ranges
assert abs(ranges["limiter"] - ranges["off"]) < 1.5, "the limiter alone shouldn't change the overall level balance"
assert ranges["off"] > ranges["gentle"] > ranges["broadcast"] > ranges["heavy"], ranges
assert ranges["heavy"] < 14, ranges
for mode in ("gentle", "broadcast", "heavy"):
    assert abs(louds[mode] - louds["off"]) < 2.5, f"{mode}: loud passages moved {louds[mode] - louds['off']:+.1f} dB"
    assert quiets[mode] > quiets["off"] + 2, f"{mode}: quiet passages weren't brought up"

print("-- custom settings")
o = outputs.normalize_outputs([{"kind": "udp", "host": "h", "processing": "custom", "ceiling_db": -3,
                                "comp_threshold_db": -30, "comp_ratio": 20, "comp_attack_ms": 1,
                                "comp_release_ms": 50, "comp_makeup_db": 0}])[0]
q, l, pk = measure(run(outputs.process_filter(o)))
print(f"   ratio 20 at -30 dB: quiet {q:.1f}, loud {l:.1f}, range {l - q:.1f}, peak {pk:.1f}")
assert l - q < 6 and pk <= -3 + 0.3

print("-- out-of-range values are pulled into range")
o = outputs.normalize_outputs([{"kind": "udp", "host": "h", "processing": "custom", "ceiling_db": -99,
                                "comp_ratio": 0, "comp_makeup_db": 99}])[0]
assert (o["ceiling_db"], o["comp_ratio"], o["comp_makeup_db"]) == (-24.0, 1.0, 36.0), o
assert outputs.normalize_outputs([{"kind": "udp", "host": "h", "processing": "bogus"}])[0]["processing"] == "off"

# ---------------------------------------------------------------- end to end
print("-- end to end: A's UDP output limited to -12 dBFS, received by B")
tmp = Path(tempfile.mkdtemp(prefix="airlog_proc_"))
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]


def eng(name, **kw):
    d = tmp / name
    return Engine(Config(station_name=name, archive_dir=str(d / "a"), grabs_dir=str(d / "g"), spool_dir=str(d / "s"),
                         identify_songs=False, web_port=1, **kw))


b = eng("B", source_type="receive", receive_protocol="udp", receive_port=port)
a = eng("A", source_type="custom", custom_input_args="-re -f lavfi -i sine=frequency=440:sample_rate=48000,volume=8",
        outputs=outputs.normalize_outputs([{"kind": "udp", "host": "127.0.0.1", "port": port, "codec": "mp2",
                                            "bitrate_kbps": 384, "processing": "limiter", "ceiling_db": -12}]))
b.start()
a.start()
try:
    def peak(e):
        lt = e.live_time()
        return max(e.peak_buckets(lt - 2, lt, 20)) / 255 if lt and e.status == "recording" else 0

    end = time.time() + 25
    while time.time() < end and peak(b) == 0:
        time.sleep(0.3)
    time.sleep(3)
    pa, pb = db(peak(a)), db(peak(b))
    print(f"   A records {pa:.1f} dBFS (unprocessed); B receives {pb:.1f} dBFS")
    assert pa > -5, "A's own recording should be unprocessed"
    assert -13.5 < pb < -10.5, f"B should be near the -12 dBFS ceiling, got {pb:.1f}"
    assert "limiter -12" in a.outputs.status()[0]["processing"]
finally:
    a.stop()
    b.stop()
    shutil.rmtree(tmp, ignore_errors=True)
print("processing OK")
