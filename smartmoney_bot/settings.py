"""Runtime configuration. Secrets come from the environment only."""

from __future__ import annotations

import logging
import os
import re

SOLANA_MINT = "So11111111111111111111111111111111111111112"

DEFAULTS = {
    "chain": "solana",
    "poll_seconds": 45,
    "max_entry_mcap": 250_000,
    "min_liquidity_usd": 12_000,
    "min_volume_h1": 8_000,
    "max_pair_age_hours": 72,
    "min_cluster_wallets": 2,
    "cluster_window_minutes": 15,
    "trade_size_sol": 0.05,
    "max_open_positions": 4,
    "max_daily_loss_sol": 0.3,
    "take_profit_multiple": 2.5,
    "stop_loss_multiple": 0.65,
    "time_stop_minutes": 180,
    "min_holders": 150,
    "max_top_holder_pct": 15.0,
    "discovery_limit": 30,
    "max_smart_wallets": 8,
    "cluster_tx_budget": 24,
    "require_cluster": False,
    "require_authorities": False,
    "require_holders": False,
    "milestone_rungs": (2.0, 3.0, 5.0),
}

_WALLET_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_TRUE = {"1", "true", "yes", "on"}
_EMPTY = {"", "none", "unused", "changeme", "null", "-", "na", "n/a"}

log = logging.getLogger(__name__)
_cluster_warning_sent = False


def setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def load_settings() -> dict:
    """Read defaults, then override from the environment."""
    cfg = dict(DEFAULTS)
    cfg["poll_seconds"] = _clamp_int("POLL_SECONDS", 45, low=15, high=3600)
    cfg["max_entry_mcap"] = _float("MAX_ENTRY_MCAP", DEFAULTS["max_entry_mcap"], low=0)
    cfg["min_liquidity_usd"] = _float("MIN_LIQUIDITY_USD", DEFAULTS["min_liquidity_usd"], low=0)
    cfg["min_volume_h1"] = _float("MIN_VOLUME_H1", DEFAULTS["min_volume_h1"], low=0)
    cfg["max_pair_age_hours"] = _float("MAX_PAIR_AGE_HOURS", DEFAULTS["max_pair_age_hours"], low=0)
    cfg["min_cluster_wallets"] = _clamp_int("MIN_CLUSTER_WALLETS", 2, low=1, high=50)
    cfg["cluster_window_minutes"] = _clamp_int("CLUSTER_WINDOW_MINUTES", 15, low=1, high=24 * 60)
    cfg["trade_size_sol"] = _float("TRADE_SIZE_SOL", DEFAULTS["trade_size_sol"], low=0)
    cfg["max_open_positions"] = _clamp_int("MAX_OPEN_POSITIONS", 4, low=1, high=50)
    cfg["max_daily_loss_sol"] = _float("MAX_DAILY_LOSS_SOL", DEFAULTS["max_daily_loss_sol"], low=0)
    cfg["take_profit_multiple"] = _float("TAKE_PROFIT_MULTIPLE", DEFAULTS["take_profit_multiple"], low=1.01)
    cfg["stop_loss_multiple"] = _float("STOP_LOSS_MULTIPLE", DEFAULTS["stop_loss_multiple"], low=0.01, high=0.99)
    cfg["time_stop_minutes"] = _clamp_int("TIME_STOP_MINUTES", 180, low=1, high=7 * 24 * 60)
    cfg["min_holders"] = _clamp_int("MIN_HOLDERS", 150, low=0, high=1_000_000)
    cfg["max_top_holder_pct"] = _float("MAX_TOP_HOLDER_PCT", DEFAULTS["max_top_holder_pct"], low=0, high=100)
    cfg["discovery_limit"] = _clamp_int("DISCOVERY_LIMIT", 30, low=1, high=60)
    cfg["max_smart_wallets"] = _clamp_int("MAX_SMART_WALLETS", 8, low=1, high=25)
    cfg["cluster_tx_budget"] = _clamp_int("CLUSTER_TX_BUDGET", 24, low=1, high=100)
    cfg["require_cluster"] = _bool("REQUIRE_CLUSTER", False)
    cfg["require_authorities"] = _bool("REQUIRE_AUTHORITIES", False)
    cfg["require_holders"] = _bool("REQUIRE_HOLDERS", False)
    cfg["chain"] = (os.environ.get("CHAIN") or "solana").strip().lower() or "solana"
    cfg["trade_mode"] = _mode("TRADE_MODE")
    cfg["gmgn_api_key"] = _blank("GMGN_API_KEY")
    cfg["gmgn_private_key"] = _pem("GMGN_PRIVATE_KEY")
    cfg["gmgn_wallet"] = _wallet("GMGN_WALLET")
    cfg["gmgn_host"] = _blank("GMGN_HOST") or "https://openapi.gmgn.ai"
    cfg["gmgn_slippage"] = _optional_int("GMGN_SLIPPAGE", low=0, high=100)
    cfg["gmgn_anti_mev"] = _bool("GMGN_ANTI_MEV", True)
    cfg["gmgn_priority_fee"] = _blank("GMGN_PRIORITY_FEE") or "0.00001"
    cfg["gmgn_tip_fee"] = _blank("GMGN_TIP_FEE") or "0.00001"
    cfg["gmgn_confirm_attempts"] = _clamp_int("GMGN_CONFIRM_ATTEMPTS", 3, low=1, high=6)
    cfg["gmgn_live_storage_ok"] = _live_storage_ok()
    cfg["gmgn_ready"] = bool(cfg["gmgn_api_key"] and cfg["gmgn_private_key"] and cfg["gmgn_wallet"])

    cfg["telegram_token"] = _blank("TELEGRAM_BOT_TOKEN")
    cfg["telegram_chat_ids"] = _chat_ids(os.environ.get("TELEGRAM_CHAT_ID", ""))
    cfg["birdeye_api_key"] = _blank("BIRDEYE_API_KEY")
    cfg["helius_api_key"] = _blank("HELIUS_API_KEY")
    cfg["solana_rpc"] = _rpc_url(cfg["helius_api_key"])
    raw_wallets = os.environ.get("SMART_WALLETS", "")
    if raw_wallets.strip().lower() in _EMPTY:
        raw_wallets = ""
    cfg["smart_wallets"] = parse_wallets(raw_wallets, cfg["max_smart_wallets"])
    cfg["db_path"] = os.environ.get("SMARTMONEY_DB") or ""
    cfg["log_level"] = os.environ.get("LOG_LEVEL", "INFO")
    public_url = _blank("KEEP_AWAKE_URL") or _blank("RENDER_EXTERNAL_URL")
    cfg["keep_awake_url"] = public_url
    cfg["keep_awake"] = _bool("KEEP_AWAKE", bool(public_url))
    cfg["enable_profile_stream"] = _bool("ENABLE_PROFILE_STREAM", True)
    if cfg["require_cluster"] and not cfg["smart_wallets"]:
        global _cluster_warning_sent
        if not _cluster_warning_sent:
            log.warning("REQUIRE_CLUSTER is on and SMART_WALLETS is empty, so no new calls can pass")
            _cluster_warning_sent = True
    return cfg


