"""Linux sound card capture through PortAudio, for when the bundled ffmpeg has no ALSA input.

Uses a stand-in sound card (CI machines have none) that produces a tone in real time. Checks it
records, device names resolve (default, plughw:X,Y, a PortAudio name), a missing card is reported
and retried, an unplug mid-recording recovers, and the device list works without arecord.

    python tests/test_alsacapture.py          (Linux; needs ffmpeg)
"""
import math
import shutil
import sys
import tempfile
import time
import types
from array import array
from pathlib import Path

if not sys.platform.startswith("linux"):
    print("alsacapture: Linux only, skipped")
    sys.exit(0)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class PortAudioError(Exception):
    pass


class FakeInput:
    """Like an ALSA capture device: only one opener at a time; read() blocks until audio is there,
    which is for ever when the card opened but sends nothing ("stalled")."""

    def __init__(self, samplerate, channels, dtype, device, latency, blocksize):
        if FAKE.missing or FAKE.open_now:
            raise PortAudioError("Error opening RawInputStream: Device unavailable [PaErrorCode -9985]")
        assert dtype == "int16"
        self.sr, self.ch, self.device = samplerate, channels, device
        self.t = 0
        self.next = time.monotonic()
        self.closed = False
        FAKE.opened.append(device)
        FAKE.open_now += 1

    def start(self):
        self.next = time.monotonic()

    @property
    def read_available(self):
        return 0 if FAKE.stalled else max(0, int((time.monotonic() - self.next) * self.sr))

    def abort(self):
        pass

    def close(self):
        if not self.closed:
            self.closed = True
            FAKE.open_now -= 1

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *a):
        self.close()
        return False

    def read(self, frames):
        while FAKE.stalled:                           # a real blocking read never returns here
            time.sleep(0.05)
        self.next += frames / self.sr                 # real time, like a card
        time.sleep(max(0.0, self.next - time.monotonic()))
        if FAKE.unplugged:
            raise PortAudioError("Unanticipated host error [PaErrorCode -9999]")
        a = array("h")
        for i in range(frames):
            v = int(0.5 * 32767 * math.sin(2 * math.pi * 1000 * (self.t + i) / self.sr))
            a.extend([v] * self.ch)
        self.t += frames
        return a.tobytes(), False


FAKE = types.SimpleNamespace(missing=False, unplugged=False, stalled=False, open_now=0, opened=[])
sd = types.ModuleType("sounddevice")
sd.PortAudioError = PortAudioError
sd.RawInputStream = FakeInput
DEVS = [{"name": "HDA Intel PCH: ALC269 Analog (hw:0,0)", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 2},
        {"name": "USB Audio CODEC: - (hw:1,0)", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 2},
        {"name": "pulse", "hostapi": 0, "max_input_channels": 32, "max_output_channels": 32},
        {"name": "default", "hostapi": 0, "max_input_channels": 32, "max_output_channels": 32}]
sd.query_devices = lambda index=None: DEVS[index] if index is not None else DEVS
sd.query_hostapis = lambda i: {"name": "ALSA"}
sys.modules["sounddevice"] = sd

from airlog import sources  # noqa: E402
from airlog.config import Config  # noqa: E402
from airlog.engine import Engine  # noqa: E402

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
sources._ffmpeg_devices[FFMPEG] = {"pulse", "lavfi"}      # like the bundled build: no ALSA input


def wait(pred, secs):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


def engine(name, device):
    d = Path(tempfile.mkdtemp(prefix=f"airlog_alsa_{name}_"))
    cfg = Config(station_name=name, source_type="device", device_backend="alsa", device_name=device,
                 ffmpeg_path=FFMPEG, archive_dir=str(d / "arch"), grabs_dir=str(d / "grabs"),
                 spool_dir=str(d / "spool"), identify_songs=False, announce=False)
    return Engine(cfg)


print("-- device names resolve")
assert sources.portaudio_input(sd, "default")[0] == 3
assert sources.portaudio_input(sd, "")[0] == 3
assert sources.portaudio_input(sd, "plughw:1,0")[0] == 1
assert sources.portaudio_input(sd, "hw:0,0")[0] == 0
assert sources.portaudio_input(sd, "pulse")[0] == 2
try:
    sources.portaudio_input(sd, "plughw:5,0")
    raise AssertionError("a missing card should be an error")
except ValueError as e:
    assert "not found" in str(e)
print("   OK")

