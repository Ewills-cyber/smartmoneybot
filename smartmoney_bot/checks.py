"""Mint authority (Solana RPC) and holder concentration (Birdeye, optional)."""

from __future__ import annotations

import logging

from .http import ApiError, request_json
from .settings import load_settings

log = logging.getLogger(__name__)

BIRDEYE_HOLDERS = "https://public-api.birdeye.so/defi/v3/token/holder"


def authorities_from_account(value: dict | None) -> dict:
    """Interpret getAccountInfo value. Missing data stays unknown, not 'revoked'."""
    unknown = {"mint_revoked": None, "freeze_revoked": None}
    if not isinstance(value, dict):
        return unknown
    data = value.get("data")
    if not isinstance(data, dict):
        return unknown
    info = (data.get("parsed") or {}).get("info")
    if not isinstance(info, dict):
        return unknown
    if "mintAuthority" not in info and "freezeAuthority" not in info:
        return unknown

    def flag(key: str):
        if key not in info:
            return None
        return info.get(key) in (None, "")

    return {"mint_revoked": flag("mintAuthority"), "freeze_revoked": flag("freezeAuthority")}


def fetch_authorities(mint: str, rpc_call, rpc_url: str) -> dict:
    try:
        result = rpc_call(
            rpc_url,
            "getAccountInfo",
            [mint, {"encoding": "jsonParsed"}],
        )
    except (ApiError, OSError, ValueError) as exc:
        log.warning("authority lookup failed for %s: %s", mint, exc)
        return {"mint_revoked": None, "freeze_revoked": None}
    value = None
    if isinstance(result, dict):
        value = result.get("value") if "value" in result else result
    return authorities_from_account(value if isinstance(value, dict) else None)


def holder_stats(payload: dict | None, supply: float | None = None) -> dict:
    """Top-holder share.

    Birdeye's holder list has ``ui_amount`` (a token count), not a percentage.
    Prefer an explicit percentage field. Otherwise estimate share from supply.
    ``top10HoldPercent`` is reported separately and is not used as the top-1 share.
    """
    data = {}
    if isinstance(payload, dict):
        data = payload.get("data") or {}
    items = data.get("items") or []
    holders = data.get("holder")
    if holders is None:
        holders = data.get("holders")
    top10 = data.get("top10HoldPercent")
    if top10 is None:
        top10 = data.get("top10_hold_percent")
    top_pct = None
    if items and isinstance(items[0], dict):
        first = items[0]
        for key in ("percentage", "percent", "holdPercent", "hold_percent"):
            if first.get(key) is not None:
                try:
                    top_pct = float(first[key])
                except (TypeError, ValueError):
                    top_pct = None
                break
        if top_pct is None and supply and supply > 0 and first.get("ui_amount") is not None:
            try:
                top_pct = float(first["ui_amount"]) / float(supply) * 100
            except (TypeError, ValueError):
                top_pct = None
    try:
        top10_value = float(top10) if top10 is not None else None
    except (TypeError, ValueError):
        top10_value = None
    try:
        holder_count = int(holders) if holders is not None else None
    except (TypeError, ValueError):
        holder_count = None
    return {"holders": holder_count, "top_pct": top_pct, "top10_pct": top10_value}


def fetch_holders(mint: str, supply: float | None, api_key: str | None, get=None) -> dict:
    empty = {"holders": None, "top_pct": None, "top10_pct": None}
    if not api_key:
        return empty
    headers = {"X-API-KEY": api_key, "x-chain": "solana", "Accept": "application/json"}
    params = {"address": mint, "limit": 10}
    try:
        if get is not None:
            payload = get(BIRDEYE_HOLDERS, params=params, headers=headers)
        else:
            payload = request_json("GET", BIRDEYE_HOLDERS, params=params, headers=headers)
    except (ApiError, OSError, ValueError) as exc:
        log.warning("holder lookup failed for %s: %s", mint, exc)
        return empty
    if not isinstance(payload, dict) or payload.get("success") is False:
        return empty
    return holder_stats(payload, supply)


def inspect_token(pair: dict, *, rpc_call, rpc_url: str, birdeye_key: str | None, supply: float | None) -> dict:
    info = fetch_authorities(pair["token"], rpc_call, rpc_url)
    info.update(fetch_holders(pair["token"], supply, birdeye_key))
    return info


def default_rpc_call(url: str, method: str, params: list):
    payload = request_json(
        "POST",
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"Content-Type": "application/json"},
    )
    if not isinstance(payload, dict):
        raise ApiError("RPC response was not an object")
    if payload.get("error"):
        message = payload["error"]
        status = None
        text = str(message).lower()
        if "429" in text or "rate" in text:
            status = 429
        raise ApiError(f"RPC {method} failed: {message}", status)
    return payload.get("result")


def check_pair(pair: dict) -> dict:
    """Convenience used by the scan loop. Reads current settings."""
    from .market import estimated_supply

    cfg = load_settings()
    return inspect_token(
        pair,
        rpc_call=default_rpc_call,
        rpc_url=cfg["solana_rpc"],
        birdeye_key=cfg.get("birdeye_api_key"),
        supply=estimated_supply(pair),
    )
