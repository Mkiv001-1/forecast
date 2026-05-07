"""
Integration tests — portfolio risk mode end-to-end broker flow (6d).

Scope: capital_provider → position_sizer → order_manager.submit_signal

No real IB, AI, or network connections. IB gateway is mocked.
The `accounts` table is populated to simulate live IB data.

Run:  python -m pytest scripts/tests/test_integration_portfolio_risk.py -v
"""

import gc
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_ROOT = os.path.dirname(__file__)
for _p in (
    os.path.join(_ROOT, "..", "core"),
    os.path.join(_ROOT, ".."),
    os.path.join(_ROOT, "..", "shared"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _stale_ts() -> str:
    """Timestamp 2 hours in the past (older than any staleness threshold)."""
    return (datetime.now(tz=timezone.utc) - timedelta(hours=2)).isoformat()


def _create_db(
    risk_account_id: str = "DU123456",
    failsafe: str = "manual_only",
    manual_override: str = "",
    account_net_liq: float = 120_000.0,
    account_last_sync: str = None,        # None → fresh (now)
    risk_pct: float = 1.0,
    max_position_pct: float = 0.05,
    include_account: bool = True,
) -> str:
    db_file = tempfile.mktemp(suffix="_port_risk.db")
    con = sqlite3.connect(db_file)
    con.executescript("""
        CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, description TEXT);
        CREATE TABLE settings (ticker TEXT PRIMARY KEY, trading_blocked INTEGER DEFAULT 0, sector TEXT DEFAULT '');
        CREATE TABLE portfolio (ticker TEXT PRIMARY KEY, market_value REAL DEFAULT 0);
        CREATE TABLE accounts (
            account_id TEXT PRIMARY KEY,
            type TEXT,
            net_liquidation REAL,
            last_sync TEXT
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_id TEXT, ticker TEXT,
            ib_order_id INTEGER, ib_parent_id INTEGER,
            order_role TEXT, order_type TEXT, action TEXT,
            quantity REAL, limit_price REAL, stop_price REAL,
            status TEXT, account_type TEXT,
            created_at TEXT, submitted_at TEXT,
            filled_at TEXT DEFAULT '',
            spread_at_submission REAL, error_message TEXT
        );
        CREATE TABLE method_config (
            method TEXT PRIMARY KEY, timeframe_hours INTEGER NOT NULL,
            trigger TEXT DEFAULT 'both', active INTEGER DEFAULT 1,
            execute TEXT DEFAULT 'yes'
        );
        CREATE TABLE providers (
            name TEXT PRIMARY KEY, type TEXT DEFAULT 'ai',
            base_url TEXT, api_key TEXT, model TEXT,
            temperature REAL DEFAULT 0.2, max_tokens INTEGER DEFAULT 2000,
            rate_limit INTEGER DEFAULT 60, active INTEGER DEFAULT 1,
            execute TEXT DEFAULT 'yes', ema_accuracy REAL DEFAULT 0.5,
            ema_updated_at TEXT DEFAULT '', forecast_count INTEGER DEFAULT 0
        );
        CREATE TABLE Logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            forecast_date TEXT, created_at TEXT, ticker TEXT,
            method TEXT, confidence INTEGER, side TEXT,
            entry_conditions TEXT, exit_target TEXT, exit_stop TEXT,
            position_size TEXT, rationale TEXT, model TEXT, prompt TEXT,
            api_response TEXT, stop_loss REAL, rr_ratio REAL,
            timeframe_hours INTEGER, risk_amount REAL, risk_pct REAL,
            sector TEXT, sector_exposure_at_signal REAL, horizon_days INTEGER
        );
        CREATE TABLE consensus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, ticker TEXT, signal TEXT, confidence REAL,
            methods_long TEXT, methods_short TEXT, methods_neutral TEXT,
            rationale TEXT, target_price REAL, stop_loss REAL
        );
    """)

    config_rows = [
        ("RISK_MODE",                "percent_of_portfolio_on_stop",  "Risk mode"),
        ("RISK_ACCOUNT_ID",          risk_account_id,                 "IB account for portfolio"),
        ("IB_CAPITAL_FAILSAFE",      failsafe,                        "Failsafe policy"),
        ("MANUAL_CAPITAL_OVERRIDE",  manual_override,                 "Manual capital"),
        ("RISK_PERCENT_ON_STOP",     str(risk_pct),                   "Risk % on stop"),
        ("MAX_POSITION_PCT",         str(max_position_pct),           "Max pos %"),
        ("MAX_SECTOR_EXPOSURE_PCT",  "0.15",                          "Sector soft limit"),
        ("MAX_SECTOR_HARD_LIMIT_PCT","0.25",                          "Sector hard limit"),
        ("SECTOR_OVERWEIGHT_FACTOR", "0.5",                           "Sector overweight factor"),
        ("CAPITAL_STALENESS_MINUTES","15",                            "Staleness minutes"),
        ("PREFERRED_ACCOUNT_TYPE",   "paper",                         "Account type"),
        ("ORDER_MODE",               "paper",                         "Order mode"),
        ("MAX_OPEN_ORDERS",          "5",                             "Max orders"),
        ("MAX_SPREAD_PCT",           "0.005",                         "Max spread"),
        ("USE_STOP_LIMIT",           "false",                         "Stop-limit"),
        ("STOP_LIMIT_OFFSET_PCT",    "0.0005",                        "Stop-limit offset"),
        ("ALLOW_EXTENDED_HOURS",     "true",                          "Extended hours"),
        ("LIVE_TRADING_CONFIRMED",   "false",                         "Live confirmed"),
        ("DEFAULT_RISK_PCT",         "0.01",                          "Default risk"),
        ("ORDER_QUEUE_MAX_AGE_HOURS","24",                            "Order queue age"),
    ]
    con.executemany("INSERT OR REPLACE INTO config VALUES (?,?,?)", config_rows)
    con.execute("INSERT OR REPLACE INTO settings VALUES ('AAPL', 0, 'Tech')")

    if include_account:
        last_sync = account_last_sync if account_last_sync is not None else _now_utc()
        con.execute(
            "INSERT INTO accounts VALUES (?,?,?,?)",
            (risk_account_id, "paper", account_net_liq, last_sync)
        )

    con.commit()
    con.close()
    return db_file


def _cleanup(db_file: str):
    gc.collect()
    try:
        if os.path.exists(db_file):
            os.unlink(db_file)
    except OSError:
        pass


class FakeDb:
    """Thin wrapper that exposes db_file and get_config_value for capital_provider."""
    def __init__(self, db_file: str):
        self.db_file = db_file

    def get_config_value(self, key: str, default: str = None):
        with sqlite3.connect(self.db_file) as con:
            row = con.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        v = row[0] if row else None
        if v is None:
            return default
        return v


# ---------------------------------------------------------------------------
# Mock IB gateway — simulates sync_accounts_with_ib writing updated data
# ---------------------------------------------------------------------------

def _make_ib_sync(db_file: str, new_net_liq: float = 125_000.0, account_id: str = "DU123456"):
    """Return a side_effect function that updates the accounts table (simulates IB sync)."""
    def _sync(db_manager, *args, **kwargs):
        now = _now_utc()
        with sqlite3.connect(db_file) as con:
            con.execute(
                "UPDATE accounts SET net_liquidation=?, last_sync=? WHERE account_id=?",
                (new_net_liq, now, account_id)
            )
            con.commit()
        return True
    return _sync


def _mock_place_bracket_order(*args, **kwargs):
    """Minimal bracket order mock — returns IB-like response."""
    return {
        "status": "submitted",
        "parent_id": 2001,
        "target_id": 2002,
        "stop_id":   2003,
        "symbol":    kwargs.get("symbol", args[0] if args else "AAPL"),
    }


def _mock_get_bid_ask_spread(symbol, *args, **kwargs):
    return {
        "status": "ok",
        "symbol": symbol,
        "bid": 149.85,
        "ask": 150.15,
        "spread_pct": 0.002,
    }


def _ib_patches(db_file: str, new_net_liq: float = 125_000.0, account_id: str = "DU123456"):
    return [
        patch("ib_gateway_client.place_bracket_order", side_effect=_mock_place_bracket_order),
        patch("ib_gateway_client.get_bid_ask_spread",  side_effect=_mock_get_bid_ask_spread),
        patch("ib_gateway_client.sync_accounts_with_ib",
              side_effect=_make_ib_sync(db_file, new_net_liq, account_id)),
    ]


# ---------------------------------------------------------------------------
# Minimal consensus helpers
# ---------------------------------------------------------------------------

def _make_consensus(signal="LONG", target=165.0, stop=140.0, confidence=75.0):
    return {
        "signal":       signal,
        "confidence":   confidence,
        "target_price": target,
        "stop_loss":    stop,
        "methods_long": "momentum_trend",
        "methods_short": "",
        "methods_neutral": "",
        "rationale": "test",
    }


def _get_orders(db_file: str, ticker: str = "AAPL") -> List[Dict[str, Any]]:
    with sqlite3.connect(db_file) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM orders WHERE UPPER(ticker)=UPPER(?) ORDER BY id",
            (ticker,)
        ).fetchall()
    return [dict(r) for r in rows]


# ===========================================================================
# 6d-1: Fresh IB data → correct qty from portfolio risk mode
#   portfolio=120 000, risk_pct=1.0 → risk=1 200
#   entry=150, stop=140 → dist=10 → qty_by_risk=120
#   max_by_pos = 120 000 × 0.05 / 150 = 40 → qty = 40
# ===========================================================================

def test_fresh_ib_data_correct_qty_and_order_placed():
    from position_sizer import calculate_position
    from order_manager import submit_signal

    db_file = _create_db(account_net_liq=120_000.0)
    db = FakeDb(db_file)
    cons = _make_consensus()

    patches = _ib_patches(db_file)
    with patches[0], patches[1], patches[2]:
        pos = calculate_position("AAPL", 150.0, 140.0, db_manager=db)

    assert pos["status"] == "OK",          f"status: {pos['status']}"
    assert pos["risk_mode"] == "percent_of_portfolio_on_stop"
    assert pos["capital_source"] == "ib"
    assert pos["risk_amount"] == pytest.approx(1_200.0)
    assert pos["quantity"] == 40           # capped by MAX_POSITION_PCT

    with patches[0], patches[1], patches[2]:
        result = submit_signal("AAPL", cons, pos, db, log_id="6d-001")

    assert result["status"] == "SUBMITTED", f"submit: {result}"
    orders = _get_orders(db_file)
    assert len(orders) == 3               # parent + target + stop
    entry = next(o for o in orders if o["order_role"] == "ENTRY")
    assert entry["quantity"] == 40
    assert entry["action"] == "BUY"

    _cleanup(db_file)


# ===========================================================================
# 6d-2: Stale IB data → sync triggered → updated value used
#   After sync: net_liq updated to 200 000
#   max_by_pos = 200 000 × 0.05 / 150 = 66 → qty = 66
# ===========================================================================

def test_stale_data_triggers_sync_and_uses_refreshed_value():
    from position_sizer import calculate_position

    db_file = _create_db(
        account_net_liq=100_000.0,
        account_last_sync=_stale_ts(),   # stale
    )
    db = FakeDb(db_file)

    patches = _ib_patches(db_file, new_net_liq=200_000.0)
    with patches[0], patches[1], patches[2] as mock_sync:
        pos = calculate_position("AAPL", 150.0, 140.0, db_manager=db)

    assert pos["status"] == "OK"
    mock_sync.assert_called_once()         # sync was triggered
    # After refresh net_liq=200 000 → risk=2 000, qty_by_risk=200 → max=66
    assert pos["quantity"] == 66
    assert pos["risk_amount"] == pytest.approx(2_000.0)

    _cleanup(db_file)


# ===========================================================================
# 6d-3: IB unavailable + failsafe=manual_only + MANUAL_CAPITAL_OVERRIDE
#   manual=80 000 → risk=800, dist=10 → qty_by_risk=80
#   max_by_pos = 80 000 × 0.05 / 150 = 26 → qty = 26
# ===========================================================================

def test_ib_unavailable_manual_only_uses_override():
    from position_sizer import calculate_position
    from order_manager import submit_signal

    db_file = _create_db(
        failsafe="manual_only",
        manual_override="80000",
        include_account=False,            # no account in DB → IB lookup fails
    )
    db = FakeDb(db_file)
    cons = _make_consensus()

    def _fail_sync(*args, **kwargs):
        return False

    patches = _ib_patches(db_file)
    with patches[0], patches[1], patch("ib_gateway_client.sync_accounts_with_ib",
                                       side_effect=_fail_sync):
        pos = calculate_position("AAPL", 150.0, 140.0, db_manager=db)

    assert pos["status"] == "OK"
    assert pos["capital_source"] == "manual_override"
    assert pos["quantity"] == 26

    with patches[0], patches[1], patch("ib_gateway_client.sync_accounts_with_ib",
                                       side_effect=_fail_sync):
        result = submit_signal("AAPL", cons, pos, db, log_id="6d-003")

    assert result["status"] == "SUBMITTED"
    orders = _get_orders(db_file)
    assert len(orders) == 3

    _cleanup(db_file)


# ===========================================================================
# 6d-4: IB unavailable + failsafe=deny → order blocked
# ===========================================================================

def test_ib_unavailable_deny_blocks_order():
    from position_sizer import calculate_position

    db_file = _create_db(
        failsafe="deny",
        include_account=False,
    )
    db = FakeDb(db_file)

    def _fail_sync(*args, **kwargs):
        return False

    with patch("ib_gateway_client.sync_accounts_with_ib", side_effect=_fail_sync):
        pos = calculate_position("AAPL", 150.0, 140.0, db_manager=db)

    assert pos["status"] == "SKIPPED_CAPITAL_UNAVAILABLE"
    assert pos["quantity"] == 0

    # No orders should be written
    orders = _get_orders(db_file)
    assert len(orders) == 0

    _cleanup(db_file)


# ===========================================================================
# 6d-5: IB unavailable + failsafe=manual_only + no MANUAL_CAPITAL_OVERRIDE
#   → CapitalUnavailableError → SKIPPED_CAPITAL_UNAVAILABLE
# ===========================================================================

def test_ib_unavailable_manual_only_no_override_blocks():
    from position_sizer import calculate_position

    db_file = _create_db(
        failsafe="manual_only",
        manual_override="",               # not set
        include_account=False,
    )
    db = FakeDb(db_file)

    def _fail_sync(*args, **kwargs):
        return False

    with patch("ib_gateway_client.sync_accounts_with_ib", side_effect=_fail_sync):
        pos = calculate_position("AAPL", 150.0, 140.0, db_manager=db)

    assert pos["status"] == "SKIPPED_CAPITAL_UNAVAILABLE"
    assert pos["quantity"] == 0

    _cleanup(db_file)


# ===========================================================================
# 6d-6: RISK_ACCOUNT_ID not set → SKIPPED_CAPITAL_UNAVAILABLE
# ===========================================================================

def test_missing_risk_account_id_blocks():
    from position_sizer import calculate_position

    db_file = _create_db(risk_account_id="")   # empty account id
    db = FakeDb(db_file)

    pos = calculate_position("AAPL", 150.0, 140.0, db_manager=db)

    assert pos["status"] == "SKIPPED_CAPITAL_UNAVAILABLE"
    assert pos["quantity"] == 0

    _cleanup(db_file)


# ===========================================================================
# 6d-7: Zero net_liquidation in DB → capital_provider treats IB as unavailable
#   → failsafe=manual_only + no override → SKIPPED_CAPITAL_UNAVAILABLE
#   (capital_provider only returns when net_liq > 0; zero is "unavailable")
# ===========================================================================

def test_zero_net_liquidation_in_db_blocks():
    from position_sizer import calculate_position

    db_file = _create_db(account_net_liq=0.0, failsafe="manual_only", manual_override="")
    db = FakeDb(db_file)

    # Sync succeeds but account still reports 0 net_liq → treated as unavailable
    def _sync_noop(*args, **kwargs):
        return True

    with patch("ib_gateway_client.sync_accounts_with_ib", side_effect=_sync_noop):
        pos = calculate_position("AAPL", 150.0, 140.0, db_manager=db)

    # capital_provider does not return 0 — it raises CapitalUnavailableError
    # position_sizer catches it → SKIPPED_CAPITAL_UNAVAILABLE
    assert pos["status"] == "SKIPPED_CAPITAL_UNAVAILABLE"
    assert pos["quantity"] == 0

    _cleanup(db_file)


# ===========================================================================
# 6d-8: SHORT signal → action=SELL in DB orders
# ===========================================================================

def test_short_signal_uses_sell_action():
    from position_sizer import calculate_position
    from order_manager import submit_signal

    db_file = _create_db(account_net_liq=120_000.0)
    db = FakeDb(db_file)
    # SHORT: stop above entry
    cons = _make_consensus(signal="SHORT", target=130.0, stop=160.0)

    patches = _ib_patches(db_file)
    with patches[0], patches[1], patches[2]:
        pos = calculate_position("AAPL", 150.0, 160.0, db_manager=db)

    assert pos["status"] == "OK"
    assert pos["quantity"] > 0

    with patches[0], patches[1], patches[2]:
        result = submit_signal("AAPL", cons, pos, db, log_id="6d-008")

    assert result["status"] == "SUBMITTED"
    orders = _get_orders(db_file)
    entry = next(o for o in orders if o["order_role"] == "ENTRY")
    assert entry["action"] == "SELL"      # SHORT → SELL

    _cleanup(db_file)


# ===========================================================================
# 6d-9: result dict always has risk_mode and capital_source
# ===========================================================================

def test_result_dict_fields_present():
    from position_sizer import calculate_position

    db_file = _create_db(account_net_liq=120_000.0)
    db = FakeDb(db_file)

    patches = _ib_patches(db_file)
    with patches[0], patches[1], patches[2]:
        pos = calculate_position("AAPL", 150.0, 140.0, db_manager=db)

    for field in ("risk_mode", "capital_source", "risk_amount",
                  "quantity", "position_value", "sector",
                  "sector_exposure_at_signal", "status"):
        assert field in pos, f"Missing field: {field}"

    assert pos["risk_mode"] == "percent_of_portfolio_on_stop"
    assert pos["capital_source"] == "ib"

    _cleanup(db_file)


# ===========================================================================
# 6d-10: Account in DB but not matching RISK_ACCOUNT_ID → sync triggered
# ===========================================================================

def test_wrong_account_id_in_config_triggers_sync():
    from position_sizer import calculate_position

    # DB has DU999999 but config asks for DU123456
    db_file = _create_db(
        risk_account_id="DU123456",
        account_net_liq=120_000.0,
    )
    # Replace account row with a different ID
    with sqlite3.connect(db_file) as con:
        con.execute("DELETE FROM accounts")
        con.execute(
            "INSERT INTO accounts VALUES (?,?,?,?)",
            ("DU999999", "paper", 120_000.0, _now_utc())
        )
        con.commit()
    db = FakeDb(db_file)

    synced = []
    def _sync_and_insert(db_manager, *args, **kwargs):
        # Simulate IB returning the correct account after sync
        with sqlite3.connect(db_file) as con:
            con.execute(
                "INSERT OR REPLACE INTO accounts VALUES (?,?,?,?)",
                ("DU123456", "paper", 100_000.0, _now_utc())
            )
            con.commit()
        synced.append(True)
        return True

    with patch("ib_gateway_client.sync_accounts_with_ib", side_effect=_sync_and_insert):
        pos = calculate_position("AAPL", 150.0, 140.0, db_manager=db)

    assert pos["status"] == "OK"
    assert len(synced) >= 1, "sync_accounts_with_ib should have been called"
    assert pos["capital_source"] == "ib"

    _cleanup(db_file)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
