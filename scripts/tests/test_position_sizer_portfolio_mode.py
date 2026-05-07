"""
Unit tests for position_sizer — portfolio risk mode (6b).

Tests are self-contained: capital_provider.get_portfolio_net_liquidation is
always patched so no real IB connection or DB config is needed for the new mode.

DB fixture provides the SQLite tables (settings, portfolio) that _get_ticker_sector
and _get_sector_exposure query directly.
"""

import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import pytest

_CORE = os.path.join(os.path.dirname(__file__), "..", "core")
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from position_sizer import calculate_position

_PATCH_CAPITAL = "capital_provider.get_portfolio_net_liquidation"
_PORTFOLIO_MODE = "percent_of_portfolio_on_stop"
_LEGACY_MODE    = "percent_of_capital"

# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------

def _make_db(
    risk_mode: str = _PORTFOLIO_MODE,
    risk_pct: float = 1.0,
    max_position_pct: float = 0.05,
    soft_sector_pct: float = 0.15,
    hard_sector_pct: float = 0.25,
    overweight_factor: float = 0.5,
    default_risk_pct: float = 0.01,
    sector_rows: list = None,
    portfolio_rows: list = None,
) -> "FakeDb":
    tmp = tempfile.mktemp(suffix=".db")
    con = sqlite3.connect(tmp)
    con.executescript("""
        CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE settings (ticker TEXT PRIMARY KEY, trading_blocked INTEGER DEFAULT 0, sector TEXT DEFAULT '');
        CREATE TABLE portfolio (ticker TEXT PRIMARY KEY, market_value REAL DEFAULT 0);
    """)
    config = [
        ("RISK_MODE",                risk_mode),
        ("RISK_PERCENT_ON_STOP",     str(risk_pct)),
        ("MAX_POSITION_PCT",         str(max_position_pct)),
        ("MAX_SECTOR_EXPOSURE_PCT",  str(soft_sector_pct)),
        ("MAX_SECTOR_HARD_LIMIT_PCT",str(hard_sector_pct)),
        ("SECTOR_OVERWEIGHT_FACTOR", str(overweight_factor)),
        ("DEFAULT_RISK_PCT",         str(default_risk_pct)),
    ]
    con.executemany("INSERT OR REPLACE INTO config (key,value) VALUES (?,?)", config)
    if sector_rows:
        con.executemany(
            "INSERT OR REPLACE INTO settings (ticker, sector) VALUES (?,?)",
            sector_rows,
        )
    if portfolio_rows:
        con.executemany(
            "INSERT OR REPLACE INTO portfolio (ticker, market_value) VALUES (?,?)",
            portfolio_rows,
        )
    con.commit()
    con.close()
    return FakeDb(tmp)


class FakeDb:
    def __init__(self, db_file):
        self.db_file = db_file

    def get_config_value(self, key):
        with sqlite3.connect(self.db_file) as con:
            row = con.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def cleanup(self):
        try:
            os.unlink(self.db_file)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 6b-1: Correct qty formula
#   portfolio=100 000, risk_pct=1.0 → risk=1 000
#   entry=150, stop=140 → stop_distance=10
#   qty_by_risk = 1000 / 10 = 100
#   max_qty_by_position = 100 000 × 0.05 / 150 = 33 → qty capped at 33
# ---------------------------------------------------------------------------

def test_qty_formula_capped_by_max_position():
    db = _make_db()
    with patch(_PATCH_CAPITAL, return_value=(100_000.0, "ib")):
        r = calculate_position("AAPL", entry_price=150.0, stop_loss=140.0, db_manager=db)

    assert r["status"] == "OK"
    assert r["risk_mode"] == _PORTFOLIO_MODE
    assert r["risk_amount"] == pytest.approx(1_000.0)
    # qty_by_risk=100, max_by_pos=33 → capped at 33
    assert r["quantity"] == 33
    assert r["capital_source"] == "ib"
    db.cleanup()


