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
    set_meta,
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


def reply(text: str, chat_id=None, username: str | None = None) -> str:
    command = normalize_command(text)
    if command.startswith("/start"):
        if chat_id is not None:
            from .community import register_subscriber

            register_subscriber(chat_id, username, text)
        return start_message()
    if command in {"/help", "help", "commands"}:
        return help_message()
    if command in {"/status", "status", "how are you doing"}:
        return status()
    if command in {"/signals", "signals", "what are you watching"}:
        return signals()
    if command in {"/positions", "positions"}:
        return positions() if chat_id is None else _user_positions(chat_id)
    if command in {"/config", "config"}:
        return config_text()
    if command in {"/wallets", "wallets"}:
        return wallets_text()
    if command == "/mode" or command.startswith("/mode "):
        return mode_text()
    if command in {"/leaderboard", "leaderboard"}:
        from .community import leaderboard_text

        return leaderboard_text(chat_id or "local")
    if command in {"/wallet", "wallet"}:
        from .community import wallet_text

        return wallet_text(chat_id or "local")
    if command in {"/fumbles", "fumbles"}:
        from .community import fumble_text

        return fumble_text(chat_id or "local")
    if command in {"/alerts", "alerts"}:
        from .community import alerts_text

        return alerts_text(chat_id or "local")
    if command.startswith("/watch"):
        return _watch_command(text, chat_id)
    if command in {"/pause", "pause"}:
        set_paused(True)
        return (
            "Paused. No new buys until you send /resume. "
            "Open positions can still be closed by their exits. "
            "Live mode cannot be turned on from chat."
        )
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
    mode = load_settings().get("trade_mode", "paper")
    if mode == "live":
        lead = "Trade mode is live. Buys that pass the gate can send a GMGN swap."
    elif mode == "off":
        lead = "Trade mode is off. I scan and report, and I do not open positions."
    else:
        lead = "Trade mode is paper. Alerts are paper calls, not swaps."
    return "\n".join(
        [
            "<b>Smartmoney bot</b>",
            "I watch new Solana pairs and trade the ones that pass the gate.",
            lead,
            "",
            "Send /help for commands.",
            "Send /status to see the last scan.",
            "Send /mode to see the trade mode. Live is enabled only with TRADE_MODE in the environment.",
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
            "/fumbles — paper sells that ran after you exited",
            "/watch <address> — follow a wallet; add the word copy to paper-copy buys",
            "/leaderboard — realized paper PnL and your invite link",
            "/wallet — paper cash and open positions",
            "/alerts — toggle launch, smart-money, and pump alerts",
            "/mode — show paper, off, or live. This does not switch to live.",
            "/pause — stop new buys",
            "/resume — allow new buys again",
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
    if cfg["trade_mode"] == "live":
        headline = "Live mode. GMGN swaps are enabled."
    elif cfg["trade_mode"] == "off":
        headline = "Trading is off."
    else:
        headline = "Paper mode. No live trades."
    lines = [
        "<b>Smartmoney bot</b>",
        headline,
        f"Signals logged: {counts['signals']}",
        f"Call rows: {counts['watches']}",
        f"Open positions: {counts['open']} / {cfg['max_open_positions']}",
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


def mode_text() -> str:
    cfg = load_settings()
    wallet = cfg.get("gmgn_wallet") or ""
    shown = f"{wallet[:4]}…{wallet[-4:]}" if len(wallet) > 12 else (wallet or "missing")
    lines = [
        f"<b>Trade mode</b>: {esc(cfg.get('trade_mode', 'paper'))}",
        "Live trading is enabled only by TRADE_MODE=live in the environment.",
        "No chat command switches into live mode.",
        f"GMGN API key: {'set' if cfg.get('gmgn_api_key') else 'missing'}",
        f"GMGN signing key: {'set' if cfg.get('gmgn_private_key') else 'missing'}",
        f"GMGN wallet: {esc(shown)}",
        "/pause stops new buys. It does not cancel an order GMGN already accepted.",
    ]
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


def _user_positions(chat_id) -> str:
    from .community import wallet_text

    return wallet_text(chat_id)


def _watch_command(text: str, chat_id) -> str:
    from .community import add_wallet, watch_text
    from .safety import detect_address

    if chat_id is None:
        return "Watch wallets are stored per Telegram chat. Send /watch from the bot."
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        set_meta(f"ui:{chat_id}", "wallet")
        return watch_text(chat_id) + "\nSend the wallet address next."
    address = detect_address(parts[1])
    if not address or address.startswith("0x"):
        return "Send a Solana wallet address."
    copy = len(parts) > 2 and parts[2].lower() == "copy"
    result = add_wallet(chat_id, address, copy_paper=copy)
    if not result.get("ok"):
        return result.get("error") or "Could not watch that wallet."
    set_meta(f"ui:{chat_id}", "")
    return watch_text(chat_id)


def respond(text: str, chat_id=None, username: str | None = None) -> tuple[str, dict | None]:
    """Reply text plus an optional inline keyboard."""
    from .safety import detect_address, format_verdict, main_menu, scan_address, token_keyboard

    command = normalize_command(text)
    if chat_id is not None and command.startswith("/start"):
        from .community import register_subscriber

        register_subscriber(chat_id, username, text)
    address = detect_address(text.strip()) if not command.startswith("/") else None
    if address:
        if chat_id is not None:
            set_meta(f"ui:{chat_id}", "")
        card = scan_address(address)
        return format_verdict(card), token_keyboard(card.get("chain") or "solana", address)
    if chat_id is not None and get_meta(f"ui:{chat_id}") == "wallet":
        return _watch_command(f"/watch {text}", chat_id), main_menu()
    body = reply(text, chat_id=chat_id, username=username)
    markup = main_menu() if command.startswith("/start") or command in {"/help", "help"} else None
    return body, markup


def handle_callback(data: str, chat_id, username: str | None = None) -> tuple[str, dict | None]:
    from .community import (
        alerts_keyboard,
        alerts_text,
        fumble_text,
        leaderboard_text,
        paper_buy,
        paper_sell,
        positions_keyboard,
        register_subscriber,
        toggle_alert,
        wallet_text,
        watch_text,
    )
    from .safety import main_menu, scan_address, token_keyboard

    register_subscriber(chat_id, username)
    if data in {"menu:home", "menu:help"}:
        return (start_message() if data == "menu:home" else help_message()), main_menu()
    if data == "menu:safety":
        set_meta(f"ui:{chat_id}", "safety")
        return "Paste a Solana or Base contract address.", None
    if data in {"menu:wallet", "menu:positions"}:
        return wallet_text(chat_id), positions_keyboard(chat_id) or main_menu()
    if data == "menu:alerts":
        return alerts_text(chat_id), alerts_keyboard()
    if data.startswith("alert:"):
        toggle_alert(chat_id, data.split(":", 1)[1])
        return alerts_text(chat_id), alerts_keyboard()
    if data == "menu:board":
        return leaderboard_text(chat_id), main_menu()
    if data == "menu:fumbles":
        return fumble_text(chat_id), main_menu()
    if data == "menu:watch":
        set_meta(f"ui:{chat_id}", "wallet")
        return watch_text(chat_id) + "\nSend a Solana address to watch it.", main_menu()
    if data.startswith(("b25:", "b50:", "b100:")):
        size_token, address = data.split(":", 1)
        usd = {"b25": 25.0, "b50": 50.0, "b100": 100.0}[size_token]
        card = scan_address(address)
        price = float(card.get("price") or 0)
        mcap = float(card.get("mcap") or 0)
        result = paper_buy(
            chat_id,
            address,
            card.get("symbol") or "?",
            card.get("chain") or "solana",
            price,
            mcap,
            usd,
        )
        chain = card.get("chain") or "solana"
        if not result.get("ok"):
            return result.get("error") or "Paper buy failed.", token_keyboard(chain, address)
        return f"Paper buy ${usd:.0f} of ${card.get('symbol') or '?'}.", token_keyboard(chain, address)
    if data.startswith(("s50:", "s100:")):
        size_token, address = data.split(":", 1)
        percent = 50 if size_token == "s50" else 100
        card = scan_address(address)
        price = float(card.get("price") or 0)
        mcap = float(card.get("mcap") or 0)
        result = paper_sell(chat_id, address, percent, price, mcap)
        if not result.get("ok"):
            return result.get("error") or "Paper sell failed.", main_menu()
        return f"Paper sell {percent:.0f}% · pnl {result['pnl']:.2f} USD", token_keyboard(card.get("chain") or "solana", address)
    return help_message(), main_menu()


def delete_webhook(token: str, poster=None) -> None:
    """Drop a webhook so getUpdates is allowed. A deploy overlap otherwise returns HTTP 409."""
    import requests

    send_post = poster or requests.post
    url = f"https://api.telegram.org/bot{token}/deleteWebhook"
    try:
        send_post(url, json={"drop_pending_updates": False}, timeout=15)
    except Exception as exc:  # noqa: BLE001
        log.warning("deleteWebhook failed: %s", redact(exc))


def telegram_loop(stop_flag: dict, *, poll=None, webhook_delete=None, sleeper=None, deliver=None) -> None:
    sleeper = sleeper or time.sleep
    cfg = load_settings()
    token = cfg.get("telegram_token")
    if not token:
        log.info("telegram idle: set TELEGRAM_BOT_TOKEN")
        while not stop_flag.get("stop"):
            sleeper(1)
        return
    try:
        from .community import seed_subscribers

        seed_subscribers()
    except Exception as exc:  # noqa: BLE001
        log.warning("subscriber seed failed: %s", redact(exc))
    (webhook_delete or delete_webhook)(token)
    try:
        offset = int(get_meta("telegram_offset", "0") or 0)
    except (TypeError, ValueError):
        offset = 0
    backoff = 1.0
    while not stop_flag.get("stop"):
        try:
            if poll is not None:
                payload = poll()
            else:
                payload = request_json(
                    "GET",
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"offset": offset, "timeout": 20, "allowed_updates": ["message", "callback_query"]},
                    timeout=30,
                    retries=1,
                )
            backoff = 1.0
        except (ApiError, OSError, ValueError) as exc:
            status = getattr(exc, "status", None)
            if status == 409:
                log.warning("telegram getUpdates conflict (409); waiting for the other poller to exit")
                wait = max(5.0, backoff)
            else:
                log.warning("telegram getUpdates failed: %s", redact(exc))
                wait = backoff
            if stop_flag.get("stop"):
                return
            sleeper(wait)
            backoff = min(30.0, max(wait * 2, 1.0))
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
            _dispatch_update(update, token, deliver)


def _dispatch_update(update: dict, token: str, deliver) -> None:
    callback = update.get("callback_query") or {}
    if callback:
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        user = callback.get("from") or {}
        if chat_id is None:
            return
        try:
            text, markup = handle_callback(callback.get("data") or "", chat_id, user.get("username"))
        except Exception as exc:  # noqa: BLE001
            log.warning("callback failed: %s", redact(exc))
            text, markup = f"Command failed: {esc(redact(exc))}", None
        _answer_callback(token, callback.get("id"), deliver)
        _send_reply(token, chat_id, text, markup, deliver)
        return
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    text = message.get("text") or ""
    if chat_id is None or not text:
        return
    user = message.get("from") or {}
    try:
        answer, markup = respond(text, chat_id, user.get("username"))
    except Exception as exc:  # noqa: BLE001
        log.warning("command failed: %s", redact(exc))
        answer, markup = f"Command failed: {esc(redact(exc))}", None
    _send_reply(token, chat_id, answer, markup, deliver)


def _send_reply(token: str, chat_id, text: str, markup, deliver) -> None:
    sender = deliver or send
    try:
        sender(text, token=token, chat_ids=[str(chat_id)], reply_markup=markup)
    except TypeError:
        try:
            sender(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram reply failed: %s", redact(exc))
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram reply failed: %s", redact(exc))


def _answer_callback(token: str, callback_id, deliver) -> None:
    if not callback_id or deliver is not None:
        return
    import requests

    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={"callback_query_id": callback_id},
            timeout=10,
        )
    except requests.RequestException as exc:
        log.warning("answerCallbackQuery failed: %s", redact(exc))


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
