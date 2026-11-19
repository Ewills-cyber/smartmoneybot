"""Render entry. Serves /health for the platform and for keep-awake pings."""

from __future__ import annotations

import logging
import os
import signal
import threading

from flask import Flask, jsonify

from smartmoney_bot import __version__
from smartmoney_bot.chat import telegram_loop
from smartmoney_bot.community import seed_subscribers
from smartmoney_bot.loop import serve
from smartmoney_bot.runtime import supervise, thread_snapshot
from smartmoney_bot.settings import load_settings, setup_logging
from smartmoney_bot.store import count_signals, db, get_meta, paused
from smartmoney_bot.stream import get_stream

log = logging.getLogger(__name__)

STOP = {"stop": False}
_start_lock = threading.Lock()
_started = False


def ensure_started() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
        setup_logging()
        cfg = load_settings()
        log.info(
            "starting paper bot chain=%s wallets=%s birdeye=%s helius=%s",
            cfg["chain"],
            len(cfg["smart_wallets"]),
            bool(cfg["birdeye_api_key"]),
            bool(cfg["helius_api_key"]),
        )
        try:
            seed_subscribers()
        except Exception as exc:  # noqa: BLE001
            log.warning("subscriber seed failed: %s", exc)
        threading.Thread(target=supervise, args=("scanner", serve, STOP), name="scanner", daemon=True).start()
        threading.Thread(target=supervise, args=("telegram", telegram_loop, STOP), name="telegram", daemon=True).start()


def request_stop(*_args) -> None:
    STOP["stop"] = True
    log.info("shutdown requested")
    raise SystemExit(0)


def health_payload() -> dict:
    stream = get_stream()
    payload = {
        "status": "ok",
        "service": "smartmoneybot",
        "version": __version__,
        "paused": False,
        "profile_stream": stream.state if stream else "off",
        "threads": thread_snapshot(),
    }
    threads = payload["threads"]
    if any(state.get("error") for state in threads.values()):
        payload["status"] = "degraded"
    try:
        payload["paused"] = paused()
        with db() as conn:
            payload.update(count_signals(conn))
        payload["last_scan"] = get_meta("last_scan_at")
        payload["last_summary"] = get_meta("last_summary")
    except Exception as exc:  # noqa: BLE001
        log.warning("health snapshot failed: %s", exc)
        payload["detail"] = "starting"
    return payload


def create_app() -> Flask:
    app = Flask(__name__)
    if os.environ.get("SMARTMONEY_DISABLE_BACKGROUND") != "1":
        ensure_started()

    @app.get("/health")
    def health():
        return jsonify(health_payload())

    @app.get("/")
    def home():
        return jsonify(health_payload())

    return app


app = create_app()


def main() -> None:
    setup_logging()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    port = int(os.environ.get("PORT", "8080"))
    log.info("listening on 0.0.0.0:%s", port)
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
