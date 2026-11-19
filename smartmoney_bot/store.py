"""SQLite state: signals, paper positions, alert dedupe, and scan metadata."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT,
    pair TEXT,
    symbol TEXT,
    mcap REAL,
    liquidity_usd REAL,
    decision TEXT,
    reason TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS paper_calls (
    token TEXT PRIMARY KEY,
    symbol TEXT,
    entry_mcap REAL,
    size_usd REAL,
    size_sol REAL,
    status TEXT,
    opened_at TEXT,
    milestones TEXT,
    closed_at TEXT,
    exit_mcap REAL,
    pnl_usd REAL,
    pnl_sol REAL,
    source TEXT,
    last_mcap REAL,
    pair TEXT,
    mode TEXT,
    order_id TEXT,
    tx_hash TEXT,
    strategy_order_id TEXT,
    exit_owner TEXT,
    exit_order_id TEXT,
    exit_reason TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT,
    symbol TEXT,
    side TEXT,
    mode TEXT,
    order_id TEXT,
    tx_hash TEXT,
    status TEXT,
    size_sol REAL,
    input_amount TEXT,
    output_amount TEXT,
    error TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(token, kind)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS seen_signatures (
    signature TEXT PRIMARY KEY,
    wallet TEXT,
    block_time INTEGER
);

CREATE INDEX IF NOT EXISTS idx_signals_token ON signals(token, id DESC);
CREATE INDEX IF NOT EXISTS idx_signals_decision ON signals(decision, id DESC);
"""

# Columns added after the original paper_calls table. Fresh databases already
# have them; older files get them here.
PAPER_EXTRA = (
    ("size_sol", "REAL"),
    ("milestones", "TEXT"),
    ("closed_at", "TEXT"),
    ("exit_mcap", "REAL"),
    ("pnl_usd", "REAL"),
    ("pnl_sol", "REAL"),
    ("source", "TEXT"),
    ("last_mcap", "REAL"),
    ("pair", "TEXT"),
    ("mode", "TEXT"),
    ("order_id", "TEXT"),
    ("tx_hash", "TEXT"),
    ("strategy_order_id", "TEXT"),
    ("exit_owner", "TEXT"),
    ("exit_order_id", "TEXT"),
    ("exit_reason", "TEXT"),
)


def db_path() -> Path:
    raw = os.environ.get("SMARTMONEY_DB")
    if raw:
        return Path(raw)
    data_dir = os.environ.get("DATA_DIR")
    if data_dir:
        return Path(data_dir) / "smartmoney.db"
    return Path(__file__).resolve().parent.parent / "smartmoney.db"


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(paper_calls)")}
    for name, ddl in PAPER_EXTRA:
        if name not in existing:
            conn.execute(f"ALTER TABLE paper_calls ADD COLUMN {name} {ddl}")


@contextmanager
def db():
    conn = connect()
    try:
        ensure_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_meta(key: str, default: str | None = None) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def set_meta(key: str, value: str) -> None:
    with db() as conn:
        write_meta(conn, key, value)


def write_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def paused() -> bool:
    return get_meta("paused", "0") == "1"


def set_paused(value: bool) -> None:
    set_meta("paused", "1" if value else "0")


