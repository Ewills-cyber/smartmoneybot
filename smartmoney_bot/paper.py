"""Paper-position rules. Nothing here sends a transaction."""

from __future__ import annotations

from datetime import datetime, timezone

from .settings import DEFAULTS


def can_open(open_count: int, daily_loss_sol: float, cfg: dict) -> tuple[bool, str]:
    limit = int(cfg.get("max_open_positions", DEFAULTS["max_open_positions"]))
    if open_count >= limit:
        return False, f"max open positions ({limit})"
    loss_cap = float(cfg.get("max_daily_loss_sol", DEFAULTS["max_daily_loss_sol"]))
    if daily_loss_sol >= loss_cap:
        return False, f"daily loss {daily_loss_sol:.3f} SOL hit the cap"
    return True, "ok"


def position_size(sol_price: float | None, cfg: dict) -> tuple[float, float | None]:
    size_sol = float(cfg.get("trade_size_sol", DEFAULTS["trade_size_sol"]))
    if sol_price and sol_price > 0:
        return size_sol, size_sol * float(sol_price)
    return size_sol, None


def pnl_sol(size_sol: float | None, multiple: float | None) -> float | None:
    if size_sol is None or multiple is None:
        return None
    return float(size_sol) * (float(multiple) - 1)


def pnl_usd(size_usd: float | None, multiple: float | None) -> float | None:
    if size_usd is None or multiple is None:
        return None
    return float(size_usd) * (float(multiple) - 1)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def milestone_set(value: str | None) -> set[str]:
    return {part for part in (value or "").split(",") if part}


def evaluate_position(position: dict, now_mcap: float | None, now: datetime, cfg: dict) -> list[dict]:
    """Milestone alerts and a single close action.

    2x / 3x / 5x are alerts. The position stays open until take-profit,
    stop-loss, or the time stop. Take-profit uses ``take_profit_multiple``.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    actions: list[dict] = []
    entry = position.get("entry_mcap")
    multiple = None
    if entry and now_mcap:
        try:
            entry_f = float(entry)
            now_f = float(now_mcap)
        except (TypeError, ValueError):
            entry_f = 0
            now_f = 0
        if entry_f > 0 and now_f > 0:
            multiple = now_f / entry_f
    fired = milestone_set(position.get("milestones"))
    if multiple is not None:
        for rung in cfg.get("milestone_rungs", DEFAULTS["milestone_rungs"]):
            label = str(int(rung)) if float(rung) == int(rung) else str(rung)
            if multiple >= float(rung) and label not in fired:
                actions.append({"type": "milestone", "rung": label, "multiple": multiple})
        take_profit = float(cfg.get("take_profit_multiple", DEFAULTS["take_profit_multiple"]))
        stop_loss = float(cfg.get("stop_loss_multiple", DEFAULTS["stop_loss_multiple"]))
        if multiple >= take_profit:
            actions.append({"type": "close", "reason": "take_profit", "multiple": multiple})
            return actions
        if multiple <= stop_loss:
            actions.append({"type": "close", "reason": "stop_loss", "multiple": multiple})
            return actions
    opened = parse_time(position.get("opened_at"))
    time_stop = float(cfg.get("time_stop_minutes", DEFAULTS["time_stop_minutes"]))
    if opened is not None:
        age_minutes = (now - opened).total_seconds() / 60
        if age_minutes >= time_stop:
            actions.append({"type": "close", "reason": "time_stop", "multiple": multiple})
    return actions