def parse_wallets(raw: str, limit: int = 8) -> list[str]:
    found: list[str] = []
    for part in re.split(r"[\s,]+", raw or ""):
        wallet = part.strip()
        if not wallet:
            continue
        if not _WALLET_RE.match(wallet):
            log.warning("ignoring invalid wallet address %s", wallet[:6])
            continue
        if wallet not in found:
            found.append(wallet)
        if len(found) >= limit:
            break
    return found


def public_config(cfg: dict) -> dict:
    """Settings safe to show in Telegram. Secrets stay out."""
    keys = (
        "chain",
        "poll_seconds",
        "max_entry_mcap",
        "min_liquidity_usd",
        "min_volume_h1",
        "max_pair_age_hours",
        "min_cluster_wallets",
        "cluster_window_minutes",
        "trade_size_sol",
        "max_open_positions",
        "max_daily_loss_sol",
        "take_profit_multiple",
        "stop_loss_multiple",
        "time_stop_minutes",
        "min_holders",
        "max_top_holder_pct",
        "require_cluster",
        "require_authorities",
        "require_holders",
        "discovery_limit",
        "trade_mode",
    )
    shown = {key: cfg[key] for key in keys}
    shown["smart_wallets"] = len(cfg.get("smart_wallets") or [])
    shown["birdeye"] = bool(cfg.get("birdeye_api_key"))
    shown["helius"] = bool(cfg.get("helius_api_key"))
    shown["telegram"] = bool(cfg.get("telegram_token") and cfg.get("telegram_chat_ids"))
    shown["gmgn"] = bool(cfg.get("gmgn_ready"))
    return shown


def _live_storage_ok() -> bool:
    """Live buys are refused on an ephemeral path unless explicitly overridden.

    Render's free disk is wiped on restart. Forgetting an open buy would make
    the next boot buy the same mint again.
    """
    if _bool("GMGN_ALLOW_EPHEMERAL_DB", False):
        return True
    from .store import db_path

    path = str(db_path())
    return not (path == "/tmp" or path.startswith("/tmp/") or path.startswith("/var/tmp/"))


def _mode(name: str) -> str:
    raw = (os.environ.get(name) or "paper").strip().lower()
    if raw in _EMPTY:
        return "paper"
    if raw not in {"off", "paper", "live"}:
        log.warning("TRADE_MODE %s is not off, paper, or live; using paper", raw)
        return "paper"
    return raw


def _pem(name: str) -> str | None:
    from .gmgn import normalize_pem

    return normalize_pem(_blank(name))


def _wallet(name: str) -> str | None:
    raw = _blank(name)
    if not raw:
        return None
    if not _WALLET_RE.match(raw):
        log.warning("GMGN_WALLET is not a Solana address")
        return None
    return raw


def _optional_int(name: str, *, low: int, high: int) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "" or raw.strip().lower() in _EMPTY:
        return None
    try:
        value = int(raw)
    except ValueError:
        log.warning("invalid %s=%r, ignoring it", name, raw)
        return None
    if value < low or value > high:
        log.warning("%s=%s outside %s-%s, ignoring it", name, value, low, high)
        return None
    return value


def _blank(name: str) -> str | None:
    raw = (os.environ.get(name) or "").strip()
    if raw.lower() in _EMPTY:
        return None
    return raw


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


def _float(name: str, default: float, *, low: float | None = None, high: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        value = float(default)
    else:
        try:
            value = float(raw)
        except ValueError:
            log.warning("invalid %s=%r, using %s", name, raw, default)
            value = float(default)
    if low is not None and value < low:
        log.warning("%s=%s is below %s, using %s", name, value, low, default)
        return float(default)
    if high is not None and value > high:
        log.warning("%s=%s is above %s, using %s", name, value, high, default)
        return float(default)
    return value


def _clamp_int(name: str, default: int, *, low: int, high: int) -> int:
    value = int(_float(name, default))
    if value < low or value > high:
        log.warning("%s=%s outside %s-%s, using %s", name, value, low, high, default)
        return default
    return value


def _chat_ids(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip() and part.strip().lower() not in _EMPTY]


def _rpc_url(helius_key: str | None) -> str:
    explicit = _blank("SOLANA_RPC")
    if explicit:
        return explicit
    if helius_key:
        return f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
    return "https://api.mainnet-beta.solana.com"
