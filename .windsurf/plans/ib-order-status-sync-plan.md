# IB Order Status Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a scheduler-driven poller that fetches open order statuses from IB, updates `orders` table (`FILLED_ENTRY`, `CANCELLED`, `REJECTED`, `filled_price`, `filled_at`), and closes `trades` records when exit orders fill.

**Architecture:** Add `fetch_open_order_statuses()` to `ib_gateway_client.py` (connect → `reqOpenOrders` → disconnect → return list). Add `process_ib_order_updates()` to `order_manager.py` (compare IB statuses against DB rows, write updates, close trades on TP/SL fill). Register a new `sync_ib_order_statuses` task in `scheduler.py` every 30s, only when `ORDER_MODE != disabled`.

**Tech Stack:** `ib_insync` (already used), `sqlite3`, Python stdlib. Tests use existing `MockIBGateway` + `simulate_ib_fills` patterns from `test_integration_ib_mock.py`.

---

## File Map

| File | Change |
|------|--------|
| `scripts/core/ib_gateway_client.py` | Add `fetch_open_order_statuses()` at end |
| `scripts/core/order_manager.py` | Add `process_ib_order_updates()` + helper `_close_trade_on_exit_fill()` |
| `scripts/core/scheduler.py` | Register `sync_ib_order_statuses` task (30s) |
| `scripts/tests/test_ib_order_sync.py` | New test file |

---

### Task 1: fetch_open_order_statuses() in ib_gateway_client.py

Connects to IB, calls `reqAllOpenOrders()`, waits 2s, returns a list of dicts with IB order status.

**Files:**
- Modify: `scripts/core/ib_gateway_client.py` (append at end, before last line)
- Test: `scripts/tests/test_ib_order_sync.py`

- [ ] **Step 1: Write failing test**

Create `scripts/tests/test_ib_order_sync.py`:

```python
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
    # We patch ib_insync.IB entirely so no network is needed
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

    with patch("ib_gateway_client.IB", return_value=mock_ib_instance):
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

    with patch("ib_gateway_client.IB", return_value=mock_ib_instance):
        from ib_gateway_client import fetch_open_order_statuses
        result = fetch_open_order_statuses(port=7497)

    assert result == []
```

- [ ] **Step 2: Run test to confirm it fails**

```
python -m pytest scripts/tests/test_ib_order_sync.py::test_fetch_open_order_statuses_returns_list -v
```
Expected: `ImportError: cannot import name 'fetch_open_order_statuses'`

- [ ] **Step 3: Implement fetch_open_order_statuses in ib_gateway_client.py**

Append to `scripts/core/ib_gateway_client.py` **before** the final line:

```python
def fetch_open_order_statuses(
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 14,
    timeout: float = 3.0,
) -> list:
    """
    Fetch status of all currently open/recently-filled orders from IB.

    Returns list of dicts:
      {ib_order_id, status, avg_fill_price, filled_qty, last_update}

    IB status values we care about:
      Submitted, PreSubmitted, Filled, Cancelled, Inactive (rejected)

    Returns [] on any connection error (caller must tolerate empty result).
    """
    records = []
    try:
        from ib_insync import IB
    except ImportError:
        logger.error("ib_insync not installed")
        return records

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=15)
        trades = ib.openTrades()
        ib.sleep(timeout)

        for t in trades:
            try:
                order_id = int(t.order.orderId)
                status   = str(t.orderStatus.status)
                avg_fill = _safe_float(t.orderStatus.avgFillPrice)
                filled   = _safe_float(t.orderStatus.filled)
                last_upd = ""
                if t.log:
                    last_upd = t.log[-1].time.isoformat() if hasattr(t.log[-1].time, "isoformat") else str(t.log[-1].time)
                records.append({
                    "ib_order_id":    order_id,
                    "status":         status,
                    "avg_fill_price": avg_fill,
                    "filled_qty":     filled,
                    "last_update":    last_upd,
                })
            except Exception as inner:
                logger.warning(f"[IB] fetch_open_order_statuses: skipping trade: {inner}")

        logger.info(f"[IB] fetch_open_order_statuses: got {len(records)} records")
    except Exception as e:
        logger.error(f"[IB] fetch_open_order_statuses failed: {e}")
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass
    return records
```