# ---------------------------------------------------------------------------
# 6b-2: qty NOT capped — small entry price, wide position budget
#   portfolio=100 000, risk_pct=1.0 → risk=1 000
#   entry=10, stop=9 → stop_distance=1 → qty_by_risk=1000
#   max_qty_by_pos = 100 000 × 0.05 / 10 = 500 → capped at 500
# ---------------------------------------------------------------------------

def test_qty_formula_capped_by_max_position_small_price():
    db = _make_db()
    with patch(_PATCH_CAPITAL, return_value=(100_000.0, "ib")):
        r = calculate_position("AAPL", entry_price=10.0, stop_loss=9.0, db_manager=db)

    assert r["status"] == "OK"
    assert r["quantity"] == 500
    assert r["risk_amount"] == pytest.approx(1_000.0)
    db.cleanup()


# ---------------------------------------------------------------------------
# 6b-3: No position cap — qty_by_risk < max_by_position
#   portfolio=100 000, risk_pct=0.5 → risk=500
#   entry=150, stop=148 → stop_distance=2 → qty_by_risk=250
#   max_qty_by_pos = 100 000 × 0.05 / 150 = 33 → capped at 33
# ---------------------------------------------------------------------------

def test_qty_always_min_of_risk_and_position_cap():
    db = _make_db(risk_pct=0.5)
    with patch(_PATCH_CAPITAL, return_value=(100_000.0, "ib")):
        r = calculate_position("AAPL", entry_price=150.0, stop_loss=148.0, db_manager=db)

    assert r["status"] == "OK"
    # qty_by_risk = 500/2 = 250 but max_by_pos = 33
    assert r["quantity"] == 33
    db.cleanup()


# ---------------------------------------------------------------------------
# 6b-4: risk_pct drives risk_amount correctly (2%)
#   portfolio=200 000, risk_pct=2.0 → risk=4 000
#   entry=100, stop=90 → stop_distance=10 → qty_by_risk=400
#   max_qty_by_pos = 200 000 × 0.05 / 100 = 100 → capped at 100
# ---------------------------------------------------------------------------

def test_risk_pct_drives_risk_amount():
    db = _make_db(risk_pct=2.0, max_position_pct=0.05)
    with patch(_PATCH_CAPITAL, return_value=(200_000.0, "ib")):
        r = calculate_position("TSLA", entry_price=100.0, stop_loss=90.0, db_manager=db)

    assert r["status"] == "OK"
    assert r["risk_amount"] == pytest.approx(4_000.0)
    assert r["quantity"] == 100
    db.cleanup()


# ---------------------------------------------------------------------------
# 6b-5: SKIPPED_CAPITAL_UNAVAILABLE when capital_provider raises
# ---------------------------------------------------------------------------

def test_capital_unavailable_returns_skipped_status():
    from capital_provider import CapitalUnavailableError
    db = _make_db()
    with patch(_PATCH_CAPITAL, side_effect=CapitalUnavailableError("no IB")):
        r = calculate_position("AAPL", 150.0, 140.0, db_manager=db)

    assert r["status"] == "SKIPPED_CAPITAL_UNAVAILABLE"
    assert r["quantity"] == 0
    assert r["risk_mode"] == _PORTFOLIO_MODE
    assert r["capital_source"] == "unavailable"
    db.cleanup()


# ---------------------------------------------------------------------------
# 6b-6: SKIPPED_NO_CAPITAL when portfolio_value=0
# ---------------------------------------------------------------------------

def test_zero_portfolio_value_returns_no_capital():
    db = _make_db()
    with patch(_PATCH_CAPITAL, return_value=(0.0, "ib")):
        r = calculate_position("AAPL", 150.0, 140.0, db_manager=db)

    assert r["status"] == "SKIPPED_NO_CAPITAL"
    assert r["quantity"] == 0
    db.cleanup()


# ---------------------------------------------------------------------------
# 6b-7: SKIPPED_INVALID_STOP when stop_loss=0
# ---------------------------------------------------------------------------

def test_invalid_stop_loss_zero():
    db = _make_db()
    with patch(_PATCH_CAPITAL, return_value=(100_000.0, "ib")):
        r = calculate_position("AAPL", 150.0, 0.0, db_manager=db)

    assert r["status"] == "SKIPPED_INVALID_STOP"
    assert r["quantity"] == 0
    db.cleanup()


# ---------------------------------------------------------------------------
# 6b-8: SKIPPED_INVALID_STOP when entry_price=0
# ---------------------------------------------------------------------------

def test_invalid_entry_price_zero():
    db = _make_db()
    with patch(_PATCH_CAPITAL, return_value=(100_000.0, "ib")):
        r = calculate_position("AAPL", 0.0, 145.0, db_manager=db)

    assert r["status"] == "SKIPPED_INVALID_STOP"
    assert r["quantity"] == 0
    db.cleanup()


# ---------------------------------------------------------------------------
# 6b-9: SKIPPED_ZERO_RISK when entry == stop (distance < 0.0001)
# ---------------------------------------------------------------------------

def test_zero_stop_distance_skipped():
    db = _make_db()
    with patch(_PATCH_CAPITAL, return_value=(100_000.0, "ib")):
        r = calculate_position("AAPL", 150.0, 150.0, db_manager=db)

    assert r["status"] == "SKIPPED_ZERO_RISK"
    assert r["quantity"] == 0
    db.cleanup()


# ---------------------------------------------------------------------------
# 6b-10: Sector soft limit → qty reduced by SECTOR_OVERWEIGHT_FACTOR
#   portfolio=100 000, risk_pct=1.0 → risk=1 000
#   entry=150, stop=100 → stop_distance=50 → qty_by_risk=20
#   max_qty_by_pos = 100 000 × 0.05 / 150 = 33
#   sector_exposure = 20 000 → 20% > soft(15%) but < hard(25%)
#   → qty = int(20 × 0.5) = 10
# ---------------------------------------------------------------------------

def test_sector_soft_limit_reduces_qty():
    db = _make_db(
        sector_rows=[("AAPL", "Tech")],
        portfolio_rows=[("MSFT", 20_000.0)],  # same sector, 20% of 100k
    )
    # Patch sector lookup to say MSFT is also Tech
    # The portfolio table + settings join handles this: add MSFT to settings
    with sqlite3.connect(db.db_file) as con:
        con.execute("INSERT OR REPLACE INTO settings (ticker, sector) VALUES ('MSFT','Tech')")
        con.commit()

    with patch(_PATCH_CAPITAL, return_value=(100_000.0, "ib")):
        r = calculate_position("AAPL", 150.0, 100.0, db_manager=db)

    assert r["status"] == "OK"
    assert r["quantity"] == 10          # 20 × 0.5
    assert r["sector"] == "Tech"
    assert r["sector_exposure_at_signal"] == pytest.approx(20_000.0)
    db.cleanup()


# ---------------------------------------------------------------------------
# 6b-11: Sector hard limit → SKIPPED_SECTOR_OVERWEIGHT
#   sector_exposure = 30 000 → 30% > hard(25%)
# ---------------------------------------------------------------------------

def test_sector_hard_limit_rejects():
    db = _make_db(
        sector_rows=[("AAPL", "Tech")],
        portfolio_rows=[("MSFT", 30_000.0)],
    )
    with sqlite3.connect(db.db_file) as con:
        con.execute("INSERT OR REPLACE INTO settings (ticker, sector) VALUES ('MSFT','Tech')")
        con.commit()

    with patch(_PATCH_CAPITAL, return_value=(100_000.0, "ib")):
        r = calculate_position("AAPL", 150.0, 100.0, db_manager=db)

    assert r["status"] == "SKIPPED_SECTOR_OVERWEIGHT"
    assert r["quantity"] == 0
    assert r["risk_mode"] == _PORTFOLIO_MODE
    db.cleanup()


# ---------------------------------------------------------------------------
# 6b-12: manual_override source propagates to result
# ---------------------------------------------------------------------------

