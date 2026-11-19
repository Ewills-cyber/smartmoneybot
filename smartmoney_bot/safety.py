"""Token safety verdicts from documented public APIs.

Solana: RugCheck report, GoPlus Solana token security, DexScreener, and the
mint account from Solana JSON-RPC.
Base and other EVM chains: GoPlus token security and DexScreener.
Scores are a screening aid, not a promise that a token is safe.
"""

from __future__ import annotations

import logging
import re
import time

from .http import ApiError, request_json

log = logging.getLogger(__name__)

_SOL = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_EVM = re.compile(r"^0x[a-fA-F0-9]{40}$")
_cache: dict[str, tuple[float, object]] = {}

RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{address}/report"
GOPLUS_SOL = "https://api.gopluslabs.io/api/v1/solana/token_security"
GOPLUS_EVM = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search"
DEX_TOKENS = "https://api.dexscreener.com/tokens/v1/{chain}/{address}"

# GoPlus chain ids we actually call. Base is the EVM chain this bot supports.
EVM_CHAINS = {"base": "8453", "ethereum": "1", "bsc": "56"}


def clear_cache() -> None:
    _cache.clear()


def detect_address(text: str) -> str | None:
    raw = (text or "").strip()
    if _EVM.match(raw) or _SOL.match(raw):
        return raw
    return None


def score_facts(facts: dict) -> dict:
    """Turn normalised facts into a 0–100 score and BUY / CAUTION / SKIP."""
    score = 100
    reasons: list[str] = []
    critical = False

    if facts.get("honeypot") or facts.get("non_transferable"):
        score -= 60
        critical = True
        reasons.append("sells look blocked (honeypot or non-transferable)")
    sell_tax = _num(facts.get("sell_tax_pct"))
    if sell_tax is not None and sell_tax > 10:
        score -= 25
        reasons.append(f"sell tax {sell_tax:.0f}%")
    elif sell_tax is not None and sell_tax > 5:
        score -= 10
        reasons.append(f"sell tax {sell_tax:.0f}%")
    if facts.get("mint_revoked") is False:
        score -= 25
        reasons.append("mint authority is still active")
    if facts.get("freeze_revoked") is False:
        score -= 20
        reasons.append("freeze authority is still active")
    top = _num(facts.get("top_pct"))
    if top is not None and top > 20:
        score -= 20
        reasons.append(f"top holder owns {top:.0f}%")
    elif top is not None and top > 10:
        score -= 10
        reasons.append(f"top holder owns {top:.0f}%")
    dev = _num(facts.get("dev_pct"))
    if dev is not None and dev > 10:
        score -= 15
        reasons.append(f"dev or insider wallets hold {dev:.0f}%")
    lp = _num(facts.get("lp_locked_pct"))
    if facts.get("lp_known") and not facts.get("lp_burned") and (lp is None or lp < 50):
        score -= 15
        reasons.append("liquidity is not mostly locked or burned")
    holders = facts.get("holders")
    if isinstance(holders, int) and holders < 50:
        score -= 10
        reasons.append(f"only {holders} holders")
    dangers = int(facts.get("danger_count") or 0)
    if dangers:
        score -= min(45, dangers * 15)
        reasons.append(f"{dangers} high-risk flag(s) from RugCheck")
    if not reasons:
        reasons.append("no major flags in the sources that answered")
    score = max(0, min(100, score))
    if critical or score < 45:
        verdict = "SKIP"
    elif score < 70:
        verdict = "CAUTION"
    else:
        verdict = "BUY"
    return {"score": score, "verdict": verdict, "reasons": reasons}


