"""Archive split times across UK clock changes (runs on Linux/macOS: needs time.tzset).

    python tests/test_boundary.py
"""
import datetime as dt
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["TZ"] = "Europe/London"
time.tzset()

from airlog.engine import next_boundary  # noqa: E402


def utc(*a) -> float:
    return dt.datetime(*a, tzinfo=dt.timezone.utc).timestamp()


def walk(start: float, split: int, n: int) -> list[tuple[str, float]]:
    out, t = [], start
    for _ in range(n):
        b = next_boundary(t, split)
        out.append((dt.datetime.fromtimestamp(t).strftime("%H:%M"), (b - t) / 60))
        t = b
    return out


# Clocks go back 02:00 BST -> 01:00 GMT on 25 Oct 2026: the 01:00 hour happens twice.
back = walk(utc(2026, 10, 24, 23, 0), 60, 4)          # 00:00 BST
assert back == [("00:00", 60), ("01:00", 60), ("01:00", 60), ("02:00", 60)], back

# Clocks go forward 01:00 GMT -> 02:00 BST on 29 Mar 2026: there is no 01:00.
fwd = walk(utc(2026, 3, 29, 0, 0), 60, 3)             # 00:00 GMT
assert fwd == [("00:00", 60), ("02:00", 60), ("03:00", 60)], fwd

# 15-minute splits through the repeated hour stay 15 minutes long.
q = walk(utc(2026, 10, 25, 0, 30), 15, 8)             # 01:30 BST
assert all(mins == 15 for _, mins in q), q
assert [h for h, _ in q] == ["01:30", "01:45", "01:00", "01:15", "01:30", "01:45", "02:00", "02:15"], q

# A start mid-hour runs to the next boundary only.
assert next_boundary(utc(2026, 6, 1, 11, 17), 60) == utc(2026, 6, 1, 12, 0)     # 12:17 BST -> 13:00 BST

print("boundary OK")