def record_signal(conn: sqlite3.Connection, pair: dict, decision: str, reason: str, now: datetime) -> bool:
    """Insert a signal when the decision or reason changed. Returns True if inserted."""
    row = conn.execute(
        "SELECT decision, reason FROM signals WHERE token = ? ORDER BY id DESC LIMIT 1",
        (pair.get("token"),),
    ).fetchone()
    if row and row["decision"] == decision and row["reason"] == reason:
        return False
    conn.execute(
        """
        INSERT INTO signals (token, pair, symbol, mcap, liquidity_usd, decision, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pair.get("token"),
            pair.get("pair"),
            pair.get("symbol"),
            pair.get("mcap"),
            pair.get("liquidity_usd"),
            decision,
            reason,
            now.isoformat(),
        ),
    )
    return True


def claim_alert(conn: sqlite3.Connection, token: str, kind: str, now: datetime) -> bool:
    try:
        conn.execute(
            "INSERT INTO alerts (token, kind, created_at) VALUES (?, ?, ?)",
            (token, kind, now.isoformat()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def known_tokens(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT token FROM paper_calls").fetchall()
    return {row["token"] for row in rows}


def open_positions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_calls WHERE status IN ('open', 'pending') ORDER BY opened_at"
    ).fetchall()
    return [dict(row) for row in rows]


def exposure_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_calls WHERE status IN ('open', 'pending')"
    ).fetchone()
    return int(row["n"])


def open_position(
    conn: sqlite3.Connection,
    pair: dict,
    *,
    size_sol: float,
    size_usd: float | None,
    source: str,
    now: datetime,
    status: str = "open",
    mode: str = "paper",
    order_id: str | None = None,
    tx_hash: str | None = None,
    strategy_order_id: str | None = None,
    exit_owner: str = "bot",
) -> bool:
    conn.execute(
        """
        INSERT INTO paper_calls (
            token, symbol, entry_mcap, size_usd, size_sol, status, opened_at,
            milestones, source, last_mcap, pair, mode, order_id, tx_hash,
            strategy_order_id, exit_owner
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(token) DO NOTHING
        """,
        (
            pair["token"],
            pair.get("symbol"),
            pair.get("mcap"),
            size_usd,
            size_sol,
            status,
            now.isoformat(),
            source,
            pair.get("mcap"),
            pair.get("pair"),
            mode,
            order_id,
            tx_hash,
            strategy_order_id,
            exit_owner,
        ),
    )
    return _changed(conn) == 1


def record_fill(
    conn: sqlite3.Connection,
    *,
    token: str,
    symbol: str | None,
    side: str,
    mode: str,
    status: str,
    now: datetime,
    order_id: str | None = None,
    tx_hash: str | None = None,
    size_sol: float | None = None,
    input_amount: str | None = None,
    output_amount: str | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO fills (
            token, symbol, side, mode, order_id, tx_hash, status, size_sol,
            input_amount, output_amount, error, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            token,
            symbol,
            side,
            mode,
            order_id,
            tx_hash,
            status,
            size_sol,
            input_amount,
            output_amount,
            (error or "")[:300],
            now.isoformat(),
        ),
    )


def daily_loss_sol(conn: sqlite3.Connection, now: datetime) -> float:
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).isoformat()
    row = conn.execute(
        """
        SELECT COALESCE(SUM(pnl_sol), 0) AS loss
        FROM paper_calls
        WHERE status != 'open' AND closed_at >= ? AND pnl_sol < 0
        """,
        (start,),
    ).fetchone()
    return abs(float(row["loss"] or 0))


def seen_signatures(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT signature FROM seen_signatures").fetchall()
    return {row["signature"] for row in rows}


def remember_signatures(conn: sqlite3.Connection, rows: list[tuple[str, str, int]]) -> None:
    conn.executemany(
        """
        INSERT INTO seen_signatures (signature, wallet, block_time) VALUES (?, ?, ?)
        ON CONFLICT(signature) DO NOTHING
        """,
        rows,
    )


def _changed(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT changes()").fetchone()
    return int(row[0] if row else 0)


def prune(conn: sqlite3.Connection, now: datetime, *, signal_days: int = 14) -> None:
    cutoff = now.timestamp() - signal_days * 86400
    stamp = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    conn.execute("DELETE FROM signals WHERE created_at < ?", (stamp,))
    conn.execute("DELETE FROM seen_signatures WHERE block_time > 0 AND block_time < ?", (int(cutoff),))


def recent_signals(conn: sqlite3.Connection, *, decision: str | None = None, limit: int = 8) -> list[dict]:
    if decision:
        rows = conn.execute(
            """
            SELECT symbol, mcap, decision, reason, token, created_at
            FROM signals WHERE decision = ? ORDER BY id DESC LIMIT ?
            """,
            (decision, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT symbol, mcap, decision, reason, token, created_at
            FROM signals ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def count_signals(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"]
    watches = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE decision = 'watch'").fetchone()["n"]
    opens = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_calls WHERE status IN ('open', 'pending')"
    ).fetchone()["n"]
    return {"signals": total, "watches": watches, "open": opens}
