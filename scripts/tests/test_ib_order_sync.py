"""Tests for IB order status sync."""
import sys
import os
import sqlite3
import tempfile
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Test: fetch_open_order_statuses returns expected shape
# ---------------------------------------------------------------------------

def test_fetch_open_order_statuses_returns_list():
    """fetch_open_order_statuses must return a list of dicts with required keys."""
    mock_trade1 = MagicMock()
    mock_trade1.order.orderId = 1001
    mock_trade1.orderStatus.status = "Filled"
    mock_trade1.orderStatus.avgFillPrice = 150.25
    mock_trade1.orderStatus.filled = 10
    mock_trade1.log = [MagicMock(time=datetime(2024, 1, 15, 14, 35, 0))]

    mock_trade2 = MagicMock()
    mock_trade2.order.orderId = 1002
    mock_trade2.orderStatus.status = "Submitted"
    mock_trade2.orderStatus.avgFillPrice = 0.0
    mock_trade2.orderStatus.filled = 0
    mock_trade2.log = []

    mock_ib_instance = MagicMock()
    mock_ib_instance.isConnected.return_value = True
    mock_ib_instance.openTrades.return_value = [mock_trade1, mock_trade2]

    with patch("ib_insync.IB", return_value=mock_ib_instance):
        from ib_gateway_client import fetch_open_order_statuses
        result = fetch_open_order_statuses(port=7497)

    assert isinstance(result, list)
    assert len(result) == 2
    r1 = next(r for r in result if r["ib_order_id"] == 1001)
    assert r1["status"] == "Filled"
    assert r1["avg_fill_price"] == 150.25
    r2 = next(r for r in result if r["ib_order_id"] == 1002)
    assert r2["status"] == "Submitted"


def test_fetch_open_order_statuses_returns_empty_on_ib_error():
    """On IB connection failure, return empty list (do not raise)."""
    mock_ib_instance = MagicMock()
    mock_ib_instance.connect.side_effect = ConnectionRefusedError("IB not available")

    with patch("ib_insync.IB", return_value=mock_ib_instance):
        from ib_gateway_client import fetch_open_order_statuses
        result = fetch_open_order_statuses(port=7497)

    assert result == []


def test_fetch_open_order_statuses_includes_completed_terminal_orders():
    """Completed terminal orders must be visible even when openTrades is empty."""
    completed_trade = MagicMock()
    completed_trade.order.orderId = 2001
    completed_trade.orderStatus.status = "Cancelled"
    completed_trade.orderStatus.avgFillPrice = 0.0
    completed_trade.orderStatus.filled = 0
    completed_trade.log = [MagicMock(time=datetime(2024, 1, 15, 15, 0, 0))]

    mock_ib_instance = MagicMock()
    mock_ib_instance.isConnected.return_value = True
    mock_ib_instance.openTrades.return_value = []
    mock_ib_instance.reqCompletedOrders.return_value = [completed_trade]
    mock_ib_instance.executions.return_value = []

    with patch("ib_insync.IB", return_value=mock_ib_instance):
        from ib_gateway_client import fetch_open_order_statuses
        result = fetch_open_order_statuses(port=7497)

    assert len(result) == 1
    assert result[0]["ib_order_id"] == 2001
    assert result[0]["status"] == "Cancelled"


def test_fetch_open_order_statuses_falls_back_to_executions_for_fills():
    """Filled orders absent from openTrades must still be reflected via executions."""
    exec1 = MagicMock(orderId=3001, shares=4, price=100.0, time=datetime(2024, 1, 15, 16, 0, 0))
    exec2 = MagicMock(orderId=3001, shares=6, price=101.0, time=datetime(2024, 1, 15, 16, 1, 0))

    mock_ib_instance = MagicMock()
    mock_ib_instance.isConnected.return_value = True
    mock_ib_instance.openTrades.return_value = []
    mock_ib_instance.reqCompletedOrders.return_value = []
    mock_ib_instance.executions.return_value = [exec1, exec2]

    with patch("ib_insync.IB", return_value=mock_ib_instance):
        from ib_gateway_client import fetch_open_order_statuses
        result = fetch_open_order_statuses(port=7497)

    assert len(result) == 1
    assert result[0]["ib_order_id"] == 3001
    assert result[0]["status"] == "Filled"
    assert result[0]["filled_qty"] == 10.0
    assert result[0]["avg_fill_price"] == pytest.approx(100.6, abs=1e-6)


