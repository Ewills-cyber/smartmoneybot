"""Hard gate for the original Telegram bot."""

from __future__ import annotations


def hard_filter(pair: dict, cfg: dict) -> tuple[bool, str]:
    if pair.get("chain") != "solana":
        return False, "not solana"
    if not pair.get("token"):
        return False, "missing token"
    if pair["mcap"] <= 0 or pair["mcap"] > cfg["max_entry_mcap"]:
        return False, f"mcap {pair['mcap']:.0f} outside cap"
    if pair["liquidity_usd"] < cfg["min_liquidity_usd"]:
        return False, f"liquidity {pair['liquidity_usd']:.0f} too thin"
    if pair["volume_h1"] < cfg["min_volume_h1"]:
        return False, "h1 volume too low"
    age = pair.get("age_hours")
    if age is not None and age > cfg["max_pair_age_hours"]:
        return False, "pair too old"
    return True, "pass"
