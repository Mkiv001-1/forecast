"""Tests for consensus synchronization from trade data."""
import sys
import os
import sqlite3
import tempfile
import pytest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_db_with_consensus(tmp_path):
    """Create a test DB with orders, trades, and consensus tables."""
    db_file = str(tmp_path / "test.db")
    with sqlite3.connect(db_file) as con:
        con.executescript("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                ib_order_id INTEGER DEFAULT 0,
                ib_parent_id INTEGER DEFAULT 0,
                order_role TEXT DEFAULT '',
                status TEXT DEFAULT 'QUEUED'
            );
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                consensus_id INTEGER DEFAULT NULL,
                ib_parent_id INTEGER DEFAULT 0,
                signal TEXT DEFAULT '',
                quantity REAL DEFAULT 0,
                entry_price REAL DEFAULT NULL,
                stop_loss REAL DEFAULT NULL,
                target_price REAL DEFAULT NULL,
                exit_price REAL DEFAULT NULL,
                close_reason TEXT DEFAULT '',
                realized_pnl REAL DEFAULT NULL,
                status TEXT DEFAULT 'OPEN'
            );
            CREATE TABLE consensus (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                ticker TEXT,
                signal TEXT,
                confidence REAL,
                eval_status TEXT DEFAULT 'PENDING',
                entry_price_actual REAL,
                target_hit INTEGER,
                stop_hit INTEGER,
                pnl_pct REAL,
                r_multiple REAL,
                exit_successful INTEGER,
                realized_pnl REAL,
                updated_at TEXT
            );
        """)
    return db_file


def test_sync_consensus_from_trade_long_target_hit(tmp_path):
    """Sync consensus when LONG trade hits target."""
    db_file = _make_db_with_consensus(tmp_path)
    
    with sqlite3.connect(db_file) as con:
        con.row_factory = sqlite3.Row
        # Create consensus
        con.execute(
            "INSERT INTO consensus (ticker, signal, eval_status) VALUES ('AAPL', 'LONG', 'PENDING')"
        )
        consensus_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        # Create trade linked to consensus
        con.execute(
            """INSERT INTO trades (ticker, consensus_id, signal, quantity, entry_price, 
                                   stop_loss, target_price, exit_price, close_reason, realized_pnl, status)
               VALUES ('AAPL', ?, 'LONG', 10, 150.0, 142.0, 165.0, 165.5, 'TAKE_PROFIT', 150.0, 'CLOSED')""",
            (consensus_id,)
        )
        trade_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        # Test sync
        from order_status_sync import _sync_consensus_from_trade
        
        result = _sync_consensus_from_trade(con, trade_id, "exit_fill")
        assert result == 1
        
        # Verify consensus updated
        consensus = con.execute(
            "SELECT eval_status, entry_price_actual, target_hit, stop_hit, pnl_pct, exit_successful, realized_pnl FROM consensus WHERE id=?",
            (consensus_id,)
        ).fetchone()
        
        assert consensus["eval_status"] == "EVALUATED"
        assert consensus["entry_price_actual"] == 150.0
        assert consensus["target_hit"] == 1
        assert consensus["stop_hit"] == 0
        assert consensus["pnl_pct"] == pytest.approx(10.33, abs=0.1)  # (165.5 - 150) / 150 * 100
        assert consensus["exit_successful"] == 1
        assert consensus["realized_pnl"] == 150.0


def test_sync_consensus_from_trade_long_stop_hit(tmp_path):
    """Sync consensus when LONG trade hits stop loss."""
    db_file = _make_db_with_consensus(tmp_path)
    
    with sqlite3.connect(db_file) as con:
        con.row_factory = sqlite3.Row
        # Create consensus
        con.execute(
            "INSERT INTO consensus (ticker, signal, eval_status) VALUES ('AAPL', 'LONG', 'PENDING')"
        )
        consensus_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        # Create trade linked to consensus - stopped out
        con.execute(
            """INSERT INTO trades (ticker, consensus_id, signal, quantity, entry_price, 
                                   stop_loss, target_price, exit_price, close_reason, realized_pnl, status)
               VALUES ('AAPL', ?, 'LONG', 10, 150.0, 142.0, 165.0, 141.5, 'STOP_LOSS', -85.0, 'CLOSED')""",
            (consensus_id,)
        )
        trade_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        # Test sync
        from order_status_sync import _sync_consensus_from_trade
        
        result = _sync_consensus_from_trade(con, trade_id, "exit_fill")
        assert result == 1
        
        # Verify consensus updated
        consensus = con.execute(
            "SELECT eval_status, entry_price_actual, target_hit, stop_hit, pnl_pct, exit_successful, realized_pnl FROM consensus WHERE id=?",
            (consensus_id,)
        ).fetchone()
        
        assert consensus["eval_status"] == "EVALUATED"
        assert consensus["entry_price_actual"] == 150.0
        assert consensus["target_hit"] == 0
        assert consensus["stop_hit"] == 1
        assert consensus["pnl_pct"] == pytest.approx(-5.67, abs=0.1)  # (141.5 - 150) / 150 * 100
        assert consensus["exit_successful"] == 0
        assert consensus["realized_pnl"] == -85.0


def test_sync_consensus_from_trade_short_target_hit(tmp_path):
    """Sync consensus when SHORT trade hits target."""
    db_file = _make_db_with_consensus(tmp_path)
    
    with sqlite3.connect(db_file) as con:
        con.row_factory = sqlite3.Row
        # Create consensus
        con.execute(
            "INSERT INTO consensus (ticker, signal, eval_status) VALUES ('AAPL', 'SHORT', 'PENDING')"
        )
        consensus_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        # Create trade linked to consensus - SHORT with target hit
        con.execute(
            """INSERT INTO trades (ticker, consensus_id, signal, quantity, entry_price, 
                                   stop_loss, target_price, exit_price, close_reason, realized_pnl, status)
               VALUES ('AAPL', ?, 'SHORT', 10, 150.0, 158.0, 140.0, 139.5, 'TAKE_PROFIT', 105.0, 'CLOSED')""",
            (consensus_id,)
        )
        trade_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        # Test sync
        from order_status_sync import _sync_consensus_from_trade
        
        result = _sync_consensus_from_trade(con, trade_id, "exit_fill")
        assert result == 1
        
        # Verify consensus updated
        consensus = con.execute(
            "SELECT eval_status, entry_price_actual, target_hit, stop_hit, pnl_pct, exit_successful, realized_pnl FROM consensus WHERE id=?",
            (consensus_id,)
        ).fetchone()
        
        assert consensus["eval_status"] == "EVALUATED"
        assert consensus["entry_price_actual"] == 150.0
        assert consensus["target_hit"] == 1
        assert consensus["stop_hit"] == 0
        assert consensus["pnl_pct"] == pytest.approx(7.0, abs=0.1)  # (150 - 139.5) / 150 * 100
        assert consensus["exit_successful"] == 1
        assert consensus["realized_pnl"] == 105.0


def test_sync_consensus_from_trade_r_multiple_calculation(tmp_path):
    """Verify R-multiple calculation in consensus sync."""
    db_file = _make_db_with_consensus(tmp_path)
    
    with sqlite3.connect(db_file) as con:
        con.row_factory = sqlite3.Row
        # Create consensus
        con.execute(
            "INSERT INTO consensus (ticker, signal, eval_status) VALUES ('AAPL', 'LONG', 'PENDING')"
        )
        consensus_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        # Create trade: entry=100, stop=95 (risk=5%), exit=110 (profit=10%)
        # R-multiple = 10% / 5% = 2.0
        con.execute(
            """INSERT INTO trades (ticker, consensus_id, signal, quantity, entry_price, 
                                   stop_loss, target_price, exit_price, close_reason, realized_pnl, status)
               VALUES ('AAPL', ?, 'LONG', 10, 100.0, 95.0, 115.0, 110.0, 'TAKE_PROFIT', 100.0, 'CLOSED')""",
            (consensus_id,)
        )
        trade_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        # Test sync
        from order_status_sync import _sync_consensus_from_trade
        
        result = _sync_consensus_from_trade(con, trade_id, "exit_fill")
        assert result == 1
        
        # Verify r_multiple
        consensus = con.execute(
            "SELECT pnl_pct, r_multiple FROM consensus WHERE id=?",
            (consensus_id,)
        ).fetchone()
        
        assert consensus["pnl_pct"] == pytest.approx(10.0, abs=0.01)
        assert consensus["r_multiple"] == pytest.approx(2.0, abs=0.01)


def test_sync_consensus_from_trade_no_consensus_link(tmp_path):
    """Return 0 when trade has no consensus_id."""
    db_file = _make_db_with_consensus(tmp_path)
    
    with sqlite3.connect(db_file) as con:
        con.row_factory = sqlite3.Row
        
        # Create trade with NO consensus_id
        con.execute(
            """INSERT INTO trades (ticker, consensus_id, signal, quantity, entry_price, 
                                   stop_loss, target_price, exit_price, close_reason, realized_pnl, status)
               VALUES ('AAPL', NULL, 'LONG', 10, 150.0, 142.0, 165.0, 165.5, 'TAKE_PROFIT', 150.0, 'CLOSED')"""
        )
        trade_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        # Test sync
        from order_status_sync import _sync_consensus_from_trade
        
        result = _sync_consensus_from_trade(con, trade_id, "exit_fill")
        assert result == 0  # Should return 0 as no consensus to update


def test_sync_consensus_from_trade_trade_not_found(tmp_path):
    """Return 0 when trade_id doesn't exist."""
    db_file = _make_db_with_consensus(tmp_path)
    
    with sqlite3.connect(db_file) as con:
        con.row_factory = sqlite3.Row
        
        # Test sync with non-existent trade
        from order_status_sync import _sync_consensus_from_trade
        
        result = _sync_consensus_from_trade(con, 9999, "exit_fill")
        assert result == 0  # Should return 0 as trade doesn't exist


