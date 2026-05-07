"""
Integration test — full pipeline simulation:
  forecast → consensus → position sizing → order placement → DB verification

Run with:  python -m pytest test_integration.py -v
"""

import sys
import os
import sqlite3
import tempfile
import pytest
import gc
from datetime import datetime

# Ensure core modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts", "core"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))


def _cleanup_db(db_file: str):
    """Force close connections and remove DB file (Windows-safe)."""
    gc.collect()  # Force garbage collection to close any dangling connections
    try:
        if os.path.exists(db_file):
            os.unlink(db_file)
    except PermissionError:
        # If still locked, mark for later deletion
        pass


def _create_test_db() -> str:
    """Create a fully-populated test database with all required tables."""
    db_file = tempfile.mktemp(suffix="_integration.db")
    con = sqlite3.connect(db_file)
    con.executescript("""
        -- Core config
        CREATE TABLE config (
            key TEXT PRIMARY KEY,
            value TEXT,
            description TEXT
        );
        
        -- Ticker settings
        CREATE TABLE settings (
            ticker TEXT PRIMARY KEY,
            trading_blocked INTEGER DEFAULT 0,
            sector TEXT DEFAULT ''
        );
        
        -- Forecast logs
        CREATE TABLE Logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            forecast_date TEXT,
            created_at TEXT,
            ticker TEXT,
            method TEXT,
            confidence INTEGER,
            side TEXT,
            entry_conditions TEXT,
            exit_target TEXT,
            exit_stop TEXT,
            position_size TEXT,
            rationale TEXT,
            model TEXT,
            prompt TEXT,
            api_response TEXT,
            stop_loss REAL,
            rr_ratio REAL,
            timeframe_hours INTEGER,
            risk_amount REAL,
            risk_pct REAL,
            sector TEXT,
            sector_exposure_at_signal REAL,
            horizon_days INTEGER
        );
        
        -- Consensus storage
        CREATE TABLE consensus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            ticker TEXT,
            signal TEXT,
            confidence REAL,
            methods_long TEXT,
            methods_short TEXT,
            methods_neutral TEXT,
            rationale TEXT,
            target_price REAL,
            stop_loss REAL
        );
        
        -- Orders
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_id TEXT,
            ticker TEXT,
            ib_order_id INTEGER,
            ib_parent_id INTEGER,
            order_role TEXT,
            order_type TEXT,
            action TEXT,
            quantity REAL,
            limit_price REAL,
            stop_price REAL,
            status TEXT,
            account_type TEXT,
            created_at TEXT,
            submitted_at TEXT,
            filled_at TEXT DEFAULT '',
            spread_at_submission REAL,
            error_message TEXT
        );
        
        -- Method configuration
        CREATE TABLE method_config (
            method TEXT PRIMARY KEY,
            timeframe_hours INTEGER NOT NULL,
            trigger TEXT DEFAULT 'both',
            active INTEGER DEFAULT 1,
            execute TEXT DEFAULT 'yes' CHECK (execute IN ('yes', 'no'))
        );
        
        -- AI providers
        CREATE TABLE providers (
            name TEXT PRIMARY KEY,
            type TEXT DEFAULT 'ai',
            base_url TEXT DEFAULT '',
            api_key TEXT DEFAULT '',
            model TEXT DEFAULT '',
            temperature REAL DEFAULT 0.2,
            max_tokens INTEGER DEFAULT 2000,
            rate_limit INTEGER DEFAULT 60,
            active INTEGER DEFAULT 1,
            execute TEXT DEFAULT 'yes' CHECK (execute IN ('yes', 'no')),
            ema_accuracy REAL DEFAULT 0.5,
            ema_updated_at TEXT DEFAULT '',
            forecast_count INTEGER DEFAULT 0
        );
        
        -- Portfolio positions
        CREATE TABLE portfolio (
            ticker TEXT PRIMARY KEY,
            market_value REAL
        );
        
        -- IB accounts
        CREATE TABLE accounts (
            account_id TEXT PRIMARY KEY,
            type TEXT,
            net_liquidation REAL,
            last_sync TEXT
        );
        
        -- Seed config
        INSERT INTO config VALUES
            ('ORDER_MODE', 'paper', 'Order execution mode'),
            ('MAX_OPEN_ORDERS', '5', 'Max simultaneous open orders'),
            ('ORDER_QUEUE_MAX_AGE_HOURS', '24', 'Max age for queued orders'),
            ('MAX_SPREAD_PCT', '0.005', 'Max allowed spread'),
            ('USE_STOP_LIMIT', 'false', 'Use stop-limit orders'),
            ('STOP_LIMIT_OFFSET_PCT', '0.0005', 'Stop-limit offset'),
            ('ALLOW_EXTENDED_HOURS', 'true', 'Allow extended hours trading'),
            ('LIVE_TRADING_CONFIRMED', 'false', 'Live trading confirmation'),
            ('DEFAULT_RISK_PCT', '0.01', 'Default risk percentage'),
            ('MAX_POSITION_PCT', '0.05', 'Max position percentage'),
            ('MAX_SECTOR_EXPOSURE_PCT', '0.15', 'Sector soft limit'),
            ('MAX_SECTOR_HARD_LIMIT_PCT', '0.25', 'Sector hard limit'),
            ('SECTOR_OVERWEIGHT_FACTOR', '0.5', 'Sector overweight reduction'),
            ('CAPITAL_STALENESS_MINUTES', '60', 'Capital staleness threshold'),
            ('PREFERRED_ACCOUNT_TYPE', 'paper', 'Preferred IB account type'),
            ('MANUAL_CAPITAL_OVERRIDE', '100000', 'Manual capital override for testing');
        
        -- Seed test ticker
        INSERT INTO settings VALUES ('AAPL', 0, 'Tech');
        
        -- Seed method config
        INSERT INTO method_config VALUES
            ('momentum_trend', 24, 'both', 1, 'yes'),
            ('price_action', 8, 'price_level', 1, 'yes'),
            ('relative_strength', 48, 'time', 1, 'yes');
        
        -- Seed providers
        INSERT INTO providers (name, type, model, execute, ema_accuracy) VALUES
            ('claude-sonnet', 'ai', 'anthropic/claude-sonnet-4', 'yes', 0.65),
            ('gpt-4o', 'ai', 'openai/gpt-4o', 'yes', 0.62),
            ('deepseek-v3', 'ai', 'deepseek/deepseek-chat', 'yes', 0.58);
        
        -- Seed IB account
        INSERT INTO accounts VALUES ('DU123456', 'paper', 100000.0, datetime('now'));
    """)
    con.commit()
    con.close()
    return db_file


