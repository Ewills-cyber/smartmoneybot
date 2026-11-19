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
from .http import redact, safe_url
from .notify import format_call, format_close, format_milestone, send
from .paper import can_open, evaluate_position, milestone_set, pnl_sol, pnl_usd, position_size
from .settings import SOLANA_MINT, load_settings
from .store import (
    claim_alert,
    daily_loss_sol,
    db,
    known_tokens,
    open_position,
    open_positions,
    paused,
    prune,
    record_signal,
    remember_signatures,
    seen_signatures,
    set_meta,
)
from .stream import get_stream, start_stream

log = logging.getLogger(__name__)

_scan_lock = threading.Lock()
_last_ping = 0.0


def run_once(*, now: datetime | None = None, get=None, rpc_call=None, sender=None, check_token=None) -> dict:
    """Run a single scan. Overlapping scans return immediately."""
    if not _scan_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "scan already running", "scanned": 0, "watches": 0, "open": 0}
    try:
        return _run_once(now=now, get=get, rpc_call=rpc_call, sender=sender, check_token=check_token)
    finally:
        _scan_lock.release()


def serve(stop_flag: dict) -> None:
    log.info("scanner started in paper mode")
    cfg = load_settings()
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


def _run_once(*, now, get, rpc_call, sender, check_token) -> dict:
    cfg = load_settings()
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    fetch = get or market.default_get
    maybe_keep_awake(cfg)

    if paused():
        summary = {"paused": True, "scanned": 0, "watches": 0, "open": 0, "clusters": 0}
        _save_summary(summary, moment)
        return summary

    with db() as conn:
        opens = open_positions(conn)
        known = known_tokens(conn)
        seen = seen_signatures(conn)
        open_count = len(opens)
        loss = daily_loss_sol(conn, moment)

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

    messages: list[str] = []
    watches = 0
    with db() as conn:
        for item in candidates:
            pair = item["pair"]
            token = pair["token"]
            ok = item["ok"]
            reason = item["reason"]
            if not ok:
                record_signal(conn, pair, "skip", reason, moment)
                continue
            allowed, why = can_open(open_count, loss, cfg)
            if not allowed:
                record_signal(conn, pair, "skip", why, moment)
                continue
            size_sol, size_usd = position_size(sol, cfg)
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
            record_signal(conn, pair, "watch", reason, moment)
            open_count += 1
            known.add(token)
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
            pair = best.get(position["token"])
            now_mcap = pair.get("mcap") if pair else None
            if now_mcap:
                conn.execute(
                    "UPDATE paper_calls SET last_mcap = ?, pair = COALESCE(?, pair) WHERE token = ?",
                    (now_mcap, pair.get("pair") if pair else None, position["token"]),
                )
            actions = evaluate_position(position, now_mcap, moment, cfg)
            milestones = milestone_set(position.get("milestones"))
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
        open_now = conn.execute("SELECT COUNT(*) AS n FROM paper_calls WHERE status = 'open'").fetchone()["n"]

    summary = {
        "paused": False,
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
            deliver(message)
        except Exception as exc:  # noqa: BLE001
            log.warning("alert delivery failed: %s", redact(exc))
    return summary


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
