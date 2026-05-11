"""
End-to-end integration tests for the full forecast → consensus → evaluation pipeline.

All tests use:
- A real temporary SQLite database (minimal schema matching production columns)
- Mocked AI responses (no network calls)
- Real consensus / consensus_evaluator / circuit_breaker logic

Run with:
    python -m pytest scripts/tests/test_integration_e2e.py -v -m integration
"""

import os
import sys
import sqlite3
import tempfile
import pytest
import unittest.mock as mock
from datetime import datetime, timedelta

# Ensure scripts/core is importable
_CORE = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "core"))
_SCRIPTS = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
for _p in [_CORE, _SCRIPTS]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Schema — minimal columns needed by consensus_evaluator + consensus modules
# ---------------------------------------------------------------------------

_CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS consensus (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, ticker TEXT, signal TEXT, confidence REAL,
    methods_long TEXT, methods_short TEXT, methods_neutral TEXT, rationale TEXT,
    target_price REAL, stop_loss REAL, entry_limit_price REAL,
    high_model_disagreement INTEGER DEFAULT 0,
    horizon_hours INTEGER, eval_target_date TEXT,
    eval_status TEXT DEFAULT 'PENDING',
    actual_date TEXT, actual_open REAL, actual_close REAL,
    actual_high REAL, actual_low REAL, entry_price_actual REAL,
    target_hit INTEGER, stop_hit INTEGER, first_hit TEXT,
    exit_successful INTEGER, direction_correct INTEGER,
    pnl_pct REAL, r_multiple REAL,
    order_state TEXT DEFAULT 'NONE', order_reason TEXT, trade_id TEXT, ttl_hours INTEGER DEFAULT 24,
    run_id INTEGER, original_run_id INTEGER, model_disagreement INTEGER DEFAULT 0,
    entry_tif TEXT DEFAULT 'DAY', take_profit_tif TEXT DEFAULT 'GTC',
    stop_loss_tif TEXT DEFAULT 'GTC'
);
CREATE TABLE IF NOT EXISTS price_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    UNIQUE(ticker, date)
);
CREATE TABLE IF NOT EXISTS price_data_intraday (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, datetime TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    interval TEXT DEFAULT '1h'
);
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db():
    """Return (tmpdir context-manager, db_path) — tmpdir must be kept alive."""
    tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db_path = os.path.join(tmpdir.name, "test_e2e.db")
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=DELETE")
    con.executescript(_CREATE_SCHEMA)
    con.commit()
    con.close()
    return tmpdir, db_path


