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


def tone(a: array, f: float, t1: float, t2: float) -> float:
    """Level of one frequency in [t1, t2), dBFS (Goertzel)."""
    seg = a[int(t1 * SR):int(t2 * SR)]
    k = 2 * math.cos(2 * math.pi * f / SR)
    s1 = s2 = 0.0
    for v in seg:
        s0 = v / 32768 + k * s1 - s2
        s2, s1 = s1, s0
    p = s1 * s1 + s2 * s2 - k * s1 * s2
    return db(2 * math.sqrt(max(p, 0)) / len(seg) / math.sqrt(2))


def run_src(src: str, af: str | None) -> array:
    global SIGNAL
    saved, SIGNAL = SIGNAL, src
    try:
        return run(af)
    finally:
        SIGNAL = saved


def measure(a: array) -> tuple[float, float, float]:
    """(quiet dB, loud dB, sample peak dB) over the last two cycles, avoiding bursts and edges."""
    quiet = sum(rms(a, c + 1.1, c + 1.9) for c in (8, 12)) / 2
    loud = sum(rms(a, c + 3.1, c + 3.9) for c in (8, 12)) / 2
    peak = db(max(abs(v) for v in a[SR * 4:]) / 32768)
    return quiet, loud, peak


print("-- presets on a test signal (quiet -30 dBFS / loud -6 dBFS / peaks near 0 dBFS)")
print(f"   {'preset':10} {'quiet':>7} {'loud':>7} {'range':>7} {'peak':>7}")
ranges, louds, quiets, peaks = {}, {}, {}, {}
for mode in outputs.PROCESSING:
    if mode == "custom":
        continue
    o = outputs.normalize_outputs([{"kind": "udp", "host": "h", "processing": mode, "ceiling_db": -1}])[0]
    q, l, pk = measure(run(outputs.process_filter(o)))
    ranges[mode], louds[mode], quiets[mode], peaks[mode] = l - q, l, q, pk
    print(f"   {mode:10} {q:7.1f} {l:7.1f} {l - q:7.1f} {pk:7.1f}")
    if mode != "off":
        assert pk <= -1.0 + 0.3, f"{mode}: peak {pk:.2f} dBFS over the -1 dBFS ceiling"
assert ranges["off"] > 23, ranges
assert abs(ranges["limiter"] - ranges["off"]) < 1.5, "the limiter alone shouldn't change the overall level balance"
assert ranges["off"] > ranges["gentle"] > ranges["broadcast"] > ranges["heavy"], ranges
for mode in outputs.MB_PRESETS:
    assert peaks[mode] <= -1.0 + 0.3, (mode, peaks[mode])
assert ranges["heavy"] < 14, ranges
for mode in ("gentle", "broadcast", "heavy"):
    assert abs(louds[mode] - louds["off"]) < 2.5, f"{mode}: loud passages moved {louds[mode] - louds['off']:+.1f} dB"
    assert quiets[mode] > quiets["off"] + 2, f"{mode}: quiet passages weren't brought up"

# ---------------------------------------------------------------- multiband
# Treble at -24 dBFS all the time; a 60 Hz bass note at -6 dBFS on for 2 s, off for 2 s. A wideband
# compressor turns everything down when the bass comes in ("pumping"); multiband shouldn't.
PUMP = "aevalsrc='0.063*sin(2*PI*4000*t)+if(gte(mod(t,4),2),0.5,0)*sin(2*PI*60*t)':s=48000:d=20"
# Four quiet tones, one per band, to check the tonal balance and the band trims.
BANDS = "aevalsrc='0.03*(sin(2*PI*60*t)+sin(2*PI*400*t)+sin(2*PI*2000*t)+sin(2*PI*8000*t))':s=48000:d=6"
FREQS = (60, 400, 2000, 8000)


def pump_dip(af):
    b = run_src(PUMP, af)
    off = sum(tone(b, 4000, c + 0.8, c + 1.8) for c in (12, 16)) / 2
    on = sum(tone(b, 4000, c + 2.8, c + 3.8) for c in (12, 16)) / 2
    return on - off


def band_levels(af):
    b = run_src(BANDS, af)
    return [tone(b, f, 3, 5) for f in FREQS]


def mb(mode, **kw):
    return outputs.normalize_outputs([{"kind": "udp", "host": "h", "processing": mode, "ceiling_db": -1, **kw}])[0]


print("-- multiband: treble dip when a loud bass note comes in (pumping)")
dips = {m: pump_dip(outputs.process_filter(mb(m))) for m in ("broadcast", "heavy", *outputs.MB_PRESETS)}
for m, d in dips.items():
    print(f"   {m:12} {d:+5.1f} dB")
assert dips["broadcast"] < -6 and dips["heavy"] < -6, "the wideband presets should show the problem"
for m in outputs.MB_PRESETS:
    assert dips[m] > -2.0, f"{m}: treble dips {dips[m]:.1f} dB when the bass comes in"
    assert louds[m] - louds["off"] > -2.5 and louds[m] - louds["off"] < 2.5, (m, louds[m])
    assert quiets[m] > quiets["off"] + 6, f"{m}: quiet passages weren't brought up"
assert ranges["mb_smooth"] > ranges["mb_punchy"] > ranges["mb_loud"], ranges

print("-- multiband: tonal balance and band trims (quiet tones at 60 / 400 / 2k / 8k Hz)")
ref = band_levels(None)
flat = [x - r for x, r in zip(band_levels(outputs.process_filter(mb("mb_punchy"))), ref)]
print("   punchy, no trims:     " + "  ".join(f"{f}Hz {d:+.1f}" for f, d in zip(FREQS, flat)))
assert max(flat) - min(flat) < 1.5, f"multiband changed the tonal balance: {flat}"
trim = [x - r for x, r in zip(band_levels(outputs.process_filter(mb("mb_punchy", mb_bass_db=6, mb_air_db=-6))), ref)]
print("   bass +6, air -6:      " + "  ".join(f"{f}Hz {d:+.1f}" for f, d in zip(FREQS, trim)))
rel = [t - f for t, f in zip(trim, flat)]
assert abs(rel[0] - 6) < 1.0 and abs(rel[3] + 6) < 1.0 and abs(rel[1]) < 1.0 and abs(rel[2]) < 1.0, rel
q0, l0, _ = measure(run(outputs.process_filter(mb("mb_smooth"))))
q1, l1, pk1 = measure(run(outputs.process_filter(mb("mb_smooth", mb_drive_db=6))))
print(f"   smooth, drive +6:     gap {l0 - q0:.1f} -> {l1 - q1:.1f} dB, peak {pk1:.1f}")
assert l1 - q1 < l0 - q0 - 1 and pk1 <= -0.7, "drive should make it denser without passing the ceiling"
assert "multiband punchy" in outputs.describe_processing(mb("mb_punchy"))

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