def test_fetch_open_order_statuses_prefers_api_cancelled_over_submitted():
    """When sources disagree, cancel-related status must win over Submitted."""
    open_trade = MagicMock()
    open_trade.order.orderId = 2101
    open_trade.orderStatus.status = "Submitted"
    open_trade.orderStatus.avgFillPrice = 0.0
    open_trade.orderStatus.filled = 0
    open_trade.log = [MagicMock(time=datetime(2024, 1, 15, 14, 40, 0))]

    completed_trade = MagicMock()
    completed_trade.order.orderId = 2101
    completed_trade.orderStatus.status = "ApiCancelled"
    completed_trade.orderStatus.avgFillPrice = 0.0
    completed_trade.orderStatus.filled = 0
    completed_trade.log = [MagicMock(time=datetime(2024, 1, 15, 14, 42, 0))]

    mock_ib_instance = MagicMock()
    mock_ib_instance.isConnected.return_value = True
    mock_ib_instance.openTrades.return_value = [open_trade]
    mock_ib_instance.reqCompletedOrders.return_value = [completed_trade]
    mock_ib_instance.executions.return_value = []

    with patch("ib_insync.IB", return_value=mock_ib_instance):
        from ib_gateway_client import fetch_open_order_statuses
        result = fetch_open_order_statuses(port=7497)

    assert len(result) == 1
    assert result[0]["ib_order_id"] == 2101
    assert result[0]["status"] == "ApiCancelled"


def test_fetch_open_order_statuses_uses_noarg_completed_orders_fallback():
    """If reqCompletedOrders(apiOnly=...) is unsupported, fallback call must still work."""
    completed_trade = MagicMock()
    completed_trade.order.orderId = 2201
    completed_trade.orderStatus.status = "Cancelled"
    completed_trade.orderStatus.avgFillPrice = 0.0
    completed_trade.orderStatus.filled = 0
    completed_trade.log = [MagicMock(time=datetime(2024, 1, 15, 15, 10, 0))]

    mock_ib_instance = MagicMock()
    mock_ib_instance.isConnected.return_value = True
    mock_ib_instance.openTrades.return_value = []
    mock_ib_instance.reqCompletedOrders.side_effect = [TypeError("apiOnly not supported"), [completed_trade]]
    mock_ib_instance.executions.return_value = []

    with patch("ib_insync.IB", return_value=mock_ib_instance):
        from ib_gateway_client import fetch_open_order_statuses
        result = fetch_open_order_statuses(port=7497)

    assert len(result) == 1
    assert result[0]["ib_order_id"] == 2201
    assert result[0]["status"] == "Cancelled"
    assert mock_ib_instance.reqCompletedOrders.call_count == 2


def test_fetch_open_order_statuses_includes_req_all_open_orders():
    """Orders not present in openTrades should still be seen via reqAllOpenOrders."""
    all_open_trade = MagicMock()
    all_open_trade.order.orderId = 2301
    all_open_trade.orderStatus.status = "Submitted"
    all_open_trade.orderStatus.avgFillPrice = 0.0
    all_open_trade.orderStatus.filled = 0
    all_open_trade.log = [MagicMock(time=datetime(2024, 1, 15, 15, 20, 0))]

    mock_ib_instance = MagicMock()
    mock_ib_instance.isConnected.return_value = True
    mock_ib_instance.openTrades.return_value = []
    mock_ib_instance.reqAllOpenOrders.return_value = [all_open_trade]
    mock_ib_instance.reqCompletedOrders.return_value = []
    mock_ib_instance.executions.return_value = []

    with patch("ib_insync.IB", return_value=mock_ib_instance):
        from ib_gateway_client import fetch_open_order_statuses
        result = fetch_open_order_statuses(port=7497)

    assert len(result) == 1
    assert result[0]["ib_order_id"] == 2301
    assert result[0]["status"] == "Submitted"


