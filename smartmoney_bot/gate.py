"""Entry rules for a Solana pair and its safety checks."""

from __future__ import annotations

from .settings import DEFAULTS, SOLANA_MINT


def _num(pair: dict, key: str) -> float:
    try:
        return float(pair.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def hard_filter(pair: dict, cfg: dict) -> tuple[bool, str]:
    """Market-structure gate from the original bot."""
    chain = str(pair.get("chain") or "").lower()
    if chain != str(cfg.get("chain", DEFAULTS["chain"])).lower():
        return False, "not solana"
    token = pair.get("token")
    if not token:
        return False, "missing token"
    if token == SOLANA_MINT:
        return False, "wrapped sol"
    mcap = _num(pair, "mcap")
    cap = float(cfg.get("max_entry_mcap", DEFAULTS["max_entry_mcap"]))
    if mcap <= 0 or mcap > cap:
        return False, f"mcap {mcap:.0f} outside cap"
    liquidity = _num(pair, "liquidity_usd")
    min_liq = float(cfg.get("min_liquidity_usd", DEFAULTS["min_liquidity_usd"]))
    if liquidity < min_liq:
        return False, f"liquidity {liquidity:.0f} too thin"
    volume = _num(pair, "volume_h1")
    min_vol = float(cfg.get("min_volume_h1", DEFAULTS["min_volume_h1"]))
    if volume < min_vol:
        return False, "h1 volume too low"
    age = pair.get("age_hours")
    max_age = float(cfg.get("max_pair_age_hours", DEFAULTS["max_pair_age_hours"]))
    if age is not None and float(age) > max_age:
        return False, "pair too old"
    return True, "pass"


def safety_filter(info: dict, cfg: dict) -> tuple[bool, str]:
    """Authority and holder checks. Missing data fails open unless required."""
    if info.get("mint_revoked") is False:
        return False, "mint authority still active"
    if info.get("freeze_revoked") is False:
        return False, "freeze authority still active"
    if cfg.get("require_authorities"):
        if info.get("mint_revoked") is not True or info.get("freeze_revoked") is not True:
            return False, "authority check unavailable"
    holders = info.get("holders")
    min_holders = int(cfg.get("min_holders", DEFAULTS["min_holders"]))
    if holders is not None and int(holders) < min_holders:
        return False, f"holders {holders} below {min_holders}"
    if cfg.get("require_holders") and holders is None:
        return False, "holder data unavailable"
    top = info.get("top_pct")
    max_top = float(cfg.get("max_top_holder_pct", DEFAULTS["max_top_holder_pct"]))
    if top is not None and float(top) > max_top:
        return False, f"top holder {float(top):.1f}% too concentrated"
    return True, "pass"


def passes(pair: dict, info: dict, cfg: dict, cluster_count: int = 0) -> tuple[bool, str]:
    ok, reason = hard_filter(pair, cfg)
    if not ok:
        return ok, reason
    ok, reason = safety_filter(info or {}, cfg)
    if not ok:
        return ok, reason
    if cfg.get("require_cluster"):
        need = int(cfg.get("min_cluster_wallets", DEFAULTS["min_cluster_wallets"]))
        if cluster_count < need:
            return False, f"smart-money cluster {cluster_count}/{need}"
    return True, "pass"
