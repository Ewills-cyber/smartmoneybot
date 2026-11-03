"""One poll: latest Solana profiles, gate, watch alert."""

from __future__ import annotations

from datetime import datetime, timezone

from . import checks, gate, market, notify
from .settings import DEFAULTS
from .store import db


def cfg() -> dict:
    return dict(DEFAULTS)


def run_once() -> dict:
    addresses = market.discovery_addresses()
    raw_pairs = market.pairs_for_tokens(addresses)
    pairs = [market.normalize_pair(raw) for raw in raw_pairs]
    summary = {"scanned": len(pairs), "watches": 0}
    seen = set()
    settings = cfg()
    for pair in pairs:
        if not pair.get("token") or pair["token"] in seen:
            continue
        seen.add(pair["token"])
        ok, reason = gate.hard_filter(pair, settings)
        decision = "watch" if ok else "skip"
        with db() as conn:
            conn.execute(
                """
                INSERT INTO signals (token, pair, symbol, mcap, liquidity_usd, decision, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pair["token"],
                    pair.get("pair"),
                    pair["symbol"],
                    pair["mcap"],
                    pair["liquidity_usd"],
                    decision,
                    reason,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        if ok:
            info = {**checks.authorities(pair["token"]), **checks.holders(pair["token"])}
            if info.get("mint_revoked") is False or info.get("freeze_revoked") is False:
                continue
            if info.get("holders") is not None and info["holders"] < 150:
                continue
            if info.get("top_pct") is not None and info["top_pct"] > 15:
                continue
            summary["watches"] += 1
            with db() as conn:
                conn.execute(
                    """
                    INSERT INTO paper_calls (token, symbol, entry_mcap, size_usd, status, opened_at)
                    VALUES (?, ?, ?, 20, 'open', ?)
                    ON CONFLICT(token) DO NOTHING
                    """,
                    (pair["token"], pair["symbol"], pair["mcap"], datetime.now(timezone.utc).isoformat()),
                )
            notify.send(notify.format_call(pair, info))
    with db() as conn:
        open_calls = conn.execute("SELECT * FROM paper_calls WHERE status = 'open'").fetchall()
        prices = {pair["token"]: pair["mcap"] for pair in pairs}
        for row in open_calls:
            now_mcap = prices.get(row["token"])
            if not now_mcap or not row["entry_mcap"]:
                continue
            multiple = now_mcap / row["entry_mcap"]
            rung = None
            if multiple >= 5:
                rung = "5x"
            elif multiple >= 3:
                rung = "3x"
            elif multiple >= 2:
                rung = "2x"
            if not rung:
                continue
            pnl = 20 * (multiple - 1)
            conn.execute("UPDATE paper_calls SET status = ? WHERE token = ?", (rung, row["token"]))
            notify.send(f"${row['symbol']} paper {rung} | entry ${row['entry_mcap']:,.0f} now ${now_mcap:,.0f} | +${pnl:.2f}")
    return summary