def test_fetch_ib_order_status_by_order_id_uses_completed_orders_for_cancelled():
    """Specific order lookup must report completed cancelled orders, not Unknown."""
    completed_trade = MagicMock()
    completed_trade.order.orderId = 4001
    completed_trade.order.permId = 90001
    completed_trade.order.account = "DU123"
    completed_trade.order.action = "SELL"
    completed_trade.order.orderType = "STP"
    completed_trade.order.totalQuantity = 5
    completed_trade.orderStatus.status = "Cancelled"
    completed_trade.orderStatus.filled = 0
    completed_trade.orderStatus.remaining = 0
    completed_trade.orderStatus.avgFillPrice = 0.0
    completed_trade.orderStatus.lastFillPrice = 0.0
    completed_trade.contract.symbol = "NVDA"
    completed_trade.log = [MagicMock(time=datetime(2024, 1, 15, 16, 30, 0))]

    mock_ib_instance = MagicMock()
    mock_ib_instance.isConnected.return_value = True
    mock_ib_instance.openTrades.return_value = []
    mock_ib_instance.reqCompletedOrders.return_value = [completed_trade]
    mock_ib_instance.executions.return_value = []

    with patch("ib_insync.IB", return_value=mock_ib_instance):
        from ib_gateway_client import fetch_ib_order_status_by_order_id
        result = fetch_ib_order_status_by_order_id(order_id=4001, port=7497)

    assert result["found"] is True
    assert result["ib_order_id"] == 4001
    assert result["status"] == "Cancelled"
    assert result["source"] == "completedOrders"
    assert result["order"]["symbol"] == "NVDA"


def test_fetch_ib_order_status_by_order_id_returns_unknown_when_absent():
    """Specific order lookup should stay Unknown only when no IB source has the order."""
    mock_ib_instance = MagicMock()
    mock_ib_instance.isConnected.return_value = True
    mock_ib_instance.openTrades.return_value = []
    mock_ib_instance.reqCompletedOrders.return_value = []
    mock_ib_instance.executions.return_value = []

    with patch("ib_insync.IB", return_value=mock_ib_instance):
        from ib_gateway_client import fetch_ib_order_status_by_order_id
        result = fetch_ib_order_status_by_order_id(order_id=4999, port=7497)

    assert result["found"] is False
    assert result["status"] == "Unknown"
    assert result["source"] == "none"


def test_fetch_ib_order_status_by_order_id_uses_req_all_open_orders():
    """Specific order lookup should use reqAllOpenOrders when openTrades is empty."""
    open_trade = MagicMock()
    open_trade.order.orderId = 4101
    open_trade.order.permId = 91001
    open_trade.order.account = "DU123"
    open_trade.order.action = "BUY"
    open_trade.order.orderType = "LMT"
    open_trade.order.totalQuantity = 3
    open_trade.orderStatus.status = "Submitted"
    open_trade.orderStatus.filled = 0
    open_trade.orderStatus.remaining = 3
    open_trade.orderStatus.avgFillPrice = 0.0
    open_trade.orderStatus.lastFillPrice = 0.0
    open_trade.contract.symbol = "AAPL"
    open_trade.log = [MagicMock(time=datetime(2024, 1, 15, 16, 35, 0))]

    mock_ib_instance = MagicMock()
    mock_ib_instance.isConnected.return_value = True
    mock_ib_instance.openTrades.return_value = []
    mock_ib_instance.reqAllOpenOrders.return_value = [open_trade]
    mock_ib_instance.reqCompletedOrders.return_value = []
    mock_ib_instance.executions.return_value = []

    with patch("ib_insync.IB", return_value=mock_ib_instance):
        from ib_gateway_client import fetch_ib_order_status_by_order_id
        result = fetch_ib_order_status_by_order_id(order_id=4101, port=7497)

    assert result["found"] is True
    assert result["ib_order_id"] == 4101
    assert result["status"] == "Submitted"
    assert result["source"] == "reqAllOpenOrders"
    assert result["order"]["symbol"] == "AAPL"