- [ ] **Step 4: Run tests**

```
python -m pytest scripts/tests/test_ib_order_sync.py::test_fetch_open_order_statuses_returns_list scripts/tests/test_ib_order_sync.py::test_fetch_open_order_statuses_returns_empty_on_ib_error -v
```
Expected: 2 PASSED

- [ ] **Step 5: Commit**

```
git add scripts/core/ib_gateway_client.py scripts/tests/test_ib_order_sync.py
git commit -m "feat: add fetch_open_order_statuses to ib_gateway_client"
```

---

### Task 2: process_ib_order_updates() in order_manager.py

Reads SUBMITTED/FILLED_ENTRY orders from DB, cross-references IB statuses, writes updates. Closes `trades` record when exit order fills.

**Files:**
- Modify: `scripts/core/order_manager.py` (append two functions before last line)
- Test: `scripts/tests/test_ib_order_sync.py` (add tests)

- [ ] **Step 1: Write failing tests**

Append to `scripts/tests/test_ib_order_sync.py`:

```python
# ---------------------------------------------------------------------------
# Helpers shared by order_manager tests
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    """Create a minimal in-memory SQLite DB with orders and trades tables."""
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
            CREATE TABLE config (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT '',
                description TEXT DEFAULT ''
            );
            INSERT INTO config (key, value) VALUES ('ORDER_MODE', 'paper');
        """)
        # Insert parent ENTRY order (SUBMITTED, ib_order_id=1001, ib_parent_id=1001)
        con.execute("""
            INSERT INTO orders (ticker, ib_order_id, ib_parent_id, order_role, action,
                                quantity, status, account_type, created_at, submitted_at)
            VALUES ('AAPL', 1001, 1001, 'ENTRY', 'BUY', 10, 'SUBMITTED', 'paper',
                    '2024-01-15T14:00:00', '2024-01-15T14:00:01')
        """)
        # Insert TP child order (SUBMITTED, ib_order_id=1002)
        con.execute("""
            INSERT INTO orders (ticker, ib_order_id, ib_parent_id, order_role, action,
                                quantity, limit_price, status, account_type, created_at, submitted_at)
            VALUES ('AAPL', 1002, 1001, 'TAKE_PROFIT', 'SELL', 10, 165.0, 'SUBMITTED', 'paper',
                    '2024-01-15T14:00:00', '2024-01-15T14:00:01')
        """)
        # Insert SL child order (SUBMITTED, ib_order_id=1003)
        con.execute("""
            INSERT INTO orders (ticker, ib_order_id, ib_parent_id, order_role, action,
                                quantity, stop_price, status, account_type, created_at, submitted_at)
            VALUES ('AAPL', 1003, 1001, 'STOP_LOSS', 'SELL', 10, 142.0, 'SUBMITTED', 'paper',
                    '2024-01-15T14:00:00', '2024-01-15T14:00:01')
        """)
        # Insert open trade record
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

    # Trade entry_price and entry_filled_at should be set
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
    # Pre-set entry as already filled
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
        # TP order marked FILLED
        tp_row = con.execute(
            "SELECT status, filled_price FROM orders WHERE ib_order_id=1002"
        ).fetchone()
        # Trade closed
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
    process_ib_order_updates(db, [])  # must not raise
```

- [ ] **Step 2: Run tests to confirm they fail**

```
python -m pytest scripts/tests/test_ib_order_sync.py -k "process_ib_order_updates" -v
```
Expected: `ImportError: cannot import name 'process_ib_order_updates'`

- [ ] **Step 3: Implement process_ib_order_updates in order_manager.py**

Append the following two functions at the very end of `scripts/core/order_manager.py`:

