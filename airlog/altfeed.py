"""When the internet outputs carry the alternative input instead of the programme.

For stations that may not stream some programmes online (e.g. football commentary that's licensed for
the hospital service only). Outputs whose feed is "switch" follow this; the recording never does.

Modes:
  manual     the button on the main page decides (alt_override "alternative" / "programme")
  schedule   times entered in advance decide; the button overrides until set back to Schedule
  permanent  switched outputs always carry the alternative
"""
from __future__ import annotations

import datetime as dt
import re

from .config import ALT_MODES, Config

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
OVERRIDES = ("", "programme", "alternative")


def _hhmm(s: str, what: str) -> dt.time:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(s or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"{what} must be a time like 15:00")
    return dt.time(int(m.group(1)), int(m.group(2)))


def normalize_schedule(entries) -> list[dict]:
    """Checked, tidied schedule entries. Raises ValueError naming the entry that's wrong."""
    if entries in (None, ""):
        return []
    if not isinstance(entries, list):
        raise ValueError("Alternative schedule must be a list")
    out = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise ValueError(f"Schedule entry {i + 1} isn't valid")
        label = str(e.get("label") or "").strip()[:80]
        name = f"Schedule entry {i + 1}" + (f" ({label})" if label else "")
        start = _hhmm(e.get("start"), f"{name}: the start")
        end = _hhmm(e.get("end"), f"{name}: the end")
        if start == end:
            raise ValueError(f"{name}: the start and end are the same")
        date = str(e.get("date") or "").strip()
        days = []
        if date:
            try:
                dt.date.fromisoformat(date)
            except ValueError:
                raise ValueError(f"{name}: the date must be like 2026-11-14") from None
        else:
            raw = e.get("days") or ""
            words = raw if isinstance(raw, list) else re.split(r"[\s,]+", str(raw))
            for word in words:
                word = word.strip()
                w = word[:3].title()
                if not w:
                    continue
                if w not in DAYS or not "monday tuesday wednesday thursday friday saturday sunday".split()[DAYS.index(w)].startswith(word.lower()):
                    raise ValueError(f"{name}: '{word}' isn't a day (use Mon Tue Wed Thu Fri Sat Sun)")
                if w not in days:
                    days.append(w)
            if not days:
                raise ValueError(f"{name}: pick at least one day, or a date")
            days.sort(key=DAYS.index)
        out.append({"label": label, "date": date, "days": " ".join(days),
                    "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")})
    return out


def _windows(e: dict, around: dt.datetime):
    """(start, end) datetimes of an entry's occurrences near `around` (yesterday to a week ahead).
    An end at or before the start means it runs past midnight."""
    start_t, end_t = _hhmm(e["start"], ""), _hhmm(e["end"], "")
    if e.get("date"):
        dates = [dt.date.fromisoformat(e["date"])]
    else:
        days = set(e["days"].split())
        dates = [around.date() + dt.timedelta(days=k) for k in range(-1, 8)]
        dates = [d for d in dates if DAYS[d.weekday()] in days]
    for d in dates:
        s = dt.datetime.combine(d, start_t)
        en = dt.datetime.combine(d, end_t)
        if en <= s:
            en += dt.timedelta(days=1)
        yield s, en


def window_now(entries: list[dict], now: dt.datetime) -> tuple[dict, dt.datetime] | None:
    """The schedule entry in force at `now`, and when it ends."""
    best = None
    for e in entries:
        for s, en in _windows(e, now):
            if s <= now < en and (best is None or en > best[1]):
                best = (e, en)
    return best


def next_window(entries: list[dict], now: dt.datetime) -> tuple[dict, dt.datetime, dt.datetime] | None:
    best = None
    for e in entries:
        for s, en in _windows(e, now):
            if s > now and (best is None or s < best[1]):
                best = (e, s, en)
    return best


def decide(cfg: Config, now: dt.datetime | None = None) -> tuple[bool, str]:
    """(on, why): whether switched outputs carry the alternative now, and a short reason."""
    if not cfg.alt_enabled:
        return False, "off"
    now = now or dt.datetime.now()
    mode = cfg.alt_mode if cfg.alt_mode in ALT_MODES else "manual"
    if mode == "permanent":
        return True, "permanent"
    if cfg.alt_override == "alternative":
        return True, "manual" if mode == "manual" else "override"
    if cfg.alt_override == "programme" or mode == "manual":
        return False, "manual" if mode == "manual" else "override"
    w = window_now(cfg.alt_schedule or [], now)
    if w:
        e, _end = w
        return True, "schedule" + (f": {e['label']}" if e.get("label") else "")
    return False, "schedule"


def describe(cfg: Config, now: dt.datetime | None = None) -> dict:
    """For the status API: the state, why, and what the schedule does next."""
    now = now or dt.datetime.now()
    on, why = decide(cfg, now)
    d = {"enabled": bool(cfg.alt_enabled), "mode": cfg.alt_mode, "override": cfg.alt_override,
         "on": on, "why": why, "until": None, "next": None}
    if cfg.alt_enabled and cfg.alt_mode == "schedule":
        w = window_now(cfg.alt_schedule or [], now)
        if w:
            d["until"] = {"label": w[0].get("label", ""), "at": w[1].isoformat(timespec="minutes")}
        n = next_window(cfg.alt_schedule or [], now)
        if n:
            d["next"] = {"label": n[0].get("label", ""), "start": n[1].isoformat(timespec="minutes"),
                         "end": n[2].isoformat(timespec="minutes")}
    return d