def test_fetch_ib_order_status_by_order_id_falls_back_to_executions_when_completed_errors():
    """Completed-orders API failures must not prevent executions fallback."""
    exec1 = MagicMock(orderId=4201, shares=2, price=200.0, time=datetime(2024, 1, 15, 17, 0, 0))
    exec2 = MagicMock(orderId=4201, shares=1, price=201.0, time=datetime(2024, 1, 15, 17, 1, 0))

    mock_ib_instance = MagicMock()
    mock_ib_instance.isConnected.return_value = True
    mock_ib_instance.openTrades.return_value = []
    mock_ib_instance.reqAllOpenOrders.return_value = []
    mock_ib_instance.reqCompletedOrders.side_effect = RuntimeError("completed unavailable")
    mock_ib_instance.executions.return_value = [exec1, exec2]

    with patch("ib_insync.IB", return_value=mock_ib_instance):
        from ib_gateway_client import fetch_ib_order_status_by_order_id
        result = fetch_ib_order_status_by_order_id(order_id=4201, port=7497)

    assert result["found"] is True
    assert result["ib_order_id"] == 4201
    assert result["status"] == "Filled"
    assert result["source"] == "executions"
    assert result["order"]["filled_qty"] == 3.0
    assert result["order"]["avg_fill_price"] == pytest.approx(200.333333, abs=1e-6)


def test_fetch_ib_positions_normalizes_smart_to_primary_exchange():
    """Portfolio sync must use primaryExchange for SMART-routed positions."""
    contract = MagicMock()
    contract.exchange = "SMART"
    contract.primaryExchange = "NASDAQ"
    contract.symbol = "NVDA"
    contract.conId = 123456
    contract.currency = "USD"
    contract.secType = "STK"

    position = MagicMock()
    position.contract = contract
    position.account = "DU123"
    position.position = 7
    position.averageCost = 820.5
    position.marketPrice = 830.0
    position.marketValue = 5810.0
    position.unrealizedPNL = 66.5
    position.realizedPNL = 0.0

    mock_ib_instance = MagicMock()
    mock_ib_instance.isConnected.return_value = True
    mock_ib_instance.portfolio.return_value = [position]

    with patch("ib_insync.IB", return_value=mock_ib_instance):
        from ib_gateway_client import fetch_ib_positions
        result = fetch_ib_positions(port=7497)

    assert len(result) == 1
    assert result[0]["ticker"] == "NASDAQ:NVDA"
    assert result[0]["con_id"] == 123456


def test_sync_orders_with_ib_maps_api_cancelled_to_cancelled(tmp_path, monkeypatch):
    """ApiCancelled from IB must update local order state to CANCELLED."""
    db_file = _make_db(tmp_path)
    db = FakeDbManager(db_file)

    monkeypatch.setattr(
        "order_status_sync._fetch_statuses_with_event_loop",
        lambda host, port, client_id: [
            {
                "ib_order_id": 1001,
                "status": "ApiCancelled",
                "avg_fill_price": 0.0,
                "filled_qty": 0.0,
                "last_update": "2024-01-15T15:05:00",
            }
        ],
    )

    from order_status_sync import sync_orders_with_ib

    result = sync_orders_with_ib(db, port=7497)
    assert result["ok"] is True
    assert result["updated_orders"] == 1

    with sqlite3.connect(db_file) as con:
        row = con.execute(
            "SELECT status FROM orders WHERE ib_order_id=1001"
        ).fetchone()

    assert row[0] == "CANCELLED"


