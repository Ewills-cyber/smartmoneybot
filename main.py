"""Local entry: scanner, Telegram poller, and a terminal prompt."""

from __future__ import annotations

import signal
import threading

from smartmoney_bot.chat import cli_loop, telegram_loop
from smartmoney_bot.loop import serve
from smartmoney_bot.settings import setup_logging

STOP = {"stop": False}


def request_stop(*_args) -> None:
    STOP["stop"] = True
    raise SystemExit(0)


def main() -> None:
    setup_logging()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    threading.Thread(target=serve, args=(STOP,), name="scanner", daemon=True).start()
    threading.Thread(target=telegram_loop, args=(STOP,), name="telegram", daemon=True).start()
    try:
        cli_loop()
    finally:
        STOP["stop"] = True


if __name__ == "__main__":
    main()