```python
# ---------------------------------------------------------------------------
# IB order status synchronisation
# ---------------------------------------------------------------------------

_IB_FILL_STATUSES = {"filled"}
_IB_CANCEL_STATUSES = {"cancelled", "inactive"}  # "inactive" = rejected by IB


def _close_trade_on_exit_fill(
    db_manager,
    ib_parent_id: int,
    exit_role: str,
    fill_price: float,
    fill_ts: str,
) -> None:
    """
    Mark a trade CLOSED when its TAKE_PROFIT or STOP_LOSS order fills.

    Calculates realized_pnl = (exit - entry) * qty  (negative for stop on LONG).
    Signal direction is taken from the trades.signal column.
    """
    try:
        with sqlite3.connect(db_manager.db_file) as con:
            con.row_factory = sqlite3.Row
            trade = con.execute(
                "SELECT id, entry_price, quantity, signal FROM trades "
                "WHERE ib_parent_id=? AND status='OPEN'",
                (ib_parent_id,)
            ).fetchone()
        if trade is None:
            return

        entry_price = trade["entry_price"]
        quantity    = trade["quantity"] or 0
        signal      = (trade["signal"] or "LONG").upper()

        pnl: Optional[float] = None
        if entry_price is not None and fill_price and quantity:
            if signal == "LONG":
                pnl = (fill_price - entry_price) * quantity
            else:  # SHORT
                pnl = (entry_price - fill_price) * quantity

        r_mult: Optional[float] = None

        now_ts = _now_utc()
        with sqlite3.connect(db_manager.db_file) as con:
            con.execute(
                """UPDATE trades
                   SET status='CLOSED', exit_price=?, exit_filled_at=?,
                       close_reason=?, realized_pnl=?, r_multiple=?, updated_at=?
                   WHERE id=?""",
                (fill_price, fill_ts, exit_role, pnl, r_mult, now_ts, trade["id"]),
            )
        logger.info(
            f"order_manager: trade id={trade['id']} CLOSED via {exit_role} "
            f"fill={fill_price} pnl={pnl}"
        )
    except Exception as e:
        logger.warning(f"order_manager: _close_trade_on_exit_fill error: {e}")


def process_ib_order_updates(db_manager, ib_statuses: list) -> None:
    """
    Cross-reference IB order statuses against open DB orders and write updates.

    For each IB record:
      - Filled ENTRY  → status=FILLED_ENTRY, filled_price, filled_at; update trade.entry_price/entry_filled_at
      - Filled TP/SL  → status=FILLED; close trade via _close_trade_on_exit_fill
      - Cancelled/Inactive → status=CANCELLED

    Called by the scheduler every ~30s when ORDER_MODE != disabled.
    `ib_statuses` is a list of dicts returned by fetch_open_order_statuses().
    """
    if not ib_statuses:
        return

    # Build lookup: ib_order_id → IB status dict
    ib_map = {int(r["ib_order_id"]): r for r in ib_statuses}

    try:
        with sqlite3.connect(db_manager.db_file) as con:
            con.row_factory = sqlite3.Row
            open_orders = con.execute(
                """SELECT id, ticker, ib_order_id, ib_parent_id, order_role, status,
                          filled_price, quantity, account_type
                   FROM orders
                   WHERE ib_order_id != 0
                     AND status IN ('SUBMITTED', 'QUEUED', 'FILLED_ENTRY')"""
            ).fetchall()
    except Exception as e:
        logger.error(f"process_ib_order_updates: DB read failed: {e}")
        return

    for order in open_orders:
        ib_order_id = int(order["ib_order_id"])
        ib_rec = ib_map.get(ib_order_id)
        if ib_rec is None:
            continue  # IB has no record — might have just been submitted; skip

        ib_status_raw = (ib_rec.get("status") or "").strip()
        ib_status_lc  = ib_status_raw.lower()
        fill_price     = ib_rec.get("avg_fill_price") or 0.0
        fill_ts        = ib_rec.get("last_update") or _now_utc()
        role           = (order["order_role"] or "").upper()
        current_status = (order["status"] or "").upper()

        try:
            if ib_status_lc in _IB_FILL_STATUSES:
                if role == "ENTRY" and current_status not in ("FILLED_ENTRY", "FILLED"):
                    now_ts = _now_utc()
                    with sqlite3.connect(db_manager.db_file) as con:
                        con.execute(
                            """UPDATE orders
                               SET status='FILLED_ENTRY', filled_price=?, filled_at=?
                               WHERE id=?""",
                            (fill_price or None, fill_ts, order["id"]),
                        )
                    # Update trade: entry_price and entry_filled_at
                    try:
                        with sqlite3.connect(db_manager.db_file) as con:
                            con.execute(
                                """UPDATE trades
                                   SET entry_price=?, entry_filled_at=?, updated_at=?
                                   WHERE ib_parent_id=? AND status='OPEN' AND entry_price IS NULL""",
                                (fill_price or None, fill_ts, now_ts, order["ib_parent_id"]),
                            )
                    except Exception as te:
                        logger.warning(f"process_ib_order_updates: trade entry update failed: {te}")
                    logger.info(
                        f"order_manager: ENTRY ib#{ib_order_id} {order['ticker']} → FILLED_ENTRY @ {fill_price}"
                    )

                elif role in ("TAKE_PROFIT", "STOP_LOSS") and current_status not in ("FILLED", "CANCELLED"):
                    with sqlite3.connect(db_manager.db_file) as con:
                        con.execute(
                            "UPDATE orders SET status='FILLED', filled_price=?, filled_at=? WHERE id=?",
                            (fill_price or None, fill_ts, order["id"]),
                        )
                    _close_trade_on_exit_fill(
                        db_manager,
                        int(order["ib_parent_id"]),
                        role,
                        fill_price,
                        fill_ts,
                    )
                    logger.info(
                        f"order_manager: {role} ib#{ib_order_id} {order['ticker']} → FILLED @ {fill_price}"
                    )

            elif ib_status_lc in _IB_CANCEL_STATUSES:
                if current_status not in ("CANCELLED", "FILLED", "FILLED_ENTRY"):
                    with sqlite3.connect(db_manager.db_file) as con:
                        con.execute(
                            "UPDATE orders SET status='CANCELLED', error_message=? WHERE id=?",
                            (f"IB:{ib_status_raw}", order["id"]),
                        )
                    logger.info(
                        f"order_manager: ib#{ib_order_id} {order['ticker']} → CANCELLED ({ib_status_raw})"
                    )

        except Exception as e:
            logger.error(
                f"process_ib_order_updates: error processing ib#{ib_order_id}: {e}"
            )
```

