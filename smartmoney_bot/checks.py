"""Extra checks. Holder data needs BIRDEYE_API_KEY. Authority uses public Solana RPC."""

from __future__ import annotations

import os

import requests

RPC = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")


def authorities(mint: str) -> dict:
    try:
        resp = requests.post(
            RPC,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [mint, {"encoding": "jsonParsed"}],
            },
            timeout=20,
        )
        info = ((resp.json().get("result") or {}).get("value") or {}).get("data", {}).get("parsed", {}).get("info", {})
    except requests.RequestException:
        return {"mint_revoked": None, "freeze_revoked": None}
    return {
        "mint_revoked": info.get("mintAuthority") in (None, ""),
        "freeze_revoked": info.get("freezeAuthority") in (None, ""),
    }


def holders(mint: str) -> dict:
    key = os.environ.get("BIRDEYE_API_KEY")
    if not key:
        return {"holders": None, "top_pct": None}
    try:
        resp = requests.get(
            "https://public-api.birdeye.so/defi/v3/token/holder",
            params={"address": mint, "limit": 10},
            headers={"X-API-KEY": key, "x-chain": "solana"},
            timeout=20,
        )
        if resp.status_code != 200:
            return {"holders": None, "top_pct": None}
        data = resp.json().get("data") or {}
        items = data.get("items") or []
        top = float(items[0].get("percentage") or items[0].get("ui_amount") or 0) if items else None
        return {"holders": data.get("total") or data.get("holder") or len(items), "top_pct": top}
    except (requests.RequestException, ValueError, IndexError):
        return {"holders": None, "top_pct": None}