def test_sync_orders_with_ib_maps_pending_submit_to_submitted(tmp_path, monkeypatch):
    """PendingSubmit from IB must keep the local order in active submitted state."""
    db_file = _make_db(tmp_path)
    with sqlite3.connect(db_file) as con:
        con.execute("UPDATE orders SET status='QUEUED' WHERE ib_order_id=1001")
    db = FakeDbManager(db_file)

    monkeypatch.setattr(
        "order_status_sync._fetch_statuses_with_event_loop",
        lambda host, port, client_id: [
            {
                "ib_order_id": 1001,
                "status": "PendingSubmit",
                "avg_fill_price": 0.0,
                "filled_qty": 0.0,
                "last_update": "2024-01-15T14:05:00",
            }
        ],
    )

    from order_status_sync import sync_orders_with_ib

    result = sync_orders_with_ib(db, port=7497)
    assert result["ok"] is True
    assert result["updated_orders"] == 1

    with sqlite3.connect(db_file) as con:
        row = con.execute(
            "SELECT status FROM orders WHERE ib_order_id=1001"
        ).fetchone()

    assert row[0] == "SUBMITTED"


def test_sync_orders_with_ib_maps_api_pending_to_submitted(tmp_path, monkeypatch):
    """ApiPending from IB should keep local order in active submitted state."""
    db_file = _make_db(tmp_path)
    with sqlite3.connect(db_file) as con:
        con.execute("UPDATE orders SET status='QUEUED' WHERE ib_order_id=1001")
    db = FakeDbManager(db_file)

    monkeypatch.setattr(
        "order_status_sync._fetch_statuses_with_event_loop",
        lambda host, port, client_id: [
            {
                "ib_order_id": 1001,
                "status": "ApiPending",
                "avg_fill_price": 0.0,
                "filled_qty": 0.0,
                "last_update": "2024-01-15T14:06:00",
            }
        ],
    )

    from order_status_sync import sync_orders_with_ib

    result = sync_orders_with_ib(db, port=7497)
    assert result["ok"] is True
    assert result["updated_orders"] == 1

    with sqlite3.connect(db_file) as con:
        row = con.execute(
            "SELECT status FROM orders WHERE ib_order_id=1001"
        ).fetchone()

    assert row[0] == "SUBMITTED"


def test_sync_orders_with_ib_maps_pending_cancel_to_submitted(tmp_path, monkeypatch):
    """PendingCancel from IB should not be treated as terminal CANCELLED yet."""
    db_file = _make_db(tmp_path)
    with sqlite3.connect(db_file) as con:
        con.execute("UPDATE orders SET status='SUBMITTED' WHERE ib_order_id=1001")
    db = FakeDbManager(db_file)

    monkeypatch.setattr(
        "order_status_sync._fetch_statuses_with_event_loop",
        lambda host, port, client_id: [
            {
                "ib_order_id": 1001,
                "status": "PendingCancel",
                "avg_fill_price": 0.0,
                "filled_qty": 0.0,
                "last_update": "2024-01-15T14:07:00",
            }
        ],
    )

    from order_status_sync import sync_orders_with_ib

    result = sync_orders_with_ib(db, port=7497)
    assert result["ok"] is True

    with sqlite3.connect(db_file) as con:
        row = con.execute(
            "SELECT status FROM orders WHERE ib_order_id=1001"
        ).fetchone()

    assert row[0] == "SUBMITTED"


def test_sync_orders_with_ib_maps_canceled_spelling_to_cancelled(tmp_path, monkeypatch):
    """Single-l Canceled spelling should still map to terminal CANCELLED."""
    db_file = _make_db(tmp_path)
    db = FakeDbManager(db_file)

    monkeypatch.setattr(
        "order_status_sync._fetch_statuses_with_event_loop",
        lambda host, port, client_id: [
            {
                "ib_order_id": 1001,
                "status": "Canceled",
                "avg_fill_price": 0.0,
                "filled_qty": 0.0,
                "last_update": "2024-01-15T14:08:00",
            }
        ],
    )

    from order_status_sync import sync_orders_with_ib

    result = sync_orders_with_ib(db, port=7497)
    assert result["ok"] is True
    assert result["updated_orders"] == 1

    with sqlite3.connect(db_file) as con:
        row = con.execute(
            "SELECT status FROM orders WHERE ib_order_id=1001"
        ).fetchone()

    assert row[0] == "CANCELLED"


