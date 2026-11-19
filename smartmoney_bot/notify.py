"""Telegram alerts. Messages stay on the log when no bot token is set."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from .http import redact
from .settings import load_settings

log = logging.getLogger(__name__)


def esc(value) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_usd(amount, *, mcap: bool = False) -> str:
    if amount is None:
        return "n/a"
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return "n/a"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if mcap or value >= 100:
        return f"{sign}${value:,.0f}"
    if value >= 1:
        return f"{sign}${value:,.2f}"
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return f"{sign}${text}"


def fmt_signed_usd(amount) -> str:
    if amount is None:
        return "n/a"
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return "n/a"
    sign = "+" if value >= 0 else "-"
    return sign + fmt_usd(abs(value))


def fmt_sol(amount, *, signed: bool = False) -> str:
    if amount is None:
        return "n/a"
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return "n/a"
    if signed:
        return f"{value:+.4f} SOL"
    return f"{value:.4f} SOL"


def fmt_pct(amount) -> str:
    if amount is None:
        return "n/a"
    try:
        return f"{float(amount):.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def yes_no(value) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def short_addr(value: str) -> str:
    text = str(value or "")
    if len(text) <= 12:
        return text
    return f"{text[:4]}…{text[-4:]}"


def pair_url(pair: dict) -> str:
    if pair.get("url"):
        return str(pair["url"])
    token = pair.get("token") or ""
    return f"https://dexscreener.com/solana/{token}"


def format_call(pair: dict, info: dict, position: dict, cluster: list[dict] | None, cfg: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%b %d, %H:%M UTC")
    wallets = cluster or []
    if wallets:
        shown = ", ".join(esc(short_addr(item["wallet"])) for item in wallets[:4])
        smart = f"{len(wallets)} watched wallets bought in the last {cfg['cluster_window_minutes']}m ({shown})"
    elif cfg.get("smart_wallets"):
        smart = f"0 watched wallets in the last {cfg['cluster_window_minutes']}m"
    else:
        smart = "wallet list not set, discovery only"
    age = pair.get("age_hours")
    age_text = f"{float(age):.1f}h" if age is not None else "unknown"
    change = pair.get("price_change_h1")
    change_text = f"{float(change):+.1f}%" if change is not None else "n/a"
    buys = pair.get("buys_h1")
    sells = pair.get("sells_h1")
    flow = f"{buys} buys / {sells} sells" if buys is not None and sells is not None else "n/a"
    size = fmt_sol(position.get("size_sol"))
    if position.get("size_usd") is not None:
        size += f" ({fmt_usd(position['size_usd'])})"
    lines = [
        f"<b>NEW CALL</b> ${esc(pair.get('symbol') or '?')}",
        esc(pair.get("name") or ""),
        f"Source: {esc(position.get('source') or 'DexScreener')}",
        f"Called: {esc(now)}",
        "",
        f"Market cap: {esc(fmt_usd(pair.get('mcap'), mcap=True))}",
        f"Liquidity: {esc(fmt_usd(pair.get('liquidity_usd'), mcap=True))}",
        f"1h volume: {esc(fmt_usd(pair.get('volume_h1'), mcap=True))}",
        f"1h change: {esc(change_text)} · {esc(flow)}",
        f"Age: {esc(age_text)} · {esc(pair.get('dex') or 'dex')}",
        f"Price: {esc(fmt_usd(pair.get('price')))}",
        "",
        f"CA: <code>{esc(pair.get('token') or '')}</code>",
        "",
        f"Mint revoked: {yes_no(info.get('mint_revoked'))}",
        f"Freeze revoked: {yes_no(info.get('freeze_revoked'))}",
        f"Holders: {esc(info.get('holders') if info.get('holders') is not None else 'n/a')}",
        f"Top holder: {esc(fmt_pct(info.get('top_pct')))}",
        f"Top 10: {esc(fmt_pct(info.get('top10_pct')))}",
        f"Smart money: {smart}",
        "",
        f"Paper size: {esc(size)}",
        f"TP {cfg['take_profit_multiple']}x · SL {cfg['stop_loss_multiple']}x · time stop {cfg['time_stop_minutes']}m",
        "Paper only. NFA. DYOR.",
        esc(pair_url(pair)),
    ]
    return "\n".join(line for line in lines if line is not None)


def format_milestone(position: dict, rung: str, multiple: float, now_mcap: float) -> str:
    gain = pnl_line(position, multiple)
    return "\n".join(
        [
            f"<b>PAPER {esc(rung)}x</b> ${esc(position.get('symbol') or '?')}",
            f"Entry {esc(fmt_usd(position.get('entry_mcap'), mcap=True))} → now {esc(fmt_usd(now_mcap, mcap=True))}",
            f"Multiple: {multiple:.2f}x · {esc(gain)}",
            "Still open. This is a paper milestone.",
            f"CA: <code>{esc(position.get('token') or '')}</code>",
        ]
    )


def format_trade(side: str, symbol: str | None, token: str, status: str, detail: str) -> str:
    title = "LIVE BUY" if side == "buy" else "LIVE SELL"
    return "\n".join(
        [
            f"<b>{title}</b> ${esc(symbol or '?')}",
            f"Status: {esc(status)}",
            esc(detail),
            f"CA: <code>{esc(token)}</code>",
        ]
    )


def format_close(position: dict, reason: str, multiple: float | None, now_mcap: float | None) -> str:
    titles = {
        "take_profit": "TAKE PROFIT",
        "stop_loss": "STOP LOSS",
        "time_stop": "TIME STOP",
    }
    title = titles.get(reason, reason.upper())
    multiple_text = f"{multiple:.2f}x" if multiple is not None else "price unavailable"
    now_text = fmt_usd(now_mcap, mcap=True) if now_mcap else "n/a"
    return "\n".join(
        [
            f"<b>{title}</b> ${esc(position.get('symbol') or '?')}",
            f"Entry {esc(fmt_usd(position.get('entry_mcap'), mcap=True))} → exit {esc(now_text)}",
            f"Result: {esc(multiple_text)} · {esc(pnl_line(position, multiple))}",
            "Paper position closed.",
            f"CA: <code>{esc(position.get('token') or '')}</code>",
        ]
    )


def pnl_line(position: dict, multiple: float | None) -> str:
    from .paper import pnl_sol, pnl_usd

    sol = pnl_sol(position.get("size_sol"), multiple)
    usd = pnl_usd(position.get("size_usd"), multiple)
    text = fmt_sol(sol, signed=True)
    if usd is not None:
        text += f" ({fmt_signed_usd(usd)})"
    return text


def infer_kind(text: str) -> str:
    head = str(text)[:120]
    if "LIVE BUY" in head or "LIVE SELL" in head:
        return "owners"
    if head.startswith("<b>PAPER") or head.startswith("<b>TAKE") or head.startswith("<b>STOP") or head.startswith("<b>TIME"):
        return "pump"
    if "watched wallets bought" in str(text):
        return "cluster"
    if "NEW CALL" in head:
        return "launch"
    return "all"


def send(
    text: str,
    *,
    token: str | None = None,
    chat_ids: list[str] | None = None,
    session=None,
    kind: str | None = None,
    reply_markup: dict | None = None,
) -> bool:
    """Deliver one message. With no chat list, send to subscribers for this alert kind."""
    cfg = None
    if token is None or chat_ids is None:
        cfg = load_settings()
    token = token if token is not None else cfg.get("telegram_token")
    if chat_ids is None:
        chat_ids = _audience(kind or infer_kind(text))
        if not chat_ids and cfg is not None:
            chat_ids = list(cfg.get("telegram_chat_ids") or [])
    if not token or not chat_ids:
        log.info("alert (no telegram):\n%s", _plain(text))
        return False
    ok = True
    for chat in chat_ids:
        markup = reply_markup
        if markup is None and "NEW CALL" in str(text):
            markup = _call_keyboard(str(text), chat)
        if not _deliver(token, chat, text, session=session, reply_markup=markup):
            ok = False
    return ok


def _audience(kind: str) -> list[str]:
    from .community import owner_ids, subscriber_ids

    if kind == "owners":
        return owner_ids() or subscriber_ids("all")
    if kind == "cluster":
        found = subscriber_ids("launch") + subscriber_ids("cluster")
        return list(dict.fromkeys(found))
    if kind in {"launch", "pump"}:
        return subscriber_ids(kind)
    return subscriber_ids("all")


def _call_keyboard(text: str, chat_id=None):
    import re

    from .safety import token_keyboard

    match = re.search(r"<code>([^<]+)</code>", text)
    if not match:
        return None
    address = match.group(1).strip()
    chain = "base" if address.startswith("0x") else "solana"
    sizes = None
    if chat_id is not None:
        from .community import get_subscriber, user_settings

        if get_subscriber(chat_id) is not None:
            prefs = user_settings(chat_id)
            sizes = (prefs["buy_a"], prefs["buy_b"], prefs["buy_c"])
    return token_keyboard(chain, address, sizes)


def _deliver(token: str, chat: str, text: str, session=None, attempts: int = 3, reply_markup: dict | None = None) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    sess = session or requests.Session()
    body = text[:3900]
    for parse_mode in ("HTML", None):
        payload = {
            "chat_id": chat,
            "text": body if parse_mode == "HTML" else _plain(body)[:3900],
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup and parse_mode == "HTML":
            payload["reply_markup"] = reply_markup
        for attempt in range(attempts):
            try:
                response = sess.post(url, json=payload, timeout=20)
            except requests.RequestException as exc:
                log.warning("telegram send failed: %s", redact(exc))
                if attempt + 1 == attempts:
                    break
                continue
            if response.status_code == 429:
                delay = _telegram_retry(response)
                log.warning("telegram rate limited; waiting %.1fs", delay)
                time_sleep(delay)
                continue
            if response.status_code >= 500:
                log.warning("telegram HTTP %s", response.status_code)
                continue
            try:
                data = response.json()
            except ValueError:
                data = {}
            if response.status_code == 400 and parse_mode == "HTML":
                log.warning("telegram rejected HTML, falling back to plain text")
                break
            if response.ok and data.get("ok", True):
                return True
            log.warning("telegram send rejected: %s", data.get("description") or response.status_code)
            return False
    return False


def _telegram_retry(response: requests.Response) -> float:
    try:
        data = response.json()
    except ValueError:
        data = {}
    retry = (data.get("parameters") or {}).get("retry_after")
    try:
        return min(60.0, float(retry))
    except (TypeError, ValueError):
        return 3.0


def time_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _plain(text: str) -> str:
    return (
        text.replace("<b>", "")
        .replace("</b>", "")
        .replace("<code>", "")
        .replace("</code>", "")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )

