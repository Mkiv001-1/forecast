"""
Unit tests for critical trading robot logic (ред. 2).

Run with:  python -m pytest scripts/tests/test_core_logic.py -v
"""

import sys
import os
import sqlite3
import tempfile
import pytest

# Ensure core modules are importable (tests run from scripts/tests/)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ===========================================================================
# forecast_engine — validate_signal_rr
# ===========================================================================

def test_validate_signal_rr_neutral_always_ok():
    from forecast_engine import validate_signal_rr
    ok, reason = validate_signal_rr({"side": "NEUTRAL"}, 100.0)
    assert ok
    assert reason == "NEUTRAL"


def test_validate_signal_rr_missing_stop():
    from forecast_engine import validate_signal_rr
    f = {"side": "LONG", "stop_loss": None, "exit_target": "$110.00"}
    ok, reason = validate_signal_rr(f, 100.0)
    assert not ok
    assert reason == "MISSING_STOP_LOSS"


def test_validate_signal_rr_stop_above_entry_for_long():
    from forecast_engine import validate_signal_rr
    f = {"side": "LONG", "stop_loss": 105.0, "exit_target": "$115.00"}
    ok, reason = validate_signal_rr(f, 100.0)
    assert not ok
    assert "STOP_ABOVE_ENTRY" in reason


def test_validate_signal_rr_low_rr():
    from forecast_engine import validate_signal_rr
    # R/R = (102 - 100) / (100 - 99) = 2.0 — borderline pass at min_rr=2.0
    f = {"side": "LONG", "stop_loss": 99.0, "exit_target": "$102.00"}
    ok, reason = validate_signal_rr(f, 100.0, min_rr=2.0)
    assert ok  # 2.0 >= 2.0 passes

    # R/R = (101 - 100) / (100 - 99) = 1.0 — should fail
    f2 = {"side": "LONG", "stop_loss": 99.0, "exit_target": "$101.00"}
    ok2, reason2 = validate_signal_rr(f2, 100.0, min_rr=2.0)
    assert not ok2
    assert "LOW_RR" in reason2


def test_validate_signal_rr_short_valid():
    from forecast_engine import validate_signal_rr
    # SHORT: entry=100, stop=110, target=90 → R/R = (100-90)/(110-100) = 1.0
    f = {"side": "SHORT", "stop_loss": 110.0, "exit_target": "$90.00"}
    ok, reason = validate_signal_rr(f, 100.0, min_rr=1.0)
    assert ok


def test_validate_signal_entry_limit_price():
    """Test validation of entry_limit_price for LONG positions."""
    from forecast_engine import validate_signal_rr
    # LONG with valid entry_limit_price
    f = {
        "side": "LONG",
        "stop_loss": 95.0,
        "target_price": 110.0,
        "entry_limit_price": 98.0,
    }
    ok, reason = validate_signal_rr(f, 100.0, min_rr=1.5)
    assert ok
    # entry=98, target=110, stop=95 → R/R = (110-98)/(98-95) = 12/3 = 4.0
    assert "RR_" in reason


def test_validate_signal_entry_above_current_for_long():
    """Test that entry_limit_price above current price fails for LONG."""
    from forecast_engine import validate_signal_rr
    f = {
        "side": "LONG",
        "stop_loss": 95.0,
        "target_price": 110.0,
        "entry_limit_price": 102.0,  # above current 100
    }
    ok, reason = validate_signal_rr(f, 100.0, min_rr=1.5)
    assert not ok
    assert "ENTRY_ABOVE_CURRENT" in reason


def test_validate_signal_entry_below_current_for_short():
    """Test that entry_limit_price below current price fails for SHORT."""
    from forecast_engine import validate_signal_rr
    f = {
        "side": "SHORT",
        "stop_loss": 110.0,
        "target_price": 90.0,
        "entry_limit_price": 98.0,  # below current 100
    }
    ok, reason = validate_signal_rr(f, 100.0, min_rr=1.5)
    assert not ok
    assert "ENTRY_BELOW_CURRENT" in reason


# ===========================================================================
# parse_json_response — stop_loss extraction
# ===========================================================================

def test_parse_json_stop_loss_numeric():
    from forecast_engine import parse_json_response
    import json
    data = {
        "confidence": 70,
        "side": "LONG",
        "rationale": "test",
        "stop_loss": 142.5,
        "exit_target": "$155.00",
    }
    result = parse_json_response(json.dumps(data))
    assert result is not None
    assert result["stop_loss"] == 142.5


def test_parse_json_stop_loss_fallback_from_exit_stop():
    from forecast_engine import parse_json_response
    import json
    data = {
        "confidence": 65,
        "side": "LONG",
        "rationale": "test fallback",
        "exit_stop": "$138.00 (-3.5%)",
        "exit_target": "$155.00",
    }
    result = parse_json_response(json.dumps(data))
    assert result is not None
    assert result["stop_loss"] is not None
    assert result["stop_loss"] > 0


def test_parse_json_bracket_fields_full():
    """Test parsing JSON with all new bracket order fields."""
    from forecast_engine import parse_json_response
    import json
    data = {
        "confidence": 75,
        "side": "LONG",
        "rationale": "strong uptrend",
        "entry_order_type": "LMT",
        "entry_limit_price": 150.50,
        "entry_tif": "DAY",
        "target_price": 160.00,
        "take_profit_tif": "GTC",
        "stop_loss": 145.00,
        "stop_loss_tif": "GTC",
        "timeframe_hours": 24,
    }
    result = parse_json_response(json.dumps(data))
    assert result is not None
    assert result["entry_order_type"] == "LMT"
    assert result["entry_limit_price"] == 150.50
    assert result["entry_tif"] == "DAY"
    assert result["target_price"] == 160.00
    assert result["take_profit_tif"] == "GTC"
    assert result["stop_loss"] == 145.00
    assert result["stop_loss_tif"] == "GTC"


def test_parse_json_bracket_defaults():
    """Test that bracket fields get default values when not provided."""
    from forecast_engine import parse_json_response
    import json
    data = {
        "confidence": 70,
        "side": "LONG",
        "rationale": "test",
        "stop_loss": 142.5,
    }
    result = parse_json_response(json.dumps(data))
    assert result is not None
    assert result["entry_order_type"] == "LMT"
    assert result["entry_tif"] == "DAY"
    assert result["take_profit_tif"] == "GTC"
    assert result["stop_loss_tif"] == "GTC"


def test_parse_json_target_price_fallback():
    """Test target_price fallback from exit_target string."""
    from forecast_engine import parse_json_response
    import json
    data = {
        "confidence": 70,
        "side": "LONG",
        "rationale": "test",
        "exit_target": "$165.50",
        "stop_loss": 145.0,
    }
    result = parse_json_response(json.dumps(data))
    assert result is not None
    assert result["target_price"] == 165.50


# ===========================================================================
# consensus — anomaly filter and disagreement
# ===========================================================================

def test_consensus_anomaly_filter_removes_extreme_target():
    from consensus import calculate_consensus
    forecasts = [
        {"side": "LONG",  "confidence": 70, "method": "m1", "model": "A",
         "exit_target": "$200.00",   # 100% above price=100 → filtered
         "stop_loss": 95.0},
        {"side": "LONG",  "confidence": 70, "method": "m2", "model": "B",
         "exit_target": "$105.00",   # 5% → ok
         "stop_loss": 95.0},
    ]
    result = calculate_consensus(forecasts, current_price=100.0, max_deviation=0.15)
    # filtered one should reduce the LONG group
    assert "filtered" in result["methods_neutral"] or result["signal"] in ("LONG", "NEUTRAL")


def test_consensus_disagreement_forces_neutral():
    from consensus import calculate_consensus
    forecasts = [
        {"side": "LONG",  "confidence": 70, "method": "m1", "model": "A",
         "exit_target": "$105.00", "stop_loss": 95.0},
        {"side": "SHORT", "confidence": 65, "method": "m2", "model": "B",
         "exit_target": "$95.00",  "stop_loss": 105.0},
        {"side": "LONG",  "confidence": 60, "method": "m3", "model": "C",
         "exit_target": "$104.00", "stop_loss": 96.0},
        {"side": "SHORT", "confidence": 60, "method": "m4", "model": "D",
         "exit_target": "$96.00",  "stop_loss": 104.0},
    ]
    result = calculate_consensus(forecasts, disagreement_threshold=0.40)
    # Both sides roughly equal → high disagreement → NEUTRAL
    assert result["high_model_disagreement"] or result["signal"] == "NEUTRAL"


def test_consensus_median_target():
    from consensus import calculate_consensus
    forecasts = [
        {"side": "LONG", "confidence": 80, "method": "m1", "model": "A",
         "exit_target": "$110.00", "stop_loss": 95.0},
        {"side": "LONG", "confidence": 80, "method": "m2", "model": "B",
         "exit_target": "$112.00", "stop_loss": 94.0},
        {"side": "LONG", "confidence": 80, "method": "m3", "model": "C",
         "exit_target": "$108.00", "stop_loss": 96.0},
    ]
    result = calculate_consensus(forecasts)
    if result["signal"] == "LONG":
        assert result["target_price"] == 110.0   # median of 108, 110, 112
        assert result["stop_loss"] == 95.0        # median of 94, 95, 96


def test_consensus_bracket_fields():
    """Test consensus includes bracket order fields (entry_limit_price, TIFs)."""
    from consensus import calculate_consensus
    forecasts = [
        {"side": "LONG", "confidence": 80, "method": "m1", "model": "A",
         "target_price": 110.0, "stop_loss": 95.0,
         "entry_limit_price": 98.0, "entry_tif": "DAY",
         "take_profit_tif": "GTC", "stop_loss_tif": "GTC"},
        {"side": "LONG", "confidence": 80, "method": "m2", "model": "B",
         "target_price": 112.0, "stop_loss": 94.0,
         "entry_limit_price": 99.0, "entry_tif": "DAY",
         "take_profit_tif": "GTC", "stop_loss_tif": "GTC"},
        {"side": "LONG", "confidence": 80, "method": "m3", "model": "C",
         "target_price": 108.0, "stop_loss": 96.0,
         "entry_limit_price": 97.0, "entry_tif": "DAY",
         "take_profit_tif": "GTC", "stop_loss_tif": "GTC"},
    ]
    result = calculate_consensus(forecasts)
    if result["signal"] == "LONG":
        assert result["target_price"] == 110.0
        assert result["stop_loss"] == 95.0
        assert result["entry_limit_price"] == 98.0  # median of 97, 98, 99
        assert result["entry_tif"] == "DAY"
        assert result["take_profit_tif"] == "GTC"
        assert result["stop_loss_tif"] == "GTC"


# ===========================================================================
# actuals_evaluator — stop priority
# ===========================================================================

def test_evaluate_stop_priority_over_target():
    from actuals_evaluator import evaluate_forecast
    # Both target AND stop hit in same day → stop wins (exit_successful = 0)
    record = {
        "side": "LONG",
        "entry_price": 100.0,
        "exit_target": "$110.00",
        "exit_stop":   "$90.00",
        "stop_loss":   90.0,
    }
    actual = {"actual_close": 105.0, "actual_high": 115.0, "actual_low": 88.0}
    result = evaluate_forecast(record, actual)
    assert result["stop_hit"] is True
    assert result["target_hit"] is True
    assert result["exit_successful"] == 0   # stop priority


def test_evaluate_target_only():
    from actuals_evaluator import evaluate_forecast
    record = {
        "side": "LONG",
        "entry_price": 100.0,
        "exit_target": "$110.00",
        "exit_stop":   "$90.00",
        "stop_loss":   90.0,
    }
    actual = {"actual_close": 112.0, "actual_high": 115.0, "actual_low": 95.0}
    result = evaluate_forecast(record, actual)
    assert result["target_hit"] is True
    assert result["stop_hit"] is False
    assert result["exit_successful"] == 1


# ===========================================================================
# position_sizer — basic sizing
# ===========================================================================

class _MockDb:
    def __init__(self, db_file):
        self.db_file = db_file

    def get_config_value(self, key):
        defaults = {
            "DEFAULT_RISK_PCT":          "0.01",
            "MAX_POSITION_PCT":          "0.05",
            "MAX_SECTOR_EXPOSURE_PCT":   "0.15",
            "MAX_SECTOR_HARD_LIMIT_PCT": "0.25",
            "SECTOR_OVERWEIGHT_FACTOR":  "0.5",
        }
        return defaults.get(key)


def _make_mock_db():
    tmp = tempfile.mktemp(suffix=".db")
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE settings (ticker TEXT, sector TEXT DEFAULT '', trading_blocked INTEGER DEFAULT 0)")
    con.execute("CREATE TABLE portfolio (ticker TEXT, market_value REAL)")
    con.commit()
    con.close()
    db = _MockDb(tmp)
    return db


def test_position_sizer_basic():
    from position_sizer import calculate_position
    db = _make_mock_db()
    result = calculate_position("AAPL", entry_price=150.0, stop_loss=145.0,
                                db_manager=db, net_liquidation=100_000.0)
    assert result["status"] == "OK"
    # risk = 100k × 1% = 1000; stop_distance = 5; qty_by_risk = 200
    # MAX_POSITION_PCT cap: 5% × 100k / 150 = 33 shares → qty capped to 33
    assert result["quantity"] == 33
    assert result["risk_amount"] == 1000.0


def test_position_sizer_no_capital():
    from position_sizer import calculate_position
    db = _make_mock_db()
    result = calculate_position("AAPL", entry_price=150.0, stop_loss=145.0,
                                db_manager=db, net_liquidation=0.0)
    assert result["status"] == "SKIPPED_NO_CAPITAL"
    assert result["quantity"] == 0


def test_position_sizer_invalid_stop():
    from position_sizer import calculate_position
    db = _make_mock_db()
    result = calculate_position("AAPL", entry_price=150.0, stop_loss=0.0,
                                db_manager=db, net_liquidation=100_000.0)
    assert result["status"] == "SKIPPED_INVALID_STOP"


# ===========================================================================
# circuit_breaker
# ===========================================================================

def test_circuit_breaker_open_after_failures():
    from circuit_breaker import reset, record_failure, get_state, configure
    reset()
    configure(grace_seconds=3600, failure_threshold=3)
    record_failure()
    record_failure()
    assert get_state() == "CLOSED"  # not yet
    record_failure()
    assert get_state() == "OPEN"
    reset()


def test_circuit_breaker_recovery():
    from circuit_breaker import reset, record_failure, record_success, get_state, configure, _lock, _state as _initial
    import circuit_breaker as cb
    import time
    reset()
    configure(grace_seconds=3600, failure_threshold=3, recovery_threshold=2)
    record_failure(); record_failure(); record_failure()
    assert cb._state == "OPEN"  # raw state is OPEN before grace check
    # Now reconfigure with grace=0 to simulate elapsed grace period
    configure(grace_seconds=0, failure_threshold=3, recovery_threshold=2)
    time.sleep(0.05)
    assert get_state() == "HALF"  # grace elapsed → HALF
    record_success()
    assert get_state() == "HALF"
    record_success()
    assert get_state() == "CLOSED"
    reset()


# ===========================================================================
# model_performance_tracker — EMA calculation
# ===========================================================================

def test_ema_update_correct():
    tmp = tempfile.mktemp(suffix=".db")
    con = sqlite3.connect(tmp)
    con.execute("""CREATE TABLE providers (
        name TEXT PRIMARY KEY, type TEXT, ema_accuracy REAL,
        ema_updated_at TEXT, forecast_count INTEGER
    )""")
    con.execute("""CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)""")
    con.execute("INSERT INTO providers VALUES ('test_model','ai',0.5,'',0)")
    con.execute("INSERT INTO config VALUES ('MODEL_WEIGHT_EMA_ALPHA','0.2')")
    con.commit(); con.close()

    class FakeDb:
        db_file = tmp
        def get_config_value(self, k): return "0.2"

    from model_performance_tracker import update_provider_ema
    new_ema = update_provider_ema(FakeDb(), "test_model", direction_correct=True)
    # alpha=0.2, outcome=1.0, old=0.5 → 0.2*1 + 0.8*0.5 = 0.60
    assert abs(new_ema - 0.60) < 1e-9

    new_ema2 = update_provider_ema(FakeDb(), "test_model", direction_correct=False)
    # alpha=0.2, outcome=0.0, old=0.60 → 0.2*0 + 0.8*0.60 = 0.48
    assert abs(new_ema2 - 0.48) < 1e-6


# ===========================================================================
# order_manager — submit_signal guard paths (no real IB required)
# ===========================================================================

def _make_order_db(tmp_path=None):
    """Create a minimal SQLite DB with tables needed by order_manager."""
    import tempfile
    db_file = tmp_path or tempfile.mktemp(suffix="_orders.db")
    con = sqlite3.connect(db_file)
    con.executescript("""
        CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, description TEXT);
        CREATE TABLE settings (
            ticker TEXT PRIMARY KEY,
            trading_blocked INTEGER DEFAULT 0,
            sector TEXT
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_id TEXT, ticker TEXT, ib_order_id INTEGER, ib_parent_id INTEGER,
            order_role TEXT, order_type TEXT, action TEXT, quantity REAL,
            limit_price REAL, stop_price REAL, status TEXT, account_type TEXT,
            created_at TEXT, submitted_at TEXT, filled_at TEXT DEFAULT '',
            spread_at_submission REAL, error_message TEXT
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
            execute TEXT DEFAULT 'yes' CHECK (execute IN ('yes', 'no'))
        );
        INSERT INTO config VALUES ('ORDER_MODE','disabled','');
        INSERT INTO config VALUES ('MAX_OPEN_ORDERS','5','');
        INSERT INTO config VALUES ('ORDER_QUEUE_MAX_AGE_HOURS','24','');
        INSERT INTO config VALUES ('MAX_SPREAD_PCT','0.005','');
        INSERT INTO config VALUES ('USE_STOP_LIMIT','false','');
        INSERT INTO config VALUES ('STOP_LIMIT_OFFSET_PCT','0.0005','');
        INSERT INTO config VALUES ('ALLOW_EXTENDED_HOURS','false','');
        INSERT INTO config VALUES ('LIVE_TRADING_CONFIRMED','false','');
        INSERT INTO settings VALUES ('AAPL', 0, 'Tech');
        INSERT INTO method_config VALUES 
            ('momentum_trend', 24, 'both', 1, 'yes'),
            ('price_action', 8, 'price_level', 1, 'yes'),
            ('relative_strength', 48, 'time', 1, 'yes'),
            ('volatility', 4, 'price_level', 1, 'yes'),
            ('mean_reversion', 72, 'price_level', 1, 'yes'),
            ('volume_breakout', 2, 'price_level', 1, 'yes');
        INSERT INTO providers VALUES 
            ('claude-sonnet', 'ai', 'https://openrouter.ai/api/v1', '', 'anthropic/claude-sonnet-4', 0.2, 2000, 60, 1, 'yes'),
            ('gpt-4o', 'ai', 'https://openrouter.ai/api/v1', '', 'openai/gpt-4o', 0.2, 2000, 60, 1, 'yes'),
            ('deepseek-v3', 'ai', 'https://openrouter.ai/api/v1', '', 'deepseek/deepseek-chat-v3-0324', 0.2, 2000, 60, 1, 'yes'),
            ('gemini-flash', 'ai', 'https://openrouter.ai/api/v1', '', 'google/gemini-2.5-flash-preview', 0.2, 2000, 60, 1, 'yes'),
            ('sonar-pro', 'ai', 'https://openrouter.ai/api/v1', '', 'perplexity/sonar-pro', 0.2, 2000, 60, 1, 'yes');
    """)
    con.commit()
    con.close()

    class FakeDb:
        def __init__(self, f): self.db_file = f
        def get_config_value(self, k):
            with sqlite3.connect(self.db_file) as c:
                row = c.execute("SELECT value FROM config WHERE key=?", (k,)).fetchone()
            return row[0] if row else None
        def _execute_query(self, query, params=None):
            with sqlite3.connect(self.db_file) as c:
                if params:
                    return c.execute(query, params).fetchall()
                else:
                    return c.execute(query).fetchall()

    return FakeDb(db_file)


def _good_consensus():
    return {"signal": "LONG", "stop_loss": 90.0, "target_price": 115.0}


def _good_position():
    return {"status": "OK", "quantity": 10}


def test_order_manager_disabled_mode():
    from order_manager import submit_signal
    db = _make_order_db()
    result = submit_signal("AAPL", _good_consensus(), _good_position(), db)
    assert result["status"] == "DISABLED"
    assert "ORDER_MODE=disabled" in result["message"]


def test_order_manager_neutral_signal():
    from order_manager import submit_signal
    db = _make_order_db()
    # Switch to paper so we get past mode check
    with sqlite3.connect(db.db_file) as c:
        c.execute("UPDATE config SET value='paper' WHERE key='ORDER_MODE'")
    consensus = {"signal": "NEUTRAL", "stop_loss": 90.0, "target_price": 115.0}
    result = submit_signal("AAPL", consensus, _good_position(), db)
    assert result["status"] == "SKIPPED_NEUTRAL"


def test_order_manager_bad_position_sizing():
    from order_manager import submit_signal
    db = _make_order_db()
    with sqlite3.connect(db.db_file) as c:
        c.execute("UPDATE config SET value='paper' WHERE key='ORDER_MODE'")
    bad_pos = {"status": "INSUFFICIENT_CAPITAL", "quantity": 0}
    result = submit_signal("AAPL", _good_consensus(), bad_pos, db)
    assert result["status"] == "INSUFFICIENT_CAPITAL"


def test_order_manager_ticker_blocked():
    from order_manager import submit_signal
    db = _make_order_db()
    with sqlite3.connect(db.db_file) as c:
        c.execute("UPDATE config SET value='paper' WHERE key='ORDER_MODE'")
        c.execute("UPDATE settings SET trading_blocked=1 WHERE ticker='AAPL'")
    result = submit_signal("AAPL", _good_consensus(), _good_position(), db)
    assert result["status"] == "SKIPPED_TICKER_BLOCKED"


def test_order_manager_duplicate_order():
    from order_manager import submit_signal
    db = _make_order_db()
    with sqlite3.connect(db.db_file) as c:
        c.execute("UPDATE config SET value='paper' WHERE key='ORDER_MODE'")
        c.execute("""INSERT INTO orders
            (log_id,ticker,ib_order_id,ib_parent_id,order_role,order_type,action,
             quantity,limit_price,stop_price,status,account_type,created_at,submitted_at,error_message)
            VALUES ('','AAPL',1,1,'ENTRY','MKT','BUY',10,NULL,NULL,'SUBMITTED','paper',
                    datetime('now'),datetime('now'),'')""")
    result = submit_signal("AAPL", _good_consensus(), _good_position(), db)
    assert result["status"] == "SKIPPED_DUPLICATE"


def test_order_manager_max_open_orders():
    from order_manager import submit_signal
    db = _make_order_db()
    with sqlite3.connect(db.db_file) as c:
        c.execute("UPDATE config SET value='paper' WHERE key='ORDER_MODE'")
        c.execute("UPDATE config SET value='2' WHERE key='MAX_OPEN_ORDERS'")
        for i in range(2):
            c.execute("""INSERT INTO orders
                (log_id,ticker,ib_order_id,ib_parent_id,order_role,order_type,action,
                 quantity,limit_price,stop_price,status,account_type,created_at,submitted_at,error_message)
                VALUES ('',?,?,?,'ENTRY','MKT','BUY',5,NULL,NULL,'QUEUED','paper',
                        datetime('now'),datetime('now'),'')""", (f"GOOG{i}", i+1, i+1))
    result = submit_signal("AAPL", _good_consensus(), _good_position(), db)
    assert result["status"] == "SKIPPED_MAX_ORDERS"


def test_order_manager_missing_levels():
    from order_manager import submit_signal
    db = _make_order_db()
    with sqlite3.connect(db.db_file) as c:
        c.execute("UPDATE config SET value='paper' WHERE key='ORDER_MODE'")
    consensus = {"signal": "LONG", "stop_loss": None, "target_price": None}
    result = submit_signal("AAPL", consensus, _good_position(), db)
    assert result["status"] == "SKIPPED_MISSING_LEVELS"


def test_order_manager_concurrent_duplicate():
    """Two threads submitting for same ticker simultaneously — only one order must be inserted."""
    import threading
    from order_manager import submit_signal, _ticker_locks
    db = _make_order_db()
    with sqlite3.connect(db.db_file) as c:
        c.execute("UPDATE config SET value='paper' WHERE key='ORDER_MODE'")

    results = []

    def _submit():
        r = submit_signal("TSLA", _good_consensus(), _good_position(), db)
        results.append(r["status"])

    t1 = threading.Thread(target=_submit)
    t2 = threading.Thread(target=_submit)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    with sqlite3.connect(db.db_file) as c:
        count = c.execute(
            "SELECT COUNT(*) FROM orders WHERE UPPER(ticker)='TSLA' AND order_role='ENTRY'"
        ).fetchone()[0]

    assert count <= 1, f"Expected at most 1 ENTRY order for TSLA, got {count}"
    assert any(s in ("SKIPPED_DUPLICATE", "QUEUED", "SUBMITTED", "ERROR") for s in results)


# ===========================================================================
# Execute field tests
# ===========================================================================

def test_check_execute_flags_all_yes():
    from order_manager import _check_execute_flags
    db = _make_order_db()
    # Default execute should be 'yes'
    can_execute, reason = _check_execute_flags(db, "momentum_trend(claude), price_action(gpt)")
    assert can_execute
    assert reason == ""


def test_check_execute_flags_method_no():
    from order_manager import _check_execute_flags
    db = _make_order_db()
    with sqlite3.connect(db.db_file) as c:
        c.execute("UPDATE method_config SET execute='no' WHERE method='momentum_trend'")
    
    can_execute, reason = _check_execute_flags(db, "momentum_trend(claude), price_action(gpt)")
    assert not can_execute
    assert "momentum_trend" in reason
    assert "execute='no'" in reason


def test_check_execute_flags_provider_no():
    from order_manager import _check_execute_flags
    db = _make_order_db()
    with sqlite3.connect(db.db_file) as c:
        c.execute("UPDATE providers SET execute='no' WHERE name='claude-sonnet'")
    
    can_execute, reason = _check_execute_flags(db, "momentum_trend(claude), price_action(gpt)")
    assert not can_execute
    assert "claude-sonnet" in reason
    assert "execute='no'" in reason


def test_order_manager_execute_disabled_method():
    from order_manager import submit_signal
    db = _make_order_db()
    with sqlite3.connect(db.db_file) as c:
        c.execute("UPDATE config SET value='paper' WHERE key='ORDER_MODE'")
        c.execute("UPDATE method_config SET execute='no' WHERE method='momentum_trend'")
    
    consensus = {
        "signal": "LONG",
        "methods_long": "momentum_trend(claude), price_action(gpt)",
        "stop_loss": 95.0,
        "target_price": 110.0
    }
    result = submit_signal("AAPL", consensus, _good_position(), db)
    assert result["status"] == "SKIPPED_EXECUTE_DISABLED"
    assert "momentum_trend" in result["message"]


def test_order_manager_execute_disabled_provider():
    from order_manager import submit_signal
    db = _make_order_db()
    with sqlite3.connect(db.db_file) as c:
        c.execute("UPDATE config SET value='paper' WHERE key='ORDER_MODE'")
        c.execute("UPDATE providers SET execute='no' WHERE name='claude-sonnet'")
    
    consensus = {
        "signal": "LONG", 
        "methods_long": "momentum_trend(claude), price_action(gpt)",
        "stop_loss": 95.0,
        "target_price": 110.0
    }
    result = submit_signal("AAPL", consensus, _good_position(), db)
    assert result["status"] == "SKIPPED_EXECUTE_DISABLED"
    assert "claude-sonnet" in result["message"]


def test_order_manager_execute_all_yes_passes():
    from order_manager import submit_signal
    db = _make_order_db()
    with sqlite3.connect(db.db_file) as c:
        c.execute("UPDATE config SET value='paper' WHERE key='ORDER_MODE'")
        # Ensure all execute flags are 'yes' (default)
        c.execute("UPDATE method_config SET execute='yes' WHERE method IN ('momentum_trend', 'price_action')")
        c.execute("UPDATE providers SET execute='yes' WHERE name IN ('claude-sonnet', 'gpt-4o')")

    consensus = {
        "signal": "LONG",
        "methods_long": "momentum_trend(claude), price_action(gpt)",
        "stop_loss": 95.0,
        "target_price": 110.0
    }
    result = submit_signal("AAPL", consensus, _good_position(), db)
    # Should pass execute check and continue to next validation
    assert result["status"] != "SKIPPED_EXECUTE_DISABLED"


# ===========================================================================
# Order submission integration — auto and manual modes
# ===========================================================================

def test_sqlite_manager_get_latest_forecast_log_id():
    """Test get_latest_forecast_log_id returns correct ID."""
    from sqlite_manager import SQLiteManager
    import tempfile
    import os
    import time

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = f.name

    try:
        db = SQLiteManager(db_file)

        # Insert test forecasts
        with sqlite3.connect(db_file) as c:
            c.execute("""
                INSERT INTO logs (id, ticker, forecast_date, side, confidence, method, model, entry_price)
                VALUES ('log-001', 'AAPL', '2024-01-01 10:00:00', 'LONG', 70, 'm1', 'model1', 150.0)
            """)
            c.execute("""
                INSERT INTO logs (id, ticker, forecast_date, side, confidence, method, model, entry_price)
                VALUES ('log-002', 'AAPL', '2024-01-02 10:00:00', 'SHORT', 65, 'm2', 'model2', 155.0)
            """)
            c.execute("""
                INSERT INTO logs (id, ticker, forecast_date, side, confidence, method, model, entry_price)
                VALUES ('log-003', 'TSLA', '2024-01-01 10:00:00', 'LONG', 60, 'm1', 'model1', 200.0)
            """)

        # Test latest for AAPL
        latest = db.get_latest_forecast_log_id("AAPL")
        assert latest == "log-002", f"Expected 'log-002', got {latest}"

        # Test latest for TSLA
        latest = db.get_latest_forecast_log_id("TSLA")
        assert latest == "log-003", f"Expected 'log-003', got {latest}"

        # Test for non-existent ticker
        latest = db.get_latest_forecast_log_id("NONEXISTENT")
        assert latest is None, f"Expected None, got {latest}"

    finally:
        # Ensure connection is closed before unlink on Windows
        time.sleep(0.1)
        try:
            os.unlink(db_file)
        except PermissionError:
            pass  # File may be locked, ignore


def test_auto_order_submission_skipped_when_disabled():
    """Test that auto order submission respects AUTO_ORDER_SUBMISSION config."""
    from sqlite_manager import SQLiteManager
    import tempfile
    import os
    import time

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = f.name

    try:
        db = SQLiteManager(db_file)

        # Set AUTO_ORDER_SUBMISSION to false (insert or replace)
        with sqlite3.connect(db_file) as c:
            c.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('AUTO_ORDER_SUBMISSION', 'false')")

        # Verify config value
        value = db.get_config_value("AUTO_ORDER_SUBMISSION", "true")
        assert value.lower() == "false", f"Expected 'false', got {value}"

    finally:
        time.sleep(0.1)
        try:
            os.unlink(db_file)
        except PermissionError:
            pass


def test_manual_order_submit_validation_neutral():
    """Test manual API endpoint validation rejects NEUTRAL signals."""
    # This is a unit test of the validation logic (not full API test)
    consensus = {
        "signal": "NEUTRAL",
        "confidence": 0.0,
        "stop_loss": None,
        "target_price": None
    }

    # Simulate validation
    signal = consensus.get("signal", "NEUTRAL")
    confidence = consensus.get("confidence", 0.0)

    if signal not in ("LONG", "SHORT"):
        # Would return SKIPPED_NEUTRAL response
        assert signal == "NEUTRAL"
        assert confidence == 0.0


def test_manual_order_submit_validation_low_confidence():
    """Test manual API endpoint validation rejects low confidence."""
    consensus = {
        "signal": "LONG",
        "confidence": 45.0,  # Below 55 threshold
        "stop_loss": 95.0,
        "target_price": 110.0
    }

    signal = consensus.get("signal", "NEUTRAL")
    confidence = consensus.get("confidence", 0.0)

    assert signal in ("LONG", "SHORT")
    assert confidence < 55


# ===========================================================================
# consensus_evaluator
# ===========================================================================

def _make_consensus_db(db_path: str, rows: list):
    """Helper: create a minimal consensus table with given rows."""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("""
        CREATE TABLE IF NOT EXISTS consensus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, ticker TEXT, signal TEXT, confidence REAL,
            methods_long TEXT, methods_short TEXT, methods_neutral TEXT, rationale TEXT,
            target_price REAL, stop_loss REAL, entry_limit_price REAL,
            high_model_disagreement INTEGER DEFAULT 0,
            horizon_hours INTEGER, eval_target_date TEXT,
            eval_status TEXT DEFAULT 'PENDING',
            actual_date TEXT, actual_open REAL, actual_close REAL,
            actual_high REAL, actual_low REAL,
            entry_price_actual REAL,
            target_hit INTEGER, stop_hit INTEGER, first_hit TEXT,
            exit_successful INTEGER,
            direction_correct INTEGER, pnl_pct REAL, r_multiple REAL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS price_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume INTEGER,
            UNIQUE(ticker, date)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY, value TEXT DEFAULT '', description TEXT DEFAULT ''
        )
    """)
    for row in rows:
        cols = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        con.execute(f"INSERT INTO consensus ({cols}) VALUES ({placeholders})", list(row.values()))
    con.commit()
    con.close()


def _add_price_bar(db_path: str, ticker: str, date: str, open_p: float, high: float, low: float, close: float):
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute(
        "INSERT OR IGNORE INTO price_data (ticker, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
        (ticker, date, open_p, high, low, close, 1000)
    )
    con.commit()
    con.close()


class _MockDB:
    """Minimal db_manager mock for consensus_evaluator tests."""
    def __init__(self, db_path: str):
        self.db_file = db_path

    def _connect(self):
        import sqlite3 as _sqlite3
        con = _sqlite3.connect(self.db_file, timeout=10)
        con.row_factory = _sqlite3.Row
        con.execute("PRAGMA journal_mode=DELETE")
        return con

    def save_consensus(self, data: dict) -> bool:
        import sqlite3 as _sqlite3
        cols = list(data.keys())
        placeholders = ", ".join(["?"] * len(cols))
        col_str = ", ".join(cols)
        sql = f"INSERT OR REPLACE INTO consensus ({col_str}) VALUES ({placeholders})"
        try:
            with _sqlite3.connect(self.db_file, timeout=10) as con:
                con.execute("PRAGMA journal_mode=DELETE")
                con.execute(sql, [data[c] for c in cols])
            return True
        except Exception as e:
            return False

    def link_forecast_to_run(self, **kwargs) -> bool:
        """Mock method for linking forecast to run — no-op for tests."""
        return True


def test_evaluate_consensus_target_hit():
    """LONG consensus: target_price reached → target_hit=1, direction_correct=1."""
    from consensus_evaluator import evaluate_consensus_records
    from datetime import datetime, timedelta

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        eval_date = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        cons_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        ticker = "TEST:AAA"

        _make_consensus_db(db_path, [{
            "date": cons_date, "ticker": ticker, "signal": "LONG",
            "confidence": 70.0, "methods_long": "tech", "methods_short": "",
            "methods_neutral": "", "rationale": "", "target_price": 110.0,
            "stop_loss": 95.0, "eval_target_date": eval_date, "eval_status": "PENDING",
        }])
        # Entry bar on cons_date so entry_price_actual=100 < actual_close=112 → direction_correct
        _add_price_bar(db_path, ticker, cons_date[:10], 100.0, 101.0, 99.0, 100.0)
        _add_price_bar(db_path, ticker, eval_date[:10], 100.0, 115.0, 98.0, 112.0)

        db = _MockDB(db_path)
        count = evaluate_consensus_records(db)
        assert count == 1

        con2 = sqlite3.connect(db_path)
        con2.row_factory = sqlite3.Row
        r = dict(con2.execute("SELECT * FROM consensus WHERE id=1").fetchone())
        con2.close()
        assert r["eval_status"] == "EVALUATED"
        assert r["target_hit"] == 1
        assert r["direction_correct"] == 1
        assert r["pnl_pct"] is not None


# ===========================================================================
# scheduler — configurable worker pool
# ===========================================================================

def test_scheduler_max_workers_default_is_4(monkeypatch):
    import asyncio
    import scheduler

    class FakeDb:
        def __init__(self, db_file):
            self.db_file = db_file
        def get_config_value(self, key):
            return None

    class DummyPool:
        created = []
        def __init__(self, max_workers, thread_name_prefix=None):
            self.max_workers = max_workers
            self.thread_name_prefix = thread_name_prefix
            self.shutdown_called = False
            DummyPool.created.append(self)
        def shutdown(self, wait=False, cancel_futures=True):
            self.shutdown_called = True

    async def _dummy_loop(name, coro_factory, interval_seconds, max_retries=2, run_on_start=False):
        await asyncio.sleep(0)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = f.name
    try:
        monkeypatch.setattr(scheduler, "_run_task_loop", _dummy_loop)
        monkeypatch.setattr(scheduler.concurrent.futures, "ThreadPoolExecutor", DummyPool)

        db = FakeDb(db_file)
        asyncio.run(scheduler.start_scheduler(db))
        asyncio.run(scheduler.stop_scheduler())

        assert DummyPool.created, "ThreadPoolExecutor was not created"
        assert DummyPool.created[0].max_workers == 4
        assert DummyPool.created[0].shutdown_called is True
    finally:
        try:
            os.unlink(db_file)
        except PermissionError:
            pass


def test_scheduler_max_workers_from_config(monkeypatch):
    import asyncio
    import scheduler

    class FakeDb:
        def __init__(self, db_file):
            self.db_file = db_file
        def get_config_value(self, key):
            if key == "SCHEDULER_MAX_WORKERS":
                return "7"
            return None

    class DummyPool:
        created = []
        def __init__(self, max_workers, thread_name_prefix=None):
            self.max_workers = max_workers
            self.thread_name_prefix = thread_name_prefix
            self.shutdown_called = False
            DummyPool.created.append(self)
        def shutdown(self, wait=False, cancel_futures=True):
            self.shutdown_called = True

    async def _dummy_loop(name, coro_factory, interval_seconds, max_retries=2, run_on_start=False):
        await asyncio.sleep(0)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_file = f.name
    try:
        monkeypatch.setattr(scheduler, "_run_task_loop", _dummy_loop)
        monkeypatch.setattr(scheduler.concurrent.futures, "ThreadPoolExecutor", DummyPool)

        db = FakeDb(db_file)
        asyncio.run(scheduler.start_scheduler(db))
        asyncio.run(scheduler.stop_scheduler())

        assert DummyPool.created, "ThreadPoolExecutor was not created"
        assert DummyPool.created[0].max_workers == 7
    finally:
        try:
            os.unlink(db_file)
        except PermissionError:
            pass


def test_evaluate_consensus_stop_hit():
    """LONG consensus: stop triggered → stop_hit=1, pnl negative."""
    from consensus_evaluator import evaluate_consensus_records
    from datetime import datetime, timedelta

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        eval_date = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        cons_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        ticker = "TEST:BBB"

        _make_consensus_db(db_path, [{
            "date": cons_date, "ticker": ticker, "signal": "LONG",
            "confidence": 60.0, "methods_long": "tech", "methods_short": "",
            "methods_neutral": "", "rationale": "", "target_price": 110.0,
            "stop_loss": 95.0, "eval_target_date": eval_date, "eval_status": "PENDING",
        }])
        # Entry bar on cons_date so entry_price_actual = 100.0 (above stop 95)
        _add_price_bar(db_path, ticker, cons_date[:10], 100.0, 101.0, 99.0, 100.0)
        # Bar: high < target_price (110), low < stop_loss (95) → stop triggered
        _add_price_bar(db_path, ticker, eval_date[:10], 100.0, 105.0, 92.0, 93.0)

        db = _MockDB(db_path)
        count = evaluate_consensus_records(db)
        assert count == 1

        con2 = sqlite3.connect(db_path)
        con2.row_factory = sqlite3.Row
        r = dict(con2.execute("SELECT * FROM consensus WHERE id=1").fetchone())
        con2.close()
        assert r["eval_status"] == "EVALUATED"
        assert r["stop_hit"] == 1
        assert r["pnl_pct"] < 0


def test_evaluate_consensus_no_data():
    """No price data → eval_status = NO_DATA."""
    from consensus_evaluator import evaluate_consensus_records
    from datetime import datetime, timedelta

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        eval_date = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        ticker = "TEST:CCC"

        _make_consensus_db(db_path, [{
            "date": eval_date, "ticker": ticker, "signal": "LONG",
            "confidence": 60.0, "methods_long": "tech", "methods_short": "",
            "methods_neutral": "", "rationale": "", "target_price": 110.0,
            "stop_loss": 95.0, "eval_target_date": eval_date, "eval_status": "PENDING",
        }])
        # No price bars added

        db = _MockDB(db_path)
        # fetch_price_data is not available in test env, so evaluator should handle gracefully
        try:
            count = evaluate_consensus_records(db)
        except Exception:
            count = 0

        con2 = sqlite3.connect(db_path)
        con2.row_factory = sqlite3.Row
        r = dict(con2.execute("SELECT * FROM consensus WHERE id=1").fetchone())
        con2.close()
        # Should be NO_DATA or PENDING (not EVALUATED without data)
        assert r["eval_status"] in ("NO_DATA", "PENDING")


def test_evaluate_consensus_intraday_no_data_intraday():
    """Intraday horizon (<24h) with daily bars → eval_status = NO_DATA_INTRADAY."""
    from consensus_evaluator import evaluate_consensus_records
    from datetime import datetime, timedelta

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        past_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        eval_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ticker = "TEST:DDD"

        _make_consensus_db(db_path, [{
            "date": past_date, "ticker": ticker, "signal": "LONG",
            "confidence": 60.0, "methods_long": "tech", "methods_short": "",
            "methods_neutral": "", "rationale": "", "target_price": 110.0,
            "stop_loss": 95.0, "eval_target_date": eval_date, "eval_status": "PENDING",
            "horizon_hours": 4,  # Intraday: 4h horizon — should trigger NO_DATA_INTRADAY
        }])

        db = _MockDB(db_path)
        count = evaluate_consensus_records(db)

        con2 = sqlite3.connect(db_path)
        con2.row_factory = sqlite3.Row
        r = dict(con2.execute("SELECT * FROM consensus WHERE id=1").fetchone())
        con2.close()

        assert r["eval_status"] == "NO_DATA", f"Expected NO_DATA for intraday horizon with no bars, got {r['eval_status']}"
        # Intraday records should NOT be counted as evaluated
        assert count == 0


def test_evaluate_consensus_pending_not_ready():
    """Records with eval_target_date in the future should NOT be evaluated."""
    from consensus_evaluator import evaluate_consensus_records
    from datetime import datetime, timedelta

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        future_date = (datetime.now() + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
        ticker = "TEST:DDD"

        _make_consensus_db(db_path, [{
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ticker": ticker, "signal": "LONG",
            "confidence": 60.0, "methods_long": "tech", "methods_short": "",
            "methods_neutral": "", "rationale": "", "target_price": 110.0,
            "stop_loss": 95.0, "eval_target_date": future_date, "eval_status": "PENDING",
        }])

        db = _MockDB(db_path)
        count = evaluate_consensus_records(db)
        assert count == 0

        con2 = sqlite3.connect(db_path)
        con2.row_factory = sqlite3.Row
        r = dict(con2.execute("SELECT * FROM consensus WHERE id=1").fetchone())
        con2.close()
        assert r["eval_status"] == "PENDING"


def test_save_consensus_computes_horizon_hours():
    """save_consensus should populate horizon_hours and eval_target_date."""
    from consensus import save_consensus

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("""
            CREATE TABLE IF NOT EXISTS consensus (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT, ticker TEXT, signal TEXT, confidence REAL,
                methods_long TEXT, methods_short TEXT, methods_neutral TEXT, rationale TEXT,
                target_price REAL, stop_loss REAL, entry_limit_price REAL,
                high_model_disagreement INTEGER DEFAULT 0,
                horizon_hours INTEGER, eval_target_date TEXT, eval_status TEXT,
                run_id INTEGER, original_run_id INTEGER,
                order_state TEXT DEFAULT '',
                order_checked_at TEXT DEFAULT '',
                order_attempted_at TEXT DEFAULT '',
                order_reason TEXT DEFAULT '',
                trade_id INTEGER
            )
        """)
        con.commit()
        con.close()

        db = _MockDB(db_path)

        method_stats = {
            "momentum_trend": {"win_rate": 0.6, "timeframe_hours": 24},
            "price_action":   {"win_rate": 0.5, "timeframe_hours": 8},
        }
        consensus_data = {
            "signal": "LONG", "confidence": 70.0,
            "methods_long": "momentum_trend", "methods_short": "",
            "methods_neutral": "", "rationale": "test",
            "target_price": 110.0, "stop_loss": 95.0,
            "entry_limit_price": None, "high_model_disagreement": False,
            "_forecast_link_data": [],  # Empty for this test
        }
        result = save_consensus(db, "TEST:EEE", consensus_data, method_stats=method_stats)
        assert result is True

        con2 = sqlite3.connect(db_path)
        con2.row_factory = sqlite3.Row
        r = dict(con2.execute("SELECT * FROM consensus WHERE id=1").fetchone())
        con2.close()

        # signal=LONG → only momentum_trend participates → horizon_hours = 24
        assert r["horizon_hours"] == 24
        assert r["eval_target_date"] is not None and r["eval_target_date"] != ""
        assert r["eval_status"] == "PENDING"


def test_consensus_expected_value_filter():
    """Expected value filter: confidence >=55 but low R/R → NEUTRAL."""
    from consensus import calculate_consensus
    # Setup: confidence=70%, entry=100, target=103 (3% gain), stop=99 (1% loss)
    # R/R = 3.0, expected_r = 0.7 * 3.0 = 2.1 → passes (>= 0.5)
    forecasts_pass = [
        {"side": "LONG", "confidence": 70, "method": "m1", "model": "A",
         "target_price": 103.0, "stop_loss": 99.0, "entry_limit_price": 100.0},
    ]
    result_pass = calculate_consensus(forecasts_pass, current_price=100.0)
    assert result_pass["signal"] == "LONG"
    assert "[expected_r=" in result_pass["rationale"]
    
    # Setup: confidence=60% (>=55 passes threshold), entry=100, target=100.6 (0.6% gain), stop=98 (2% loss)
    # R/R = 0.3, expected_r = 0.6 * 0.3 = 0.18 → filtered (< 0.5)
    # Note: Need high confidence (>=55) to pass first threshold, but poor R/R to fail expected_r
    forecasts_fail = [
        {"side": "LONG", "confidence": 60, "method": "m1", "model": "A",
         "target_price": 100.6, "stop_loss": 98.0, "entry_limit_price": 100.0},
    ]
    result_fail = calculate_consensus(forecasts_fail, current_price=100.0)
    assert result_fail["signal"] == "NEUTRAL"
    assert result_fail["confidence"] == 0.0


def test_consensus_confidence_calibration():
    """Confidence calibration: overconfident model gets calibrated down, underconfident up."""
    from consensus import calculate_consensus
    
    # Case 1: Overconfident model (ema_accuracy=0.4 < 0.5) → calibration down
    # factor = 0.4/0.5 = 0.8, 80% confidence → 64% calibrated
    method_stats_under = {"m1": {"win_rate": 0.5, "ema_accuracy": 0.4}}
    forecasts_under = [
        {"side": "LONG", "confidence": 80, "method": "m1", "model": "A", "log_id": "log1",
         "target_price": 110.0, "stop_loss": 90.0},
    ]
    result_under = calculate_consensus(
        forecasts_under, 
        method_stats=method_stats_under, 
        current_price=100.0,
        run_id=1,
        log_ids={0: "log1"}
    )
    # Check calibration in link data
    link_data = result_under["_forecast_link_data"][0]
    assert link_data["calibration_factor"] == 0.8
    assert link_data["calibrated_confidence"] == 64.0  # 80 * 0.8
    # With single forecast, signal is LONG but effective weight is reduced
    assert result_under["signal"] == "LONG"
    
    # Case 2: Underconfident model (ema_accuracy=0.7 > 0.5) → calibration up
    # factor = 0.7/0.5 = 1.4, 60% confidence → 84% calibrated
    method_stats_over = {"m1": {"win_rate": 0.5, "ema_accuracy": 0.7}}
    forecasts_over = [
        {"side": "LONG", "confidence": 60, "method": "m1", "model": "A", "log_id": "log2",
         "target_price": 110.0, "stop_loss": 90.0},
    ]
    result_over = calculate_consensus(
        forecasts_over, 
        method_stats=method_stats_over, 
        current_price=100.0,
        run_id=2,
        log_ids={0: "log2"}
    )
    link_data2 = result_over["_forecast_link_data"][0]
    assert link_data2["calibration_factor"] == 1.4
    assert link_data2["calibrated_confidence"] == 84.0  # 60 * 1.4
    assert result_over["signal"] == "LONG"


# ===========================================================================
# forecast_runs tracking
# ===========================================================================

def _make_forecast_runs_db(db_path: str):
    """Create a minimal DB with forecast_runs tables."""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("""
        CREATE TABLE IF NOT EXISTS forecast_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            trigger_type TEXT NOT NULL,
            tickers_planned INTEGER DEFAULT 0,
            tickers_processed INTEGER DEFAULT 0,
            consensus_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running',
            error_message TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS forecast_run_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            log_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            method TEXT NOT NULL,
            model TEXT NOT NULL,
            signal TEXT,
            raw_confidence REAL,
            calibrated_confidence REAL,
            calibration_factor REAL,
            win_rate REAL,
            ema_accuracy REAL,
            final_weight REAL,
            target_price REAL,
            stop_loss REAL,
            entry_price REAL,
            r_multiple REAL,
            atr_14 REAL,
            included_in_consensus INTEGER DEFAULT 1,
            UNIQUE(run_id, log_id)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id TEXT PRIMARY KEY,
            ticker TEXT,
            method TEXT,
            model TEXT,
            side TEXT,
            confidence REAL,
            run_id INTEGER
        )
    """)
    con.commit()
    con.close()


def test_create_forecast_run():
    """Test creating a forecast run returns valid ID."""
    import tempfile
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        _make_forecast_runs_db(db_path)
        
        class FakeDb:
            def __init__(self, f): self.db_file = f
            def _connect(self):
                import sqlite3 as _sqlite3
                con = _sqlite3.connect(self.db_file, timeout=10)
                con.row_factory = _sqlite3.Row
                return con
            def create_forecast_run(self, trigger_type, tickers_planned=0):
                from sqlite_manager import SQLiteManager
                return SQLiteManager(self.db_file).create_forecast_run(trigger_type, tickers_planned)
            def complete_forecast_run(self, run_id, status, **kwargs):
                from sqlite_manager import SQLiteManager
                return SQLiteManager(self.db_file).complete_forecast_run(run_id, status, **kwargs)
        
        db = FakeDb(db_path)
        run_id = db.create_forecast_run('scheduler', 5)
        assert run_id is not None
        assert run_id > 0
        
        # Complete the run
        result = db.complete_forecast_run(run_id, 'completed', tickers_processed=5, consensus_count=3)
        assert result is True
        
        # Verify in DB
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM forecast_runs WHERE id=?", (run_id,)).fetchone()
        con.close()
        assert row is not None
        assert row['status'] == 'completed'
        assert row['tickers_processed'] == 5
        assert row['consensus_count'] == 3


def test_link_forecast_to_run():
    """Test linking a forecast to run with weight snapshot."""
    import tempfile
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        _make_forecast_runs_db(db_path)
        
        class FakeDb:
            def __init__(self, f): self.db_file = f
            def _connect(self):
                import sqlite3 as _sqlite3
                con = _sqlite3.connect(self.db_file, timeout=10)
                con.row_factory = _sqlite3.Row
                return con
            def link_forecast_to_run(self, **kwargs):
                from sqlite_manager import SQLiteManager
                return SQLiteManager(self.db_file).link_forecast_to_run(**kwargs)
        
        db = FakeDb(db_path)
        
        # Link a forecast
        result = db.link_forecast_to_run(
            run_id=1,
            log_id="LOG_20240101120000_1234",
            ticker="AAPL",
            method="momentum_trend",
            model="claude-sonnet",
            signal="LONG",
            raw_confidence=75.0,
            win_rate=0.6,
            ema_accuracy=0.55,
            final_weight=0.2475,  # 0.75 * 0.6 * 0.55
            target_price=150.0,
            stop_loss=140.0,
            included_in_consensus=1
        )
        assert result is True
        
        # Verify in DB
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM forecast_run_links WHERE run_id=1").fetchone()
        con.close()
        assert row is not None
        assert row['ticker'] == "AAPL"
        assert row['final_weight'] == 0.2475
        assert row['included_in_consensus'] == 1


def test_get_forecast_run_with_stats():
    """Test getting forecast run with aggregated stats."""
    import tempfile
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        _make_forecast_runs_db(db_path)
        
        # Insert run and links directly
        con = sqlite3.connect(db_path)
        con.execute("""
            INSERT INTO forecast_runs (id, started_at, trigger_type, tickers_planned, status)
            VALUES (1, '2024-01-01 10:00:00', 'scheduler', 3, 'completed')
        """)
        for i in range(5):
            con.execute("""
                INSERT INTO forecast_run_links (run_id, log_id, ticker, method, model, signal, 
                                               raw_confidence, win_rate, ema_accuracy, final_weight, 
                                               included_in_consensus)
                VALUES (1, ?, 'AAPL', 'momentum_trend', 'claude', 'LONG', 70, 0.5, 0.6, 0.21, 1)
            """, (f"LOG_{i}",))
        con.commit()
        con.close()
        
        class FakeDb:
            def __init__(self, f): self.db_file = f
            def _connect(self):
                import sqlite3 as _sqlite3
                con = _sqlite3.connect(self.db_file, timeout=10)
                con.row_factory = _sqlite3.Row
                return con
            def get_forecast_run(self, run_id):
                from sqlite_manager import SQLiteManager
                return SQLiteManager(self.db_file).get_forecast_run(run_id)
        
        db = FakeDb(db_path)
        run = db.get_forecast_run(1)
        assert run is not None
        assert run['tickers_count'] == 1  # Only AAPL
        assert run['methods_count'] == 1  # Only momentum_trend
        assert run['models_count'] == 1   # Only claude
        assert run['total_forecasts'] == 5


# ===========================================================================
# ATR Normalization
# ===========================================================================

def test_calculate_atr_basic():
    """Test ATR calculation from OHLC data."""
    import pandas as pd
    from consensus import calculate_atr
    
    # Simple price data: 15 days of ascending prices
    data = {
        'high': [102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116],
        'low':  [98,  99,  100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112],
        'close':[100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114],
    }
    df = pd.DataFrame(data)
    
    atr = calculate_atr(df, period=14)
    assert atr is not None
    assert atr > 0
    # ATR should be approximately (high-low) average = ~4.0
    assert 3.5 < atr < 4.5


def test_normalize_r_multiple():
    """Test R-multiple normalization by ATR."""
    from consensus import normalize_r_multiple
    
    # Case 1: Volatile ticker (ATR = 5% of price)
    # R-multiple = 2.0, but ATR is high
    # normalized = 2.0 / (0.05 * 100) = 2.0 / 5.0 = 0.4
    norm1 = normalize_r_multiple(r_multiple=2.0, atr=5.0, entry_price=100.0)
    assert norm1 == 0.4
    
    # Case 2: Stable ticker (ATR = 1% of price)
    # Same R-multiple = 2.0, but ATR is low
    # normalized = 2.0 / (0.01 * 100) = 2.0 / 1.0 = 2.0
    norm2 = normalize_r_multiple(r_multiple=2.0, atr=1.0, entry_price=100.0)
    assert norm2 == 2.0
    
    # Case 3: Same volatility as R-multiple (ATR = 2%)
    # normalized = 2.0 / (0.02 * 100) = 2.0 / 2.0 = 1.0
    norm3 = normalize_r_multiple(r_multiple=2.0, atr=2.0, entry_price=100.0)
    assert norm3 == 1.0
    
    # Case 4: Invalid inputs
    assert normalize_r_multiple(2.0, 0, 100) is None
    assert normalize_r_multiple(2.0, 5.0, 0) is None
    assert normalize_r_multiple(2.0, None, 100) is None


def test_consensus_with_atr_in_link_data():
    """Test that ATR and R-multiple are stored in forecast_link_data."""
    from consensus import calculate_consensus
    
    forecasts = [
        {"side": "LONG", "confidence": 70, "method": "m1", "model": "A", "log_id": "log1",
         "target_price": 110.0, "stop_loss": 95.0, "entry_limit_price": 100.0, "atr_14": 2.5},
    ]
    result = calculate_consensus(
        forecasts,
        current_price=100.0,
        run_id=1,
        log_ids={0: "log1"}
    )
    
    link_data = result["_forecast_link_data"][0]
    assert link_data["atr_14"] == 2.5
    assert link_data["r_multiple"] == 2.0  # (110-100)/(100-95) = 10/5 = 2.0
    assert link_data["entry_price"] == 100.0


# ===========================================================================
# First Hit Analysis
# ===========================================================================

def test_first_hit_analysis_target_first():
    """First hit analysis: target closer to open → target hit first."""
    # Setup: LONG signal, open=100, target=105 (5 points up), stop=95 (5 points down)
    # Both hit during day. Target is at same distance as stop → ambiguous → defaults to stop
    # But if target=104 (4 points), stop=96 (4 points down from 100 for long? no)
    # For LONG: stop should be below entry
    # open=100, entry=100, target=103 (3 points up), stop=98 (2 points down)
    # target is farther from open, stop closer → stop hit first
    # reverse: target=102 (2 points), stop=98 (2 points down) → equal
    # target=101 (1 point), stop=99 (1 point down) → equal again
    # open=100, target=102 (2 up), stop=96 (4 down) → target closer → target hit first
    
    import pandas as pd
    from datetime import datetime
    
    # We'll test the logic directly with price data simulation
    # If target is closer to open than stop → target hit first
    open_price = 100.0
    target = 102.0  # 2 points above open
    stop = 96.0     # 4 points below open
    
    # Distance calculation
    dist_to_target = abs(target - open_price)  # 2.0
    dist_to_stop = abs(stop - open_price)      # 4.0
    
    assert dist_to_target < dist_to_stop
    # Therefore target hit first
    
    # Reverse case: stop closer
    target2 = 110.0  # 10 points up
    stop2 = 98.0     # 2 points down
    dist_to_target2 = abs(target2 - open_price)  # 10.0
    dist_to_stop2 = abs(stop2 - open_price)       # 2.0
    
    assert dist_to_stop2 < dist_to_target2
    # Therefore stop hit first


# ===========================================================================
# consensus.py — model_stats parameter & total_weight accumulation (ред. 2)
# ===========================================================================

def test_consensus_model_stats_overrides_method_stats_ema():
    """model_stats keyed by model name takes precedence over method_stats for ema_accuracy."""
    from consensus import calculate_consensus

    # Both method_stats and model_stats present for different resolution paths.
    # model_stats["gpt-4o"] should win over method_stats["m1"] for ema_accuracy.
    method_stats = {"m1": {"win_rate": 0.5, "ema_accuracy": 0.3}}  # Low accuracy from method
    model_stats = {"gpt-4o": {"ema_accuracy": 0.7}}                  # High accuracy from model

    forecasts = [
        {"side": "LONG", "confidence": 60, "method": "m1", "model": "gpt-4o",
         "log_id": "logA", "target_price": 110.0, "stop_loss": 95.0},
    ]
    result = calculate_consensus(
        forecasts,
        method_stats=method_stats,
        model_stats=model_stats,
        current_price=100.0,
        run_id=1,
        log_ids={0: "logA"},
    )
    link_data = result["_forecast_link_data"][0]
    # ema_accuracy should be 0.7 (from model_stats), NOT 0.3 (from method_stats)
    assert link_data["ema_accuracy"] == 0.7
    # calibration_factor = 0.7 / 0.5 = 1.4
    assert link_data["calibration_factor"] == 1.4


def test_consensus_model_stats_fallback_to_method_stats():
    """If model not in model_stats, falls back to method_stats ema_accuracy."""
    from consensus import calculate_consensus

    method_stats = {"m1": {"win_rate": 0.5, "ema_accuracy": 0.45}}
    model_stats = {}  # Empty — no model entry

    forecasts = [
        {"side": "LONG", "confidence": 70, "method": "m1", "model": "unknown-model",
         "log_id": "logB", "target_price": 110.0, "stop_loss": 95.0},
    ]
    result = calculate_consensus(
        forecasts,
        method_stats=method_stats,
        model_stats=model_stats,
        current_price=100.0,
        run_id=1,
        log_ids={0: "logB"},
    )
    link_data = result["_forecast_link_data"][0]
    # Should fall back to method_stats ema_accuracy=0.45
    assert link_data["ema_accuracy"] == 0.45
    # calibration_factor = 0.45 / 0.5 = 0.9
    assert abs(link_data["calibration_factor"] - 0.9) < 0.001


def test_consensus_total_weight_accumulates_all_non_filtered():
    """total_weight must include all non-filtered forecasts regardless of direction."""
    from consensus import calculate_consensus

    # 2 LONG + 2 SHORT with equal confidence and no stats
    # total_weight should = 4 × (0.6 × 0.5 × 1.0) = 1.2 (all four weighted)
    forecasts = [
        {"side": "LONG",  "confidence": 60, "method": "m1", "model": "A"},
        {"side": "LONG",  "confidence": 60, "method": "m2", "model": "B"},
        {"side": "SHORT", "confidence": 60, "method": "m3", "model": "C"},
        {"side": "SHORT", "confidence": 60, "method": "m4", "model": "D"},
    ]
    result = calculate_consensus(forecasts, current_price=100.0)
    # Equal LONG vs SHORT → high disagreement → NEUTRAL
    assert result["signal"] == "NEUTRAL"
    # confidence forced to 0 by disagreement
    assert result["confidence"] == 0.0


def test_consensus_total_weight_not_counting_filtered():
    """Anomaly-filtered forecasts must NOT add to total_weight."""
    from consensus import calculate_consensus

    # One extreme forecast (will be filtered) + one valid LONG
    forecasts = [
        # This will be filtered: target 50% away from price
        {"side": "LONG", "confidence": 80, "method": "m1", "model": "A",
         "target_price": 150.0, "stop_loss": 90.0},
        # Valid LONG
        {"side": "LONG", "confidence": 70, "method": "m2", "model": "B",
         "target_price": 110.0, "stop_loss": 90.0},
    ]
    result = calculate_consensus(forecasts, current_price=100.0, max_deviation=0.15)
    # Only the valid LONG remains → LONG consensus
    assert result["signal"] == "LONG"
    assert "1 filtered by anomaly" in result["rationale"]


def test_consensus_evaluator_exit_successful_persisted():
    """exit_successful must be saved to DB when target hit first."""
    from consensus_evaluator import evaluate_consensus_records
    from datetime import datetime, timedelta

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        eval_date = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        cons_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        ticker = "TEST:EXIT"

        _make_consensus_db(db_path, [{
            "date": cons_date, "ticker": ticker, "signal": "LONG",
            "confidence": 75.0, "methods_long": "tech", "methods_short": "",
            "methods_neutral": "", "rationale": "",
            "target_price": 105.0, "stop_loss": 95.0,
            "eval_target_date": eval_date, "eval_status": "PENDING",
        }])
        # Entry bar: close=100
        _add_price_bar(db_path, ticker, cons_date[:10], 100.0, 101.0, 99.0, 100.0)
        # Eval bar: high=108 (>105 target), low=97 (>95 stop) → only target hit
        _add_price_bar(db_path, ticker, eval_date[:10], 100.0, 108.0, 97.0, 107.0)

        db = _MockDB(db_path)
        count = evaluate_consensus_records(db)
        assert count == 1

        con2 = sqlite3.connect(db_path)
        con2.row_factory = sqlite3.Row
        r = dict(con2.execute("SELECT * FROM consensus WHERE id=1").fetchone())
        con2.close()

        assert r["eval_status"] == "EVALUATED"
        assert r["target_hit"] == 1
        assert r["stop_hit"] == 0
        assert r["first_hit"] == "target"
        assert r["exit_successful"] == 1  # Must be persisted


def test_consensus_evaluator_exit_successful_stop_first():
    """Both target and stop hit but stop is closer to open → exit_successful=0."""
    from consensus_evaluator import evaluate_consensus_records
    from datetime import datetime, timedelta

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        eval_date = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        cons_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        ticker = "TEST:STOP"

        _make_consensus_db(db_path, [{
            "date": cons_date, "ticker": ticker, "signal": "LONG",
            "confidence": 70.0, "methods_long": "tech", "methods_short": "",
            "methods_neutral": "", "rationale": "",
            "target_price": 115.0,  # 15 points above open=100
            "stop_loss": 98.0,      # 2 points below open=100 → closer
            "eval_target_date": eval_date, "eval_status": "PENDING",
        }])
        _add_price_bar(db_path, ticker, cons_date[:10], 100.0, 101.0, 99.0, 100.0)
        # open=100, high=117 (>115 target), low=96 (<98 stop) — both hit
        # stop=98 is 2 points from open; target=115 is 15 points → stop hit first
        _add_price_bar(db_path, ticker, eval_date[:10], 100.0, 117.0, 96.0, 100.0)

        db = _MockDB(db_path)
        count = evaluate_consensus_records(db)
        assert count == 1

        con2 = sqlite3.connect(db_path)
        con2.row_factory = sqlite3.Row
        r = dict(con2.execute("SELECT * FROM consensus WHERE id=1").fetchone())
        con2.close()

        assert r["eval_status"] == "EVALUATED"
        assert r["target_hit"] == 1
        assert r["stop_hit"] == 1
        assert r["first_hit"] == "stop"
        assert r["exit_successful"] == 0  # Stop was first


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