- [ ] **Step 4: Run all new tests**

```
python -m pytest scripts/tests/test_ib_order_sync.py -v
```
Expected: all 7 tests PASS

- [ ] **Step 5: Run existing order_manager tests to check no regression**

```
python -m pytest scripts/tests/test_integration_ib_mock.py -v
```
Expected: all PASS

- [ ] **Step 6: Commit**

```
git add scripts/core/order_manager.py scripts/tests/test_ib_order_sync.py
git commit -m "feat: add process_ib_order_updates to order_manager"
```

---

### Task 3: Register sync_ib_order_statuses in scheduler.py

Wire together `fetch_open_order_statuses` + `process_ib_order_updates` as a scheduler task every 30s, skip when `ORDER_MODE=disabled`.

**Files:**
- Modify: `scripts/core/scheduler.py`

- [ ] **Step 1: Add the sync task function and register it**

In `scripts/core/scheduler.py`, after the `_order_timeout_task` function (line ~218) add:

```python
async def _sync_ib_order_statuses_task() -> None:
    """Poll IB for open order statuses and update DB. No-op when ORDER_MODE=disabled."""
    if _state.db_manager is None:
        return
    mode = _cfg("ORDER_MODE", "disabled").lower()
    if mode == "disabled":
        return
    try:
        from ib_gateway_client import fetch_open_order_statuses
        from order_manager import process_ib_order_updates
        port = 7496 if mode == "live" else 7497
        host = _cfg("IB_HOST", "127.0.0.1")
        statuses = fetch_open_order_statuses(host=host, port=port)
        if statuses:
            process_ib_order_updates(_state.db_manager, statuses)
    except Exception as e:
        logger.error(f"scheduler: sync_ib_order_statuses error: {e}")
```

