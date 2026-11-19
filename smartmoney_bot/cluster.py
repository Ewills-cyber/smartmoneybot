"""Smart-money clusters: several watched wallets buying one mint in a short window.

Uses documented Solana JSON-RPC (public RPC, or Helius RPC when a key is set).
A buy is a parsed transaction where the watched wallet is a signer and its
balance of a non-SOL mint increased. Transfers that the wallet did not sign
are ignored, which drops most airdrops.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .http import ApiError
from .settings import SOLANA_MINT

log = logging.getLogger(__name__)

IGNORE_MINTS = {SOLANA_MINT}


def ui_amount(balance: dict) -> float:
    token = balance.get("uiTokenAmount") or {}
    if token.get("uiAmount") is not None:
        try:
            return float(token["uiAmount"])
        except (TypeError, ValueError):
            return 0.0
    raw = token.get("uiAmountString")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def signer_set(tx: dict) -> set[str]:
    message = ((tx.get("transaction") or {}).get("message") or {})
    keys = message.get("accountKeys") or []
    signers: set[str] = set()
    for index, key in enumerate(keys):
        if isinstance(key, str):
            if index == 0:
                signers.add(key)
        elif isinstance(key, dict) and key.get("signer") and key.get("pubkey"):
            signers.add(key["pubkey"])
    return signers


def buys_from_transaction(tx: dict | None, wallet: str, ignore_mints: set[str] | None = None) -> list[dict]:
    if not isinstance(tx, dict):
        return []
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
    if wallet not in signer_set(tx):
        return []
    ignored = ignore_mints if ignore_mints is not None else IGNORE_MINTS

    def totals(rows) -> dict[str, float]:
        out: dict[str, float] = {}
        for row in rows or []:
            if not isinstance(row, dict) or row.get("owner") != wallet:
                continue
            mint = row.get("mint")
            if not mint or mint in ignored:
                continue
            out[mint] = out.get(mint, 0.0) + ui_amount(row)
        return out

    before = totals(meta.get("preTokenBalances"))
    after = totals(meta.get("postTokenBalances"))
    stamp = tx.get("blockTime")
    buys = []
    for mint, amount in after.items():
        delta = amount - before.get(mint, 0.0)
        if delta > 0:
            buys.append({"mint": mint, "wallet": wallet, "ts": stamp, "delta": delta})
    return buys


def find_clusters(
    buys: list[dict],
    *,
    min_wallets: int,
    window_minutes: int,
    now: datetime,
) -> dict[str, list[dict]]:
    """Map mint -> unique wallet buys inside the window, when the count qualifies."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(minutes=window_minutes)
    grouped: dict[str, dict[str, dict]] = {}
    for buy in buys:
        mint = buy.get("mint")
        wallet = buy.get("wallet")
        stamp = _as_datetime(buy.get("ts"))
        if not mint or not wallet or stamp is None or stamp < cutoff:
            continue
        grouped.setdefault(mint, {})[wallet] = buy
    return {mint: list(wallets.values()) for mint, wallets in grouped.items() if len(wallets) >= min_wallets}


def collect_buys(
    wallets: list[str],
    *,
    rpc_url: str,
    rpc_call,
    window_minutes: int,
    now: datetime,
    seen: set[str],
    tx_budget: int,
) -> tuple[list[dict], list[tuple[str, str, int]]]:
    """Fetch recent signed buys. Stops the cycle early when the RPC returns 429."""
    buys: list[dict] = []
    fresh: list[tuple[str, str, int]] = []
    budget = tx_budget
    cutoff = now.timestamp() - window_minutes * 60
    for wallet in wallets:
        if budget <= 0:
            break
        try:
            signatures = rpc_call(rpc_url, "getSignaturesForAddress", [wallet, {"limit": 15}])
        except (ApiError, OSError, ValueError) as exc:
            log.warning("signature lookup failed for %s: %s", _short(wallet), exc)
            if getattr(exc, "status", None) == 429:
                break
            continue
        if not isinstance(signatures, list):
            continue
        for row in signatures:
            if budget <= 0:
                break
            if not isinstance(row, dict) or row.get("err"):
                continue
            signature = row.get("signature")
            block_time = int(row.get("blockTime") or 0)
            if not signature or signature in seen or block_time < cutoff:
                continue
            budget -= 1
            try:
                tx = rpc_call(
                    rpc_url,
                    "getTransaction",
                    [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                )
            except (ApiError, OSError, ValueError) as exc:
                log.warning("transaction lookup failed: %s", exc)
                fresh.append((signature, wallet, block_time))
                if getattr(exc, "status", None) == 429:
                    return buys, fresh
                continue
            fresh.append((signature, wallet, block_time))
            for buy in buys_from_transaction(tx, wallet):
                if buy.get("ts") is None:
                    buy["ts"] = block_time
                buys.append(buy)
    return buys, fresh


def _as_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    return None


def _short(wallet: str) -> str:
    if len(wallet) <= 10:
        return wallet
    return f"{wallet[:4]}…{wallet[-4:]}"
