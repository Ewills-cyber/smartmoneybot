"""Per-user subscribers, paper wallets, referrals, copy-trades, and fumbles."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from .notify import esc, fmt_usd
from .settings import load_settings
from .store import db

STARTING_CASH = 1000.0
COPY_USD = 50.0
SETTING_FIELDS = ("buy_a", "buy_b", "buy_c", "tp_multiple", "sl_multiple", "copy_usd")
SETTING_DEFAULTS = {
    "buy_a": 25.0,
    "buy_b": 50.0,
    "buy_c": 100.0,
    "tp_multiple": 2.0,
    "sl_multiple": 0.5,
    "copy_usd": 50.0,
}


def parse_setting_number(text: str) -> float | None:
    raw = (text or "").strip().lower().replace("$", "").replace(",", "").rstrip("x")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def validate_setting(field: str, value: float, cash_usd: float) -> tuple[bool, str]:
    """TP must be above 1x, stop-loss between 0 and 1, and sizes must fit the cash on hand."""
    if field not in SETTING_FIELDS:
        return False, "Unknown setting."
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False, "Send a number."
    if field == "tp_multiple":
        if number <= 1:
            return False, "Take profit has to be above 1x."
        return True, "ok"
    if field == "sl_multiple":
        if number <= 0 or number >= 1:
            return False, "Stop loss has to be above 0 and below 1x."
        return True, "ok"
    if number <= 0:
        return False, "Size has to be above 0."
    if number > float(cash_usd):
        return False, f"That is above the paper cash (${float(cash_usd):.2f})."
    return True, "ok"


def user_settings(chat_id) -> dict:
    user = get_subscriber(chat_id)
    settings = dict(SETTING_DEFAULTS)
    if not user:
        settings["cash_usd"] = 0.0
        return settings
    for field, default in SETTING_DEFAULTS.items():
        value = user.get(field)
        settings[field] = float(value) if value is not None else default
    settings["cash_usd"] = float(user.get("cash_usd") or 0)
    return settings


def apply_setting(chat_id, field: str, value: float) -> dict:
    cid = str(chat_id)
    if get_subscriber(cid) is None:
        return {"ok": False, "error": "Send /start first."}
    current = user_settings(cid)
    ok, reason = validate_setting(field, value, current["cash_usd"])
    if not ok:
        return {"ok": False, "error": reason, "settings": current}
    with db() as conn:
        conn.execute(f"UPDATE subscribers SET {field} = ? WHERE chat_id = ?", (float(value), cid))
    return {"ok": True, "settings": user_settings(cid)}


def settings_text(chat_id) -> str:
    user = get_subscriber(chat_id)
    if not user:
        return "Send /start first."
    prefs = user_settings(chat_id)
    return "\n".join(
        [
            "<b>Trade settings</b>",
            f"Paper cash: {fmt_usd(prefs['cash_usd'])}",
            f"Buy buttons: {fmt_usd(prefs['buy_a'])} · {fmt_usd(prefs['buy_b'])} · {fmt_usd(prefs['buy_c'])}",
            f"Take profit: {prefs['tp_multiple']:g}x",
            f"Stop loss: {prefs['sl_multiple']:g}x",
            f"Copy-trade size: {fmt_usd(prefs['copy_usd'])}",
            "These apply to new paper buys. Open positions keep the exits they were opened with.",
            "Tap a preset, or tap Type to send a number.",
        ]
    )


def settings_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Buy1 $10", "callback_data": "stg:buy_a:10"},
                {"text": "$25", "callback_data": "stg:buy_a:25"},
                {"text": "$50", "callback_data": "stg:buy_a:50"},
                {"text": "Type", "callback_data": "set:buy_a"},
            ],
            [
                {"text": "Buy2 $25", "callback_data": "stg:buy_b:25"},
                {"text": "$50", "callback_data": "stg:buy_b:50"},
                {"text": "$100", "callback_data": "stg:buy_b:100"},
                {"text": "Type", "callback_data": "set:buy_b"},
            ],
            [
                {"text": "Buy3 $50", "callback_data": "stg:buy_c:50"},
                {"text": "$100", "callback_data": "stg:buy_c:100"},
                {"text": "$250", "callback_data": "stg:buy_c:250"},
                {"text": "Type", "callback_data": "set:buy_c"},
            ],
            [
                {"text": "TP 1.5x", "callback_data": "stg:tp_multiple:1.5"},
                {"text": "2x", "callback_data": "stg:tp_multiple:2"},
                {"text": "3x", "callback_data": "stg:tp_multiple:3"},
                {"text": "5x", "callback_data": "stg:tp_multiple:5"},
                {"text": "Type", "callback_data": "set:tp_multiple"},
            ],
            [
                {"text": "SL 0.3x", "callback_data": "stg:sl_multiple:0.3"},
                {"text": "0.5x", "callback_data": "stg:sl_multiple:0.5"},
                {"text": "0.7x", "callback_data": "stg:sl_multiple:0.7"},
                {"text": "Type", "callback_data": "set:sl_multiple"},
            ],
            [
                {"text": "Copy $25", "callback_data": "stg:copy_usd:25"},
                {"text": "$50", "callback_data": "stg:copy_usd:50"},
                {"text": "$100", "callback_data": "stg:copy_usd:100"},
                {"text": "Type", "callback_data": "set:copy_usd"},
            ],
            [{"text": "« Menu", "callback_data": "menu:home"}],
        ]
    }


def referral_code(chat_id) -> str:
    digest = hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()
    return digest[:8]


def invite_link(code: str) -> str:
    name = (os.environ.get("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@")
    if not name:
        return f"Ask a friend to send /start {code}"
    return f"https://t.me/{name}?start={code}"


def seed_subscribers() -> int:
    """Insert env chat ids so a redeploy on an empty disk still alerts the owner."""
    added = 0
    for chat_id in load_settings().get("telegram_chat_ids") or []:
        if register_subscriber(chat_id, source="env").get("created"):
            added += 1
    return added


def register_subscriber(chat_id, username: str | None = None, start_payload: str | None = None, source: str = "start") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    cid = str(chat_id)
    code = referral_code(cid)
    created = False
    with db() as conn:
        row = conn.execute("SELECT * FROM subscribers WHERE chat_id = ?", (cid,)).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO subscribers (
                    chat_id, username, referral_code, cash_usd, alerts_launch, alerts_cluster, alerts_pump, created_at
                ) VALUES (?, ?, ?, ?, 1, 1, 1, ?)
                """,
                (cid, username, code, STARTING_CASH, now),
            )
            created = True
        elif username and not row["username"]:
            conn.execute("UPDATE subscribers SET username = ? WHERE chat_id = ?", (username, cid))
        referrer_code = _payload_code(start_payload)
        if referrer_code and referrer_code != code:
            owner = conn.execute(
                "SELECT chat_id FROM subscribers WHERE referral_code = ?",
                (referrer_code,),
            ).fetchone()
            current = conn.execute("SELECT referrer FROM subscribers WHERE chat_id = ?", (cid,)).fetchone()
            if owner and current and not current["referrer"] and owner["chat_id"] != cid:
                conn.execute(
                    "UPDATE subscribers SET referrer = ? WHERE chat_id = ?",
                    (owner["chat_id"], cid),
                )
    return {"chat_id": cid, "referral_code": code, "created": created, "source": source}


