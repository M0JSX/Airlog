"""Process-wide "we're shutting down" flag.

When a service is stopped, its ffmpeg child processes can die a moment *before* Airlog itself hears
about the stop: systemd (without KillMode=mixed) and a terminal's Ctrl+C signal the whole process group
at once. Without this, a capture or output that sees its ffmpeg end would take it for a dropped source
and start a new one, during shutdown, sometimes leaving a stub archive file behind.
"""
from __future__ import annotations

import threading

SHUTTING_DOWN = threading.Event()
GRACE_S = 0.75     # how long a process that just ended waits to see whether a shutdown is the reason


def begin_shutdown() -> None:
    SHUTTING_DOWN.set()


def ended_by_shutdown(grace: float = GRACE_S) -> bool:
    """Call when a child process ended unexpectedly: True if the process is shutting down (now, or
    within `grace` seconds, which covers the stop signal arriving just after the child died)."""
    return SHUTTING_DOWN.wait(grace)