def parse_rugcheck(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    risks = payload.get("risks") or []
    dangers = 0
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        if str(risk.get("level") or "").lower() in {"danger", "error", "critical"}:
            dangers += 1
    top_pct = None
    dev_pct = 0.0
    dev_seen = False
    for holder in payload.get("topHolders") or []:
        if not isinstance(holder, dict):
            continue
        pct = _num(holder.get("pct") if holder.get("pct") is not None else holder.get("percentage"))
        if pct is None:
            continue
        if top_pct is None:
            top_pct = pct
        if holder.get("insider") or holder.get("isInsider"):
            dev_pct += pct
            dev_seen = True
    lp_known = False
    lp_locked = None
    burned = False
    for market in payload.get("markets") or []:
        if not isinstance(market, dict):
            continue
        lp = market.get("lp") or {}
        if not isinstance(lp, dict):
            continue
        lp_known = True
        locked = _num(lp.get("lpLockedPct") if lp.get("lpLockedPct") is not None else lp.get("lpLocked"))
        if locked is not None:
            lp_locked = locked if lp_locked is None else max(lp_locked, locked)
        if lp.get("lpBurned") or lp.get("lpBurnt"):
            burned = True
    out = {"danger_count": dangers, "lp_known": lp_known, "lp_burned": burned}
    if top_pct is not None:
        out["top_pct"] = top_pct
    if dev_seen:
        out["dev_pct"] = dev_pct
    if lp_locked is not None:
        out["lp_locked_pct"] = lp_locked
    return out


def parse_goplus_solana(payload: dict | None, address: str) -> dict:
    row = _goplus_row(payload, address)
    if not row:
        return {}
    out = {}
    if _status_on(row.get("mintable")):
        out["mint_revoked"] = False
    if _status_on(row.get("freezable")):
        out["freeze_revoked"] = False
    if str(row.get("non_transferable") or "0") == "1":
        out["non_transferable"] = True
    fee = row.get("transfer_fee") or {}
    rate = None
    if isinstance(fee, dict):
        rate = _num(fee.get("current_fee_rate") or fee.get("fee_rate") or fee.get("transfer_fee_percentage"))
    if rate is not None:
        out["sell_tax_pct"] = rate * 100 if rate <= 1 else rate
    return out


def parse_goplus_evm(payload: dict | None, address: str) -> dict:
    row = _goplus_row(payload, address)
    if not row:
        return {}
    out = {}
    if str(row.get("is_honeypot") or "0") == "1":
        out["honeypot"] = True
    sell = _num(row.get("sell_tax"))
    if sell is not None:
        out["sell_tax_pct"] = sell * 100 if sell <= 1 else sell
    owner = str(row.get("owner_address") or "")
    if owner and owner.lower() != "0x0000000000000000000000000000000000000000":
        out["mint_revoked"] = False
    elif owner:
        out["mint_revoked"] = True
    locked = _num(row.get("lp_holder_count"))
    if str(row.get("is_open_source") or "") == "1" and locked == 0:
        out["lp_known"] = True
        out["lp_locked_pct"] = 0
    return out


def _goplus_row(payload: dict | None, address: str) -> dict:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("result") or payload.get("data") or {}
    if not isinstance(data, dict):
        return {}
    for key in (address, address.lower(), address.upper()):
        row = data.get(key)
        if isinstance(row, dict):
            return row
    return {}


def _status_on(value) -> bool:
    if isinstance(value, dict):
        return str(value.get("status") or "0") == "1"
    return str(value or "0") == "1"


def _num(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def chain_for_address(address: str, search_payload=None) -> str:
    if not address.startswith("0x"):
        return "solana"
    if isinstance(search_payload, dict):
        for pair in search_payload.get("pairs") or []:
            if isinstance(pair, dict) and str(pair.get("chainId") or "") == "base":
                return "base"
    return "base"


def format_verdict(result: dict) -> str:
    from .notify import esc, fmt_usd

    icon = {"BUY": "🟢", "CAUTION": "🟡", "SKIP": "🔴"}.get(result.get("verdict"), "⚪")
    lines = [
        f"{icon} <b>{esc(result.get('verdict') or 'SKIP')}</b> · score {int(result.get('score') or 0)}/100",
        f"{esc(result.get('symbol') or 'token')} · {esc(result.get('chain') or '')}",
        f"<code>{esc(result.get('address') or '')}</code>",
    ]
    if result.get("mcap"):
        lines.append(f"Market cap {esc(fmt_usd(result.get('mcap'), mcap=True))}")
    lines.append("")
    for reason in result.get("reasons") or []:
        lines.append(f"• {esc(reason)}")
    lines.append("")
    lines.append("Screening only. Not a buy recommendation. NFA.")
    return "\n".join(lines)


def token_keyboard(chain: str, address: str, sizes: tuple[float, float, float] | None = None) -> dict:
    """Inline keyboard: paper buy sizes plus outbound quick-buy links.

    Callbacks are b1/b2/b3 so each tap spends that user's saved size, even on
    a broadcast where the label was built for one chat.
    """
    chain = chain or "solana"
    amounts = sizes or (25.0, 50.0, 100.0)
    rows = [
        [
            {"text": f"Buy ${amounts[0]:g}", "callback_data": f"b1:{address}"},
            {"text": f"Buy ${amounts[1]:g}", "callback_data": f"b2:{address}"},
            {"text": f"Buy ${amounts[2]:g}", "callback_data": f"b3:{address}"},
        ]
    ]
    links = quick_buy_links(chain, address)
    link_row = [{"text": name, "url": url} for name, url in links[:3]]
    if link_row:
        rows.append(link_row)
    if len(links) > 3:
        rows.append([{"text": name, "url": url} for name, url in links[3:5]])
    rows.append([{"text": "« Menu", "callback_data": "menu:home"}])
    return {"inline_keyboard": rows}


def quick_buy_links(chain: str, address: str) -> list[tuple[str, str]]:
    if chain == "solana":
        return [
            ("GMGN", f"https://gmgn.ai/sol/token/{address}"),
            ("Photon", f"https://photon-sol.tinyastro.io/en/lp/{address}"),
            ("BullX", f"https://bullx.io/terminal?chainId=1399811149&address={address}"),
            ("Axiom", f"https://axiom.trade/t/{address}"),
            ("Trojan", f"https://t.me/solana_trojanbot?start=r-{address}"),
        ]
    if chain == "base":
        return [
            ("GMGN", f"https://gmgn.ai/base/token/{address}"),
            ("BasedBot", f"https://t.me/based_eth_bot?start=r_{address}"),
            ("Dex", f"https://dexscreener.com/base/{address}"),
        ]
    return [("Dex", f"https://dexscreener.com/{chain}/{address}")]


def main_menu() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "🔎 Safety check", "callback_data": "menu:safety"},
                {"text": "💼 Wallet", "callback_data": "menu:wallet"},
            ],
            [
                {"text": "📈 Positions", "callback_data": "menu:positions"},
                {"text": "🚨 Alerts", "callback_data": "menu:alerts"},
            ],
            [
                {"text": "👀 Watch wallets", "callback_data": "menu:watch"},
                {"text": "🏆 Leaderboard", "callback_data": "menu:board"},
            ],
            [
                {"text": "😬 Fumbles", "callback_data": "menu:fumbles"},
                {"text": "❓ Help", "callback_data": "menu:help"},
            ],
            [{"text": "⚙️ Settings", "callback_data": "menu:settings"}],
        ]
    }


