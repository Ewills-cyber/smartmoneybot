"""Restart background threads and remember the last crash for /health."""

from __future__ import annotations

import logging
import threading
import time

from .http import redact

log = logging.getLogger(__name__)

_lock = threading.Lock()
_threads: dict[str, dict] = {}


def thread_snapshot() -> dict:
    with _lock:
        return {name: dict(state) for name, state in _threads.items()}


def note_thread(name: str, *, alive: bool, error: str | None = None, restart: bool = False) -> None:
    with _lock:
        state = _threads.setdefault(name, {"restarts": 0, "alive": False, "error": None})
        state["alive"] = alive
        state["error"] = error
        if restart:
            state["restarts"] = int(state.get("restarts") or 0) + 1


def supervise(name: str, target, stop_flag: dict, *, sleeper=time.sleep) -> None:
    """Run `target(stop_flag)` until asked to stop. Restart after a crash with backoff."""
    delay = 1.0
    while not stop_flag.get("stop"):
        note_thread(name, alive=True, error=None)
        try:
            target(stop_flag)
        except Exception as exc:  # noqa: BLE001
            message = redact(exc)[:300]
            note_thread(name, alive=False, error=message, restart=True)
            log.warning("%s crashed (%s); restarting in %.0fs", name, message, delay)
            _wait(stop_flag, delay, sleeper)
            delay = min(60.0, delay * 2)
            continue
        if stop_flag.get("stop"):
            note_thread(name, alive=False, error=None)
            return
        message = f"{name} exited"
        note_thread(name, alive=False, error=message, restart=True)
        log.warning("%s; restarting in %.0fs", message, delay)
        _wait(stop_flag, delay, sleeper)
        delay = min(60.0, delay * 2)


def _wait(stop_flag: dict, delay: float, sleeper) -> None:
    waited = 0.0
    step = 0.2 if delay > 0.2 else delay
    while waited < delay and not stop_flag.get("stop"):
        sleeper(step)
        waited += step
