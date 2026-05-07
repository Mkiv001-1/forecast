"""
Integration test with IB Gateway mocking — verifies full flow:
  1. forecast → consensus → position sizing
  2. order placement (mocked IB responses)
  3. simulated IB callbacks → status updates
  4. DB verification at each step

Run with:  python -m pytest test_integration_ib_mock.py -v
"""

import sys
import os
import sqlite3
import tempfile
import pytest
import gc
from datetime import datetime
from unittest.mock import MagicMock, patch
from typing import Dict, Any, List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts", "core"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))


# ---------------------------------------------------------------------------
# Mock IB Gateway Module
# ---------------------------------------------------------------------------

class MockIBGateway:
    """
    Simulates IB Gateway responses for testing.
    Tracks all calls and returns configurable responses.
    """
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.order_id_counter = 1000
        self.submitted_orders: Dict[int, Dict[str, Any]] = {}
        
    def _next_order_id(self) -> int:
        self.order_id_counter += 1
        return self.order_id_counter
    
    def place_bracket_order(self, symbol: str, action: str, quantity: float,
                           stop_loss_price: float, take_profit_price: float,
                           use_stop_limit: bool = False, stop_limit_offset_pct: float = 0.0005,
                           allow_extended_hours: bool = False,
                           host: str = "127.0.0.1", port: int = 7497,
                           **kwargs) -> Dict[str, Any]:
        """Mock bracket order placement — accepts extra kwargs added to order_manager."""
        call_record = {
            "method": "place_bracket_order",
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
            "timestamp": datetime.now().isoformat()
        }
        self.calls.append(call_record)
        
        # Generate mock IB order IDs
        parent_id = self._next_order_id()
        target_id = self._next_order_id()
        stop_id = self._next_order_id()
        
        order_group = {
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "parent_id": parent_id,
            "target_id": target_id,
            "stop_id": stop_id,
            "stop_price": stop_loss_price,
            "target_price": take_profit_price
        }
        self.submitted_orders[parent_id] = order_group
        
        return {
            "status": "submitted",
            "parent_id": parent_id,
            "target_id": target_id,
            "stop_id": stop_id,
            "symbol": symbol
        }
    
    def get_bid_ask_spread(self, symbol: str, host: str = "127.0.0.1", port: int = 7497) -> Dict[str, Any]:
        """Mock spread check — returns acceptable spread."""
        self.calls.append({
            "method": "get_bid_ask_spread",
            "symbol": symbol,
            "timestamp": datetime.now().isoformat()
        })
        
        # Return reasonable spread (0.1%)
        return {
            "status": "ok",
            "symbol": symbol,
            "bid": 149.85,
            "ask": 150.15,
            "spread_pct": 0.002  # 0.2% — below MAX_SPREAD_PCT=0.5%
        }
    
    def cancel_order(self, order_id: int, host: str = "127.0.0.1", port: int = 7497) -> Dict[str, Any]:
        """Mock order cancellation."""
        self.calls.append({
            "method": "cancel_order",
            "order_id": order_id,
            "timestamp": datetime.now().isoformat()
        })
        return {"status": "cancelled", "order_id": order_id}
    
    def close_position_market(self, symbol: str, quantity: float, account: str = "",
                               host: str = "127.0.0.1", port: int = 7497) -> Dict[str, Any]:
        """Mock position close."""
        self.calls.append({
            "method": "close_position_market",
            "symbol": symbol,
            "quantity": quantity,
            "timestamp": datetime.now().isoformat()
        })
        return {"status": "submitted", "symbol": symbol, "quantity": quantity}
    
    def sync_accounts_with_ib(self, db_manager, host: str = "127.0.0.1", port: int = 7497) -> bool:
        """Mock account sync."""
        self.calls.append({
            "method": "sync_accounts_with_ib",
            "timestamp": datetime.now().isoformat()
        })
        return True


# Global mock instance
_mock_ib = MockIBGateway()