def _payload_code(payload: str | None) -> str | None:
    text = (payload or "").strip()
    if not text:
        return None
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        text = parts[1].strip() if len(parts) > 1 else ""
    return text or None


def subscriber_ids(kind: str = "all") -> list[str]:
    column = {
        "launch": "alerts_launch",
        "cluster": "alerts_cluster",
        "pump": "alerts_pump",
    }.get(kind)
    with db() as conn:
        if column:
            rows = conn.execute(
                f"SELECT chat_id FROM subscribers WHERE {column} = 1 ORDER BY created_at"
            ).fetchall()
        else:
            rows = conn.execute("SELECT chat_id FROM subscribers ORDER BY created_at").fetchall()
    return [str(row["chat_id"]) for row in rows]


def owner_ids() -> list[str]:
    return [str(item) for item in load_settings().get("telegram_chat_ids") or []]


def get_subscriber(chat_id) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM subscribers WHERE chat_id = ?", (str(chat_id),)).fetchone()
    return dict(row) if row else None


def toggle_alert(chat_id, field: str) -> dict | None:
    if field not in {"alerts_launch", "alerts_cluster", "alerts_pump"}:
        return None
    cid = str(chat_id)
    with db() as conn:
        row = conn.execute(f"SELECT {field} AS flag FROM subscribers WHERE chat_id = ?", (cid,)).fetchone()
        if row is None:
            return None
        nxt = 0 if int(row["flag"] or 0) else 1
        conn.execute(f"UPDATE subscribers SET {field} = ? WHERE chat_id = ?", (nxt, cid))
    return get_subscriber(cid)


