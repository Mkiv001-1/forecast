"""Expanded integration test that writes test-marked bracket data into working DB.

This test creates:
- one trade row
- three order rows (ENTRY, TAKE_PROFIT, STOP_LOSS)

Rows are left in the working DB intentionally, so they can be seen in Trading Tab.
Use scripts/tools/cleanup_test_orders.py for cleanup.

Run:
  set FORECAST_ALLOW_WORKING_DB_TEST=1
  python -m pytest scripts/tests/test_working_db_trading_tab_visibility.py -v -m integration
"""

import asyncio
import os
import sqlite3
import sys
import time
from unittest.mock import patch

import pytest

PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CORE_DIR = os.path.join(PROJECT_ROOT, "core")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

from scripts.core.sqlite_manager import SQLiteManager
from scripts.server.config import ServerConfig


@pytest.mark.integration
def test_create_trade_and_three_orders_visible_in_trading_tab():
    if os.getenv("FORECAST_ALLOW_WORKING_DB_TEST", "0") != "1":
        pytest.skip("Set FORECAST_ALLOW_WORKING_DB_TEST=1 to allow writing to working DB")

    from scripts.core.order_manager import submit_signal
    import scripts.server.api as api_module
    from scripts.server.api import get_orders, get_trades

    cfg = ServerConfig()
    api_module._config = cfg
    db_file = cfg.db_file
    db = SQLiteManager(db_file)

    ticker = f"TESTGUI{int(time.time()) % 100000}"
    test_tag = "TEST:TRADING_TAB:EXPANDED"

    # Ensure order mode and ticker settings are safe for test submission.
    with sqlite3.connect(db_file) as con:
        con.execute("INSERT OR REPLACE INTO config(key,value,description) VALUES ('ORDER_MODE','paper','')")
        con.execute("INSERT OR REPLACE INTO config(key,value,description) VALUES ('LIVE_TRADING_CONFIRMED','false','')")
        con.execute("INSERT OR REPLACE INTO config(key,value,description) VALUES ('MAX_OPEN_ORDERS','9999','integration test override')")
        con.execute("INSERT OR REPLACE INTO settings(ticker,active,comment,sector,trading_blocked) VALUES (?,?,?,?,?)",
                    (ticker, 1, 'integration test ticker', 'Testing', 0))
        con.commit()

    # Generate deterministic IB ids above current max to avoid collisions.
    with sqlite3.connect(db_file) as con:
        row = con.execute("SELECT COALESCE(MAX(ib_order_id), 10000) FROM orders").fetchone()
    base_ib_id = int(row[0] or 10000) + 10

    consensus = {
        "signal": "LONG",
        "stop_loss": 95.0,
        "target_price": 110.0,
        "entry_limit_price": 100.0,
        "entry_tif": "DAY",
        "take_profit_tif": "GTC",
        "stop_loss_tif": "GTC",
        "methods_long": "",
    }
    position_size = {
        "status": "OK",
        "quantity": 5,
    }

    def _fake_place_bracket_order(**kwargs):
        assert kwargs.get("order_ref")
        return {
            "status": "submitted",
            "parent_id": base_ib_id,
            "target_id": base_ib_id + 1,
            "stop_id": base_ib_id + 2,
            "error": None,
        }

    def _fake_spread(*args, **kwargs):
        return {"status": "no_data", "spread_pct": 0.0}

    with patch("ib_gateway_client.place_bracket_order", side_effect=_fake_place_bracket_order):
        with patch("ib_gateway_client.get_bid_ask_spread", side_effect=_fake_spread):
            with patch("order_manager._is_market_hours", return_value=True):
                result = submit_signal(
                    ticker=ticker,
                    consensus=consensus,
                    position_size=position_size,
                    db_manager=db,
                    log_id="TEST:WORKING_DB:EXPANDED",
                    is_test=True,
                    test_tag=test_tag,
                )

    assert result["status"] == "SUBMITTED", f"Unexpected submission result: {result}"
    assert "trade_id" in result and result["trade_id"] is not None

    with sqlite3.connect(db_file) as con:
        con.row_factory = sqlite3.Row
        orders = con.execute(
            """
            SELECT * FROM orders
            WHERE ticker=? AND ib_parent_id=? AND COALESCE(is_test,0)=1 AND test_tag=?
            ORDER BY id ASC
            """,
            (ticker, base_ib_id, test_tag),
        ).fetchall()

        trades = con.execute(
            """
            SELECT * FROM trades
            WHERE ticker=? AND ib_parent_id=? AND COALESCE(is_test,0)=1 AND test_tag=?
            ORDER BY id DESC
            """,
            (ticker, base_ib_id, test_tag),
        ).fetchall()

    assert len(orders) == 3, f"Expected 3 orders, got {len(orders)}"
    roles = {str(r["order_role"]) for r in orders}
    assert roles == {"ENTRY", "TAKE_PROFIT", "STOP_LOSS"}, f"Unexpected roles: {roles}"

    assert len(trades) >= 1, "Expected at least one trade row"
    latest_trade = trades[0]
    assert latest_trade["status"] == "OPEN"

    # Validate Trading Tab data path via API endpoints used by GUI.
    orders_api = asyncio.run(get_orders(ticker=ticker, limit=200))
    trades_api = asyncio.run(get_trades(ticker=ticker, limit=200))

    api_order_items = orders_api.get("items", [])
    api_trade_items = trades_api.get("trades", [])

    assert any(int(o.get("ib_parent_id", 0) or 0) == base_ib_id for o in api_order_items)
    assert any(int(t.get("ib_parent_id", 0) or 0) == base_ib_id for t in api_trade_items)
