"""Offline tests for the paper scanner."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("KEEP_AWAKE", "0")
os.environ.setdefault("ENABLE_PROFILE_STREAM", "0")
os.environ.setdefault("SMARTMONEY_DISABLE_BACKGROUND", "1")

from smartmoney_bot.chat import normalize_command, reply
from smartmoney_bot.checks import authorities_from_account, holder_stats
from smartmoney_bot.cluster import buys_from_transaction, collect_buys, find_clusters
from smartmoney_bot.gate import hard_filter, passes, safety_filter
from smartmoney_bot.http import ApiError, redact, request_json, safe_url
from smartmoney_bot.loop import reset_runtime_state, run_once, source_label
from smartmoney_bot.market import discover, normalize_pair, pair_age_hours, select_best_pairs, sol_price_from_pairs
from smartmoney_bot.notify import _deliver, esc, format_call, format_close, format_milestone
from smartmoney_bot.paper import can_open, evaluate_position, pnl_sol, position_size
from smartmoney_bot.settings import SOLANA_MINT, load_settings, parse_wallets
from smartmoney_bot.store import claim_alert, db, db_path, open_position, record_signal
from smartmoney_bot.stream import ProfileStream, backoff_seconds, profiles_from_message

MINT = "Mint111111111111111111111111111111111111111"
WALLET_A = "1" * 32
WALLET_B = "2" * 32
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
CFG = {
    "chain": "solana",
    "max_entry_mcap": 250_000,
    "min_liquidity_usd": 12_000,
    "min_volume_h1": 8_000,
    "max_pair_age_hours": 72,
    "min_holders": 150,
    "max_top_holder_pct": 15,
    "min_cluster_wallets": 2,
    "require_cluster": False,
    "require_authorities": False,
    "require_holders": False,
}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMONEY_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("KEEP_AWAKE", "0")
    monkeypatch.setenv("ENABLE_PROFILE_STREAM", "0")
    monkeypatch.setenv("SMARTMONEY_DISABLE_BACKGROUND", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("BIRDEYE_API_KEY", raising=False)
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.delenv("SMART_WALLETS", raising=False)
    monkeypatch.delenv("REQUIRE_CLUSTER", raising=False)
    monkeypatch.delenv("MAX_OPEN_POSITIONS", raising=False)
    monkeypatch.delenv("SOLANA_RPC", raising=False)
    monkeypatch.delenv("TRADE_MODE", raising=False)
    monkeypatch.delenv("GMGN_API_KEY", raising=False)
    monkeypatch.delenv("GMGN_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GMGN_WALLET", raising=False)
    monkeypatch.delenv("GMGN_ALLOW_EPHEMERAL_DB", raising=False)
    reset_runtime_state()
    yield


def raw_pair(**overrides):
    created = int(NOW.timestamp() * 1000) - 3_600_000
    pair = {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": "Pair111111111111111111111111111111111111",
        "url": "https://dexscreener.com/solana/Pair111",
        "baseToken": {"address": MINT, "symbol": "PEPE", "name": "Pepe"},
        "priceUsd": "0.001",
        "marketCap": 100_000,
        "fdv": 120_000,
        "liquidity": {"usd": 20_000},
        "volume": {"h1": 10_000},
        "pairCreatedAt": created,
        "txns": {"h1": {"buys": 40, "sells": 20}},
        "priceChange": {"h1": 12.5},
    }
    pair.update(overrides)
    return pair


def safe_info():
    return {
        "mint_revoked": True,
        "freeze_revoked": True,
        "holders": 400,
        "top_pct": 4.0,
        "top10_pct": 22.0,
    }


def scanner(pairs, profiles=None, sol="150"):
    profiles = profiles if profiles is not None else [{"chainId": "solana", "tokenAddress": MINT}]
    calls = {"checks": 0}

    def get(path):
        if path.startswith("/token-profiles"):
            return profiles
        if path.startswith("/token-boosts"):
            return []
        if SOLANA_MINT in path:
            return [
                {
                    "baseToken": {"address": SOLANA_MINT},
                    "priceUsd": sol,
                    "liquidity": {"usd": 5_000_000},
                }
            ]
        if path.startswith("/tokens/"):
            return pairs
        raise AssertionError(path)

    def check(pair):
        calls["checks"] += 1
        return safe_info()

    return get, check, calls


def test_hard_filter_accepts_the_original_gate():
    pair = normalize_pair(raw_pair(), NOW)
    ok, reason = hard_filter(pair, CFG)
    assert ok and reason == "pass"


def test_hard_filter_rejects_each_boundary():
    rich = normalize_pair(raw_pair(marketCap=400_000), NOW)
    thin = normalize_pair(raw_pair(liquidity={"usd": 1000}), NOW)
    quiet = normalize_pair(raw_pair(volume={"h1": 100}), NOW)
    old = normalize_pair(raw_pair(pairCreatedAt=int(NOW.timestamp() * 1000) - 80 * 3600 * 1000), NOW)
    other = normalize_pair(raw_pair(chainId="ethereum"), NOW)
    wrapped = normalize_pair(
        raw_pair(baseToken={"address": SOLANA_MINT, "symbol": "SOL", "name": "SOL"}),
        NOW,
    )
    assert hard_filter(rich, CFG)[0] is False
    assert "too thin" in hard_filter(thin, CFG)[1]
    assert "volume" in hard_filter(quiet, CFG)[1]
    assert "too old" in hard_filter(old, CFG)[1]
    assert hard_filter(other, CFG)[1] == "not solana"
    assert hard_filter(wrapped, CFG)[1] == "wrapped sol"


def test_unknown_age_is_allowed():
    pair = normalize_pair(raw_pair(pairCreatedAt=None), NOW)
    assert pair["age_hours"] is None
    assert hard_filter(pair, CFG)[0] is True


def test_pair_age_accepts_seconds_and_milliseconds():
    seconds = int(NOW.timestamp()) - 7200
    millis = seconds * 1000
    assert pair_age_hours({"pairCreatedAt": seconds}, NOW) == pytest.approx(2, abs=0.01)
    assert pair_age_hours({"pairCreatedAt": millis}, NOW) == pytest.approx(2, abs=0.01)
    assert pair_age_hours({"pairCreatedAt": 0}, NOW) is None


def test_best_pair_keeps_the_deeper_pool():
    thin = raw_pair(pairAddress="thin", liquidity={"usd": 1000})
    deep = raw_pair(pairAddress="deep", liquidity={"usd": 50_000})
    best = select_best_pairs([thin, deep], NOW)
    assert best[MINT]["pair"] == "deep"
    assert best[MINT]["liquidity_usd"] == 50_000


def test_discover_keeps_solana_and_merges_sources():
    def get(path):
        if "profiles" in path:
            return [
                {"chainId": "ethereum", "tokenAddress": "0xabc"},
                {"chainId": "solana", "tokenAddress": MINT},
            ]
        if "boosts" in path:
            return [
                {"chainId": "solana", "tokenAddress": MINT},
                {"chainId": "solana", "tokenAddress": "BoostMint"},
            ]
        raise AssertionError(path)

    found = discover(get, "solana", 30, extra=[{"address": "OpenMint", "source": "open"}])
    by_address = {item["address"]: item["sources"] for item in found}
    assert "0xabc" not in by_address
    assert by_address[MINT] == {"profile", "boost"}
    assert by_address["BoostMint"] == {"boost"}
    assert by_address["OpenMint"] == {"open"}


def test_discover_survives_a_failed_feed():
    def get(path):
        if "profiles" in path:
            raise ApiError("down", 500)
        return [{"chainId": "solana", "tokenAddress": MINT}]

    found = discover(get, "solana", 30)
    assert found[0]["address"] == MINT
    assert found[0]["sources"] == {"boost"}


def test_boosts_are_kept_when_profiles_already_fill_their_cap():
    def get(path):
        if "profiles" in path:
            return [{"chainId": "solana", "tokenAddress": f"Profile{i}"} for i in range(5)]
        return [{"chainId": "solana", "tokenAddress": "BoostMint"}]

    found = discover(get, "solana", 2)
    addresses = [item["address"] for item in found]
    assert addresses[:2] == ["Profile0", "Profile1"]
    assert "BoostMint" in addresses


def test_sol_price_uses_the_sol_base_token():
    pairs = [
        {"baseToken": {"address": "other"}, "priceUsd": "1", "liquidity": {"usd": 999}},
        {"baseToken": {"address": SOLANA_MINT}, "priceUsd": "150.5", "liquidity": {"usd": 10}},
        {"baseToken": {"address": SOLANA_MINT}, "priceUsd": "151", "liquidity": {"usd": 80}},
    ]
    assert sol_price_from_pairs(pairs) == 151


def test_missing_account_is_not_treated_as_revoked():
    assert authorities_from_account(None) == {"mint_revoked": None, "freeze_revoked": None}
    assert authorities_from_account({})["mint_revoked"] is None
    revoked = authorities_from_account(
        {"data": {"parsed": {"info": {"mintAuthority": None, "freezeAuthority": None}}}}
    )
    assert revoked == {"mint_revoked": True, "freeze_revoked": True}
    active = authorities_from_account(
        {"data": {"parsed": {"info": {"mintAuthority": "SomeAuth", "freezeAuthority": None}}}}
    )
    assert active["mint_revoked"] is False
    assert active["freeze_revoked"] is True


def test_holder_share_does_not_treat_token_count_as_a_percent():
    payload = {"data": {"holder": 10, "top10HoldPercent": 40, "items": [{"ui_amount": 1_000_000}]}}
    stats = holder_stats(payload, supply=None)
    assert stats["holders"] == 10
    assert stats["top_pct"] is None
    assert stats["top10_pct"] == 40
    estimated = holder_stats(payload, supply=2_000_000)
    assert estimated["top_pct"] == pytest.approx(50)
    explicit = holder_stats(
        {"data": {"holder": 3, "items": [{"ui_amount": 9, "percentage": 12.5}]}},
        supply=1,
    )
    assert explicit["top_pct"] == 12.5


def test_safety_filter_blocks_active_mint_and_concentration():
    assert safety_filter({"mint_revoked": False, "freeze_revoked": True}, CFG)[0] is False
    assert safety_filter({"mint_revoked": True, "freeze_revoked": True, "holders": 10, "top_pct": 1}, CFG)[0] is False
    assert "concentrated" in safety_filter({"mint_revoked": True, "holders": 500, "top_pct": 40}, CFG)[1]
    assert safety_filter({"mint_revoked": None, "holders": None, "top_pct": None}, CFG)[0] is True
    strict = dict(CFG, require_authorities=True)
    pair = {"chain": "solana", "token": MINT, "mcap": 100_000, "liquidity_usd": 20_000, "volume_h1": 10_000, "age_hours": 1}
    assert passes(pair, {}, strict)[1] == "authority check unavailable"


def test_require_cluster_uses_the_wallet_count():
    pair = normalize_pair(raw_pair(), NOW)
    assert passes(pair, safe_info(), dict(CFG, require_cluster=True), 1)[0] is False
    assert passes(pair, safe_info(), dict(CFG, require_cluster=True), 2)[0] is True


def test_airdrop_is_not_a_buy_and_a_signed_increase_is():
    minted = {
        "blockTime": 1_700_000_000,
        "meta": {
            "preTokenBalances": [],
            "postTokenBalances": [
                {"mint": MINT, "owner": WALLET_A, "uiTokenAmount": {"uiAmount": 5}}
            ],
        },
        "transaction": {"message": {"accountKeys": [{"pubkey": WALLET_A, "signer": False}]}},
    }
    assert buys_from_transaction(minted, WALLET_A) == []
    minted["transaction"]["message"]["accountKeys"][0]["signer"] = True
    buys = buys_from_transaction(minted, WALLET_A)
    assert buys[0]["mint"] == MINT
    assert buys[0]["delta"] == 5
    sol_only = {
        "meta": {
            "preTokenBalances": [],
            "postTokenBalances": [
                {"mint": SOLANA_MINT, "owner": WALLET_A, "uiTokenAmount": {"uiAmount": 2}}
            ],
        },
        "transaction": {"message": {"accountKeys": [{"pubkey": WALLET_A, "signer": True}]}},
    }
    assert buys_from_transaction(sol_only, WALLET_A) == []


def test_find_clusters_respects_the_window_and_unique_wallets():
    moment = datetime(2026, 6, 1, tzinfo=timezone.utc)
    recent = int(moment.timestamp()) - 60
    old = int(moment.timestamp()) - 3600
    buys = [
        {"mint": MINT, "wallet": WALLET_A, "ts": recent},
        {"mint": MINT, "wallet": WALLET_A, "ts": recent},
        {"mint": MINT, "wallet": WALLET_B, "ts": recent},
        {"mint": "other", "wallet": WALLET_A, "ts": recent},
        {"mint": "stale", "wallet": WALLET_A, "ts": old},
        {"mint": "stale", "wallet": WALLET_B, "ts": old},
    ]
    found = find_clusters(buys, min_wallets=2, window_minutes=15, now=moment)
    assert set(found) == {MINT}
    assert len(found[MINT]) == 2


def test_collect_buys_stops_when_rpc_is_rate_limited():
    calls = []

    def rpc(url, method, params):
        calls.append(method)
        raise ApiError("slow down", 429)

    buys, seen = collect_buys(
        [WALLET_A, WALLET_B],
        rpc_url="https://rpc.example",
        rpc_call=rpc,
        window_minutes=15,
        now=NOW,
        seen=set(),
        tx_budget=10,
    )
    assert buys == []
    assert seen == []
    assert calls == ["getSignaturesForAddress"]


def test_collect_buys_parses_a_swap_and_remembers_the_signature():
    stamp = int(NOW.timestamp()) - 30

    def rpc(url, method, params):
        if method == "getSignaturesForAddress":
            wallet = params[0]
            return [{"signature": f"sig-{wallet}", "blockTime": stamp, "err": None}]
        signature = params[0]
        wallet = WALLET_A if signature.endswith(WALLET_A) else WALLET_B
        return {
            "blockTime": stamp,
            "meta": {
                "preTokenBalances": [],
                "postTokenBalances": [
                    {"mint": MINT, "owner": wallet, "uiTokenAmount": {"uiAmountString": "3"}}
                ],
            },
            "transaction": {"message": {"accountKeys": [{"pubkey": wallet, "signer": True}]}},
        }

    buys, seen = collect_buys(
        [WALLET_A],
        rpc_url="https://rpc.example",
        rpc_call=rpc,
        window_minutes=15,
        now=NOW,
        seen=set(),
        tx_budget=5,
    )
    assert len(buys) == 1
    assert buys[0]["delta"] == 3
    assert seen[0][0] == f"sig-{WALLET_A}"


def test_paper_rules_size_cap_and_exits():
    assert position_size(150, {"trade_size_sol": 0.05}) == (0.05, 7.5)
    assert position_size(None, {"trade_size_sol": 0.05}) == (0.05, None)
    assert can_open(4, 0, {"max_open_positions": 4, "max_daily_loss_sol": 0.3})[0] is False
    assert can_open(1, 0.3, {"max_open_positions": 4, "max_daily_loss_sol": 0.3})[0] is False
    assert can_open(1, 0.1, {"max_open_positions": 4, "max_daily_loss_sol": 0.3})[0] is True
    position = {
        "entry_mcap": 100_000,
        "opened_at": NOW.isoformat(),
        "milestones": "",
        "size_sol": 0.05,
        "size_usd": 7.5,
    }
    cfg = {
        "milestone_rungs": (2, 3, 5),
        "take_profit_multiple": 2.5,
        "stop_loss_multiple": 0.65,
        "time_stop_minutes": 180,
    }
    two_x = evaluate_position(position, 210_000, NOW, cfg)
    assert [item["type"] for item in two_x] == ["milestone"]
    assert two_x[0]["rung"] == "2"
    jump = evaluate_position(position, 600_000, NOW, cfg)
    assert [item["rung"] for item in jump if item["type"] == "milestone"] == ["2", "3", "5"]
    assert jump[-1] == {"type": "close", "reason": "take_profit", "multiple": 6}
    stopped = evaluate_position(position, 60_000, NOW, cfg)
    assert stopped == [{"type": "close", "reason": "stop_loss", "multiple": 0.6}]
    later = NOW + timedelta(minutes=181)
    timed = evaluate_position(position, None, later, cfg)
    assert timed[0]["reason"] == "time_stop"
    assert pnl_sol(0.05, 2) == pytest.approx(0.05)
    assert pnl_sol(0.05, 0.65) == pytest.approx(-0.0175)


def test_signal_and_alert_dedupe_and_old_schema_migrates(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    monkeypatch.setenv("SMARTMONEY_DB", str(path))
    raw = sqlite3.connect(path)
    raw.execute(
        """
        CREATE TABLE paper_calls (
            token TEXT PRIMARY KEY,
            symbol TEXT,
            entry_mcap REAL,
            size_usd REAL,
            status TEXT,
            opened_at TEXT
        )
        """
    )
    raw.commit()
    raw.close()
    pair = normalize_pair(raw_pair(), NOW)
    with db() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_calls)")}
        assert "pnl_sol" in columns
        assert "milestones" in columns
        assert record_signal(conn, pair, "skip", "h1 volume too low", NOW) is True
        assert record_signal(conn, pair, "skip", "h1 volume too low", NOW) is False
        assert record_signal(conn, pair, "skip", "pair too old", NOW) is True
        assert claim_alert(conn, MINT, "call", NOW) is True
        assert claim_alert(conn, MINT, "call", NOW) is False
        assert open_position(conn, pair, size_sol=0.05, size_usd=7.5, source="test", now=NOW) is True
        assert open_position(conn, pair, size_sol=0.05, size_usd=7.5, source="test", now=NOW) is False
    assert db_path() == path


def test_scan_alerts_once_and_then_tracks_milestones_without_closing():
    pair = raw_pair()
    sent = []
    get, check, _calls = scanner([pair])
    first = run_once(now=NOW, get=get, sender=sent.append, check_token=check)
    assert first["watches"] == 1
    assert len(sent) == 1
    assert "NEW CALL" in sent[0]
    assert "0.0500 SOL" in sent[0]
    assert "$7.50" in sent[0]
    assert MINT in sent[0]

    pair["marketCap"] = 210_000
    second = run_once(now=NOW + timedelta(minutes=10), get=get, sender=sent.append, check_token=check)
    assert second["watches"] == 0
    assert len(sent) == 2
    assert "PAPER 2x" in sent[1]
    with db() as conn:
        row = conn.execute("SELECT status, milestones FROM paper_calls").fetchone()
    assert row["status"] == "open"
    assert row["milestones"] == "2"


def test_scan_closes_on_stop_loss_and_does_not_repeat_the_alert():
    pair = raw_pair()
    sent = []
    get, check, _calls = scanner([pair])
    run_once(now=NOW, get=get, sender=sent.append, check_token=check)
    pair["marketCap"] = 60_000
    closed = run_once(now=NOW + timedelta(minutes=5), get=get, sender=sent.append, check_token=check)
    assert closed["open"] == 0
    assert any("STOP LOSS" in message for message in sent)
    again = run_once(now=NOW + timedelta(minutes=6), get=get, sender=sent.append, check_token=check)
    assert again["watches"] == 0
    assert len(sent) == 2


def test_failed_hard_filter_does_not_spend_a_safety_check():
    pair = raw_pair(marketCap=900_000)
    sent = []
    get, check, calls = scanner([pair])
    summary = run_once(now=NOW, get=get, sender=sent.append, check_token=check)
    assert summary["watches"] == 0
    assert sent == []
    assert calls["checks"] == 0
    with db() as conn:
        row = conn.execute("SELECT decision, reason FROM signals").fetchone()
    assert row["decision"] == "skip"
    assert "outside cap" in row["reason"]


def test_active_mint_and_position_cap_block_calls(monkeypatch):
    pair = raw_pair()
    sent = []
    get, _check, _calls = scanner([pair])

    def active(_pair):
        return {"mint_revoked": False, "freeze_revoked": True, "holders": 400, "top_pct": 1}

    summary = run_once(now=NOW, get=get, sender=sent.append, check_token=active)
    assert summary["watches"] == 0
    assert sent == []

    other_mint = "OtherMint11111111111111111111111111111111"
    other = raw_pair(
        baseToken={"address": other_mint, "symbol": "DOG", "name": "Dog"},
        pairAddress="Pair222",
    )
    profiles = [
        {"chainId": "solana", "tokenAddress": MINT},
        {"chainId": "solana", "tokenAddress": other_mint},
    ]
    get, check, _calls = scanner([pair, other], profiles=profiles)
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "1")
    capped = run_once(now=NOW, get=get, sender=sent.append, check_token=check)
    assert capped["watches"] == 1
    assert len(sent) == 1


def test_daily_loss_blocks_a_new_call():
    pair = raw_pair()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO paper_calls (
                token, symbol, entry_mcap, size_sol, status, opened_at, closed_at, pnl_sol
            ) VALUES ('old', 'OLD', 1, 0.05, 'stop_loss', ?, ?, -0.3)
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
    sent = []
    get, check, _calls = scanner([pair])
    summary = run_once(now=NOW, get=get, sender=sent.append, check_token=check)
    assert summary["watches"] == 0
    assert sent == []


def test_pause_skips_the_network():
    from smartmoney_bot.store import set_paused

    set_paused(True)

    def get(_path):
        raise AssertionError("paused scan should not fetch")

    summary = run_once(now=NOW, get=get, sender=lambda _message: None, check_token=lambda _pair: {})
    assert summary["paused"] is True


def test_cluster_requirement_uses_watched_wallet_buys(monkeypatch):
    monkeypatch.setenv("SMART_WALLETS", f"{WALLET_A},{WALLET_B}")
    monkeypatch.setenv("REQUIRE_CLUSTER", "1")
    stamp = int(NOW.timestamp()) - 30
    pair = raw_pair()
    sent = []

    def rpc(url, method, params):
        if method == "getSignaturesForAddress":
            wallet = params[0]
            return [{"signature": f"sig-{wallet}", "blockTime": stamp, "err": None}]
        wallet = WALLET_A if str(params[0]).endswith(WALLET_A) else WALLET_B
        return {
            "blockTime": stamp,
            "meta": {
                "preTokenBalances": [],
                "postTokenBalances": [
                    {"mint": MINT, "owner": wallet, "uiTokenAmount": {"uiAmount": 4}}
                ],
            },
            "transaction": {"message": {"accountKeys": [{"pubkey": wallet, "signer": True}]}},
        }

    get, check, _calls = scanner([pair])
    summary = run_once(now=NOW, get=get, rpc_call=rpc, sender=sent.append, check_token=check)
    assert summary["watches"] == 1
    assert summary["clusters"] == 1
    assert "watched wallets" in sent[0]


def test_open_position_is_priced_even_after_it_leaves_the_discovery_feed():
    pair = raw_pair()
    sent = []
    get, check, _calls = scanner([pair])
    run_once(now=NOW, get=get, sender=sent.append, check_token=check)
    pair["marketCap"] = 220_000
    get_open, check_open, calls = scanner([pair], profiles=[])
    later = run_once(
        now=NOW + timedelta(minutes=20),
        get=get_open,
        sender=sent.append,
        check_token=check_open,
    )
    assert later["open"] == 1
    assert calls["checks"] == 0
    assert any("PAPER 2x" in message for message in sent)
    with db() as conn:
        last = conn.execute("SELECT last_mcap, status FROM paper_calls").fetchone()
    assert last["status"] == "open"
    assert last["last_mcap"] == 220_000


def test_time_stop_closes_without_a_fresh_price():
    pair = normalize_pair(raw_pair(), NOW)
    with db() as conn:
        open_position(conn, pair, size_sol=0.05, size_usd=7.5, source="test", now=NOW - timedelta(hours=5))
    sent = []

    def get(path):
        if path.startswith("/token-profiles") or path.startswith("/token-boosts"):
            return []
        if path.startswith("/tokens/"):
            return []
        raise AssertionError(path)

    summary = run_once(now=NOW, get=get, sender=sent.append, check_token=lambda _pair: {})
    assert summary["open"] == 0
    assert "TIME STOP" in sent[0]


def test_commands_and_summary_text(monkeypatch):
    assert normalize_command("/status@SmartMoneyBot") == "/status"
    assert "Commands" in reply("hello")
    assert "Paper mode" in reply("how are you doing")
    assert "No signals yet" in reply("/signals")
    monkeypatch.setattr(
        "smartmoney_bot.chat.run_once",
        lambda **_kwargs: {"scanned": 3, "watches": 1, "open": 1, "clusters": 2, "paused": False, "skipped": False},
    )
    assert "Scanned 3" in reply("/scan")
    assert "Paused" in reply("/pause")
    assert "paused" in reply("status").lower()
    assert "Resumed" in reply("/resume")
    assert "wallet" in reply("/wallets").lower()
    assert "max_entry_mcap" in reply("/config")


def test_alert_escapes_html_and_names_the_source():
    pair = normalize_pair(
        raw_pair(baseToken={"address": MINT, "symbol": "<PEPE>", "name": "A&B"}),
        NOW,
    )
    cfg = load_settings()
    text = format_call(
        pair,
        safe_info(),
        {"size_sol": 0.05, "size_usd": 7.5, "source": "DexScreener profile", "symbol": pair["symbol"]},
        [],
        cfg,
    )
    assert esc("<PEPE>") in text
    assert "<script>" not in text
    assert "A&amp;B" in text
    assert "NFA" in text
    milestone = format_milestone(
        {"symbol": "PEPE", "entry_mcap": 1000, "token": MINT, "size_sol": 0.05, "size_usd": 1},
        "2",
        2.0,
        2000,
    )
    assert "PAPER 2x" in milestone
    closed = format_close(
        {"symbol": "PEPE", "entry_mcap": 1000, "token": MINT, "size_sol": 0.05, "size_usd": 1},
        "stop_loss",
        0.5,
        500,
    )
    assert "STOP LOSS" in closed
    assert source_label({"profile", "cluster"}, 2) == "DexScreener profile + smart money"


def test_settings_env_and_wallet_parsing(monkeypatch):
    monkeypatch.setenv("MAX_ENTRY_MCAP", "1000")
    monkeypatch.setenv("POLL_SECONDS", "30")
    monkeypatch.setenv("BIRDEYE_API_KEY", "-")
    monkeypatch.setenv("HELIUS_API_KEY", "abc123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111, 222")
    monkeypatch.setenv("SMART_WALLETS", f"{WALLET_A} not-a-wallet {WALLET_B}")
    monkeypatch.setenv("REQUIRE_CLUSTER", "yes")
    cfg = load_settings()
    assert cfg["max_entry_mcap"] == 1000
    assert cfg["poll_seconds"] == 30
    assert cfg["birdeye_api_key"] is None
    assert cfg["solana_rpc"].startswith("https://mainnet.helius-rpc.com/?api-key=")
    assert cfg["telegram_chat_ids"] == ["111", "222"]
    assert cfg["smart_wallets"] == [WALLET_A, WALLET_B]
    assert cfg["require_cluster"] is True
    assert parse_wallets("nope", 8) == []


def test_safe_url_redacts_bot_token_and_api_key():
    hidden = safe_url("https://api.telegram.org/bot123:secret/getUpdates")
    assert "secret" not in hidden
    assert "bot<redacted>" in hidden
    hidden_rpc = safe_url("https://mainnet.helius-rpc.com/?api-key=super-secret")
    assert "super-secret" not in hidden_rpc
    assert "REDACTED" in hidden_rpc
    assert redact("https://api.telegram.org/bot123:secret/getUpdates?api-key=abc") == (
        "https://api.telegram.org/bot<redacted>/getUpdates?api-key=REDACTED"
    )


def test_request_json_backs_off_after_429():
    sleeps = []

    class Response:
        def __init__(self, status, payload, headers=None):
            self.status_code = status
            self._payload = payload
            self.headers = headers or {}

        def json(self):
            return self._payload

    class Session:
        def __init__(self):
            self.calls = 0

        def request(self, method, url, timeout=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Response(429, {}, {"Retry-After": "0"})
            return Response(200, {"ok": True})

    payload = request_json(
        "GET",
        "https://example.test/items",
        session=Session(),
        sleeper=sleeps.append,
        retries=3,
    )
    assert payload == {"ok": True}
    assert sleeps == [0.5]


def test_telegram_retries_rate_limit_and_falls_back_from_bad_html(monkeypatch):
    monkeypatch.setattr("smartmoney_bot.notify.time_sleep", lambda _seconds: None)

    class Response:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.ok = 200 <= status < 300
            self.headers = {}

        def json(self):
            return self._payload

    class Session:
        def __init__(self, responses):
            self._responses = list(responses)
            self.sent = []

        def post(self, url, json=None, timeout=None):
            self.sent.append(json)
            return self._responses.pop(0)

    limited = Session(
        [
            Response(429, {"parameters": {"retry_after": 0}}),
            Response(200, {"ok": True}),
        ]
    )
    assert _deliver("token", "1", "hello", session=limited) is True
    assert len(limited.sent) == 2

    broken = Session(
        [
            Response(400, {"ok": False, "description": "can't parse entities"}),
            Response(200, {"ok": True}),
        ]
    )
    assert _deliver("token", "1", "<b>hi</b>", session=broken) is True
    assert broken.sent[0]["parse_mode"] == "HTML"
    assert "parse_mode" not in broken.sent[1]


def test_stream_parser_and_backoff():
    assert backoff_seconds(0) == 1
    assert backoff_seconds(3) == 8
    assert backoff_seconds(10) == 60
    payload = {
        "limit": 90,
        "data": [
            {"chainId": "solana", "tokenAddress": MINT},
            {"chainId": "ethereum", "tokenAddress": "0x1"},
        ],
    }
    stream = ProfileStream()
    assert stream.ingest(payload) == 1
    assert stream.ingest(payload) == 0
    assert stream.drain() == [MINT]
    assert profiles_from_message([{"tokenAddress": "abc", "chainId": "solana"}])[0]["tokenAddress"] == "abc"


def test_health_endpoints_do_not_start_the_scanner():
    from web import STOP, _started, app

    assert _started is False
    assert STOP["stop"] is False
    client = app.test_client()
    health = client.get("/health")
    home = client.get("/")
    assert health.status_code == 200
    assert home.status_code == 200
    body = health.get_json()
    assert body["status"] == "ok"
    assert body["service"] == "smartmoneybot"
    assert "secret" not in str(body).lower()