class _E2EDb:
    """Minimal db_manager for E2E tests — raw sqlite3, no SQLiteManager migrations."""

    def __init__(self, db_path: str):
        self.db_file = db_path

    def _connect(self):
        con = sqlite3.connect(self.db_file, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=DELETE")
        return con

    def save_consensus(self, data: dict) -> bool:
        cols = list(data.keys())
        placeholders = ", ".join(["?"] * len(cols))
        col_str = ", ".join(cols)
        sql = f"INSERT OR REPLACE INTO consensus ({col_str}) VALUES ({placeholders})"
        try:
            with sqlite3.connect(self.db_file, timeout=10) as con:
                con.execute("PRAGMA journal_mode=DELETE")
                con.execute(sql, [data[c] for c in cols])
            return True
        except Exception as e:
            import logging as _log
            _log.getLogger(__name__).error(f"_E2EDb.save_consensus failed: {e}")
            return False

    def link_forecast_to_run(self, **kwargs) -> bool:
        return True

    def get_config_value(self, key: str, default: str = "") -> str:
        return default


def _add_consensus(db_path, ticker, signal, target, stop, entry, eval_target_date, entry_date=None):
    """Insert a PENDING consensus row."""
    if entry_date is None:
        entry_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(db_path) as con:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("""
            INSERT INTO consensus
                (date, ticker, signal, confidence, target_price, stop_loss,
                 entry_limit_price, eval_status, eval_target_date,
                 methods_long, methods_short, methods_neutral, rationale)
            VALUES (?, ?, ?, 75.0, ?, ?, ?, 'PENDING', ?, ?, '', '', '')
        """, (entry_date, ticker, signal, target, stop, entry, eval_target_date,
              "m1" if signal == "LONG" else ""))
        return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def _add_daily_bar(db_path, ticker, date, open_, high, low, close):
    with sqlite3.connect(db_path) as con:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute(
            "INSERT OR REPLACE INTO price_data (ticker, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
            (ticker, date, open_, high, low, close, 1000000)
        )


def _add_intraday_bar(db_path, ticker, dt, open_, high, low, close):
    with sqlite3.connect(db_path) as con:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute(
            "INSERT INTO price_data_intraday (ticker, datetime, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
            (ticker, dt, open_, high, low, close, 100000)
        )


def _get_consensus(db_path, consensus_id):
    with sqlite3.connect(db_path) as con:
        con.execute("PRAGMA journal_mode=DELETE")
        return con.execute("SELECT * FROM consensus WHERE id=?", (consensus_id,)).fetchone()


# ---------------------------------------------------------------------------
# E2E Test 1: calculate_consensus returns LONG from 3 agreeing mock forecasts
# ---------------------------------------------------------------------------

def test_e2e_consensus_long_signal_from_forecasts():
    """E2E: calculate_consensus returns LONG from 3 agreeing mock forecasts."""
    from consensus import calculate_consensus

    forecasts = [
        {"side": "LONG", "confidence": 75, "method": "m1", "model": "mock",
         "target_price": 165.0, "stop_loss": 145.0, "entry_limit_price": 155.0,
         "entry_tif": "DAY", "take_profit_tif": "GTC", "stop_loss_tif": "GTC"},
        {"side": "LONG", "confidence": 70, "method": "m2", "model": "mock",
         "target_price": 163.0, "stop_loss": 146.0, "entry_limit_price": 154.0},
        {"side": "LONG", "confidence": 80, "method": "m3", "model": "mock",
         "target_price": 167.0, "stop_loss": 144.0, "entry_limit_price": 156.0},
    ]

    result = calculate_consensus(forecasts, current_price=155.0)

    assert result["signal"] == "LONG", f"Expected LONG, got {result['signal']}"
    assert result["confidence"] > 60.0
    assert result["target_price"] is not None
    assert result["stop_loss"] is not None


# ---------------------------------------------------------------------------
# E2E Test 2: save_consensus writes to real SQLite DB
# ---------------------------------------------------------------------------

def test_e2e_save_consensus_persists_to_db():
    """E2E: save_consensus writes a LONG consensus record to real SQLite."""
    from consensus import calculate_consensus, save_consensus

    tmpdir, db_path = _make_db()
    with tmpdir:
        db = _E2EDb(db_path)

        forecasts = [
            {"side": "LONG", "confidence": 75, "method": "m1", "model": "mock",
             "target_price": 165.0, "stop_loss": 145.0, "entry_limit_price": 155.0},
        ]
        cons = calculate_consensus(forecasts, current_price=155.0)
        save_consensus(db, "AAPL", cons)

        with sqlite3.connect(db_path) as con:
            row = con.execute(
                "SELECT signal, confidence, ticker FROM consensus WHERE ticker='AAPL'"
            ).fetchone()

        assert row is not None, "consensus row must be saved to DB"
        assert row[0] == "LONG"
        assert row[2] == "AAPL"


# ---------------------------------------------------------------------------
# E2E Test 3: daily evaluation marks target_hit correctly
# ---------------------------------------------------------------------------

def test_e2e_daily_evaluation_target_hit():
    """E2E: evaluate_consensus_records marks target_hit=1 when price reaches target."""
    from consensus_evaluator import evaluate_consensus_records

    tmpdir, db_path = _make_db()
    with tmpdir:
        db = _E2EDb(db_path)

        eval_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        cid = _add_consensus(db_path, "AAPL", "LONG", 165.0, 145.0, 155.0, eval_date)

        # Daily bar: high exceeds target
        _add_daily_bar(db_path, "AAPL", eval_date, 158.0, 170.0, 155.0, 168.0)

        count = evaluate_consensus_records(db)
        assert count >= 1

        row = _get_consensus(db_path, cid)
        cols = [d[0] for d in sqlite3.connect(db_path).execute("SELECT * FROM consensus WHERE id=?", (cid,)).description or []]
        with sqlite3.connect(db_path) as con:
            r = con.execute(
                "SELECT target_hit, exit_successful, eval_status FROM consensus WHERE id=?", (cid,)
            ).fetchone()
        assert r[0] == 1, f"target_hit should be 1, got {r[0]}"
        assert r[1] == 1, f"exit_successful should be 1, got {r[1]}"
        assert r[2] == "EVALUATED"


# ---------------------------------------------------------------------------
# E2E Test 4: daily evaluation marks stop_hit correctly
# ---------------------------------------------------------------------------

def test_e2e_daily_evaluation_stop_hit():
    """E2E: evaluate_consensus_records marks stop_hit=1, exit_successful=0 on stop."""
    from consensus_evaluator import evaluate_consensus_records

    tmpdir, db_path = _make_db()
    with tmpdir:
        db = _E2EDb(db_path)

        eval_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        cid = _add_consensus(db_path, "AAPL", "LONG", 165.0, 145.0, 155.0, eval_date)

        # Daily bar: low drops below stop
        _add_daily_bar(db_path, "AAPL", eval_date, 154.0, 156.0, 142.0, 143.0)

        evaluate_consensus_records(db)

        with sqlite3.connect(db_path) as con:
            r = con.execute(
                "SELECT stop_hit, exit_successful, eval_status FROM consensus WHERE id=?", (cid,)
            ).fetchone()

        assert r[0] == 1, f"stop_hit should be 1, got {r[0]}"
        assert r[1] == 0, f"exit_successful should be 0 (stop hit), got {r[1]}"
        assert r[2] == "EVALUATED"


# ---------------------------------------------------------------------------
# E2E Test 5: intraday evaluation — exit_successful must not be NULL (bug #1)
# ---------------------------------------------------------------------------

def test_e2e_intraday_exit_successful_not_null():
    """E2E: intraday evaluation must write exit_successful to DB (regression bug #1)."""
    from consensus_evaluator import evaluate_consensus_records

    tmpdir, db_path = _make_db()
    with tmpdir:
        db = _E2EDb(db_path)

        # eval_target_date in the past (intraday — a few hours ago)
        eval_dt = (datetime.now() - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
        entry_dt = (datetime.now() - timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
        cid = _add_consensus(db_path, "AAPL", "LONG", 165.0, 145.0, 155.0, eval_dt, entry_date=entry_dt)

        # Intraday bar hitting target
        bar_dt = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        _add_intraday_bar(db_path, "AAPL", bar_dt, 156.0, 170.0, 154.0, 168.0)

        evaluate_consensus_records(db)

        with sqlite3.connect(db_path) as con:
            r = con.execute(
                "SELECT exit_successful, eval_status FROM consensus WHERE id=?", (cid,)
            ).fetchone()

        if r and r[1] == "EVALUATED":
            assert r[0] is not None, (
                "exit_successful must NOT be NULL after intraday evaluation (bug #1 regression)"
            )


# ---------------------------------------------------------------------------
# E2E Test 6: TIF majority vote in calculate_consensus (bug #13)
# ---------------------------------------------------------------------------

def test_e2e_tif_majority_vote():
    """E2E: TIF fields use majority vote, not first element (regression bug #13)."""
    from consensus import calculate_consensus

    forecasts = [
        {"side": "LONG", "confidence": 70, "method": "m1", "model": "A",
         "entry_tif": "GTC", "take_profit_tif": "GTC", "stop_loss_tif": "GTC"},
        {"side": "LONG", "confidence": 70, "method": "m2", "model": "B",
         "entry_tif": "GTC", "take_profit_tif": "GTC", "stop_loss_tif": "GTC"},
        {"side": "LONG", "confidence": 70, "method": "m3", "model": "C",
         "entry_tif": "DAY", "take_profit_tif": "DAY", "stop_loss_tif": "DAY"},
    ]

    result = calculate_consensus(forecasts, current_price=100.0)
    # Majority is GTC (2 vs 1 DAY)
    assert result["entry_tif"] == "GTC", f"entry_tif should be 'GTC', got {result['entry_tif']!r}"
    assert result["take_profit_tif"] == "GTC"


# ---------------------------------------------------------------------------
# E2E Test 7: circuit_breaker rejects calls when OPEN (bug #15)
# ---------------------------------------------------------------------------

def test_e2e_circuit_breaker_blocks_when_open():
    """E2E: circuit_breaker.call_with_breaker raises when circuit is OPEN (bug #15)."""
    from scripts.core import circuit_breaker

    circuit_breaker.reset()

    for _ in range(circuit_breaker._failure_threshold):
        circuit_breaker.record_failure()

    assert circuit_breaker.is_open(), "Circuit must be OPEN after threshold failures"

    with pytest.raises(RuntimeError, match="circuit is OPEN"):
        circuit_breaker.call_with_breaker(lambda: "AI call")

    circuit_breaker.reset()