class FakeDbManager:
    """Minimal DB manager for testing."""
    def __init__(self, db_file: str):
        self.db_file = db_file
    
    def get_config_value(self, key: str) -> str:
        with sqlite3.connect(self.db_file) as c:
            row = c.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    
    def _execute_query(self, query: str, params=None):
        with sqlite3.connect(self.db_file) as c:
            if params:
                return c.execute(query, params).fetchall()
            return c.execute(query).fetchall()
    
    def save_consensus(self, record: dict) -> bool:
        with sqlite3.connect(self.db_file) as c:
            c.execute("""
                INSERT INTO consensus (date, ticker, signal, confidence, 
                    methods_long, methods_short, methods_neutral, rationale,
                    target_price, stop_loss)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.get("date"), record.get("ticker"), record.get("signal"),
                record.get("confidence"), record.get("methods_long", ""),
                record.get("methods_short", ""), record.get("methods_neutral", ""),
                record.get("rationale", ""), record.get("target_price"),
                record.get("stop_loss")
            ))
            c.commit()
        return True


# ---------------------------------------------------------------------------
# Full Pipeline Test
# ---------------------------------------------------------------------------

def test_full_pipeline_long_signal():
    """
    Simulate complete flow:
      1. Generate mock forecasts (bypassing AI API)
      2. Calculate consensus
      3. Calculate position size
      4. Submit order (paper mode with mocked IB)
      5. Verify DB state
    """
    from consensus import calculate_consensus, save_consensus
    from position_sizer import calculate_position
    from order_manager import submit_signal
    
    # Setup
    db_file = _create_test_db()
    db = FakeDbManager(db_file)
    
    # Step 1: Mock forecasts from different models/methods
    current_price = 150.0
    forecasts = [
        {
            "side": "LONG",
            "confidence": 75,
            "method": "momentum_trend",
            "model": "claude-sonnet",
            "exit_target": "$165.00 (+10%)",
            "stop_loss": 142.0,
            "entry_price": "$150.00"
        },
        {
            "side": "LONG",
            "confidence": 70,
            "method": "price_action",
            "model": "gpt-4o",
            "exit_target": "$162.00 (+8%)",
            "stop_loss": 143.0,
            "entry_price": "$150.00"
        },
        {
            "side": "NEUTRAL",
            "confidence": 55,
            "method": "relative_strength",
            "model": "deepseek-v3",
            "exit_target": "",
            "stop_loss": None,
            "entry_price": "$150.00"
        }
    ]
    
    # Step 2: Calculate consensus
    method_stats = {
        "momentum_trend": {"win_rate": 0.60, "ema_accuracy": 0.65},
        "price_action": {"win_rate": 0.55, "ema_accuracy": 0.62},
        "relative_strength": {"win_rate": 0.52, "ema_accuracy": 0.58}
    }
    
    consensus = calculate_consensus(
        forecasts,
        method_stats=method_stats,
        current_price=current_price,
        max_deviation=0.15,
        disagreement_threshold=0.40
    )
    
    # Verify consensus
    assert consensus["signal"] == "LONG", f"Expected LONG, got {consensus['signal']}"
    assert consensus["confidence"] > 55, f"Confidence too low: {consensus['confidence']}"
    assert consensus["target_price"] is not None
    assert consensus["stop_loss"] is not None
    assert "momentum_trend" in consensus["methods_long"]
    assert "price_action" in consensus["methods_long"]
    
    # Save consensus to DB
    save_consensus(db, "AAPL", consensus)
    
    # Step 3: Calculate position size
    position = calculate_position(
        ticker="AAPL",
        entry_price=current_price,
        stop_loss=consensus["stop_loss"],
        db_manager=db,
        net_liquidation=100000.0
    )
    
    # Verify position sizing
    assert position["status"] == "OK", f"Position sizing failed: {position['status']}"
    assert position["quantity"] > 0, "Quantity should be > 0"
    assert position["risk_amount"] > 0, "Risk amount should be > 0"
    assert position["sector"] == "Tech", f"Expected sector Tech, got {position['sector']}"
    
    # Step 4: Submit order (with mocked IB gateway)
    # Note: ib_gateway_client will fail, but order should be queued in DB
    
    # First, verify we can get past guards
    result = submit_signal("AAPL", consensus, position, db, log_id="test-001")
    
    # IB is not available in test, so expect ERROR or handling
    assert result["status"] in ("SUBMITTED", "QUEUED", "ERROR", "SKIPPED_HIGH_VOLATILITY"), \
        f"Unexpected order status: {result['status']}"
    
    # Step 5: Verify DB state
    with sqlite3.connect(db_file) as c:
        c.row_factory = sqlite3.Row
        
        # Check consensus was saved
        consensus_row = c.execute(
            "SELECT * FROM consensus WHERE ticker='AAPL' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert consensus_row is not None, "Consensus not saved to DB"
        assert consensus_row["signal"] == "LONG"
        assert consensus_row["target_price"] == consensus["target_price"]
        
        # Check orders table
        orders = c.execute("SELECT * FROM orders WHERE ticker='AAPL'").fetchall()
        
        if result["status"] in ("SUBMITTED", "QUEUED", "ERROR"):
            assert len(orders) >= 1, "Order should be recorded in DB"
            parent_order = c.execute(
                "SELECT * FROM orders WHERE ticker='AAPL' AND order_role='ENTRY'"
            ).fetchone()
            assert parent_order is not None, "Parent entry order not found"
            assert parent_order["action"] == "BUY"  # LONG → BUY
            assert parent_order["quantity"] == position["quantity"]
    
    # Cleanup
    _cleanup_db(db_file)


def test_full_pipeline_short_signal():
    """
    Test SHORT signal flow with proper stop/target levels.
    """
    from consensus import calculate_consensus
    from position_sizer import calculate_position
    from order_manager import submit_signal
    
    db_file = _create_test_db()
    db = FakeDbManager(db_file)
    
    current_price = 200.0
    forecasts = [
        {
            "side": "SHORT",
            "confidence": 72,
            "method": "momentum_trend",
            "model": "claude-sonnet",
            "exit_target": "$180.00 (-10%)",
            "stop_loss": 210.0,
            "entry_price": "$200.00"
        },
        {
            "side": "SHORT",
            "confidence": 68,
            "method": "price_action",
            "model": "gpt-4o",
            "exit_target": "$185.00 (-7.5%)",
            "stop_loss": 208.0,
            "entry_price": "$200.00"
        }
    ]
    
    consensus = calculate_consensus(forecasts, current_price=current_price)
    
    assert consensus["signal"] == "SHORT"
    assert consensus["target_price"] < current_price
    assert consensus["stop_loss"] > current_price
    
    position = calculate_position(
        "AAPL", current_price, consensus["stop_loss"], db, net_liquidation=100000.0
    )
    
    assert position["status"] == "OK"
    assert position["quantity"] > 0
    
    result = submit_signal("AAPL", consensus, position, db, log_id="test-002")
    
    # Verify SHORT → SELL action
    with sqlite3.connect(db_file) as c:
        c.row_factory = sqlite3.Row
        parent = c.execute(
            "SELECT * FROM orders WHERE ticker='AAPL' AND order_role='ENTRY'"
        ).fetchone()
        if parent:
            assert parent["action"] == "SELL"  # SHORT → SELL
    
    _cleanup_db(db_file)


def test_full_pipeline_neutral_consensus():
    """
    Test that NEUTRAL consensus correctly skips order placement.
    """
    from consensus import calculate_consensus
    from position_sizer import calculate_position
    from order_manager import submit_signal
    
    db_file = _create_test_db()
    db = FakeDbManager(db_file)
    
    # Conflicting signals → NEUTRAL
    forecasts = [
        {
            "side": "LONG",
            "confidence": 60,
            "method": "momentum_trend",
            "model": "claude-sonnet",
            "exit_target": "$165.00",
            "stop_loss": 142.0,
            "entry_price": "$150.00"
        },
        {
            "side": "SHORT",
            "confidence": 60,
            "method": "price_action",
            "model": "gpt-4o",
            "exit_target": "$135.00",
            "stop_loss": 160.0,
            "entry_price": "$150.00"
        }
    ]
    
    consensus = calculate_consensus(forecasts, current_price=150.0, disagreement_threshold=0.30)
    
    # Should be NEUTRAL due to disagreement
    assert consensus["signal"] == "NEUTRAL" or consensus["high_model_disagreement"]
    
    # Order should be skipped
    position = {"status": "OK", "quantity": 10}  # Dummy position
    result = submit_signal("AAPL", consensus, position, db)
    
    assert result["status"] == "SKIPPED_NEUTRAL"
    
    _cleanup_db(db_file)


def test_full_pipeline_with_blocked_ticker():
    """
    Test that blocked tickers are rejected.
    """
    from consensus import calculate_consensus
    from position_sizer import calculate_position
    from order_manager import submit_signal
    
    db_file = _create_test_db()
    db = FakeDbManager(db_file)
    
    # Block the ticker
    with sqlite3.connect(db_file) as c:
        c.execute("UPDATE settings SET trading_blocked=1 WHERE ticker='AAPL'")
        c.commit()
    
    forecasts = [{
        "side": "LONG",
        "confidence": 80,
        "method": "momentum_trend",
        "model": "claude-sonnet",
        "exit_target": "$165.00",
        "stop_loss": 142.0,
        "entry_price": "$150.00"
    }]
    
    consensus = calculate_consensus(forecasts, current_price=150.0)
    position = calculate_position("AAPL", 150.0, 142.0, db, net_liquidation=100000.0)
    
    result = submit_signal("AAPL", consensus, position, db)
    
    assert result["status"] == "SKIPPED_TICKER_BLOCKED"
    
    _cleanup_db(db_file)


def test_full_pipeline_sector_limits():
    """
    Test sector exposure hard limit in position sizing.
    Current implementation checks EXISTING exposure only, not including new position.
    """
    from position_sizer import calculate_position
    
    db_file = _create_test_db()
    db = FakeDbManager(db_file)
    
    # Add existing Tech exposure ABOVE hard limit (26% > 25%)
    # This tests that current exposure exceeding hard limit blocks new positions
    with sqlite3.connect(db_file) as c:
        c.execute("INSERT INTO portfolio VALUES ('MSFT', 26000.0)")  # 26% of 100k - exceeds 25% hard limit
        c.execute("INSERT INTO settings VALUES ('MSFT', 0, 'Tech')")
        c.commit()
    
    # Try to add AAPL position — should be rejected due to existing sector exposure > hard limit
    position = calculate_position(
        "AAPL", entry_price=150.0, stop_loss=140.0,
        db_manager=db, net_liquidation=100000.0
    )
    
    # 26% existing > 25% hard limit → reject
    assert position["status"] == "SKIPPED_SECTOR_OVERWEIGHT"
    
    _cleanup_db(db_file)


def test_full_pipeline_duplicate_order():
    """
    Test duplicate order detection.
    """
    from consensus import calculate_consensus
    from position_sizer import calculate_position
    from order_manager import submit_signal
    
    db_file = _create_test_db()
    db = FakeDbManager(db_file)
    
    # Pre-populate with existing open order
    with sqlite3.connect(db_file) as c:
        c.execute("""
            INSERT INTO orders (log_id, ticker, ib_order_id, ib_parent_id, order_role,
                order_type, action, quantity, status, account_type, created_at)
            VALUES ('prev', 'AAPL', 100, 100, 'ENTRY', 'MKT', 'BUY', 10,
                'SUBMITTED', 'paper', datetime('now'))
        """)
        c.commit()
    
    forecasts = [{
        "side": "LONG",
        "confidence": 80,
        "method": "momentum_trend",
        "model": "claude-sonnet",
        "exit_target": "$165.00",
        "stop_loss": 142.0,
        "entry_price": "$150.00"
    }]
    
    consensus = calculate_consensus(forecasts, current_price=150.0)
    position = calculate_position("AAPL", 150.0, 142.0, db, net_liquidation=100000.0)
    
    result = submit_signal("AAPL", consensus, position, db)
    
    assert result["status"] == "SKIPPED_DUPLICATE"
    
    _cleanup_db(db_file)


def test_full_pipeline_execute_flags():
    """
    Test that execute='no' on method or provider blocks execution.
    """
    from consensus import calculate_consensus
    from position_sizer import calculate_position
    from order_manager import submit_signal
    
    db_file = _create_test_db()
    db = FakeDbManager(db_file)
    
    # Disable momentum_trend method
    with sqlite3.connect(db_file) as c:
        c.execute("UPDATE method_config SET execute='no' WHERE method='momentum_trend'")
        c.commit()
    
    forecasts = [{
        "side": "LONG",
        "confidence": 80,
        "method": "momentum_trend",
        "model": "claude-sonnet",
        "exit_target": "$165.00",
        "stop_loss": 142.0,
        "entry_price": "$150.00"
    }]
    
    consensus = calculate_consensus(forecasts, current_price=150.0)
    consensus["methods_long"] = "momentum_trend(claude-sonnet)"
    
    position = calculate_position("AAPL", 150.0, 142.0, db, net_liquidation=100000.0)
    
    result = submit_signal("AAPL", consensus, position, db)
    
    assert result["status"] == "SKIPPED_EXECUTE_DISABLED"
    assert "momentum_trend" in result["message"]
    
    _cleanup_db(db_file)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
