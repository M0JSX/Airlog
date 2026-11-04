"""Sound card output, with a stand-in sound card (CI machines have none).

The fake card consumes audio in real time like a real one: it holds at most `latency` seconds and
makes writes wait for room. Checks playback, a fixed delay to the card, silence when the source
stalls, dropping a backlog, processing (real ffmpeg), unplug/replug, device lookup and messages.

    python tests/test_device.py
"""
import math
import sys
import tempfile
import threading
import time
import types
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------- a fake `sounddevice`
class PortAudioError(Exception):
    pass


class FakeStream:
    def __init__(self, samplerate, channels, dtype, device, latency, extra_settings=None):
        if FAKE.refuse_rate:
            raise PortAudioError("Error opening RawOutputStream: Invalid sample rate [PaErrorCode -9997]")
        self.sr, self.ch, self.latency, self.device = samplerate, channels, latency, device
        self.until = 0.0                  # monotonic time the queued audio runs out
        self.chunks = []                  # (play start, bytes)
        self.aborted = False
        FAKE.streams.append(self)

    def start(self):
        self.until = time.monotonic()

    def write(self, b):
        while self.until - time.monotonic() > self.latency:        # the card's buffer is full
            if self.aborted:
                raise PortAudioError("Stream aborted")
            time.sleep(0.003)
        if FAKE.unplugged:
            raise PortAudioError("Unanticipated host error [PaErrorCode -9999]")
        start = max(self.until, time.monotonic())
        self.until = start + len(b) / (2 * self.ch * self.sr)
        self.chunks.append((start, bytes(b)))

    @property
    def write_available(self):
        """Frames that fit in the card's buffer without waiting."""
        return int(max(0.0, self.latency - self.ahead()) * self.sr)

    def abort(self):
        self.aborted = True

    def stop(self):
        pass

    def close(self):
        pass

    def ahead(self):
        """Seconds of audio queued in the card right now."""
        return max(0.0, self.until - time.monotonic())


DEVICES = [
    {"name": "Built-in Output", "hostapi": 0, "max_output_channels": 2, "max_input_channels": 0},
    {"name": "USB Audio CODEC", "hostapi": 0, "max_output_channels": 2, "max_input_channels": 2},
    {"name": "Line In", "hostapi": 0, "max_output_channels": 0, "max_input_channels": 2},
    {"name": "USB Audio CODEC", "hostapi": 1, "max_output_channels": 2, "max_input_channels": 2},
]
FAKE = types.SimpleNamespace(streams=[], unplugged=False, refuse_rate=False)
sd = types.ModuleType("sounddevice")
sd.PortAudioError = PortAudioError
sd.RawOutputStream = FakeStream
sd.query_devices = lambda index=None: DEVICES[index] if index is not None else DEVICES
sd.query_hostapis = lambda i: {"name": ["ALSA", "Windows WASAPI"][i]}
sd.WasapiSettings = lambda auto_convert=False: ("wasapi", auto_convert)
sys.modules["sounddevice"] = sd

from airlog import outputs  # noqa: E402
from airlog.config import Config  # noqa: E402

SR, CH = 48000, 2
BLOCK = SR // 10


class FakeEngine:
    """Just what an Output needs from the engine."""

    def __init__(self):
        tmp = Path(tempfile.mkdtemp(prefix="airlog_dev_"))
        self.cfg = Config(spool_dir=str(tmp))
        self.sr, self.ch, self.block_frames, self.bpf = SR, CH, BLOCK, 2 * CH
        self.lines = []
        self.tracks = types.SimpleNamespace(current=lambda: None)

    def log(self, msg, error=False):
        self.lines.append(msg)


def tone_block(amp=0.5, f=440.0, n=[0]):
    a = array("h")
    for k in range(BLOCK):
        v = int(amp * 32767 * math.sin(2 * math.pi * f * (n[0] + k) / SR))
        a.extend((v, v))
    n[0] += BLOCK
    return a.tobytes()


def peak_db(chunks):
    a = array("h")
    for _, b in chunks:
        a.frombytes(b)
    pk = max((abs(v) for v in a), default=0)
    return 20 * math.log10(max(pk, 1) / 32768)


def wait(pred, secs):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


class Feeder:
    """Feeds the output one 100 ms block every 100 ms, like the engine, until paused."""

    def __init__(self, out, amp=0.5):
        self.out, self.amp, self.on, self.stop = out, amp, True, False
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        nxt = time.monotonic()
        while not self.stop:
            if self.on:
                self.out.feed(tone_block(self.amp))
            nxt += 0.1
            time.sleep(max(0, nxt - time.monotonic()))


def make(**kw):
    spec = outputs.normalize_outputs([{"kind": "device", "name": "Desk", **kw}])[0]
    return outputs.Output(FakeEngine(), spec)