def alerts_text(chat_id) -> str:
    user = get_subscriber(chat_id)
    if not user:
        return "Send /start first."
    def flag(value) -> str:
        return "on" if int(value or 0) else "off"

    return "\n".join(
        [
            "<b>Alerts</b>",
            f"New launches: {flag(user['alerts_launch'])}",
            f"Smart-money clusters: {flag(user['alerts_cluster'])}",
            f"Pump milestones (2x and up): {flag(user['alerts_pump'])}",
            "Tap a button to toggle.",
        ]
    )


def alerts_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "Toggle launches", "callback_data": "alert:alerts_launch"}],
            [{"text": "Toggle smart money", "callback_data": "alert:alerts_cluster"}],
            [{"text": "Toggle pump calls", "callback_data": "alert:alerts_pump"}],
            [{"text": "« Menu", "callback_data": "menu:home"}],
        ]
    }


def paper_buy(
    chat_id,
    token: str,
    symbol: str,
    chain: str,
    price: float,
    mcap: float,
    usd: float,
    tp: float | None = None,
    sl: float | None = None,
) -> dict:
    cid = str(chat_id)
    user = get_subscriber(cid)
    if not user:
        return {"ok": False, "error": "Send /start first."}
    prefs = user_settings(cid)
    if tp is None:
        tp = prefs["tp_multiple"]
    if sl is None:
        sl = prefs["sl_multiple"]
    if usd <= 0 or price <= 0:
        return {"ok": False, "error": "Price is not available yet."}
    if float(user["cash_usd"] or 0) < usd:
        return {"ok": False, "error": f"Paper cash is ${float(user['cash_usd'] or 0):.2f}."}
    now = datetime.now(timezone.utc).isoformat()
    qty = usd / float(price)
    with db() as conn:
        conn.execute("UPDATE subscribers SET cash_usd = cash_usd - ? WHERE chat_id = ?", (usd, cid))
        existing = conn.execute(
            "SELECT * FROM user_positions WHERE chat_id = ? AND token = ? AND status = 'open'",
            (cid, token),
        ).fetchone()
        if existing:
            new_size = float(existing["size_usd"] or 0) + usd
            new_qty = float(existing["qty"] or 0) + qty
            entry_price = new_size / new_qty if new_qty else price
            entry_mcap = ((float(existing["entry_mcap"] or mcap) * float(existing["size_usd"] or 0)) + (mcap * usd)) / new_size
            conn.execute(
                """
                UPDATE user_positions
                SET size_usd = ?, qty = ?, entry_price = ?, entry_mcap = ?, symbol = ?
                WHERE id = ?
                """,
                (new_size, new_qty, entry_price, entry_mcap, symbol, existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO user_positions (
                    chat_id, token, symbol, chain, entry_price, entry_mcap, size_usd, qty,
                    tp_multiple, sl_multiple, status, opened_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (cid, token, symbol, chain, price, mcap, usd, qty, tp, sl, now),
            )
    return {"ok": True, "usd": usd, "qty": qty}


def paper_sell(chat_id, token: str, percent: float, price: float, mcap: float) -> dict:
    cid = str(chat_id)
    if price <= 0 or percent <= 0:
        return {"ok": False, "error": "Cannot sell at that price."}
    percent = min(100.0, float(percent))
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM user_positions WHERE chat_id = ? AND token = ? AND status = 'open'",
            (cid, token),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "No open paper position."}
        fraction = percent / 100.0
        qty = float(row["qty"] or 0) * fraction
        proceeds = qty * float(price)
        cost = float(row["size_usd"] or 0) * fraction
        pnl = proceeds - cost
        left_qty = float(row["qty"] or 0) - qty
        left_size = float(row["size_usd"] or 0) - cost
        conn.execute("UPDATE subscribers SET cash_usd = cash_usd + ? WHERE chat_id = ?", (proceeds, cid))
        now = datetime.now(timezone.utc).isoformat()
        if left_qty <= float(row["qty"] or 0) * 0.001 or percent >= 99.9:
            conn.execute(
                """
                UPDATE user_positions
                SET status = 'closed', qty = 0, size_usd = 0, closed_at = ?, exit_mcap = ?, exit_price = ?, pnl_usd = COALESCE(pnl_usd, 0) + ?
                WHERE id = ?
                """,
                (now, mcap, price, pnl, row["id"]),
            )
        else:
            conn.execute(
                "UPDATE user_positions SET qty = ?, size_usd = ?, pnl_usd = COALESCE(pnl_usd, 0) + ? WHERE id = ?",
                (left_qty, left_size, pnl, row["id"]),
            )
    return {"ok": True, "proceeds": proceeds, "pnl": pnl, "closed": percent >= 99.9}


def set_exit(chat_id, token: str, *, tp: float | None = None, sl: float | None = None) -> bool:
    sets = []
    params: list = []
    if tp is not None:
        sets.append("tp_multiple = ?")
        params.append(tp)
    if sl is not None:
        sets.append("sl_multiple = ?")
        params.append(sl)
    if not sets:
        return False
    params.extend([str(chat_id), token])
    with db() as conn:
        conn.execute(
            f"UPDATE user_positions SET {', '.join(sets)} WHERE chat_id = ? AND token = ? AND status = 'open'",
            params,
        )
    return True


def settle_positions(prices: dict) -> list[tuple[str, str]]:
    """Auto-close paper positions that hit their own TP or SL. Returns (chat_id, message)."""
    notes = []
    with db() as conn:
        rows = conn.execute("SELECT * FROM user_positions WHERE status = 'open'").fetchall()
    for row in rows:
        quote = prices.get(row["token"])
        if not quote:
            continue
        price = float(quote.get("price") or 0)
        mcap = float(quote.get("mcap") or 0)
        entry_mcap = float(row["entry_mcap"] or 0)
        entry_price = float(row["entry_price"] or 0)
        multiple = None
        if entry_mcap > 0 and mcap > 0:
            multiple = mcap / entry_mcap
        elif entry_price > 0 and price > 0:
            multiple = price / entry_price
        if multiple is None:
            continue
        hit = None
        if multiple >= float(row["tp_multiple"] or 99):
            hit = "take profit"
        elif multiple <= float(row["sl_multiple"] or 0):
            hit = "stop loss"
        if not hit:
            continue
        result = paper_sell(row["chat_id"], row["token"], 100, price or entry_price, mcap or entry_mcap)
        if result.get("ok"):
            notes.append(
                (
                    str(row["chat_id"]),
                    f"<b>PAPER {hit.upper()}</b> ${esc(row['symbol'] or '?')} at {multiple:.2f}x · pnl {fmt_usd(result['pnl'])}",
                )
            )
    return notes


def update_fumbles(prices: dict) -> list[tuple[str, str]]:
    """Alert once when a closed paper sell later runs at least 2x from the exit."""
    notes = []
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM user_positions WHERE status = 'closed' AND exit_mcap > 0"
        ).fetchall()
        for row in rows:
            quote = prices.get(row["token"])
            if not quote:
                continue
            later = float(quote.get("mcap") or 0)
            exit_mcap = float(row["exit_mcap"] or 0)
            if exit_mcap <= 0 or later < exit_mcap * 2:
                continue
            existing = conn.execute(
                "SELECT 1 FROM fumbles WHERE chat_id = ? AND token = ?",
                (row["chat_id"], row["token"]),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                "INSERT INTO fumbles (chat_id, token, exit_mcap, later_mcap, created_at) VALUES (?, ?, ?, ?, ?)",
                (row["chat_id"], row["token"], exit_mcap, later, now),
            )
            multiple = later / exit_mcap
            notes.append(
                (
                    str(row["chat_id"]),
                    f"😬 <b>Fumble</b> ${esc(row['symbol'] or '?')} is {multiple:.1f}x from where you sold.",
                )
            )
    return notes


