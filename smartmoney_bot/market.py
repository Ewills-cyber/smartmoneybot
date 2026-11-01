"""DexScreener new-launch feed. Solana only."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

BASE = "https://api.dexscreener.com"


def _get(path: str, retries: int = 3):
    url = f"{BASE}{path}"
    for attempt in range(retries):
        resp = requests.get(url, timeout=20)
        if resp.status_code == 429:
            time.sleep(2 + attempt * 2)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"rate limited: {path}")


def discovery_addresses() -> list[str]:
    data = _get("/token-profiles/latest/v1")
    if not isinstance(data, list):
        return []
    out = []
    for row in data:
        if row.get("chainId") != "solana":
            continue
        address = row.get("tokenAddress")
        if address and address not in out:
            out.append(address)
        if len(out) >= 30:
            break
    return out


def pairs_for_tokens(addresses: list[str]) -> list[dict]:
    if not addresses:
        return []
    joined = ",".join(addresses[:30])
    payload = _get(f"/tokens/v1/solana/{joined}")
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("pairs") or []
    return []


def pair_age_hours(pair: dict):
    created = pair.get("pairCreatedAt")
    if not created:
        return None
    created_dt = datetime.fromtimestamp(created / 1000, tz=timezone.utc)
    return (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600


def normalize_pair(pair: dict) -> dict:
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    vol = (pair.get("volume") or {}).get("h1") or 0
    price = float(pair.get("priceUsd") or 0)
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    base = pair.get("baseToken") or {}
    return {
        "chain": pair.get("chainId"),
        "pair": pair.get("pairAddress"),
        "token": base.get("address"),
        "symbol": base.get("symbol") or "?",
        "name": base.get("name") or "",
        "price": price,
        "mcap": float(mcap or 0),
        "liquidity_usd": float(liq or 0),
        "volume_h1": float(vol or 0),
        "age_hours": pair_age_hours(pair),
        "url": pair.get("url"),
        "dex": pair.get("dexId"),
    }