# ---------------------------------------------------------------- settings and lookup
print("-- settings and device lookup")
o = outputs.normalize_outputs([{"kind": "device"}])[0]
assert o["host"] == "" and o["port"] == 0 and o["codec"] == "pcm" and o["buffer_ms"] == 200 and o["name"] == "Sound card 1"
assert outputs.normalize_outputs([{"kind": "device", "buffer_ms": 5}])[0]["buffer_ms"] == 20
devs, err = outputs.list_output_devices()
labels = [d["label"] for d in devs]
print("   listed:", labels)
assert not err and devs[0]["value"] == "" and len(devs) == 4 and not any("Line In" in x for x in labels)
assert outputs.resolve_device(sd, "USB Audio CODEC | Windows WASAPI")[0] == 3
assert outputs.resolve_device(sd, "USB Audio CODEC | Windows WASAPI")[2] == ("wasapi", True)   # rate conversion on
assert outputs.resolve_device(sd, "USB Audio CODEC | ALSA")[0] == 1
assert outputs.resolve_device(sd, "")[0] is None
try:
    outputs.resolve_device(sd, "Gone Card | ALSA")
    raise AssertionError("missing card accepted")
except outputs.OutputError as e:
    assert "not found" in str(e)
print("   OK")

# ---------------------------------------------------------------- playback, fixed delay
print("-- playback: real time, fixed delay to the card")
out = make(device="USB Audio CODEC | ALSA", buffer_ms=200)
out.start()
f = Feeder(out)
assert wait(lambda: out.state == "playing" and FAKE.streams, 5), (out.state, out.detail)
st = FAKE.streams[-1]
assert st.device == 1 and abs(st.latency - 0.2) < 1e-9
time.sleep(1)
startup_silence = out.underruns           # a moment of silence while the first audio arrives is fine
time.sleep(3)
aheads = []
for _ in range(20):
    aheads.append(st.ahead())
    time.sleep(0.1)
played = sum(len(b) for _, b in st.chunks) / (2 * CH * SR)
print(f"   {played:.1f}s played, card holds {min(aheads):.2f}-{max(aheads):.2f}s, peak {peak_db(st.chunks[-20:]):.1f} dBFS")
assert max(aheads) <= 0.2 + 0.11, aheads                 # never more than the buffer (+ one block) ahead
assert min(aheads) >= 0.08, aheads                       # and a cushion is kept, so jitter doesn't cause gaps
assert out.underruns == startup_silence, "gaps while the source was running steadily"
assert abs(peak_db(st.chunks[-20:]) - 20 * math.log10(0.5)) < 0.5
assert out.status()["state"] == "playing" and "USB Audio CODEC" in out.status()["detail"]

print("-- source stalls: the card gets silence, no glitch, no drift")
f.on = False
n0 = len(st.chunks)
time.sleep(1.5)
gap = st.chunks[n0:]
assert gap and all(max(array("h", b)) == 0 for _, b in gap[3:]), "should play silence while nothing arrives"
assert out.underruns >= 8, out.underruns
f.on = True
time.sleep(1.0)
assert peak_db(st.chunks[-5:]) > -7, "audio should resume"
print(f"   OK ({out.underruns} blocks of silence; audio resumed)")

print("-- a burst of backlog is dropped, the delay stays fixed")
for _ in range(15):
    out.feed(tone_block())
time.sleep(0.6)
print(f"   dropped {out.dropped} blocks; card holds {st.ahead():.2f}s")
assert out.dropped >= 8 and st.ahead() <= 0.31

print("-- unplugged, then plugged back in")
FAKE.unplugged = True
assert wait(lambda: out.state == "error", 5), out.state
print("   error:", out.detail)
assert "unplugged" in out.detail
FAKE.unplugged = False
assert wait(lambda: out.state == "playing" and len(FAKE.streams) >= 2, 10), (out.state, out.detail)
print("   OK (playing again on a new stream)")
f.stop = True
out.stop()

print("-- processing on a sound card output (limiter at -12 dBFS, real ffmpeg)")
out = make(processing="limiter", ceiling_db=-12)
out.start()
f = Feeder(out, amp=0.9)
assert wait(lambda: out.state == "playing", 5), (out.state, out.detail)
time.sleep(3)
pk = peak_db(FAKE.streams[-1].chunks[-15:])
print(f"   input -0.9 dBFS -> card {pk:.1f} dBFS")
assert -12.5 < pk < -11.3, pk
assert "limiter -12" in out.status()["processing"]
f.stop = True
out.stop()

print("-- clear messages")
FAKE.refuse_rate = True
out = make(device="Built-in Output | ALSA")
out.start()
assert wait(lambda: out.state == "error", 5)
print("   unsupported rate:", out.detail)
assert "48000 Hz" in out.detail and "try another entry" in out.detail
out.stop()
FAKE.refuse_rate = False

del sys.modules["sounddevice"]
real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__


def no_portaudio(name, *a, **k):
    if name == "sounddevice":
        raise OSError("PortAudio library not found")
    return real_import(name, *a, **k)


import builtins  # noqa: E402
builtins.__import__ = no_portaudio
try:
    devs, err = outputs.list_output_devices()
finally:
    builtins.__import__ = real_import
print("   no PortAudio:", err)
assert devs == [] and ("libportaudio2" in err if sys.platform.startswith("linux") else "PortAudio" in err)
print("device OK")
