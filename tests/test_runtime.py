"""Startup paths that crashed in production, plus the user-facing paper desk."""

from __future__ import annotations

import pytest

from smartmoney_bot.chat import handle_callback, reply, respond, telegram_loop
from smartmoney_bot.community import (
    add_wallet,
    leaderboard_text,
    paper_buy,
    paper_sell,
    referral_code,
    register_subscriber,
    seed_subscribers,
    settle_positions,
    subscriber_ids,
    toggle_alert,
    update_fumbles,
)
from smartmoney_bot.http import ApiError
from smartmoney_bot.loop import serve
from smartmoney_bot.runtime import supervise, thread_snapshot
from smartmoney_bot.safety import parse_goplus_solana, parse_rugcheck, score_facts
from smartmoney_bot.store import db, get_meta, qmark_to_percent


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMONEY_DB", str(tmp_path / "rt.db"))
    monkeypatch.setenv("KEEP_AWAKE", "0")
    monkeypatch.setenv("ENABLE_PROFILE_STREAM", "0")
    monkeypatch.setenv("SMARTMONEY_DISABLE_BACKGROUND", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from smartmoney_bot.loop import reset_runtime_state
    from smartmoney_bot.safety import clear_cache

    reset_runtime_state()
    clear_cache()
    yield


def test_serve_does_not_crash_before_reading_config(monkeypatch):
    monkeypatch.setenv("ENABLE_PROFILE_STREAM", "0")
    serve({"stop": True})


def test_telegram_start_registers_a_subscriber_without_env_chat(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    stop = {"stop": False}
    calls = {"n": 0}

    def poll():
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "ok": True,
                "result": [
                    {
                        "update_id": 7,
                        "message": {
                            "message_id": 1,
                            "chat": {"id": 42, "username": "dart"},
                            "text": "/start",
                        },
                    }
                ],
            }
        stop["stop"] = True
        return {"ok": True, "result": []}

    sent = []
    deleted = []
    telegram_loop(
        stop,
        poll=poll,
        webhook_delete=lambda token: deleted.append(token),
        sleeper=lambda _seconds: None,
        deliver=lambda text, **_kwargs: sent.append(text),
    )
    assert deleted == ["123:ABC"]
    assert get_meta("telegram_offset") == "8"
    assert "42" in subscriber_ids()
    assert "paper" in sent[0].lower()
    assert "inline_keyboard" in str(respond("/help", chat_id=42)[1])


def test_telegram_409_waits_instead_of_dying(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    stop = {"stop": False}
    waits = []

    def poll():
        if not waits:
            raise ApiError("HTTP 409 from telegram", 409)
        stop["stop"] = True
        return {"result": []}

    telegram_loop(
        stop,
        poll=poll,
        webhook_delete=lambda _token: None,
        sleeper=lambda seconds: waits.append(seconds),
        deliver=lambda text, **_kwargs: None,
    )
    assert waits[0] >= 5


def test_supervisor_restarts_after_a_crash():
    seen = []
    runs = {"n": 0}

    def target(stop):
        runs["n"] += 1
        if runs["n"] == 1:
            raise RuntimeError("cannot access local variable cfg")
        stop["stop"] = True

    def sleeper(_seconds):
        seen.append(thread_snapshot()["scanner"]["error"])

    supervise("scanner", target, {"stop": False}, sleeper=sleeper)
    assert runs["n"] == 2
    assert "cfg" in seen[0]
    assert thread_snapshot()["scanner"]["restarts"] == 1


def test_score_skips_a_honeypot_and_accepts_a_clean_token():
    bad = score_facts({"honeypot": True, "mint_revoked": True, "freeze_revoked": True})
    assert bad["verdict"] == "SKIP"
    clean = score_facts(
        {
            "mint_revoked": True,
            "freeze_revoked": True,
            "top_pct": 4,
            "holders": 400,
            "sell_tax_pct": 0,
            "lp_known": True,
            "lp_burned": True,
            "danger_count": 0,
        }
    )
    assert clean["verdict"] == "BUY"
    rug = parse_rugcheck(
        {
            "risks": [{"level": "danger", "name": "mutable"}],
            "topHolders": [{"pct": 30, "insider": True}, {"pct": 2}],
            "markets": [{"lp": {"lpLockedPct": 10, "lpBurned": False}}],
        }
    )
    assert rug["danger_count"] == 1
    assert rug["top_pct"] == 30
    assert rug["dev_pct"] == 30
    goplus = parse_goplus_solana(
        {"result": {"Mint": {"mintable": {"status": "1"}, "non_transferable": "1"}}},
        "Mint",
    )
    assert goplus["mint_revoked"] is False
    assert goplus["non_transferable"] is True


def test_paper_wallet_referral_fumble_and_alerts():
    first = register_subscriber(7, "ann")
    second = register_subscriber(8, "bob", f"/start {first['referral_code']}")
    assert second["referral_code"] == referral_code(8)
    bought = paper_buy(8, "Mint", "PEPE", "solana", price=1, mcap=100, usd=100, tp=2, sl=0.5)
    assert bought["ok"] is True
    closed = settle_positions({"Mint": {"price": 2.5, "mcap": 250, "symbol": "PEPE"}})
    assert closed and "TAKE PROFIT" in closed[0][1]
    sold = paper_buy(7, "Other", "DOG", "solana", price=1, mcap=50, usd=40)
    assert sold["ok"] is True
    paper_sell(7, "Other", 100, price=1, mcap=50)
    notes = update_fumbles({"Other": {"mcap": 150, "price": 3, "symbol": "DOG"}})
    assert notes and "Fumble" in notes[0][1]
    assert update_fumbles({"Other": {"mcap": 200, "price": 4, "symbol": "DOG"}}) == []
    board = leaderboard_text(8)
    assert "ann" in board or "7" in board
    assert "Invites" in board or "t.me" in board or "/start" in board
    toggle_alert(8, "alerts_launch")
    assert "8" not in subscriber_ids("launch")
    assert "8" in subscriber_ids("pump")
    add_wallet(8, "1" * 32, copy_paper=True)
    assert "Wallet" in reply("/wallet", chat_id=8) or "Cash" in reply("/wallet", chat_id=8)


def test_env_chat_is_seeded_and_placeholders_translate(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99")
    assert seed_subscribers() == 1
    assert subscriber_ids() == ["99"]
    assert qmark_to_percent("SELECT * FROM subscribers WHERE chat_id = ?") == (
        "SELECT * FROM subscribers WHERE chat_id = %s"
    )


def test_callback_buy_uses_the_quoted_price(monkeypatch):
    register_subscriber(5, "cara")

    def fake_scan(_address):
        return {"symbol": "PEPE", "chain": "solana", "price": 2, "mcap": 80, "verdict": "BUY", "score": 80, "reasons": ["ok"]}

    monkeypatch.setattr("smartmoney_bot.chat.scan_address", fake_scan, raising=False)
    # handle_callback imports scan_address inside the function, so patch the safety module.
    monkeypatch.setattr("smartmoney_bot.safety.scan_address", fake_scan)
    text, _markup = handle_callback("b25:" + ("M" * 32), 5)
    assert "Paper buy $25" in text
    with db() as conn:
        cash = conn.execute("SELECT cash_usd FROM subscribers WHERE chat_id = '5'").fetchone()["cash_usd"]
    assert cash == 975