def test_sync_orders_with_ib_cancels_sibling_after_take_profit_fill(tmp_path, monkeypatch):
    """When TP fills in sync, sibling SL should be inferred as CANCELLED."""
    db_file = _make_db(tmp_path)
    with sqlite3.connect(db_file) as con:
        con.execute(
            "UPDATE orders SET status='FILLED_ENTRY', filled_price=150.0, filled_at='2024-01-15T14:35:00' WHERE ib_order_id=1001"
        )
        con.execute(
            "UPDATE trades SET entry_price=150.0, entry_filled_at='2024-01-15T14:35:00' WHERE ib_parent_id=1001"
        )
    db = FakeDbManager(db_file)

    monkeypatch.setattr(
        "order_status_sync._fetch_statuses_with_event_loop",
        lambda host, port, client_id: [
            {
                "ib_order_id": 1002,
                "status": "Filled",
                "avg_fill_price": 165.0,
                "filled_qty": 10,
                "last_update": "2024-01-15T16:00:00",
            }
        ],
    )

    from order_status_sync import sync_orders_with_ib
    result = sync_orders_with_ib(db, port=7497)
    assert result["ok"] is True

    with sqlite3.connect(db_file) as con:
        tp_row = con.execute("SELECT status FROM orders WHERE ib_order_id=1002").fetchone()
        sl_row = con.execute("SELECT status FROM orders WHERE ib_order_id=1003").fetchone()

    assert tp_row[0] == "FILLED"
    assert sl_row[0] == "CANCELLED"


def test_sync_orders_with_ib_cancels_sibling_after_stop_loss_fill(tmp_path, monkeypatch):
    """When SL fills in sync, sibling TP should be inferred as CANCELLED."""
    db_file = _make_db(tmp_path)
    with sqlite3.connect(db_file) as con:
        con.execute(
            "UPDATE orders SET status='FILLED_ENTRY', filled_price=150.0, filled_at='2024-01-15T14:35:00' WHERE ib_order_id=1001"
        )
        con.execute(
            "UPDATE trades SET entry_price=150.0, entry_filled_at='2024-01-15T14:35:00' WHERE ib_parent_id=1001"
        )
    db = FakeDbManager(db_file)

    monkeypatch.setattr(
        "order_status_sync._fetch_statuses_with_event_loop",
        lambda host, port, client_id: [
            {
                "ib_order_id": 1003,
                "status": "Filled",
                "avg_fill_price": 142.0,
                "filled_qty": 10,
                "last_update": "2024-01-15T16:30:00",
            }
        ],
    )

    from order_status_sync import sync_orders_with_ib
    result = sync_orders_with_ib(db, port=7497)
    assert result["ok"] is True

    with sqlite3.connect(db_file) as con:
        sl_row = con.execute("SELECT status FROM orders WHERE ib_order_id=1003").fetchone()
        tp_row = con.execute("SELECT status FROM orders WHERE ib_order_id=1002").fetchone()

    assert sl_row[0] == "FILLED"
    assert tp_row[0] == "CANCELLED"


