"""GMGN signing, request shape, and live-mode gates. No network."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from smartmoney_bot.gmgn import (
    GmgnClient,
    HOST,
    QUERY_ORDER_PATH,
    SWAP_PATH,
    TradeResult,
    build_buy_body,
    build_message,
    build_request,
    build_sell_body,
    condition_orders,
    encode_uri_component,
    entry_decision,
    lamports_for,
    normalize_pem,
    request_url,
    should_submit_sell,
    sign_message,
    sorted_query,
)
from smartmoney_bot.loop import reset_runtime_state, run_once
from smartmoney_bot.settings import SOLANA_MINT, load_settings
from smartmoney_bot.store import db, open_position

MINT = "Mint111111111111111111111111111111111111111"
WALLET = "1" * 32
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMONEY_DB", str(tmp_path / "gmgn.db"))
    monkeypatch.setenv("KEEP_AWAKE", "0")
    monkeypatch.setenv("ENABLE_PROFILE_STREAM", "0")
    monkeypatch.delenv("TRADE_MODE", raising=False)
    monkeypatch.delenv("GMGN_API_KEY", raising=False)
    monkeypatch.delenv("GMGN_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GMGN_WALLET", raising=False)
    monkeypatch.delenv("GMGN_ALLOW_EPHEMERAL_DB", raising=False)
    reset_runtime_state()
    yield


def pem_pair():
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode("ascii")
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return key, pem, public


def test_ed25519_signature_verifies_and_escaped_pem_matches():
    key, pem, public = pem_pair()
    message = "/v1/trade/swap:client_id=abc&timestamp=1700000000:{}:1700000000"
    signature = sign_message(message, pem)
    key.public_key().verify(base64.b64decode(signature), message.encode("utf-8"))
    escaped = pem.replace("\n", "\\n")
    assert sign_message(message, normalize_pem(escaped)) == signature
    assert public  # the raw public key is what GMGN stores; we only need it to exist


def test_canonical_string_matches_the_official_client():
    """signer.ts buildMessage + OpenApiClient query insertion order."""
    query = {"timestamp": 1700000000, "client_id": "11111111-1111-4111-8111-111111111111"}
    body = '{"chain":"sol","from_address":"wallet"}'
    message = build_message(SWAP_PATH, query, body, 1700000000)
    assert message == (
        "/v1/trade/swap:client_id=11111111-1111-4111-8111-111111111111"
        "&timestamp=1700000000:" + body + ":1700000000"
    )
    assert encode_uri_component("a b") == "a%20b"
    assert encode_uri_component("a+b") == "a%2Bb"
    assert sorted_query({"filter": ["b", "a"], "chain": "sol"}) == "chain=sol&filter=a&filter=b"
    url = request_url(HOST, SWAP_PATH, query)
    assert url == (
        "https://openapi.gmgn.ai/v1/trade/swap?timestamp=1700000000"
        "&client_id=11111111-1111-4111-8111-111111111111"
    )


def test_get_order_signs_an_empty_body():
    _, pem, _public = pem_pair()
    request = build_request(
        method="GET",
        sub_path=QUERY_ORDER_PATH,
        query_extra={"order_id": "ord_1", "chain": "sol"},
        body=None,
        api_key="test-key",
        private_key_pem=pem,
        timestamp=1700000000,
        client_id="11111111-1111-4111-8111-111111111111",
    )
    assert request.body is None
    assert request.message == (
        "/v1/trade/query_order:chain=sol"
        "&client_id=11111111-1111-4111-8111-111111111111"
        "&order_id=ord_1&timestamp=1700000000::1700000000"
    )
    assert request.headers["X-APIKEY"] == "test-key"
    assert request.headers["X-Signature"]
    assert "timestamp=1700000000" in request.url
    assert request.url.index("order_id=") < request.url.index("timestamp=")


def test_buy_body_uses_lamports_condition_orders_and_js_json():
    cfg = {
        "gmgn_wallet": WALLET,
        "trade_size_sol": 0.05,
        "take_profit_multiple": 2.5,
        "stop_loss_multiple": 0.65,
        "gmgn_anti_mev": True,
        "gmgn_priority_fee": "0.00001",
        "gmgn_tip_fee": "0.00001",
        "gmgn_slippage": None,
    }
    assert lamports_for("0.05") == "50000000"
    orders = condition_orders(2.5, 0.65)
    assert orders[0]["price_scale"] == "150"
    assert orders[1]["price_scale"] == "35"
    body = build_buy_body(cfg, MINT)
    _, pem, _public = pem_pair()
    request = build_request(
        method="POST",
        sub_path=SWAP_PATH,
        query_extra={},
        body=body,
        api_key="k",
        private_key_pem=pem,
        timestamp=1700000000,
        client_id="cid",
    )
    assert '"input_amount":"50000000"' in request.body
    assert '"auto_slippage":true' in request.body
    assert '"is_anti_mev":true' in request.body
    assert '"priority_fee":"0.00001"' in request.body
    assert '"order_type":"profit_stop"' in request.body
    assert '"sell_ratio_type":"buy_amount"' in request.body
    assert request.message.endswith(":" + request.body + ":1700000000")
    sell = build_sell_body(cfg, MINT, 100)
    assert sell["input_amount"] == "0"
    assert sell["input_amount_bps"] == "10000"
    assert sell["output_token"] == SOLANA_MINT


def test_swap_transport_is_signed_and_confirmed_without_a_second_poll():
    _, pem, _public = pem_pair()
    seen = []

    def transport(request):
        seen.append(request)
        assert request.headers["X-APIKEY"] == "secret-key"
        assert request.method == "POST"
        return {
            "code": 0,
            "data": {
                "order_id": "ord",
                "status": "confirmed",
                "hash": "sig",
                "strategy_order_id": "strat",
            },
        }

    cfg = load_settings()
    cfg.update(
        {
            "gmgn_api_key": "secret-key",
            "gmgn_private_key": pem,
            "gmgn_wallet": WALLET,
            "trade_size_sol": 0.05,
            "take_profit_multiple": 2.5,
            "stop_loss_multiple": 0.65,
            "gmgn_anti_mev": True,
            "gmgn_slippage": 30,
        }
    )
    client = GmgnClient(cfg, transport=transport, clock=lambda: 1700000000, client_ids=["cid"], confirm_attempts=3)
    result = client.buy(MINT, "PEPE", 0.05)
    assert result.ok
    assert result.order_id == "ord"
    assert result.strategy_order_id == "strat"
    assert len(seen) == 1
    assert '"slippage":30' in seen[0].body
    assert "auto_slippage" not in seen[0].body


def test_entry_decision_gates_mode_caps_and_pause():
    base = {"trade_mode": "paper", "max_open_positions": 4, "max_daily_loss_sol": 0.3, "gmgn_ready": True, "gmgn_live_storage_ok": True}
    assert entry_decision(base, paused=False, open_count=0, daily_loss=0) == ("paper", "pass")
    assert entry_decision(dict(base, trade_mode="off"), paused=False, open_count=0, daily_loss=0)[0] == "skip"
    assert entry_decision(base, paused=True, open_count=0, daily_loss=0)[1] == "paused"
    assert "max open" in entry_decision(base, paused=False, open_count=4, daily_loss=0)[1]
    assert "daily loss" in entry_decision(base, paused=False, open_count=0, daily_loss=0.3)[1]
    live = dict(base, trade_mode="live")
    assert entry_decision(live, paused=False, open_count=0, daily_loss=0) == ("live", "pass")
    assert entry_decision(dict(live, gmgn_ready=False), paused=False, open_count=0, daily_loss=0)[1] == "gmgn credentials missing"
    blocked = entry_decision(dict(live, gmgn_live_storage_ok=False), paused=False, open_count=0, daily_loss=0)
    assert blocked[1] == "live mode needs a persistent database path"
    assert should_submit_sell({"exit_owner": "gmgn", "exit_order_id": None}, {"type": "close", "reason": "take_profit"}) is False
    assert should_submit_sell({"exit_owner": "gmgn"}, {"type": "close", "reason": "time_stop"}) is True
    assert should_submit_sell({"exit_owner": "bot", "exit_order_id": "already"}, {"type": "close", "reason": "time_stop"}) is False


def _pair():
    created = int(NOW.timestamp() * 1000) - 3_600_000
    return {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": "Pair111",
        "url": "https://dexscreener.com/solana/Pair111",
        "baseToken": {"address": MINT, "symbol": "PEPE", "name": "Pepe"},
        "priceUsd": "0.001",
        "marketCap": 100_000,
        "fdv": 120_000,
        "liquidity": {"usd": 20_000},
        "volume": {"h1": 10_000},
        "pairCreatedAt": created,
    }


def _get(pairs):
    def get(path):
        if path.startswith("/token-profiles"):
            return [{"chainId": "solana", "tokenAddress": MINT}]
        if path.startswith("/token-boosts"):
            return []
        if SOLANA_MINT in path:
            return [{"baseToken": {"address": SOLANA_MINT}, "priceUsd": "150", "liquidity": {"usd": 1}}]
        if path.startswith("/tokens/"):
            return pairs
        raise AssertionError(path)

    return get


class Guard:
    def __init__(self):
        self.calls = []

    def buy(self, token, symbol, size_sol):
        self.calls.append(("buy", token, size_sol))
        raise AssertionError("paper mode must not buy")

    def sell(self, *args):
        raise AssertionError("paper mode must not sell")

    def refresh(self, *args):
        raise AssertionError("paper mode must not refresh")


class BuyOnce:
    def __init__(self, result):
        self.calls = []
        self.result = result

    def buy(self, token, symbol, size_sol):
        self.calls.append(("buy", token, symbol, size_sol))
        return self.result

    def sell(self, token, symbol, reason):
        self.calls.append(("sell", token, reason))
        return TradeResult(ok=True, status="confirmed", order_id="sell-1", terminal=True)

    def refresh(self, order_id):
        self.calls.append(("refresh", order_id))
        return TradeResult(ok=True, status="confirmed", order_id=order_id, terminal=True, occupies_slot=True)


def _arm_live(monkeypatch, *, allow_ephemeral=True):
    _key, pem, _public = pem_pair()
    monkeypatch.setenv("TRADE_MODE", "live")
    monkeypatch.setenv("GMGN_API_KEY", "secret-key")
    monkeypatch.setenv("GMGN_PRIVATE_KEY", pem.replace("\n", "\\n"))
    monkeypatch.setenv("GMGN_WALLET", WALLET)
    if allow_ephemeral:
        monkeypatch.setenv("GMGN_ALLOW_EPHEMERAL_DB", "1")
    return pem


def test_paper_mode_does_not_call_the_trader():
    sent = []
    summary = run_once(
        now=NOW,
        get=_get([_pair()]),
        sender=sent.append,
        check_token=lambda _pair: {"mint_revoked": True, "freeze_revoked": True, "holders": 400, "top_pct": 2},
        trader=Guard(),
    )
    assert summary["watches"] == 1
    assert "NEW CALL" in sent[0]
    assert "LIVE BUY" not in "\n".join(sent)


def test_live_buy_respects_caps_pause_and_ephemeral_disk(monkeypatch):
    _arm_live(monkeypatch, allow_ephemeral=False)
    trader = BuyOnce(TradeResult(ok=True, status="confirmed", order_id="ord", strategy_order_id="strat", terminal=True, occupies_slot=True))
    sent = []
    blocked = run_once(
        now=NOW,
        get=_get([_pair()]),
        sender=sent.append,
        check_token=lambda _pair: {"mint_revoked": True, "freeze_revoked": True, "holders": 400, "top_pct": 2},
        trader=trader,
    )
    assert blocked["watches"] == 0
    assert trader.calls == []
    with db() as conn:
        reason = conn.execute("SELECT reason FROM signals ORDER BY id DESC LIMIT 1").fetchone()["reason"]
    assert reason == "live mode needs a persistent database path"

    monkeypatch.setenv("GMGN_ALLOW_EPHEMERAL_DB", "1")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "1")
    with db() as conn:
        open_position(
            conn,
            {"token": "AlreadyHeld111111111111111111111111111111", "symbol": "OLD", "mcap": 1, "pair": "p"},
            size_sol=0.05,
            size_usd=1,
            source="test",
            now=NOW,
            mode="live",
        )
    capped = run_once(
        now=NOW,
        get=_get([_pair()]),
        sender=sent.append,
        check_token=lambda _pair: {"mint_revoked": True, "freeze_revoked": True, "holders": 400, "top_pct": 2},
        trader=trader,
    )
    assert capped["watches"] == 0
    assert trader.calls == []

    with db() as conn:
        conn.execute("DELETE FROM paper_calls")
    from smartmoney_bot.store import set_paused

    set_paused(True)
    paused = run_once(
        now=NOW,
        get=_get([_pair()]),
        sender=sent.append,
        check_token=lambda _pair: {"mint_revoked": True, "freeze_revoked": True, "holders": 400, "top_pct": 2},
        trader=trader,
    )
    assert paused["paused"] is True
    assert trader.calls == []
    set_paused(False)

    bought = run_once(
        now=NOW,
        get=_get([_pair()]),
        sender=sent.append,
        check_token=lambda _pair: {"mint_revoked": True, "freeze_revoked": True, "holders": 400, "top_pct": 2},
        trader=trader,
    )
    assert bought["watches"] == 1
    assert trader.calls[0][0] == "buy"
    assert trader.calls[0][3] == pytest.approx(0.05)
    assert any("LIVE BUY" in message for message in sent)
    again = run_once(
        now=NOW + timedelta(minutes=5),
        get=_get([_pair()]),
        sender=sent.append,
        check_token=lambda _pair: {"mint_revoked": True, "freeze_revoked": True, "holders": 400, "top_pct": 2},
        trader=trader,
    )
    assert again["watches"] == 0
    assert [call[0] for call in trader.calls] == ["buy"]
    with db() as conn:
        row = conn.execute("SELECT mode, order_id, exit_owner, status FROM paper_calls").fetchone()
        fills = conn.execute("SELECT side, order_id FROM fills").fetchall()
    assert row["mode"] == "live"
    assert row["order_id"] == "ord"
    assert row["exit_owner"] == "gmgn"
    assert row["status"] == "open"
    assert fills[0]["side"] == "buy"


def test_mode_command_does_not_enable_live():
    from smartmoney_bot.chat import reply

    text = reply("/mode live")
    assert "paper" in text.lower()
    assert "TRADE_MODE=live" in text
    assert "No chat command" in text