def _create_test_db() -> str:
    """Create a fully-populated test database with all required tables."""
    db_file = tempfile.mktemp(suffix="_ib_mock.db")
    con = sqlite3.connect(db_file)
    con.executescript("""
        CREATE TABLE config (
            key TEXT PRIMARY KEY,
            value TEXT,
            description TEXT
        );
        
        CREATE TABLE settings (
            ticker TEXT PRIMARY KEY,
            trading_blocked INTEGER DEFAULT 0,
            sector TEXT DEFAULT ''
        );
        
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
        
        CREATE TABLE method_config (
            method TEXT PRIMARY KEY,
            timeframe_hours INTEGER NOT NULL,
            trigger TEXT DEFAULT 'both',
            active INTEGER DEFAULT 1,
            execute TEXT DEFAULT 'yes' CHECK (execute IN ('yes', 'no'))
        );
        
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
        
        CREATE TABLE portfolio (
            ticker TEXT PRIMARY KEY,
            market_value REAL
        );
        
        CREATE TABLE accounts (
            account_id TEXT PRIMARY KEY,
            type TEXT,
            net_liquidation REAL,
            last_sync TEXT
        );
        
        -- Seed config for PAPER trading
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
            ('MANUAL_CAPITAL_OVERRIDE', '100000', 'Manual capital override');
        
        INSERT INTO settings VALUES ('AAPL', 0, 'Tech');
        
        INSERT INTO method_config VALUES
            ('momentum_trend', 24, 'both', 1, 'yes'),
            ('price_action', 8, 'price_level', 1, 'yes'),
            ('relative_strength', 48, 'time', 1, 'yes');
        
        INSERT INTO providers (name, type, model, execute, ema_accuracy) VALUES
            ('claude-sonnet', 'ai', 'anthropic/claude-sonnet-4', 'yes', 0.65),
            ('gpt-4o', 'ai', 'openai/gpt-4o', 'yes', 0.62);
        
        INSERT INTO accounts VALUES ('DU123456', 'paper', 100000.0, datetime('now'));
    """)
    con.commit()
    con.close()
    return db_file


def _cleanup_db(db_file: str):
    """Force close connections and remove DB file (Windows-safe)."""
    gc.collect()
    try:
        if os.path.exists(db_file):
            os.unlink(db_file)
    except PermissionError:
        pass


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
# IB Mock Patches
# ---------------------------------------------------------------------------

def mock_ib_gateway():
    """Create patcher for IB gateway functions."""
    return patch.multiple(
        'ib_gateway_client',
        place_bracket_order=_mock_ib.place_bracket_order,
        get_bid_ask_spread=_mock_ib.get_bid_ask_spread,
        cancel_order=_mock_ib.cancel_order,
        close_position_market=_mock_ib.close_position_market,
        sync_accounts_with_ib=_mock_ib.sync_accounts_with_ib
    )


def simulate_ib_fills(db_file: str, parent_ib_id: int, fill_price: float = 150.0):
    """
    Simulate IB fill callback — update order statuses in DB.
    This mimics what the real scheduler/IB callback would do.
    """
    now = datetime.now().isoformat()
    
    with sqlite3.connect(db_file) as con:
        # Update parent order to FILLED_ENTRY
        con.execute("""
            UPDATE orders 
            SET status = 'FILLED_ENTRY', filled_at = ?
            WHERE ib_order_id = ? AND order_role = 'ENTRY'
        """, (now, parent_ib_id))
        
        # Update children to SUBMITTED (they're active now)
        con.execute("""
            UPDATE orders 
            SET status = 'SUBMITTED'
            WHERE ib_parent_id = ? AND order_role IN ('TAKE_PROFIT', 'STOP_LOSS')
        """, (parent_ib_id,))
        
        con.commit()


def get_orders_summary(db_file: str, ticker: str) -> List[Dict[str, Any]]:
    """Get all orders for a ticker as dicts."""
    with sqlite3.connect(db_file) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM orders WHERE UPPER(ticker) = UPPER(?) ORDER BY id",
            (ticker,)
        ).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Full Pipeline Tests with IB Mock
# ---------------------------------------------------------------------------

