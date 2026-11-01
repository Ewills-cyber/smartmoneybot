"""Original Telegram chat. Exact commands only."""

from __future__ import annotations

import os
import time

import requests

from .store import db


def status() -> str:
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"]
        watches = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE decision = 'watch'").fetchone()["n"]
    return f"Worker is up. Signals logged: {n}. Watches: {watches}. Paper mode."


def signals() -> str:
    with db() as conn:
        rows = conn.execute(
            "SELECT symbol, mcap, decision, reason FROM signals ORDER BY id DESC LIMIT 8"
        ).fetchall()
    if not rows:
        return "No signals yet."
    return "\n".join(f"{row['symbol']} | {row['decision']} | mcap ${row['mcap']:,.0f} | {row['reason']}" for row in rows)


def reply(text: str) -> str:
    lowered = text.lower().strip()
    if lowered in {"status", "/status", "how are you doing"}:
        return status()
    if lowered in {"signals", "/signals", "what are you watching"}:
        return signals()
    if lowered in {"scan", "/scan"}:
        from .loop import run_once
        return "Scan done. " + str(run_once())
    return "Commands: status, signals, scan."


def telegram_loop(stop_flag: dict) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("chat agent idle: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return
    offset = 0
    while not stop_flag.get("stop"):
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": offset, "timeout": 25},
                timeout=30,
            )
            resp.raise_for_status()
            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                if str(message.get("chat", {}).get("id")) != str(chat):
                    continue
                text = message.get("text") or ""
                if not text:
                    continue
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat, "text": reply(text)[:3900]},
                    timeout=20,
                )
        except Exception as exc:  # noqa: BLE001
            print(f"chat loop error: {exc}")
            time.sleep(3)


def cli_loop() -> None:
    print("chat agent ready. type status, signals, or scan. exit to quit.")
    while True:
        text = input("> ").strip()
        if text in {"exit", "quit"}:
            return
        print(reply(text))