def test_manual_override_source_in_result():
    db = _make_db()
    with patch(_PATCH_CAPITAL, return_value=(80_000.0, "manual_override")):
        r = calculate_position("AAPL", 150.0, 140.0, db_manager=db)

    assert r["status"] == "OK"
    assert r["capital_source"] == "manual_override"
    db.cleanup()


# ---------------------------------------------------------------------------
# 6b-13: result always contains risk_mode and capital_source keys
# ---------------------------------------------------------------------------

def test_result_always_has_meta_fields():
    db = _make_db()
    with patch(_PATCH_CAPITAL, return_value=(100_000.0, "ib")):
        r = calculate_position("AAPL", 150.0, 140.0, db_manager=db)

    for key in ("risk_mode", "capital_source", "risk_pct", "position_value",
                "sector", "sector_exposure_at_signal"):
        assert key in r, f"Missing key: {key}"
    db.cleanup()


# ---------------------------------------------------------------------------
# 6b-14: SHORT position (stop above entry) — still works correctly
#   portfolio=100 000, risk_pct=1.0 → risk=1 000
#   entry=150, stop=160 → stop_distance=10 → qty_by_risk=100 → capped at 33
# ---------------------------------------------------------------------------

def test_short_position_stop_above_entry():
    db = _make_db()
    with patch(_PATCH_CAPITAL, return_value=(100_000.0, "ib")):
        r = calculate_position("AAPL", entry_price=150.0, stop_loss=160.0, db_manager=db)

    assert r["status"] == "OK"
    assert r["quantity"] == 33
    assert r["risk_amount"] == pytest.approx(1_000.0)
    db.cleanup()


# ---------------------------------------------------------------------------
# 6b-15 (regression): Legacy mode — RISK_MODE=percent_of_capital
#   capital_provider NOT called; net_liquidation passed explicitly
#   Existing test_position_sizer_basic values must be preserved
# ---------------------------------------------------------------------------

def test_legacy_mode_not_affected_by_new_code():
    db = _make_db(risk_mode=_LEGACY_MODE)
    # In legacy mode, capital_provider.get_portfolio_net_liquidation must NOT be called
    with patch(_PATCH_CAPITAL) as mock_cap:
        r = calculate_position(
            "AAPL", entry_price=150.0, stop_loss=145.0,
            db_manager=db, net_liquidation=100_000.0
        )

    assert r["status"] == "OK"
    # Same as original test_position_sizer_basic:
    # risk = 100k × 1% = 1000; stop_distance = 5; qty_by_risk = 200
    # MAX_POSITION_PCT cap: 5% × 100k / 150 = 33
    assert r["quantity"] == 33
    assert r["risk_amount"] == pytest.approx(1_000.0)
    assert r["risk_mode"] == _LEGACY_MODE
    mock_cap.assert_not_called()
    db.cleanup()


# ---------------------------------------------------------------------------
# 6b-16 (regression): Legacy mode SKIPPED_NO_CAPITAL still works
# ---------------------------------------------------------------------------

def test_legacy_mode_no_capital_status():
    db = _make_db(risk_mode=_LEGACY_MODE)
    with patch(_PATCH_CAPITAL) as mock_cap:
        r = calculate_position(
            "AAPL", entry_price=150.0, stop_loss=145.0,
            db_manager=db, net_liquidation=0.0
        )

    assert r["status"] == "SKIPPED_NO_CAPITAL"
    mock_cap.assert_not_called()
    db.cleanup()


# ---------------------------------------------------------------------------
# 6b-17 (regression): Legacy mode SKIPPED_INVALID_STOP still works
# ---------------------------------------------------------------------------

def test_legacy_mode_invalid_stop():
    db = _make_db(risk_mode=_LEGACY_MODE)
    with patch(_PATCH_CAPITAL) as mock_cap:
        r = calculate_position(
            "AAPL", entry_price=150.0, stop_loss=0.0,
            db_manager=db, net_liquidation=100_000.0
        )

    assert r["status"] == "SKIPPED_INVALID_STOP"
    mock_cap.assert_not_called()
    db.cleanup()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
