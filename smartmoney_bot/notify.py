"""Telegram alerts. Prints if no token is set."""

from __future__ import annotations

import os

import requests


def send(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(text)
        return
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text[:3900], "disable_web_page_preview": True},
        timeout=20,
    )


def format_signal(pair: dict, reason: str) -> str:
    lines = [
        f"{pair['symbol']} | watch",
        f"mcap ${pair['mcap']:,.0f} | liq ${pair['liquidity_usd']:,.0f} | vol1h ${pair['volume_h1']:,.0f}",
        f"gate: {reason}",
    ]
    if pair.get("url"):
        lines.append(pair["url"])
    return "\n".join(lines)