Then in `start_scheduler()`, inside `task_specs`, append the new entry after `order_timeout_check`:

```python
("sync_ib_order_statuses", _sync_ib_order_statuses_task, 30, False),
```

- [ ] **Step 2: Run scheduler-related tests**

```
python -m pytest scripts/tests/ -v -k "scheduler or order" --tb=short
```
Expected: all existing tests PASS (new task is no-op when ORDER_MODE=disabled in tests)

- [ ] **Step 3: Commit**

```
git add scripts/core/scheduler.py
git commit -m "feat: register sync_ib_order_statuses scheduler task (30s)"
```

---

### Task 4: Add ORDER_MODE to IB Settings UI

Put `ORDER_MODE` combo and `LIVE_TRADING_CONFIRMED` checkbox into `_IBSettingsSubTab.Trading Settings` group for visibility.

**Files:**
- Modify: `scripts/client/gui_main.py` (`_IBSettingsSubTab._build_ui` and `_save_settings` and `load`)

- [ ] **Step 1: Add ORDER_MODE controls to _build_ui**

In `_IBSettingsSubTab._build_ui`, locate the block that starts `order_type_row = QHBoxLayout()` (around line 1880). Insert **before** that block:

```python
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Order Mode:"))
        self.order_mode_combo = QComboBox()
        self.order_mode_combo.addItems(["disabled", "paper", "live"])
        self.order_mode_combo.setMaximumWidth(200)
        mode_row.addWidget(self.order_mode_combo)
        mode_row.addWidget(QLabel("disabled = safe default; paper = IB paper account; live = real money"))
        mode_row.addStretch()
        trading_layout.addLayout(mode_row)

        live_row = QHBoxLayout()
        self.live_confirmed_cb = QCheckBox("LIVE_TRADING_CONFIRMED")
        live_row.addWidget(self.live_confirmed_cb)
        live_row.addWidget(QLabel("Must be checked to allow live order submission"))
        live_row.addStretch()
        trading_layout.addLayout(live_row)
```

- [ ] **Step 2: Save ORDER_MODE and LIVE_TRADING_CONFIRMED in _save_settings**

In `_IBSettingsSubTab._save_settings`, the `keys` dict currently has 4 entries. Add two more:

```python
        keys["ORDER_MODE"] = self.order_mode_combo.currentText()
        keys["LIVE_TRADING_CONFIRMED"] = "true" if self.live_confirmed_cb.isChecked() else "false"
```

- [ ] **Step 3: Load values in load()**

In `_IBSettingsSubTab.load`, after the existing `try:` block body, add:

```python
            order_mode = cfg_map.get("ORDER_MODE", "disabled")
            idx = self.order_mode_combo.findText(order_mode)
            self.order_mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.live_confirmed_cb.setChecked(
                cfg_map.get("LIVE_TRADING_CONFIRMED", "false").lower() == "true"
            )
```

- [ ] **Step 4: Commit**

```
git add scripts/client/gui_main.py
git commit -m "feat: add ORDER_MODE and LIVE_TRADING_CONFIRMED to IB Settings UI"
```

---

## Self-Review

| Requirement | Task |
|---|---|
| fetch_open_order_statuses polls IB openTrades | Task 1 |
| process_ib_order_updates marks ENTRY fills, updates trade.entry_price | Task 2 |
| process_ib_order_updates closes trade on TP/SL fill with realized_pnl | Task 2 |
| process_ib_order_updates handles Cancelled/Inactive | Task 2 |
| Scheduler task registered, skips disabled mode | Task 3 |
| ORDER_MODE + LIVE_TRADING_CONFIRMED in UI | Task 4 |
| Regression tests run after each task | Every task Step 4/5 |
| No-op when no open orders / empty IB list | test_process_ib_order_updates_no_crash_on_empty |
