"""One poll: latest Solana profiles, gate, watch alert."""

from __future__ import annotations

from datetime import datetime, timezone

from . import gate, market, notify
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
            summary["watches"] += 1
            notify.send(notify.format_signal(pair, reason))
    return summary