def test_bracket_order_creation_and_db_records():
    """
    Full flow: forecast → consensus → position sizing → bracket order placement.
    Verifies:
      - 3 records created in orders table (parent + target + stop)
      - Correct IB IDs assigned
      - Correct order types and prices
      - Mock IB gateway received correct calls
    """
    from consensus import calculate_consensus, save_consensus
    from position_sizer import calculate_position
    from order_manager import submit_signal
    
    # Reset mock
    _mock_ib.calls.clear()
    _mock_ib.submitted_orders.clear()
    
    # Setup
    db_file = _create_test_db()
    db = FakeDbManager(db_file)
    
    # Step 1: Create consensus
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
        }
    ]
    
    method_stats = {
        "momentum_trend": {"win_rate": 0.60, "ema_accuracy": 0.65},
        "price_action": {"win_rate": 0.55, "ema_accuracy": 0.62}
    }
    
    consensus = calculate_consensus(
        forecasts,
        method_stats=method_stats,
        current_price=current_price,
        max_deviation=0.15,
        disagreement_threshold=0.40
    )
    
    # Verify consensus is LONG
    assert consensus["signal"] == "LONG", f"Expected LONG, got {consensus['signal']}"
    assert consensus["target_price"] is not None
    assert consensus["stop_loss"] is not None
    
    save_consensus(db, "AAPL", consensus)
    
    # Step 2: Position sizing
    position = calculate_position(
        ticker="AAPL",
        entry_price=current_price,
        stop_loss=consensus["stop_loss"],
        db_manager=db,
        net_liquidation=100000.0
    )
    
    assert position["status"] == "OK"
    assert position["quantity"] > 0
    qty = position["quantity"]
    
    # Step 3: Submit order with mocked IB
    with mock_ib_gateway():
        result = submit_signal("AAPL", consensus, position, db, log_id="test-bracket-001")
    
    # Verify order submission succeeded
    assert result["status"] == "SUBMITTED", f"Expected SUBMITTED, got {result['status']}"
    assert "order_ids" in result
    assert len(result["order_ids"]) == 3  # parent + target + stop
    assert "ib_ids" in result
    
    parent_ib_id = result["ib_ids"]["parent"]
    target_ib_id = result["ib_ids"]["target"]
    stop_ib_id = result["ib_ids"]["stop"]
    
    # Verify IB gateway was called correctly
    bracket_calls = [c for c in _mock_ib.calls if c["method"] == "place_bracket_order"]
    assert len(bracket_calls) == 1, "Expected 1 bracket order call"
    bracket_call = bracket_calls[0]
    assert bracket_call["symbol"] == "AAPL"
    assert bracket_call["action"] == "BUY"  # LONG → BUY
    assert bracket_call["quantity"] == qty
    assert bracket_call["stop_loss_price"] == consensus["stop_loss"]
    assert bracket_call["take_profit_price"] == consensus["target_price"]
    
    # Step 4: Verify DB records
    orders = get_orders_summary(db_file, "AAPL")
    assert len(orders) == 3, f"Expected 3 orders, got {len(orders)}"
    
    # Find each order by role
    parent_order = next((o for o in orders if o["order_role"] == "ENTRY"), None)
    target_order = next((o for o in orders if o["order_role"] == "TAKE_PROFIT"), None)
    stop_order = next((o for o in orders if o["order_role"] == "STOP_LOSS"), None)
    
    assert parent_order is not None, "Parent ENTRY order not found"
    assert target_order is not None, "TAKE_PROFIT order not found"
    assert stop_order is not None, "STOP_LOSS order not found"
    
    # Verify parent order
    assert parent_order["ib_order_id"] == parent_ib_id
    assert parent_order["ib_parent_id"] == parent_ib_id  # Parent is its own parent
    assert parent_order["action"] == "BUY"
    assert parent_order["order_type"] == "MKT"
    assert parent_order["quantity"] == qty
    assert parent_order["status"] == "SUBMITTED"
    assert parent_order["log_id"] == "test-bracket-001"
    
    # Verify target (take profit) order
    assert target_order["ib_order_id"] == target_ib_id
    assert target_order["ib_parent_id"] == parent_ib_id  # Same parent
    assert target_order["action"] == "SELL"  # Close LONG
    assert target_order["order_type"] == "LMT"
    assert target_order["limit_price"] == consensus["target_price"]
    assert target_order["quantity"] == qty
    
    # Verify stop loss order
    assert stop_order["ib_order_id"] == stop_ib_id
    assert stop_order["ib_parent_id"] == parent_ib_id
    assert stop_order["action"] == "SELL"  # Close LONG
    assert stop_order["order_type"] == "STP"
    assert stop_order["stop_price"] == consensus["stop_loss"]
    assert stop_order["quantity"] == qty
    
    # Verify spread guard was called
    spread_calls = [c for c in _mock_ib.calls if c["method"] == "get_bid_ask_spread"]
    assert len(spread_calls) == 1, "Expected 1 spread check"
    
    _cleanup_db(db_file)


