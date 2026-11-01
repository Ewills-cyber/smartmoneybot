import threading
import time

from smartmoney_bot.chat import cli_loop, telegram_loop
from smartmoney_bot.loop import run_once
from smartmoney_bot.settings import DEFAULTS


def worker(stop_flag: dict) -> None:
    interval = DEFAULTS["poll_seconds"]
    print("worker started in paper mode. original telegram setup.")
    while not stop_flag.get("stop"):
        try:
            print(run_once())
        except Exception as exc:  # noqa: BLE001
            print(f"cycle failed: {exc}")
        time.sleep(interval)


def main() -> None:
    stop_flag = {"stop": False}
    threading.Thread(target=worker, args=(stop_flag,), daemon=True).start()
    threading.Thread(target=telegram_loop, args=(stop_flag,), daemon=True).start()
    try:
        cli_loop()
    except (KeyboardInterrupt, EOFError):
        stop_flag["stop"] = True


if __name__ == "__main__":
    main()