def add_wallet(chat_id, address: str, copy_paper: bool = False) -> dict:
    cid = str(chat_id)
    if get_subscriber(cid) is None:
        return {"ok": False, "error": "Send /start first."}
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM user_wallets WHERE chat_id = ?", (cid,)).fetchone()["n"]
        if int(count) >= 5:
            return {"ok": False, "error": "Wallet watchlist is full (5)."}
        conn.execute(
            """
            INSERT INTO user_wallets (chat_id, address, copy_paper, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, address) DO UPDATE SET copy_paper = excluded.copy_paper
            """,
            (cid, address, 1 if copy_paper else 0, datetime.now(timezone.utc).isoformat()),
        )
    return {"ok": True}


def list_wallets(chat_id) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT address, copy_paper FROM user_wallets WHERE chat_id = ? ORDER BY created_at",
            (str(chat_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def watched_addresses(limit: int = 8) -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT address FROM user_wallets ORDER BY address LIMIT ?",
            (limit,),
        ).fetchall()
    return [row["address"] for row in rows]


def copy_paper_buys(buys: list[dict], prices: dict) -> list[tuple[str, str]]:
    """Paper-buy $50 for users who copy a watched wallet, once per open mint."""
    notes = []
    by_wallet: dict[str, list[str]] = {}
    if not buys:
        return notes
    mints = {buy.get("mint") for buy in buys if buy.get("mint")}
    wallets = {buy.get("wallet") for buy in buys if buy.get("wallet")}
    if not mints or not wallets:
        return notes
    with db() as conn:
        rows = conn.execute(
            "SELECT chat_id, address FROM user_wallets WHERE copy_paper = 1"
        ).fetchall()
    for row in rows:
        by_wallet.setdefault(row["address"], []).append(str(row["chat_id"]))
    seen = set()
    for buy in buys:
        mint = buy.get("mint")
        wallet = buy.get("wallet")
        if not mint or (mint, wallet) in seen:
            continue
        seen.add((mint, wallet))
        quote = prices.get(mint) or {}
        price = float(quote.get("price") or 0)
        mcap = float(quote.get("mcap") or 0)
        if price <= 0:
            continue
        for chat_id in by_wallet.get(wallet, []):
            with db() as conn:
                already = conn.execute(
                    "SELECT 1 FROM user_positions WHERE chat_id = ? AND token = ? AND status = 'open'",
                    (chat_id, mint),
                ).fetchone()
            if already:
                continue
            result = paper_buy(
                chat_id,
                mint,
                quote.get("symbol") or "?",
                quote.get("chain") or "solana",
                price,
                mcap,
                user_settings(chat_id)["copy_usd"],
            )
            if result.get("ok"):
                spent = user_settings(chat_id)["copy_usd"]
                notes.append((chat_id, f"👀 <b>Copy buy</b> ${esc(quote.get('symbol') or '?')} paper ${spent:.0f}"))
    return notes


def wallet_text(chat_id) -> str:
    user = get_subscriber(chat_id)
    if not user:
        return "Send /start first."
    cash = float(user["cash_usd"] or 0)
    lines = [f"<b>Paper wallet</b>", f"Cash: {fmt_usd(cash)}", f"Started at {fmt_usd(STARTING_CASH)}"]
    positions = open_positions_for(chat_id)
    if not positions:
        lines.append("No open paper positions. Paste a contract or tap Buy on a call.")
    for row in positions:
        lines.append(
            f"${esc(row['symbol'] or '?')} · {fmt_usd(row['size_usd'])} · TP {row['tp_multiple']}x · SL {row['sl_multiple']}x"
        )
        lines.append(f"<code>{esc(row['token'])}</code>")
    return "\n".join(lines)


def open_position_tokens() -> list[str]:
    with db() as conn:
        rows = conn.execute("SELECT DISTINCT token FROM user_positions WHERE status = 'open'").fetchall()
    return [row["token"] for row in rows if row["token"]]


def open_positions_for(chat_id) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM user_positions WHERE chat_id = ? AND status = 'open' ORDER BY id DESC",
            (str(chat_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def positions_keyboard(chat_id) -> dict | None:
    rows = []
    for row in open_positions_for(chat_id)[:4]:
        token = row["token"]
        rows.append(
            [
                {"text": f"Sell 50% {row['symbol'] or ''}".strip(), "callback_data": f"s50:{token}"},
                {"text": "Sell all", "callback_data": f"s100:{token}"},
            ]
        )
    if not rows:
        return None
    rows.append([{"text": "« Menu", "callback_data": "menu:home"}])
    return {"inline_keyboard": rows}


def fumble_text(chat_id) -> str:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM fumbles WHERE chat_id = ? ORDER BY created_at DESC LIMIT 8",
            (str(chat_id),),
        ).fetchall()
    if not rows:
        return "No fumbles yet. This fills in when a paper sell later runs 2x from your exit."
    lines = ["<b>Fumbles</b>"]
    for row in rows:
        multiple = float(row["later_mcap"] or 0) / float(row["exit_mcap"] or 1)
        lines.append(f"{esc(row['token'][:4])}…{esc(row['token'][-4:])} ran {multiple:.1f}x after you sold")
    return "\n".join(lines)


def leaderboard_text(chat_id) -> str:
    with db() as conn:
        rows = conn.execute("SELECT chat_id, username, cash_usd, referral_code FROM subscribers").fetchall()
        pnl_rows = conn.execute(
            "SELECT chat_id, COALESCE(SUM(pnl_usd), 0) AS pnl FROM user_positions GROUP BY chat_id"
        ).fetchall()
        refs = conn.execute(
            "SELECT referrer, COUNT(*) AS n FROM subscribers WHERE referrer IS NOT NULL GROUP BY referrer"
        ).fetchall()
    pnl = {row["chat_id"]: float(row["pnl"] or 0) for row in pnl_rows}
    ranked = []
    for row in rows:
        equity_pnl = float(row["cash_usd"] or 0) - STARTING_CASH
        # cash change misses open cost; add realized pnl which is already in cash after sells
        # show realized pnl from positions plus cash delta from buys still open is double count.
        # Rank by realized pnl only, which is what competitions can audit.
        ranked.append((pnl.get(row["chat_id"], 0.0), row))
    ranked.sort(key=lambda item: item[0], reverse=True)
    lines = ["<b>Leaderboard</b> · realized paper PnL"]
    for index, (gain, row) in enumerate(ranked[:8], start=1):
        name = row["username"] or row["chat_id"]
        lines.append(f"{index}. {esc(name)} · {fmt_usd(gain)}")
    me = get_subscriber(chat_id)
    if me:
        lines.append("")
        lines.append(f"Your invite: {esc(invite_link(me['referral_code']))}")
    ref_map = {row["referrer"]: int(row["n"]) for row in refs}
    if ref_map:
        lines.append("")
        lines.append("<b>Invites</b>")
        top_refs = sorted(ref_map.items(), key=lambda item: item[1], reverse=True)[:5]
        for referrer, count in top_refs:
            lines.append(f"{esc(referrer)} · {count}")
    return "\n".join(lines)


def watch_text(chat_id) -> str:
    wallets = list_wallets(chat_id)
    if not wallets:
        return "No watched wallets. Send a Solana address after tapping Add, or send /watch <address>."
    lines = ["<b>Watched wallets</b>"]
    for row in wallets:
        flag = "copy on" if int(row["copy_paper"] or 0) else "copy off"
        lines.append(f"<code>{esc(row['address'])}</code> · {flag}")
    lines.append("Copy mode paper-buys $50 when that wallet buys a new mint.")
    return "\n".join(lines)