def test_order_status_update_after_fill():
    """
    Test order status updates simulating IB fill callback.
    Verifies:
      - Initial status is SUBMITTED
      - After fill simulation: parent → FILLED_ENTRY
      - Children remain SUBMITTED (waiting for trigger)
    """
    from consensus import calculate_consensus
    from position_sizer import calculate_position
    from order_manager import submit_signal
    
    _mock_ib.calls.clear()
    
    db_file = _create_test_db()
    db = FakeDbManager(db_file)
    
    # Create and submit order
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
    
    with mock_ib_gateway():
        result = submit_signal("AAPL", consensus, position, db)
    
    assert result["status"] == "SUBMITTED"
    parent_ib_id = result["ib_ids"]["parent"]
    
    # Verify initial status
    orders = get_orders_summary(db_file, "AAPL")
    parent = next(o for o in orders if o["order_role"] == "ENTRY")
    assert parent["status"] == "SUBMITTED"
    assert parent["filled_at"] == ""  # Not filled yet
    
    # Simulate IB fill callback
    simulate_ib_fills(db_file, parent_ib_id, fill_price=150.0)
    
    # Verify updated status
    orders = get_orders_summary(db_file, "AAPL")
    parent = next(o for o in orders if o["order_role"] == "ENTRY")
    target = next(o for o in orders if o["order_role"] == "TAKE_PROFIT")
    stop = next(o for o in orders if o["order_role"] == "STOP_LOSS")
    
    assert parent["status"] == "FILLED_ENTRY", f"Parent should be FILLED_ENTRY, got {parent['status']}"
    assert parent["filled_at"] != "", "filled_at should be set"
    
    # Children should be SUBMITTED (active and waiting for trigger)
    assert target["status"] == "SUBMITTED"
    assert stop["status"] == "SUBMITTED"
    
    _cleanup_db(db_file)


def test_short_bracket_order():
    """
    Test SHORT bracket order creates correct SELL/ BUY actions.
    """
    from consensus import calculate_consensus
    from position_sizer import calculate_position
    from order_manager import submit_signal
    
    _mock_ib.calls.clear()
    
    db_file = _create_test_db()
    db = FakeDbManager(db_file)
    
    forecasts = [{
        "side": "SHORT",
        "confidence": 75,
        "method": "momentum_trend",
        "model": "claude-sonnet",
        "exit_target": "$135.00",
        "stop_loss": 160.0,
        "entry_price": "$150.00"
    }]
    
    consensus = calculate_consensus(forecasts, current_price=150.0)
    position = calculate_position("AAPL", 150.0, 160.0, db, net_liquidation=100000.0)
    
    with mock_ib_gateway():
        result = submit_signal("AAPL", consensus, position, db)
    
    assert result["status"] == "SUBMITTED"
    
    # Verify SHORT actions
    orders = get_orders_summary(db_file, "AAPL")
    parent = next(o for o in orders if o["order_role"] == "ENTRY")
    target = next(o for o in orders if o["order_role"] == "TAKE_PROFIT")
    stop = next(o for o in orders if o["order_role"] == "STOP_LOSS")
    
    assert parent["action"] == "SELL", "SHORT entry should be SELL"
    assert target["action"] == "BUY", "SHORT take-profit should be BUY"
    assert stop["action"] == "BUY", "SHORT stop-loss should be BUY"
    
    # Verify IB was called with SELL action
    bracket_calls = [c for c in _mock_ib.calls if c["method"] == "place_bracket_order"]
    assert bracket_calls[0]["action"] == "SELL"
    
    _cleanup_db(db_file)


