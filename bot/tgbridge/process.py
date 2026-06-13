# tgbridge/process.py — process group management.
# Depends only on stdlib (no tgbridge imports needed).
import os
import signal
import subprocess

_KILL_GRACE = 5  # seconds between SIGTERM and SIGKILL (may be overridden by config)


def _kill_tree(proc: "subprocess.Popen", grace: int = _KILL_GRACE) -> None:
    """Send SIGTERM to process group, wait grace seconds, then SIGKILL if alive."""
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        # Process already gone
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass
    # Guard both waits against TimeoutExpired leaking out
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    # Still alive — SIGKILL
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