print("-- records from the card (plughw:1,0)")
e = engine("rec", "plughw:1,0")
assert sources.via_portaudio(e.cfg)
assert sources.input_args(e.cfg)[-2:] == ["-i", "pipe:0"]
e.start()
assert wait(lambda: e.status == "recording", 15), list(e.logs)
time.sleep(3)
lt = e.live_time()
pcm = b"".join(e.spool.iter_range(lt - 2, lt))
a = array("h"); a.frombytes(pcm)
peak = max(abs(x) for x in a) / 32768
print(f"   peak {20 * math.log10(peak):.1f} dBFS over the last 2 s; opened device {FAKE.opened[-1]}")
assert FAKE.opened[-1] == 1 and 0.4 < peak < 0.6, peak

print("-- unplugged mid-recording: reported, then recording again once it's back")
FAKE.unplugged = True
assert wait(lambda: any("Sound card:" in x for x in e.logs), 10), list(e.logs)[-5:]
FAKE.unplugged = False
n = sum("Recording" in x for x in e.logs)
assert wait(lambda: sum("Recording" in x for x in e.logs) > n, 20), list(e.logs)[-6:]
print("   OK:", next(x for x in e.logs if "Sound card:" in x).split("  ", 1)[-1])
e.stop()

print("-- a card that won't open: reported and retried, not a crash")
FAKE.missing = True
e = engine("missing", "default")
e.start()
assert wait(lambda: any("Device unavailable" in x for x in e.logs), 10), list(e.logs)[-5:]
FAKE.missing = False
assert wait(lambda: e.status == "recording", 30), list(e.logs)[-6:]
print("   OK")
e.stop()

print("-- a card that opens but sends nothing: let go of, so the next attempt can open it")
assert FAKE.open_now == 0, "a stopped engine left the card open"
FAKE.stalled = True
e = engine("stalled", "default")
e.start()
assert wait(lambda: any("no audio came from it" in x for x in e.logs), 15), list(e.logs)[-6:]
assert wait(lambda: FAKE.open_now == 0, 3), "the stalled card was never closed"
assert wait(lambda: sum("Source: " in x for x in e.logs) >= 2, 15), list(e.logs)[-6:]   # retried
print("   OK:", next(x for x in e.logs if "no audio came" in x).split("  ", 1)[-1])
assert not any("Device unavailable" in x for x in e.logs), "a retry found the card still held"
FAKE.stalled = False
assert wait(lambda: e.status == "recording", 40), list(e.logs)[-6:]
print("-- stopping closes the card first (PortAudio left running at exit is what crashed)")
e.stop()
assert FAKE.open_now == 0, "stopped with the card still open"
print("   OK")

print("-- repeated failures: a few lines, then quiet with a summary, then 'working again'")
import airlog.capture as capture  # noqa: E402
capture.QUIET_SUMMARY_S = 6
FAKE.missing = True
e = engine("quiet", "default")
e.start()
assert wait(lambda: any("logging this only every" in x for x in e.logs), 40), list(e.logs)[-8:]
quiet_from = len(e.logs)
assert wait(lambda: any("still not working" in x for x in list(e.logs)[quiet_from:]), 40), list(e.logs)[-5:]
held = list(e.logs)[quiet_from:]
print(f"   while quiet, {len(held)} line(s) in the log:", [x.split("  ", 1)[-1][:90] for x in held])
assert all("still not working" in x for x in held), held       # only the summary
assert "Device unavailable" in held[-1], held                    # naming the cause
FAKE.missing = False
assert wait(lambda: any("working again after" in x for x in e.logs), 40), list(e.logs)[-5:]
assert wait(lambda: e.status == "recording", 10)
e.stop()
print("   OK")

print("-- device list without arecord comes from PortAudio")
orig = sources._run
sources._run = lambda cmd, timeout=8: "" if cmd[0] == "arecord" else orig(cmd, timeout)
devs = sources.list_devices("alsa", FFMPEG)
sources._run = orig
print("  ", devs)
assert ("default", "default") in devs and ("plughw:1,0", "USB Audio CODEC: - (plughw:1,0)") in devs
assert ("pulse", "pulse") in devs

print("-- with an ffmpeg that has ALSA, ffmpeg reads the card itself as before")
sources._ffmpeg_devices[FFMPEG] = {"alsa", "pulse"}
assert not sources.via_portaudio(e.cfg) and sources.input_args(e.cfg)[:2] == ["-f", "alsa"]
print("alsacapture OK")
