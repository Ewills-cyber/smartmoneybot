"""Per-user paper settings and the database backend choice."""

from __future__ import annotations

import logging
import sqlite3

import pytest

from smartmoney_bot.chat import handle_callback, respond
from smartmoney_bot.community import (
    apply_setting,
    paper_buy,
    parse_setting_number,
    register_subscriber,
    settle_positions,
    user_settings,
    validate_setting,
)
from smartmoney_bot.settings import load_settings
from smartmoney_bot.store import db, log_storage, storage_status


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMONEY_DB", str(tmp_path / "settings.db"))
    monkeypatch.setenv("KEEP_AWAKE", "0")
    monkeypatch.setenv("ENABLE_PROFILE_STREAM", "0")
    monkeypatch.setenv("SMARTMONEY_DISABLE_BACKGROUND", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GMGN_ALLOW_EPHEMERAL_DB", raising=False)
    monkeypatch.delenv("TRADE_MODE", raising=False)
    yield


def test_new_users_keep_the_original_defaults():
    register_subscriber(3, "cara")
    prefs = user_settings(3)
    assert prefs["buy_a"] == 25
    assert prefs["buy_b"] == 50
    assert prefs["buy_c"] == 100
    assert prefs["tp_multiple"] == 2
    assert prefs["sl_multiple"] == 0.5
    assert prefs["copy_usd"] == 50


def test_validation_bounds():
    assert validate_setting("tp_multiple", 1, 1000)[0] is False
    assert validate_setting("tp_multiple", 2.5, 1000)[0] is True
    assert validate_setting("sl_multiple", 0, 1000)[0] is False
    assert validate_setting("sl_multiple", 1, 1000)[0] is False
    assert validate_setting("sl_multiple", 0.7, 1000)[0] is True
    assert validate_setting("buy_a", 0, 1000)[0] is False
    assert validate_setting("buy_a", 1001, 1000)[0] is False
    assert validate_setting("copy_usd", 40, 1000)[0] is True
    assert parse_setting_number(" $1,250x ") == 1250


def test_open_position_keeps_the_exits_from_open_time():
    register_subscriber(4, "dee")
    assert apply_setting(4, "tp_multiple", 3)["ok"] is True
    assert apply_setting(4, "sl_multiple", 0.4)["ok"] is True
    bought = paper_buy(4, "Mint", "PEPE", "solana", price=1, mcap=100, usd=20)
    assert bought["ok"] is True
    assert apply_setting(4, "tp_multiple", 5)["ok"] is True
    paper_buy(4, "Mint", "PEPE", "solana", price=1, mcap=100, usd=10)
    with db() as conn:
        row = conn.execute("SELECT tp_multiple, sl_multiple, size_usd FROM user_positions").fetchone()
    assert row["tp_multiple"] == 3
    assert row["sl_multiple"] == 0.4
    assert row["size_usd"] == 30
    assert settle_positions({"Mint": {"price": 2.5, "mcap": 250, "symbol": "PEPE"}}) == []
    closed = settle_positions({"Mint": {"price": 3, "mcap": 300, "symbol": "PEPE"}})
    assert closed and "TAKE PROFIT" in closed[0][1]


def test_settings_command_presets_and_typed_values():
    register_subscriber(9, "eve")
    text, markup = handle_callback("menu:settings", 9)
    assert "Take profit: 2x" in text
    assert any(button.get("callback_data") == "stg:tp_multiple:3" for row in markup["inline_keyboard"] for button in row)
    saved, _keyboard = handle_callback("stg:buy_a:10", 9)
    assert "Saved" in saved
    assert user_settings(9)["buy_a"] == 10
    refused, _keyboard = handle_callback("stg:buy_c:5000", 9)
    assert "paper cash" in refused.lower()
    assert user_settings(9)["buy_c"] == 100
    handle_callback("set:sl_multiple", 9)
    typed, _keyboard = respond("0.25", chat_id=9)
    assert "Saved" in typed
    assert user_settings(9)["sl_multiple"] == 0.25
    handle_callback("set:sl_multiple", 9)
    bad, _keyboard = respond("1.2", chat_id=9)
    assert "below 1" in bad.lower()
    assert user_settings(9)["sl_multiple"] == 0.25


def test_buy_slot_uses_the_saved_size(monkeypatch):
    register_subscriber(5, "cara")
    apply_setting(5, "buy_a", 10)

    def fake_scan(_address):
        return {"symbol": "PEPE", "chain": "solana", "price": 2, "mcap": 80, "verdict": "BUY", "score": 80, "reasons": ["ok"]}

    monkeypatch.setattr("smartmoney_bot.safety.scan_address", fake_scan)
    text, markup = handle_callback("b1:" + ("M" * 32), 5)
    assert "Paper buy $10" in text
    labels = [button["text"] for row in markup["inline_keyboard"] for button in row]
    assert "Buy $10" in labels
    with db() as conn:
        cash = conn.execute("SELECT cash_usd FROM subscribers WHERE chat_id = '5'").fetchone()["cash_usd"]
        tp = conn.execute("SELECT tp_multiple FROM user_positions").fetchone()["tp_multiple"]
    assert cash == 990
    assert tp == 2


def test_old_subscriber_rows_gain_settings_columns(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    monkeypatch.setenv("SMARTMONEY_DB", str(path))
    raw = sqlite3.connect(path)
    raw.execute(
        """
        CREATE TABLE subscribers (
            chat_id TEXT PRIMARY KEY,
            username TEXT,
            referral_code TEXT,
            referrer TEXT,
            cash_usd REAL,
            alerts_launch INTEGER,
            alerts_cluster INTEGER,
            alerts_pump INTEGER,
            created_at TEXT
        )
        """
    )
    raw.execute(
        "INSERT INTO subscribers (chat_id, username, referral_code, cash_usd, alerts_launch, alerts_cluster, alerts_pump, created_at) VALUES ('4', 'old', 'abcd1234', 80, 1, 1, 1, '2026-01-01')"
    )
    raw.commit()
    raw.close()
    prefs = user_settings(4)
    assert prefs["buy_a"] == 25
    assert prefs["tp_multiple"] == 2
    assert prefs["copy_usd"] == 50
    assert prefs["cash_usd"] == 80
    refused = apply_setting(4, "copy_usd", 90)
    assert refused["ok"] is False


def test_postgres_is_durable_and_the_url_is_not_logged(monkeypatch, caplog):
    monkeypatch.setenv("SMARTMONEY_DB", "/tmp/smartmoney.db")
    assert storage_status()["backend"] == "sqlite"
    assert storage_status()["ephemeral"] is True
    assert load_settings()["gmgn_live_storage_ok"] is False
    assert load_settings()["trade_mode"] == "paper"
    monkeypatch.setenv("DATABASE_URL", "postgres://bot:s3cret@db.example/smart")
    status = storage_status()
    assert status == {"backend": "postgres", "ephemeral": False, "detail": "DATABASE_URL"}
    assert load_settings()["gmgn_live_storage_ok"] is True
    assert load_settings()["trade_mode"] == "paper"
    with caplog.at_level(logging.INFO):
        log_storage()
    assert "s3cret" not in caplog.text
    assert "postgres" in caplog.text.lower()
