"""End-to-end engine check: fakes the clock to 8 s before the top of the hour,
records a lavfi tone, drops the source mid-way, then verifies the hour split,
gap fill and grabs.  Run:  python tests/test_engine.py   (needs ffmpeg on PATH)
"""
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from airlog.config import Config            # noqa: E402
from airlog.engine import Engine, next_boundary  # noqa: E402


def duration(p: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True).stdout.strip()
    return float(out)


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="airlog_test_"))
    try:
        cfg = Config(station_name="Test FM", source_type="custom",
                     custom_input_args="-re -f lavfi -i sine=frequency=440:sample_rate=48000",
                     archive_dir=str(root / "arch"), grabs_dir=str(root / "grabs"),
                     spool_dir=str(root / "spool"), buffer_minutes=10, identify_songs=False)
        real = time.time()
        boundary = next_boundary(real, 60)
        offset = boundary - 8 - real
        eng = Engine(cfg, clock=lambda: time.time() + offset)
        eng.start()
        time.sleep(5)
        eng._proc.kill()                      # simulate a dropout
        time.sleep(9)

        g = eng.grab(boundary - 5, boundary + 3)
        with wave.open(str(g)) as w:
            got = w.getnframes() / w.getframerate()
        assert abs(got - 8.0) < 0.01, got
        eng.stop()
        time.sleep(1.5)

        files = sorted((root / "arch").rglob("*.mp3"))
        names = [f.name for f in files]
        print("archive:", names)
        assert len(files) == 2, names
        assert files[1].stem.endswith("-00"), names     # second file starts on the hour
        total = sum(duration(f) for f in files)
        print(f"archive total {total:.2f}s, grab {got:.2f}s")
        print("OK")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
