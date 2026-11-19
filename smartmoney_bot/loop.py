"""One scan: discover, gate, paper-trade, alert. Repeats until stopped."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

import requests

from . import checks, market
from .cluster import collect_buys, find_clusters
from .gate import hard_filter, passes
from .gmgn import GmgnClient, TradeResult, entry_decision, should_submit_sell
from .http import redact, safe_url
from .notify import format_call, format_close, format_milestone, format_trade, send
from .paper import evaluate_position, milestone_set, pnl_sol, pnl_usd, position_size
from .settings import SOLANA_MINT, load_settings
from .store import (
    claim_alert,
    daily_loss_sol,
    db,
    exposure_count,
    known_tokens,
    open_position,
    open_positions,
    paused,
    prune,
    record_fill,
    record_signal,
    remember_signatures,
    seen_signatures,
    set_meta,
)
from .stream import get_stream, start_stream

log = logging.getLogger(__name__)

_scan_lock = threading.Lock()
_last_ping = 0.0


def run_once(
    *,
    now: datetime | None = None,
    get=None,
    rpc_call=None,
    sender=None,
    check_token=None,
    trader=None,
) -> dict:
    """Run a single scan. Overlapping scans return immediately."""
    if not _scan_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "scan already running", "scanned": 0, "watches": 0, "open": 0}
    try:
        return _run_once(
            now=now,
            get=get,
            rpc_call=rpc_call,
            sender=sender,
            check_token=check_token,
            trader=trader,
        )
    finally:
        _scan_lock.release()


def serve(stop_flag: dict) -> None:
    from .store import log_storage

    cfg = load_settings()
    log_storage(log)
    log.info("scanner started in %s mode", cfg.get("trade_mode", "paper"))
    if cfg.get("enable_profile_stream"):
        try:
            start_stream()
        except Exception as exc:  # noqa: BLE001
            log.warning("profile stream not started: %s", redact(exc))
    while not stop_flag.get("stop"):
        cfg = load_settings()
        try:
            summary = run_once()
            log.info("scan %s", summary)
        except Exception as exc:  # noqa: BLE001
            log.warning("cycle failed: %s", redact(exc))
            try:
                set_meta("last_error", redact(exc)[:500])
            except Exception:  # noqa: BLE001
                log.debug("could not store last error", exc_info=True)
        for _ in range(int(cfg["poll_seconds"])):
            if stop_flag.get("stop"):
                return
            time.sleep(1)


def _run_once(*, now, get, rpc_call, sender, check_token, trader=None) -> dict:
    cfg = load_settings()
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    fetch = get or market.default_get
    maybe_keep_awake(cfg)
    is_paused = paused()

    with db() as conn:
        opens = open_positions(conn)
        known = known_tokens(conn)
        seen = seen_signatures(conn)
        open_count = exposure_count(conn)
        loss = daily_loss_sol(conn, moment)

    if is_paused and open_count == 0:
        summary = {"paused": True, "scanned": 0, "watches": 0, "open": 0, "clusters": 0}
        _save_summary(summary, moment)
        return summary

    active_trader = trader if cfg.get("trade_mode") == "live" else None
    if cfg.get("trade_mode") == "live" and active_trader is None and cfg.get("gmgn_ready"):
        active_trader = GmgnClient(cfg)
    messages: list[str] = []
    if active_trader is not None:
        opens, known, open_count, loss = _refresh_live_orders(
            opens, known, open_count, loss, active_trader, moment, messages
        )

    extra = [{"address": row["token"], "source": "open"} for row in opens if row.get("token")]
    stream = get_stream()
    if stream is not None:
        for address in stream.drain():
            extra.append({"address": address, "source": "stream"})

    cluster_map: dict[str, list[dict]] = {}
    new_sigs: list[tuple[str, str, int]] = []
    if cfg["smart_wallets"]:
        rpc = rpc_call or checks.default_rpc_call
        try:
            buys, new_sigs = collect_buys(
                cfg["smart_wallets"],
                rpc_url=cfg["solana_rpc"],
                rpc_call=rpc,
                window_minutes=cfg["cluster_window_minutes"],
                now=moment,
                seen=seen,
                tx_budget=cfg["cluster_tx_budget"],
            )
            cluster_map = find_clusters(
                buys,
                min_wallets=cfg["min_cluster_wallets"],
                window_minutes=cfg["cluster_window_minutes"],
                now=moment,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("cluster scan failed: %s", redact(exc))
        for mint in cluster_map:
            extra.append({"address": mint, "source": "cluster"})

    discovered = market.discover(fetch, cfg["chain"], cfg["discovery_limit"], extra)
    origins = {item["address"]: set(item["sources"]) for item in discovered}
    raw_pairs = market.pairs_for_tokens([item["address"] for item in discovered], get=fetch, chain=cfg["chain"])
    best = market.select_best_pairs(raw_pairs, moment)
    for token in cluster_map:
        origins.setdefault(token, set()).add("cluster")
    sol = market.sol_price_usd(fetch, now_ts=moment.timestamp())

    candidates = []
    for token, pair in best.items():
        if not token or token == SOLANA_MINT or token in known:
            continue
        cluster = cluster_map.get(token) or []
        try:
            ok, reason = hard_filter(pair, cfg)
            info: dict = {}
            if ok:
                checker = check_token or checks.check_pair
                try:
                    info = checker(pair) or {}
                except Exception as exc:  # noqa: BLE001
                    log.warning("checks failed for %s: %s", token, redact(exc))
                    info = {}
                ok, reason = passes(pair, info, cfg, len(cluster))
        except Exception as exc:  # noqa: BLE001
            log.warning("pair %s skipped after error: %s", token, redact(exc))
            ok, reason, info, cluster = False, "check error", {}, cluster
        candidates.append({"pair": pair, "ok": ok, "reason": reason, "info": info, "cluster": cluster})

    watches = 0
    plans = []
    simulated_open = open_count
    for item in candidates:
        pair = item["pair"]
        if not item["ok"]:
            plans.append({"kind": "skip", "pair": pair, "reason": item["reason"], "item": item})
            continue
        kind, why = entry_decision(
            cfg,
            paused=is_paused,
            open_count=simulated_open,
            daily_loss=loss,
        )
        if kind == "skip":
            plans.append({"kind": "skip", "pair": pair, "reason": why, "item": item})
            continue
        size_sol, size_usd = position_size(sol, cfg)
        result = None
        if kind == "live":
            if active_trader is None:
                plans.append({"kind": "skip", "pair": pair, "reason": "gmgn client unavailable", "item": item})
                continue
            try:
                result = active_trader.buy(pair["token"], pair.get("symbol") or "?", size_sol)
            except Exception as exc:  # noqa: BLE001
                log.warning("live buy crashed: %s", redact(exc))
                result = TradeResult(ok=False, status="failed", error=redact(exc), retryable=True)
        plans.append(
            {
                "kind": kind,
                "pair": pair,
                "reason": item["reason"],
                "item": item,
                "size_sol": size_sol,
                "size_usd": size_usd,
                "result": result,
            }
        )
        if kind == "paper" or (result is not None and result.occupies_slot):
            simulated_open += 1

    sell_jobs: list[dict] = []
    with db() as conn:
        for plan in plans:
            pair = plan["pair"]
            token = pair["token"]
            if plan["kind"] == "skip":
                record_signal(conn, pair, "skip", plan["reason"], moment)
                continue
            if plan["kind"] == "live":
                result = plan["result"]
                _record_live_buy(conn, plan, result, origins, moment, messages, cfg)
                if result is not None and (result.occupies_slot or (result.terminal and not result.retryable)):
                    watches += 1 if result.ok else 0
                continue
            size_sol = plan["size_sol"]
            size_usd = plan["size_usd"]
            item = plan["item"]
            source = source_label(origins.get(token, set()), len(item["cluster"]))
            inserted = open_position(
                conn,
                pair,
                size_sol=size_sol,
                size_usd=size_usd,
                source=source,
                now=moment,
            )
            if not inserted:
                continue
            record_signal(conn, pair, "watch", plan["reason"], moment)
            watches += 1
            if claim_alert(conn, token, "call", moment):
                position = {
                    "token": token,
                    "symbol": pair.get("symbol"),
                    "entry_mcap": pair.get("mcap"),
                    "size_sol": size_sol,
                    "size_usd": size_usd,
                    "source": source,
                }
                messages.append(format_call(pair, item["info"], position, item["cluster"], cfg))

        for position in open_positions(conn):
            if position.get("status") != "open":
                continue
            pair = best.get(position["token"])
            now_mcap = pair.get("mcap") if pair else None
            if now_mcap:
                conn.execute(
                    "UPDATE paper_calls SET last_mcap = ?, pair = COALESCE(?, pair) WHERE token = ?",
                    (now_mcap, pair.get("pair") if pair else None, position["token"]),
                )
            actions = evaluate_position(position, now_mcap, moment, cfg)
            milestones = milestone_set(position.get("milestones"))
            if (position.get("mode") or "paper") == "live":
                for action in actions:
                    if action["type"] == "milestone":
                        milestones.add(str(action["rung"]))
                        if claim_alert(conn, position["token"], f"m{action['rung']}", moment):
                            messages.append(
                                format_milestone(position, str(action["rung"]), action["multiple"], now_mcap)
                            )
                        continue
                    if should_submit_sell(position, action):
                        sell_jobs.append(
                            {
                                "token": position["token"],
                                "symbol": position.get("symbol"),
                                "reason": action["reason"],
                                "multiple": action.get("multiple"),
                                "now_mcap": now_mcap,
                                "position": position,
                            }
                        )
                        break
                if milestones != milestone_set(position.get("milestones")):
                    conn.execute(
                        "UPDATE paper_calls SET milestones = ? WHERE token = ?",
                        (_join_milestones(milestones), position["token"]),
                    )
                continue
            closed = False
            for action in actions:
                if action["type"] == "milestone":
                    milestones.add(str(action["rung"]))
                    if claim_alert(conn, position["token"], f"m{action['rung']}", moment):
                        messages.append(format_milestone(position, str(action["rung"]), action["multiple"], now_mcap))
                    continue
                multiple = action.get("multiple")
                joined = _join_milestones(milestones)
                conn.execute(
                    """
                    UPDATE paper_calls
                    SET status = ?, milestones = ?, closed_at = ?, exit_mcap = ?,
                        pnl_sol = ?, pnl_usd = ?, last_mcap = COALESCE(?, last_mcap)
                    WHERE token = ? AND status = 'open'
                    """,
                    (
                        action["reason"],
                        joined,
                        moment.isoformat(),
                        now_mcap,
                        pnl_sol(position.get("size_sol"), multiple),
                        pnl_usd(position.get("size_usd"), multiple),
                        now_mcap,
                        position["token"],
                    ),
                )
                closed = True
                changed = conn.execute("SELECT changes()").fetchone()[0]
                if changed == 1 and claim_alert(conn, position["token"], action["reason"], moment):
                    messages.append(format_close(position, action["reason"], multiple, now_mcap))
                break
            if not closed and milestones != milestone_set(position.get("milestones")):
                conn.execute(
                    "UPDATE paper_calls SET milestones = ? WHERE token = ?",
                    (_join_milestones(milestones), position["token"]),
                )
        if new_sigs:
            remember_signatures(conn, new_sigs)
        prune(conn, moment)

    for job in sell_jobs:
        _submit_live_sell(job, active_trader, moment, messages)

    with db() as conn:
        open_now = conn.execute("SELECT COUNT(*) AS n FROM paper_calls WHERE status = 'open'").fetchone()["n"]

    try:
        _extend_prices_for_users(best, fetch, cfg, moment)
        personal = _user_book(best, cfg, moment, seen, rpc_call)
    except Exception as exc:  # noqa: BLE001
        log.warning("user book failed: %s", redact(exc))
        personal = []

    summary = {
        "paused": is_paused,
        "scanned": len([token for token in best if token and token != SOLANA_MINT]),
        "watches": watches,
        "open": open_now,
        "clusters": len(cluster_map),
        "skipped": False,
    }
    _save_summary(summary, moment)
    deliver = sender or send
    for message in messages:
        try:
            if deliver is send:
                send(message)
            else:
                deliver(message)
        except Exception as exc:  # noqa: BLE001
            log.warning("alert delivery failed: %s", redact(exc))
    for chat_id, text in personal:
        try:
            if deliver is send:
                send(text, chat_ids=[str(chat_id)])
            else:
                deliver(f"{chat_id}: {text}")
        except Exception as exc:  # noqa: BLE001
            log.warning("personal alert failed: %s", redact(exc))
    return summary


def _extend_prices_for_users(best: dict, fetch, cfg: dict, moment: datetime) -> None:
    from .community import open_position_tokens

    missing = [token for token in open_position_tokens() if token not in best][:30]
    if not missing:
        return
    try:
        raw = market.pairs_for_tokens(missing, get=fetch, chain=cfg.get("chain") or "solana")
    except Exception as exc:  # noqa: BLE001
        log.warning("user price lookup failed: %s", redact(exc))
        return
    best.update(market.select_best_pairs(raw, moment))


def _user_book(best: dict, cfg: dict, moment: datetime, seen: set, rpc_call) -> list[tuple[str, str]]:
    from .community import copy_paper_buys, settle_positions, update_fumbles, watched_addresses

    prices = {
        token: {
            "price": pair.get("price"),
            "mcap": pair.get("mcap"),
            "symbol": pair.get("symbol"),
            "chain": pair.get("chain") or cfg.get("chain") or "solana",
        }
        for token, pair in best.items()
    }
    notes = []
    try:
        notes.extend(settle_positions(prices))
        notes.extend(update_fumbles(prices))
        wallets = watched_addresses(8)
        if wallets:
            from .checks import default_rpc_call
            from .cluster import collect_buys

            buys, signatures = collect_buys(
                wallets,
                rpc_url=cfg["solana_rpc"],
                rpc_call=rpc_call or default_rpc_call,
                window_minutes=cfg["cluster_window_minutes"],
                now=moment,
                seen=seen,
                tx_budget=min(8, int(cfg.get("cluster_tx_budget") or 8)),
            )
            notes.extend(copy_paper_buys(buys, prices))
            if signatures:
                with db() as conn:
                    remember_signatures(conn, signatures)
    except Exception as exc:  # noqa: BLE001
        log.warning("user book failed: %s", redact(exc))
    return notes


def _refresh_live_orders(opens, known, open_count, loss, trader, moment: datetime, messages: list):
    """Poll buy and exit orders that are still in flight."""
    tracked = [
        row
        for row in opens
        if (row.get("status") == "pending" and row.get("order_id")) or row.get("exit_order_id")
    ]
    if not tracked:
        return opens, known, open_count, loss
    from .store import daily_loss_sol as _loss

    for row in tracked:
        order_id = row.get("exit_order_id") or row.get("order_id")
        try:
            result = trader.refresh(order_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("order refresh failed: %s", redact(exc))
            continue
        with db() as conn:
            note = _apply_refresh(conn, row, result, moment)
        if note:
            messages.append(note)
    with db() as conn:
        fresh = open_positions(conn)
        return fresh, known_tokens(conn), exposure_count(conn), _loss(conn, moment)


def _apply_refresh(conn, row: dict, result: TradeResult, moment: datetime) -> str | None:
    token = row["token"]
    record_fill(
        conn,
        token=token,
        symbol=row.get("symbol"),
        side="sell" if row.get("exit_order_id") else "buy",
        mode="live",
        status=result.status,
        now=moment,
        order_id=result.order_id or row.get("exit_order_id") or row.get("order_id"),
        tx_hash=result.tx_hash,
        error=result.error,
    )
    if row.get("exit_order_id"):
        if result.ok:
            conn.execute(
                """
                UPDATE paper_calls
                SET status = COALESCE(exit_reason, 'sold'), closed_at = ?, tx_hash = COALESCE(?, tx_hash)
                WHERE token = ? AND status = 'open'
                """,
                (moment.isoformat(), result.tx_hash, token),
            )
            return format_trade("sell", row.get("symbol"), token, result.status, "exit order confirmed")
        if result.terminal:
            conn.execute(
                """
                UPDATE paper_calls
                SET status = 'exit_failed', closed_at = ?, tx_hash = COALESCE(?, tx_hash)
                WHERE token = ? AND status = 'open'
                """,
                (moment.isoformat(), result.tx_hash, token),
            )
            return format_trade("sell", row.get("symbol"), token, result.status, result.error or "exit order failed")
        return None
    if result.ok:
        conn.execute(
            """
            UPDATE paper_calls
            SET status = 'open', tx_hash = COALESCE(?, tx_hash),
                strategy_order_id = COALESCE(?, strategy_order_id)
            WHERE token = ? AND status = 'pending'
            """,
            (result.tx_hash, result.strategy_order_id, token),
        )
        return format_trade("buy", row.get("symbol"), token, result.status, "order confirmed")
    if result.terminal:
        conn.execute(
            """
            UPDATE paper_calls
            SET status = 'failed', closed_at = ?, tx_hash = COALESCE(?, tx_hash)
            WHERE token = ? AND status = 'pending'
            """,
            (moment.isoformat(), result.tx_hash, token),
        )
        return format_trade("buy", row.get("symbol"), token, result.status, result.error or "order failed")
    return None


def _record_live_buy(conn, plan: dict, result: TradeResult | None, origins: dict, moment: datetime, messages: list, cfg: dict) -> None:
    pair = plan["pair"]
    token = pair["token"]
    item = plan["item"]
    if result is None:
        record_signal(conn, pair, "skip", "live buy did not run", moment)
        return
    source = source_label(origins.get(token, set()), len(item.get("cluster") or []))
    remember = result.occupies_slot or (result.terminal and not result.retryable)
    status = "open" if result.ok else ("pending" if result.occupies_slot else "failed")
    exit_owner = "gmgn" if result.strategy_order_id else "bot"
    if remember:
        inserted = open_position(
            conn,
            pair,
            size_sol=plan["size_sol"],
            size_usd=plan["size_usd"],
            source=source,
            now=moment,
            status=status if status != "failed" else "failed",
            mode="live",
            order_id=result.order_id,
            tx_hash=result.tx_hash,
            strategy_order_id=result.strategy_order_id,
            exit_owner=exit_owner,
        )
        if inserted and result.ok:
            record_signal(conn, pair, "watch", plan["reason"], moment)
    record_fill(
        conn,
        token=token,
        symbol=pair.get("symbol"),
        side="buy",
        mode="live",
        status=result.status,
        now=moment,
        order_id=result.order_id,
        tx_hash=result.tx_hash,
        size_sol=plan["size_sol"],
        input_amount=result.input_amount,
        output_amount=result.output_amount,
        error=result.error,
    )
    detail = result.error or f"order {result.order_id or 'n/a'}"
    if result.strategy_order_id:
        detail += f" · tp/sl strategy {result.strategy_order_id}"
    messages.append(format_trade("buy", pair.get("symbol"), token, result.status, detail))


def _submit_live_sell(job: dict, trader, moment: datetime, messages: list) -> None:
    token = job["token"]
    if trader is None:
        messages.append(format_trade("sell", job.get("symbol"), token, "failed", "gmgn client unavailable"))
        return
    try:
        result = trader.sell(token, job.get("symbol") or "?", job["reason"])
    except Exception as exc:  # noqa: BLE001
        log.warning("live sell crashed: %s", redact(exc))
        result = TradeResult(ok=False, status="failed", error=redact(exc), retryable=True)
    with db() as conn:
        record_fill(
            conn,
            token=token,
            symbol=job.get("symbol"),
            side="sell",
            mode="live",
            status=result.status,
            now=moment,
            order_id=result.order_id,
            tx_hash=result.tx_hash,
            error=result.error,
        )
        if result.ok:
            gain = pnl_sol(job["position"].get("size_sol"), job.get("multiple"))
            conn.execute(
                """
                UPDATE paper_calls
                SET status = ?, closed_at = ?, exit_mcap = ?, exit_order_id = ?, exit_reason = ?,
                    pnl_sol = ?, tx_hash = COALESCE(?, tx_hash)
                WHERE token = ? AND status = 'open'
                """,
                (
                    job["reason"],
                    moment.isoformat(),
                    job.get("now_mcap"),
                    result.order_id,
                    job["reason"],
                    gain,
                    result.tx_hash,
                    token,
                ),
            )
        elif result.order_id and not result.terminal:
            conn.execute(
                """
                UPDATE paper_calls
                SET exit_order_id = ?, exit_reason = ?, tx_hash = COALESCE(?, tx_hash)
                WHERE token = ? AND status = 'open'
                """,
                (result.order_id, job["reason"], result.tx_hash, token),
            )
        elif result.terminal and not result.retryable:
            conn.execute(
                """
                UPDATE paper_calls
                SET status = 'exit_failed', closed_at = ?, exit_order_id = ?, exit_reason = ?
                WHERE token = ? AND status = 'open'
                """,
                (moment.isoformat(), result.order_id, job["reason"], token),
            )
        if claim_alert(conn, token, f"sell-{job['reason']}-{result.status}", moment):
            detail = result.error or f"order {result.order_id or 'n/a'}"
            messages.append(format_trade("sell", job.get("symbol"), token, result.status, f"{job['reason']}: {detail}"))


def source_label(tags: set[str], cluster_count: int) -> str:
    parts = []
    if "profile" in tags:
        parts.append("DexScreener profile")
    if "boost" in tags:
        parts.append("DexScreener boost")
    if "stream" in tags:
        parts.append("DexScreener stream")
    if "cluster" in tags or cluster_count:
        parts.append("smart money")
    return " + ".join(parts) or "DexScreener"


def maybe_keep_awake(cfg: dict) -> None:
    """Ping the public /health URL so a Render free instance sees traffic."""
    global _last_ping
    if not cfg.get("keep_awake") or not cfg.get("keep_awake_url"):
        return
    now_ts = time.time()
    if now_ts - _last_ping < 8 * 60:
        return
    _last_ping = now_ts
    target = cfg["keep_awake_url"].rstrip("/") + "/health"
    try:
        requests.get(target, timeout=10)
        log.info("keep-awake ping %s", safe_url(target))
    except requests.RequestException as exc:
        log.warning("keep-awake ping failed: %s", exc)


def _save_summary(summary: dict, moment: datetime) -> None:
    set_meta("last_scan_at", moment.isoformat())
    set_meta("last_summary", json.dumps(summary))
    set_meta("last_error", "")


def _join_milestones(milestones: set[str]) -> str:
    def sort_key(value: str):
        try:
            return float(value)
        except ValueError:
            return 0.0

    return ",".join(sorted(milestones, key=sort_key))


def reset_runtime_state() -> None:
    """Clear process-local scan caches. Used by tests."""
    global _last_ping
    _last_ping = 0.0
    market.clear_caches()
