"""Telegram alerts. Prints if no token is set."""

from __future__ import annotations

from datetime import datetime, timezone
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


def format_call(pair: dict, checks: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%b %d, %H:%M UTC")
    return "\n".join([
        f"NEW CALL | ${pair['symbol']}",
        f"Market cap: ${pair['mcap']:,.0f}",
        f"Called at: {now}",
        f"CA: {pair['token']}",
        f"holders: {checks.get('holders') if checks.get('holders') is not None else 'n/a'}",
        f"mint revoked: {checks.get('mint_revoked')} | freeze revoked: {checks.get('freeze_revoked')}",
        "paper $20 | NFA. DYOR.",
        pair.get("url") or "",
    ])