def test_sync_consensus_from_trade_entry_fill(tmp_path):
    """Sync consensus when entry is filled (only entry_price_actual)."""
    db_file = _make_db_with_consensus(tmp_path)
    
    with sqlite3.connect(db_file) as con:
        con.row_factory = sqlite3.Row
        # Create consensus
        con.execute(
            "INSERT INTO consensus (ticker, signal, eval_status) VALUES ('AAPL', 'LONG', 'PENDING')"
        )
        consensus_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        # Create trade with entry filled but no exit yet
        con.execute(
            """INSERT INTO trades (ticker, consensus_id, signal, quantity, entry_price, 
                                   stop_loss, target_price, exit_price, close_reason, realized_pnl, status)
               VALUES ('AAPL', ?, 'LONG', 10, 150.0, 142.0, 165.0, NULL, '', NULL, 'OPEN')""",
            (consensus_id,)
        )
        trade_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        # Test sync at entry fill stage
        from order_status_sync import _sync_consensus_from_trade
        
        result = _sync_consensus_from_trade(con, trade_id, "entry_fill")
        assert result == 1
        
        # Verify consensus updated with entry data
        consensus = con.execute(
            "SELECT eval_status, entry_price_actual FROM consensus WHERE id=?",
            (consensus_id,)
        ).fetchone()
        
        assert consensus["eval_status"] == "EVALUATED"
        assert consensus["entry_price_actual"] == 150.0
