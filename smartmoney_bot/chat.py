"""Telegram and local commands."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .http import ApiError, redact, request_json
from .loop import run_once
from .notify import esc, fmt_usd, send
from .settings import load_settings, public_config
from .store import (
    count_signals,
    daily_loss_sol,
    db,
    get_meta,
    open_positions,
    paused,
    recent_signals,
    set_paused,
)
from .stream import get_stream

log = logging.getLogger(__name__)


def normalize_command(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped:
        return ""
    parts = stripped.split(maxsplit=1)
    head = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    if head.startswith("/"):
        head = head.split("@", 1)[0]
    head = head.lower()
    return f"{head} {rest}".strip() if rest else head


def reply(text: str) -> str:
    command = normalize_command(text)
    if command in {"/start", "start"}:
        return start_message()
    if command in {"/help", "help", "commands"}:
        return help_message()
    if command in {"/status", "status", "how are you doing"}:
        return status()
    if command in {"/signals", "signals", "what are you watching"}:
        return signals()
    if command in {"/positions", "positions"}:
        return positions()
    if command in {"/config", "config"}:
        return config_text()
    if command in {"/wallets", "wallets"}:
        return wallets_text()
    if command in {"/pause", "pause"}:
        set_paused(True)
        return "Paused. Scanning stops until you send /resume. Open paper positions stay open."
    if command in {"/resume", "resume"}:
        set_paused(False)
        return "Resumed. The next cycle will scan again."
    if command in {"/scan", "scan"}:
        try:
            result = run_once()
        except Exception as exc:  # noqa: BLE001
            log.warning("manual scan failed: %s", redact(exc))
            return f"Scan failed: {esc(redact(exc))}"
        return "Scan done. " + format_summary(result)
    return help_message()


def start_message() -> str:
    return "\n".join(
        [
            "<b>Smartmoney bot</b>",
            "I watch new Solana pairs and paper-trade the ones that pass the gate.",
            "No wallet keys. No live swaps. Alerts are paper calls.",
            "",
            "Send /help for commands.",
            "Send /status to see the last scan.",
        ]
    )


def help_message() -> str:
    return "\n".join(
        [
            "<b>Commands</b>",
            "/start — what this bot does",
            "/help — this list",
            "/status — scanner, open positions, daily paper loss",
            "/signals — latest calls",
            "/positions — open paper positions",
            "/scan — run one scan now",
            "/config — thresholds (no secrets)",
            "/wallets — watched smart-money wallets",
            "/pause — stop scanning",
            "/resume — scan again",
            "",
            "Plain words work too: status, signals, scan.",
        ]
    )


def status() -> str:
    cfg = load_settings()
    moment = datetime.now(timezone.utc)
    with db() as conn:
        counts = count_signals(conn)
        loss = daily_loss_sol(conn, moment)
    stream = get_stream()
    if stream is not None:
        stream_state = stream.state
    elif cfg.get("enable_profile_stream"):
        stream_state = "starting"
    else:
        stream_state = "off"
    lines = [
        "<b>Smartmoney bot</b>",
        "Paper mode. No live trades.",
        f"Signals logged: {counts['signals']}",
        f"Call rows: {counts['watches']}",
        f"Open paper positions: {counts['open']} / {cfg['max_open_positions']}",
        f"Daily closed loss: {loss:.4f} / {cfg['max_daily_loss_sol']} SOL",
        f"Last scan: {esc(get_meta('last_scan_at') or 'not yet')}",
        f"Paused: {'yes' if paused() else 'no'}",
        f"Profile stream: {esc(stream_state)}",
        f"Watched wallets: {len(cfg['smart_wallets'])}",
        f"Birdeye holders: {'on' if cfg.get('birdeye_api_key') else 'off'}",
        f"Helius RPC: {'on' if cfg.get('helius_api_key') else 'public RPC'}",
    ]
    err = get_meta("last_error") or ""
    if err:
        lines.append(f"Last error: {esc(err)}")
    return "\n".join(lines)


def signals() -> str:
    with db() as conn:
        watches = recent_signals(conn, decision="watch", limit=8)
        skips = recent_signals(conn, decision="skip", limit=5)
    if watches:
        lines = ["<b>Latest calls</b>"]
        for row in watches:
            mcap = fmt_usd(row.get("mcap"), mcap=True)
            lines.append(f"${esc(row.get('symbol') or '?')} · {esc(mcap)} · {esc(row.get('reason') or '')}")
        return "\n".join(lines)
    if not skips:
        return "No signals yet. The first scan runs on the poll interval, or send /scan."
    lines = ["No calls yet.", "<b>Latest filters</b>"]
    for row in skips:
        lines.append(f"${esc(row.get('symbol') or '?')} · {esc(row.get('reason') or '')}")
    return "\n".join(lines)


def positions() -> str:
    with db() as conn:
        rows = open_positions(conn)
    if not rows:
        return "No open paper positions."
    lines = ["<b>Open paper positions</b>"]
    for row in rows:
        last = row.get("last_mcap") or row.get("entry_mcap")
        multiple = ""
        try:
            if row.get("entry_mcap") and last:
                multiple = f" · {float(last) / float(row['entry_mcap']):.2f}x"
        except (TypeError, ValueError, ZeroDivisionError):
            multiple = ""
        lines.append(
            f"${esc(row.get('symbol') or '?')} entry {esc(fmt_usd(row.get('entry_mcap'), mcap=True))}"
            f" now {esc(fmt_usd(last, mcap=True))}{multiple}"
        )
        lines.append(f"<code>{esc(row.get('token') or '')}</code>")
    return "\n".join(lines)


def config_text() -> str:
    shown = public_config(load_settings())
    lines = ["<b>Config</b>"]
    for key, value in shown.items():
        lines.append(f"{esc(key)}: {esc(value)}")
    return "\n".join(lines)


def wallets_text() -> str:
    wallets = load_settings()["smart_wallets"]
    if not wallets:
        return (
            "No SMART_WALLETS set. Discovery alerts still run. "
            "Add public wallet addresses to count smart-money clusters."
        )
    lines = [f"<b>Watched wallets</b> ({len(wallets)})"]
    lines.extend(f"<code>{esc(item)}</code>" for item in wallets)
    return "\n".join(lines)


def format_summary(summary: dict) -> str:
    if summary.get("skipped"):
        return "A scan is already running."
    if summary.get("paused"):
        return "Scanner is paused. Send /resume."
    return (
        f"Scanned {summary.get('scanned', 0)}. "
        f"New calls {summary.get('watches', 0)}. "
        f"Open {summary.get('open', 0)}. "
        f"Clusters {summary.get('clusters', 0)}."
    )


def telegram_loop(stop_flag: dict) -> None:
    cfg = load_settings()
    token = cfg.get("telegram_token")
    if not token:
        log.info("telegram idle: set TELEGRAM_BOT_TOKEN")
        return
    if not cfg.get("telegram_chat_ids"):
        log.info("TELEGRAM_CHAT_ID is empty. Send /start to the bot and copy the chat id from the log.")
    try:
        offset = int(get_meta("telegram_offset", "0") or 0)
    except (TypeError, ValueError):
        offset = 0
    backoff = 1.0
    while not stop_flag.get("stop"):
        try:
            payload = request_json(
                "GET",
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": offset, "timeout": 20},
                timeout=30,
                retries=1,
            )
            backoff = 1.0
        except (ApiError, OSError, ValueError) as exc:
            log.warning("telegram getUpdates failed: %s", redact(exc))
            if stop_flag.get("stop"):
                return
            time.sleep(backoff)
            backoff = min(30.0, backoff * 2)
            continue
        updates = payload.get("result", []) if isinstance(payload, dict) else []
        for update in updates:
            if stop_flag.get("stop"):
                return
            try:
                offset = int(update.get("update_id", offset)) + 1
            except (TypeError, ValueError):
                continue
            set_meta("telegram_offset", str(offset))
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            text = message.get("text") or ""
            allowed = {str(item) for item in load_settings().get("telegram_chat_ids") or []}
            if chat_id is None:
                continue
            if not allowed:
                preview = text.split()[:1]
                log.info("telegram chat %s said %s. Set TELEGRAM_CHAT_ID=%s", chat_id, preview, chat_id)
                continue
            if str(chat_id) not in allowed:
                log.info("ignoring telegram chat %s", chat_id)
                continue
            if not text:
                continue
            try:
                answer = reply(text)
            except Exception as exc:  # noqa: BLE001
                log.warning("command failed: %s", redact(exc))
                answer = f"Command failed: {esc(redact(exc))}"
            try:
                send(answer, token=token, chat_ids=[str(chat_id)])
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram reply failed: %s", redact(exc))


def cli_loop() -> None:
    from .notify import _plain

    print("Paper bot ready. Commands: status, signals, scan, positions, help. exit to quit.")
    while True:
        try:
            text = input("> ").strip()
        except EOFError:
            return
        if text.lower() in {"exit", "quit"}:
            return
        print(_plain(reply(text)))
