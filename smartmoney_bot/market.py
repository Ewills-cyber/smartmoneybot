"""DexScreener discovery and pair normalisation. Solana only."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .http import request_json
from .settings import SOLANA_MINT

log = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com"
PROFILE_PATH = "/token-profiles/latest/v1"
BOOST_PATH = "/token-boosts/latest/v1"

_sol_cache: dict = {"price": None, "at": 0.0}


def clear_caches() -> None:
    _sol_cache["price"] = None
    _sol_cache["at"] = 0.0


def rows_of(payload) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("pairs", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        if payload.get("tokenAddress") or payload.get("baseToken") or payload.get("pairAddress"):
            return [payload]
    return []


def token_address(row: dict) -> str | None:
    for key in ("tokenAddress", "address", "mint"):
        value = row.get(key)
        if value:
            return str(value)
    return None


def default_get(path: str):
    url = path if path.startswith("http") else f"{BASE}{path}"
    return request_json("GET", url, headers={"Accept": "application/json"})


def discover(get, chain: str, limit: int, extra: list[dict] | None = None) -> list[dict]:
    """Latest Solana profiles and boosts, plus caller-supplied addresses.

    Each item is ``{"address", "sources"}`` where ``sources`` is a set of tags.
    Profiles and boosts each contribute up to ``limit`` new addresses.
    ``extra`` items are kept even past that cap, so open positions and
    smart-money mints are not dropped. Tags: profile, boost, stream, cluster, open.
    """
    order: list[str] = []
    sources: dict[str, set[str]] = {}

    def add(address: str | None, source: str) -> bool:
        """Add a tag. Returns True when this call inserted a new address."""
        if not address:
            return False
        fresh = address not in sources
        if fresh:
            sources[address] = set()
            order.append(address)
        sources[address].add(source)
        return fresh

    for path, source in ((PROFILE_PATH, "profile"), (BOOST_PATH, "boost")):
        added = 0
        try:
            payload = get(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("discovery %s failed: %s", path, exc)
            continue
        for row in rows_of(payload):
            if str(row.get("chainId") or "").lower() != chain:
                continue
            if add(token_address(row), source):
                added += 1
                if added >= limit:
                    break

    for item in extra or []:
        add(item.get("address"), item.get("source") or "extra")
    return [{"address": address, "sources": sources[address]} for address in order]


def pairs_for_tokens(addresses: list[str], get=None, chain: str = "solana") -> list[dict]:
    fetch = get or default_get
    unique: list[str] = []
    for address in addresses:
        if address and address not in unique:
            unique.append(address)
    out: list[dict] = []
    for offset in range(0, len(unique), 30):
        chunk = unique[offset : offset + 30]
        path = f"/tokens/v1/{chain}/{','.join(chunk)}"
        try:
            payload = fetch(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("pair lookup failed: %s", exc)
            continue
        out.extend(rows_of(payload))
    return out


def pair_age_hours(pair: dict, now: datetime | None = None):
    created = pair.get("pairCreatedAt")
    if not created:
        return None
    try:
        stamp = float(created)
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    if stamp > 10**12:
        stamp = stamp / 1000
    moment = now or datetime.now(timezone.utc)
    created_dt = datetime.fromtimestamp(stamp, tz=timezone.utc)
    hours = (moment - created_dt).total_seconds() / 3600
    return max(0.0, hours)


def normalize_pair(pair: dict, now: datetime | None = None) -> dict:
    liquidity = (pair.get("liquidity") or {}).get("usd") or 0
    volume = (pair.get("volume") or {}).get("h1") or 0
    try:
        price = float(pair.get("priceUsd") or 0)
    except (TypeError, ValueError):
        price = 0.0
    try:
        mcap = float(pair.get("marketCap") or pair.get("fdv") or 0)
    except (TypeError, ValueError):
        mcap = 0.0
    try:
        fdv = float(pair.get("fdv") or 0)
    except (TypeError, ValueError):
        fdv = 0.0
    base = pair.get("baseToken") or {}
    txns = (pair.get("txns") or {}).get("h1") or {}
    change = pair.get("priceChange") or {}
    return {
        "chain": pair.get("chainId"),
        "pair": pair.get("pairAddress"),
        "token": base.get("address"),
        "symbol": base.get("symbol") or "?",
        "name": base.get("name") or "",
        "price": price,
        "mcap": mcap,
        "fdv": fdv,
        "liquidity_usd": float(liquidity or 0),
        "volume_h1": float(volume or 0),
        "age_hours": pair_age_hours(pair, now),
        "url": pair.get("url"),
        "dex": pair.get("dexId"),
        "price_change_h1": change.get("h1"),
        "buys_h1": txns.get("buys"),
        "sells_h1": txns.get("sells"),
    }


def select_best_pairs(raw_pairs: list[dict], now: datetime | None = None) -> dict[str, dict]:
    """Keep the deepest-liquidity pool for each token."""
    best: dict[str, dict] = {}
    for raw in raw_pairs:
        if not isinstance(raw, dict):
            continue
        norm = normalize_pair(raw, now)
        token = norm.get("token")
        if not token:
            continue
        current = best.get(token)
        if current is None or norm["liquidity_usd"] > current["liquidity_usd"]:
            best[token] = norm
    return best


def sol_price_from_pairs(raw_pairs: list[dict], mint: str = SOLANA_MINT) -> float | None:
    best_liq = -1.0
    price = None
    for raw in raw_pairs:
        base = (raw.get("baseToken") or {}).get("address")
        if base != mint:
            continue
        try:
            candidate = float(raw.get("priceUsd") or 0)
        except (TypeError, ValueError):
            continue
        if candidate <= 0:
            continue
        liquidity = float(((raw.get("liquidity") or {}).get("usd")) or 0)
        if liquidity >= best_liq:
            best_liq = liquidity
            price = candidate
    return price


def sol_price_usd(get=None, now_ts: float | None = None, ttl: float = 60) -> float | None:
    moment = time.time() if now_ts is None else now_ts
    cached = _sol_cache["price"]
    if cached and moment - _sol_cache["at"] < ttl:
        return cached
    fetch = get or default_get
    try:
        payload = fetch(f"/tokens/v1/solana/{SOLANA_MINT}")
    except Exception as exc:  # noqa: BLE001
        log.warning("SOL price lookup failed: %s", exc)
        return cached
    price = sol_price_from_pairs(rows_of(payload))
    if price:
        _sol_cache["price"] = price
        _sol_cache["at"] = moment
    return price or cached


def estimated_supply(pair: dict) -> float | None:
    """Circulating-or-FDV supply implied by price. Used only for holder share."""
    price = float(pair.get("price") or 0)
    if price <= 0:
        return None
    fdv = float(pair.get("fdv") or 0)
    mcap = float(pair.get("mcap") or 0)
    base = fdv or mcap
    if base <= 0:
        return None
    return base / price