def scan_address(address: str, *, fetch=None, rpc_call=None, now: float | None = None) -> dict:
    """Fetch documented sources and score them. `fetch(url)` is injectable."""
    getter = fetch or _fetch
    chain = "solana"
    search = None
    if address.startswith("0x"):
        search = _cached(DEX_SEARCH + "?q=" + address, getter, now)
        chain = chain_for_address(address, search)
    facts: dict = {"chain": chain, "address": address}
    if chain == "solana":
        facts.update(_safe_parse(lambda: parse_rugcheck(_cached(RUGCHECK.format(address=address), getter, now))))
        facts.update(
            _safe_parse(lambda: parse_goplus_solana(_cached(f"{GOPLUS_SOL}?contract_addresses={address}", getter, now), address))
        )
        if rpc_call is not None:
            from .checks import fetch_authorities

            try:
                facts.update(fetch_authorities(address, rpc_call, "https://api.mainnet-beta.solana.com"))
            except Exception as exc:  # noqa: BLE001
                log.warning("authority check failed: %s", exc)
    else:
        chain_id = EVM_CHAINS.get(chain, "8453")
        facts.update(
            _safe_parse(
                lambda: parse_goplus_evm(
                    _cached(f"{GOPLUS_EVM.format(chain_id=chain_id)}?contract_addresses={address}", getter, now),
                    address,
                )
            )
        )
    dex = _safe_parse(lambda: _cached(DEX_TOKENS.format(chain=chain, address=address), getter, now))
    pair = _best_pair(dex)
    if pair:
        facts["symbol"] = ((pair.get("baseToken") or {}).get("symbol")) or facts.get("symbol")
        facts["mcap"] = pair.get("marketCap") or pair.get("fdv")
        facts["price"] = pair.get("priceUsd")
    scored = score_facts(facts)
    scored.update(
        {
            "chain": chain,
            "address": address,
            "symbol": facts.get("symbol") or "?",
            "mcap": facts.get("mcap"),
            "price": _num(facts.get("price")),
        }
    )
    return scored


def _best_pair(payload):
    rows = payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        rows = payload.get("pairs") or []
    best = None
    best_liq = -1.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        liq = _num(((row.get("liquidity") or {}).get("usd"))) or 0
        if liq >= best_liq:
            best = row
            best_liq = liq
    return best


def _safe_parse(fn):
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001
        log.warning("safety source failed: %s", exc)
        return {}
    return value or {}


def _cached(url: str, fetch, now: float | None, ttl: float = 60.0):
    moment = time.time() if now is None else now
    hit = _cache.get(url)
    if hit and moment - hit[0] < ttl:
        return hit[1]
    payload = fetch(url)
    _cache[url] = (moment, payload)
    return payload


def _fetch(url: str):
    try:
        return request_json("GET", url, headers={"Accept": "application/json"}, retries=2, timeout=15)
    except ApiError as exc:
        log.warning("safety fetch failed: %s", exc)
        return {}
