"""Ending a test that ran a recorder through the web app.

Saving settings restarts the recorder in a background thread. A test that exits while that restart (or
any recorder thread) is still logging can crash Python at exit ("could not acquire lock for <stdout> at
interpreter shutdown"), after the test itself has passed. So: let any restart finish, stop everything,
then leave without the interpreter's shutdown pass.
"""
import os
import sys
import time


def finish(holder, message: str) -> None:
    time.sleep(1.0)                       # a restart thread just started has taken the lock by now
    with holder.lock:                     # ...and has finished once we have it
        if getattr(holder, "switch_listener", None):
            holder.switch_listener.stop()
        holder.engine.stop()
    print(message)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
