"""Render entry. Serves / so a pinger can keep the free tier awake, and runs the scanner."""

from __future__ import annotations

import os
import threading

from flask import Flask

from smartmoney_bot.chat import telegram_loop
from smartmoney_bot.loop import run_once
from smartmoney_bot.settings import DEFAULTS

app = Flask(__name__)
STOP = {"stop": False}


def worker() -> None:
    import time

    interval = DEFAULTS["poll_seconds"]
    print("worker started in paper mode. render keep-awake on /")
    while not STOP.get("stop"):
        try:
            print(run_once())
        except Exception as exc:  # noqa: BLE001
            print(f"cycle failed: {exc}")
        time.sleep(interval)


def start_background() -> None:
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=telegram_loop, args=(STOP,), daemon=True).start()


start_background()


@app.get("/")
def home():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