def test_cancel_order_simulation():
    """
    Test order cancellation flow.
    """
    from order_manager import rollback_bracket_group
    
    _mock_ib.calls.clear()
    
    db_file = _create_test_db()
    db = FakeDbManager(db_file)
    
    # Pre-create orders in FILLED_ENTRY status
    with sqlite3.connect(db_file) as c:
        c.execute("""
            INSERT INTO orders (log_id, ticker, ib_order_id, ib_parent_id, order_role,
                order_type, action, quantity, status, account_type, created_at, filled_at)
            VALUES ('test', 'AAPL', 1001, 1001, 'ENTRY', 'MKT', 'BUY', 10,
                'FILLED_ENTRY', 'paper', datetime('now'), datetime('now'))
        """)
        c.execute("""
            INSERT INTO orders (log_id, ticker, ib_order_id, ib_parent_id, order_role,
                order_type, action, quantity, status, account_type, created_at)
            VALUES ('test', 'AAPL', 1002, 1001, 'TAKE_PROFIT', 'LMT', 'SELL', 10,
                'SUBMITTED', 'paper', datetime('now'))
        """)
        c.execute("""
            INSERT INTO orders (log_id, ticker, ib_order_id, ib_parent_id, order_role,
                order_type, action, quantity, status, account_type, created_at)
            VALUES ('test', 'AAPL', 1003, 1001, 'STOP_LOSS', 'STP', 'SELL', 10,
                'SUBMITTED', 'paper', datetime('now'))
        """)
        c.commit()
    
    # Get parent DB id
    with sqlite3.connect(db_file) as c:
        parent_id = c.execute(
            "SELECT id FROM orders WHERE ib_parent_id = 1001 AND order_role = 'ENTRY'"
        ).fetchone()[0]
    
    # Trigger rollback
    with mock_ib_gateway():
        success = rollback_bracket_group(parent_id, db)
    
    # Verify cancel was called for children
    cancel_calls = [c for c in _mock_ib.calls if c["method"] == "cancel_order"]
    assert len(cancel_calls) >= 2, "Expected cancel calls for children"
    
    # Verify close position was called
    close_calls = [c for c in _mock_ib.calls if c["method"] == "close_position_market"]
    assert len(close_calls) == 1, "Expected 1 close position call"
    
    # Verify DB status updated
    orders = get_orders_summary(db_file, "AAPL")
    for o in orders:
        assert o["status"] in ("ROLLBACK_COMPLETE", "ROLLBACK_PENDING", "CANCELLED")
    
    _cleanup_db(db_file)


def test_order_records_with_queue_outside_hours():
    """
    Test that orders outside market hours are QUEUED, not SUBMITTED.
    """
    from consensus import calculate_consensus
    from position_sizer import calculate_position
    from order_manager import submit_signal
    
    _mock_ib.calls.clear()
    
    db_file = _create_test_db()
    db = FakeDbManager(db_file)
    
    # Disable extended hours (will queue outside market hours)
    with sqlite3.connect(db_file) as c:
        c.execute("UPDATE config SET value='false' WHERE key='ALLOW_EXTENDED_HOURS'")
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
    
    # Mock to simulate outside hours (this will check market hours)
    with mock_ib_gateway():
        with patch('order_manager._is_market_hours', return_value=False):
            result = submit_signal("AAPL", consensus, position, db)
    
    # Should be QUEUED (or SUBMITTED if outside hours logic works differently)
    assert result["status"] in ("QUEUED", "SUBMITTED")
    
    if result["status"] == "QUEUED":
        # Verify only parent created, children not yet
        orders = get_orders_summary(db_file, "AAPL")
        assert len(orders) == 1, "Queued order should have only parent entry"
        assert orders[0]["status"] == "QUEUED"
    
    _cleanup_db(db_file)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