# ---------------------------------------------------------------------------
# Helpers shared by order_manager tests
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    """Create a minimal SQLite DB with orders and trades tables."""
    db_file = str(tmp_path / "test.db")
    with sqlite3.connect(db_file) as con:
        con.executescript("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                ib_order_id INTEGER DEFAULT 0,
                ib_parent_id INTEGER DEFAULT 0,
                order_role TEXT DEFAULT '',
                order_type TEXT DEFAULT '',
                action TEXT DEFAULT '',
                quantity REAL DEFAULT 0,
                limit_price REAL DEFAULT NULL,
                stop_price REAL DEFAULT NULL,
                status TEXT DEFAULT 'QUEUED',
                account_type TEXT DEFAULT '',
                created_at TEXT DEFAULT '',
                submitted_at TEXT DEFAULT '',
                filled_at TEXT DEFAULT '',
                filled_price REAL DEFAULT NULL,
                execution_latency_ms INTEGER DEFAULT NULL,
                spread_at_submission REAL DEFAULT NULL,
                error_message TEXT DEFAULT '',
                is_test INTEGER DEFAULT 0,
                test_tag TEXT DEFAULT ''
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
                entry_filled_at TEXT DEFAULT '',
                exit_filled_at TEXT DEFAULT '',
                exit_price REAL DEFAULT NULL,
                close_reason TEXT DEFAULT '',
                realized_pnl REAL DEFAULT NULL,
                r_multiple REAL DEFAULT NULL,
                status TEXT DEFAULT 'OPEN',
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT '',
                is_test INTEGER DEFAULT 0,
                test_tag TEXT DEFAULT ''
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
            CREATE TABLE config (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT '',
                description TEXT DEFAULT ''
            );
            INSERT INTO config (key, value) VALUES ('ORDER_MODE', 'paper');
        """)
        con.execute("""
            INSERT INTO orders (ticker, ib_order_id, ib_parent_id, order_role, action,
                                quantity, status, account_type, created_at, submitted_at)
            VALUES ('AAPL', 1001, 1001, 'ENTRY', 'BUY', 10, 'SUBMITTED', 'paper',
                    '2024-01-15T14:00:00', '2024-01-15T14:00:01')
        """)
        con.execute("""
            INSERT INTO orders (ticker, ib_order_id, ib_parent_id, order_role, action,
                                quantity, limit_price, status, account_type, created_at, submitted_at)
            VALUES ('AAPL', 1002, 1001, 'TAKE_PROFIT', 'SELL', 10, 165.0, 'SUBMITTED', 'paper',
                    '2024-01-15T14:00:00', '2024-01-15T14:00:01')
        """)
        con.execute("""
            INSERT INTO orders (ticker, ib_order_id, ib_parent_id, order_role, action,
                                quantity, stop_price, status, account_type, created_at, submitted_at)
            VALUES ('AAPL', 1003, 1001, 'STOP_LOSS', 'SELL', 10, 142.0, 'SUBMITTED', 'paper',
                    '2024-01-15T14:00:00', '2024-01-15T14:00:01')
        """)
        con.execute("""
            INSERT INTO trades (ticker, ib_parent_id, signal, quantity, entry_price,
                                stop_loss, target_price, status, created_at, updated_at)
            VALUES ('AAPL', 1001, 'LONG', 10, NULL, 142.0, 165.0, 'OPEN',
                    '2024-01-15T14:00:00', '2024-01-15T14:00:00')
        """)
    return db_file


class FakeDbManager:
    def __init__(self, db_file):
        self.db_file = db_file

    def get_config_value(self, key):
        with sqlite3.connect(self.db_file) as con:
            row = con.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def _connect(self):
        return sqlite3.connect(self.db_file)


# ---------------------------------------------------------------------------
# Test: process_ib_order_updates — entry fill
# ---------------------------------------------------------------------------

def test_process_ib_order_updates_marks_entry_filled(tmp_path):
    """When IB reports ENTRY order as Filled, DB status → FILLED_ENTRY, filled_price saved."""
    db_file = _make_db(tmp_path)
    db = FakeDbManager(db_file)

    ib_statuses = [
        {"ib_order_id": 1001, "status": "Filled", "avg_fill_price": 150.25,
         "filled_qty": 10, "last_update": "2024-01-15T14:35:00"},
        {"ib_order_id": 1002, "status": "Submitted", "avg_fill_price": 0.0,
         "filled_qty": 0, "last_update": ""},
        {"ib_order_id": 1003, "status": "Submitted", "avg_fill_price": 0.0,
         "filled_qty": 0, "last_update": ""},
    ]

    from order_manager import process_ib_order_updates
    process_ib_order_updates(db, ib_statuses)

    with sqlite3.connect(db_file) as con:
        row = con.execute(
            "SELECT status, filled_price, filled_at FROM orders WHERE ib_order_id=1001"
        ).fetchone()
    assert row[0] == "FILLED_ENTRY"
    assert row[1] == 150.25
    assert row[2] != ""

    with sqlite3.connect(db_file) as con:
        trade = con.execute(
            "SELECT entry_price, entry_filled_at, status FROM trades WHERE ib_parent_id=1001"
        ).fetchone()
    assert trade[0] == 150.25
    assert trade[1] != ""
    assert trade[2] == "OPEN"


def test_process_ib_order_updates_closes_trade_on_take_profit(tmp_path):
    """When IB reports TAKE_PROFIT order as Filled, trade → CLOSED with realized_pnl."""
    db_file = _make_db(tmp_path)
    with sqlite3.connect(db_file) as con:
        con.execute(
            "UPDATE orders SET status='FILLED_ENTRY', filled_price=150.0, filled_at='2024-01-15T14:35:00' WHERE ib_order_id=1001"
        )
        con.execute(
            "UPDATE trades SET entry_price=150.0, entry_filled_at='2024-01-15T14:35:00' WHERE ib_parent_id=1001"
        )
    db = FakeDbManager(db_file)

    ib_statuses = [
        {"ib_order_id": 1002, "status": "Filled", "avg_fill_price": 165.0,
         "filled_qty": 10, "last_update": "2024-01-15T16:00:00"},
    ]

    from order_manager import process_ib_order_updates
    process_ib_order_updates(db, ib_statuses)

    with sqlite3.connect(db_file) as con:
        tp_row = con.execute(
            "SELECT status, filled_price FROM orders WHERE ib_order_id=1002"
        ).fetchone()
        trade = con.execute(
            "SELECT status, exit_price, close_reason, realized_pnl FROM trades WHERE ib_parent_id=1001"
        ).fetchone()

    assert tp_row[0] == "FILLED"
    assert tp_row[1] == 165.0
    assert trade[0] == "CLOSED"
    assert trade[1] == 165.0
    assert trade[2] == "TAKE_PROFIT"
    assert trade[3] == pytest.approx((165.0 - 150.0) * 10, abs=0.01)


def test_process_ib_order_updates_closes_trade_on_stop_loss(tmp_path):
    """When IB reports STOP_LOSS order as Filled, trade → CLOSED with realized_pnl (negative)."""
    db_file = _make_db(tmp_path)
    with sqlite3.connect(db_file) as con:
        con.execute(
            "UPDATE orders SET status='FILLED_ENTRY', filled_price=150.0, filled_at='2024-01-15T14:35:00' WHERE ib_order_id=1001"
        )
        con.execute(
            "UPDATE trades SET entry_price=150.0, entry_filled_at='2024-01-15T14:35:00' WHERE ib_parent_id=1001"
        )
    db = FakeDbManager(db_file)

    ib_statuses = [
        {"ib_order_id": 1003, "status": "Filled", "avg_fill_price": 142.0,
         "filled_qty": 10, "last_update": "2024-01-15T16:30:00"},
    ]

    from order_manager import process_ib_order_updates
    process_ib_order_updates(db, ib_statuses)

    with sqlite3.connect(db_file) as con:
        trade = con.execute(
            "SELECT status, exit_price, close_reason, realized_pnl FROM trades WHERE ib_parent_id=1001"
        ).fetchone()

    assert trade[0] == "CLOSED"
    assert trade[1] == 142.0
    assert trade[2] == "STOP_LOSS"
    assert trade[3] == pytest.approx((142.0 - 150.0) * 10, abs=0.01)


def test_process_ib_order_updates_marks_cancelled(tmp_path):
    """When IB reports Cancelled, DB order status → CANCELLED."""
    db_file = _make_db(tmp_path)
    db = FakeDbManager(db_file)

    ib_statuses = [
        {"ib_order_id": 1001, "status": "Cancelled", "avg_fill_price": 0.0,
         "filled_qty": 0, "last_update": "2024-01-15T15:00:00"},
    ]

    from order_manager import process_ib_order_updates
    process_ib_order_updates(db, ib_statuses)

    with sqlite3.connect(db_file) as con:
        row = con.execute(
            "SELECT status FROM orders WHERE ib_order_id=1001"
        ).fetchone()
    assert row[0] == "CANCELLED"


def test_process_ib_order_updates_no_crash_on_empty(tmp_path):
    """Empty IB status list must not raise."""
    db_file = _make_db(tmp_path)
    db = FakeDbManager(db_file)
    from order_manager import process_ib_order_updates
    process_ib_order_updates(db, [])
