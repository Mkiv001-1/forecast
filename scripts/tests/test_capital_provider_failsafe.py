"""
Unit tests for capital_provider.get_portfolio_net_liquidation — 6a.

Tests are fully self-contained: no real IB connection, no server running.
All IB refresh calls are patched. SQLite databases are created in-memory
(via temp files) and cleaned up after each test.
"""

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

# Make core modules importable without a full package install (tests run from scripts/tests/)
_CORE = os.path.join(os.path.dirname(__file__), "..", "core")
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from capital_provider import (
    CapitalUnavailableError,
    get_portfolio_net_liquidation,
    _FALLBACK_CAPITAL_CACHE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_sync_ts() -> str:
    """Return an ISO timestamp that is definitely NOT stale (1 minute ago)."""
    return (datetime.now(tz=timezone.utc) - timedelta(minutes=1)).isoformat()


def _stale_sync_ts() -> str:
    """Return an ISO timestamp that is definitely stale (2 hours ago)."""
    return (datetime.now(tz=timezone.utc) - timedelta(hours=2)).isoformat()


def _make_db(
    account_id: str = "DU123456",
    net_liquidation: float = 100_000.0,
    last_sync: str = None,
    risk_account_id: str = "DU123456",
    failsafe: str = "manual_only",
    manual_override: str = "",
    staleness_minutes: int = 15,
) -> "FakeDb":
    """
    Create a temp SQLite DB with accounts + config tables and return a FakeDb.
    last_sync=None → row has empty last_sync (treated as stale).
    """
    tmp = tempfile.mktemp(suffix=".db")
    con = sqlite3.connect(tmp)
    con.executescript("""
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT UNIQUE,
            type TEXT DEFAULT 'paper',
            net_liquidation REAL DEFAULT 0,
            last_sync TEXT DEFAULT '',
            last_update TEXT DEFAULT ''
        );
        CREATE TABLE config (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    if account_id:
        con.execute(
            "INSERT INTO accounts (account_id, net_liquidation, last_sync) VALUES (?, ?, ?)",
            (account_id, net_liquidation, last_sync or ""),
        )
    con.executemany(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        [
            ("RISK_ACCOUNT_ID",          risk_account_id),
            ("IB_CAPITAL_FAILSAFE",      failsafe),
            ("MANUAL_CAPITAL_OVERRIDE",  manual_override),
            ("CAPITAL_STALENESS_MINUTES", str(staleness_minutes)),
        ],
    )
    con.commit()
    con.close()
    return FakeDb(tmp)


class FakeDb:
    def __init__(self, db_file: str):
        self.db_file = db_file

    def get_config_value(self, key: str):
        with sqlite3.connect(self.db_file) as con:
            row = con.execute(
                "SELECT value FROM config WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def cleanup(self):
        try:
            os.unlink(self.db_file)
        except OSError:
            pass


# Patch target for IB sync called inside capital_provider
_PATCH_SYNC = "capital_provider._refresh_ib_sync"


# ---------------------------------------------------------------------------
# 6a-1: Fresh IB data — returns immediately, no refresh triggered
# ---------------------------------------------------------------------------

def test_fresh_ib_data_returns_value():
    db = _make_db(last_sync=_fresh_sync_ts())
    with patch(_PATCH_SYNC) as mock_sync:
        value, source = get_portfolio_net_liquidation(db)

    assert value == pytest.approx(100_000.0)
    assert source == "ib"
    mock_sync.assert_not_called()
    db.cleanup()


# ---------------------------------------------------------------------------
# 6a-2: Stale data — refresh succeeds, returns refreshed value
# ---------------------------------------------------------------------------

def test_stale_data_triggers_refresh_and_returns_value():
    db = _make_db(last_sync=_stale_sync_ts())

    def fake_refresh(db_manager):
        # Simulate IB writing fresh net_liquidation back to DB
        with sqlite3.connect(db_manager.db_file) as con:
            con.execute(
                "UPDATE accounts SET net_liquidation = 120000.0, last_sync = ? WHERE account_id = ?",
                (_fresh_sync_ts(), "DU123456"),
            )
            con.commit()
        return True

    with patch(_PATCH_SYNC, side_effect=fake_refresh):
        value, source = get_portfolio_net_liquidation(db)

    assert value == pytest.approx(120_000.0)
    assert source == "ib"
    db.cleanup()


# ---------------------------------------------------------------------------
# 6a-3: Stale + IB refresh fails + failsafe=manual_only + override set → OK
# ---------------------------------------------------------------------------

def test_stale_ib_fails_manual_override_used():
    db = _make_db(
        last_sync=_stale_sync_ts(),
        failsafe="manual_only",
        manual_override="75000",
    )
    with patch(_PATCH_SYNC, return_value=False):
        value, source = get_portfolio_net_liquidation(db)

    assert value == pytest.approx(75_000.0)
    assert source == "manual_override"
    db.cleanup()


# ---------------------------------------------------------------------------
# 6a-4: Stale + IB refresh fails + failsafe=manual_only + NO override → raises
# ---------------------------------------------------------------------------

def test_stale_ib_fails_no_manual_override_raises():
    db = _make_db(
        last_sync=_stale_sync_ts(),
        failsafe="manual_only",
        manual_override="",
    )
    with patch(_PATCH_SYNC, return_value=False):
        with pytest.raises(CapitalUnavailableError, match="MANUAL_CAPITAL_OVERRIDE"):
            get_portfolio_net_liquidation(db)
    db.cleanup()


# ---------------------------------------------------------------------------
# 6a-5: IB refresh fails + failsafe=deny → always raises
# ---------------------------------------------------------------------------

def test_failsafe_deny_always_raises():
    db = _make_db(
        last_sync=_stale_sync_ts(),
        failsafe="deny",
        manual_override="99999",  # even with override set — deny wins
    )
    with patch(_PATCH_SYNC, return_value=False):
        with pytest.raises(CapitalUnavailableError, match="deny"):
            get_portfolio_net_liquidation(db)
    db.cleanup()


# ---------------------------------------------------------------------------
# 6a-6: RISK_ACCOUNT_ID not configured → raises immediately, no DB lookup
# ---------------------------------------------------------------------------

def test_no_risk_account_id_raises():
    db = _make_db(risk_account_id="")
    with patch(_PATCH_SYNC) as mock_sync:
        with pytest.raises(CapitalUnavailableError, match="RISK_ACCOUNT_ID"):
            get_portfolio_net_liquidation(db)
    mock_sync.assert_not_called()
    db.cleanup()


# ---------------------------------------------------------------------------
# 6a-7: account_id not in DB → refresh triggered → account appears → returned
# ---------------------------------------------------------------------------

def test_account_not_in_db_triggers_refresh_then_found():
    # Create DB without any account rows; refresh will insert the account
    db = _make_db(account_id=None)  # no account row

    def fake_refresh(db_manager):
        with sqlite3.connect(db_manager.db_file) as con:
            con.execute(
                "INSERT INTO accounts (account_id, net_liquidation, last_sync) VALUES (?, ?, ?)",
                ("DU123456", 88_000.0, _fresh_sync_ts()),
            )
            con.commit()
        return True

    with patch(_PATCH_SYNC, side_effect=fake_refresh):
        value, source = get_portfolio_net_liquidation(db)

    assert value == pytest.approx(88_000.0)
    assert source == "ib"
    db.cleanup()


# ---------------------------------------------------------------------------
# 6a-8: account_id not in DB + refresh fails + manual_only → manual override
# ---------------------------------------------------------------------------

def test_account_not_in_db_refresh_fails_uses_manual():
    db = _make_db(account_id=None, manual_override="50000")

    with patch(_PATCH_SYNC, return_value=False):
        value, source = get_portfolio_net_liquidation(db)

    assert value == pytest.approx(50_000.0)
    assert source == "manual_override"
    db.cleanup()


# ---------------------------------------------------------------------------
# 6a-9: net_liquidation = 0 in DB (IB reported 0) → treated as unavailable
# ---------------------------------------------------------------------------

def test_zero_net_liquidation_treated_as_unavailable():
    db = _make_db(
        net_liquidation=0.0,
        last_sync=_fresh_sync_ts(),
        failsafe="manual_only",
        manual_override="60000",
    )
    with patch(_PATCH_SYNC, return_value=False):
        value, source = get_portfolio_net_liquidation(db)

    # net_liq == 0 → IB path skipped → fallsafe → manual_override
    assert value == pytest.approx(60_000.0)
    assert source == "manual_override"
    db.cleanup()


# ---------------------------------------------------------------------------
# 6a-10: Wrong RISK_ACCOUNT_ID (different from what's in DB) → raises
# ---------------------------------------------------------------------------

def test_wrong_risk_account_id_raises():
    db = _make_db(
        account_id="DU123456",
        risk_account_id="DU999999",  # mismatch
        failsafe="deny",
    )
    with patch(_PATCH_SYNC, return_value=False):
        with pytest.raises(CapitalUnavailableError):
            get_portfolio_net_liquidation(db)
    db.cleanup()


# ---------------------------------------------------------------------------
# 6a-11: CAPITAL_STALENESS_MINUTES=0 → always considered stale → refresh called
# ---------------------------------------------------------------------------

def test_staleness_zero_always_refreshes():
    db = _make_db(last_sync=_fresh_sync_ts(), staleness_minutes=0)

    refresh_calls = []

    def fake_refresh(db_manager):
        refresh_calls.append(True)
        with sqlite3.connect(db_manager.db_file) as con:
            con.execute(
                "UPDATE accounts SET last_sync = ? WHERE account_id = ?",
                (_fresh_sync_ts(), "DU123456"),
            )
            con.commit()
        return True

    with patch(_PATCH_SYNC, side_effect=fake_refresh):
        value, source = get_portfolio_net_liquidation(db)

    assert len(refresh_calls) == 1
    assert source == "ib"
    db.cleanup()


# ---------------------------------------------------------------------------
# 6a-12: Legacy get_net_liquidation not broken by new code (import check)
# ---------------------------------------------------------------------------

def test_legacy_get_net_liquidation_still_importable():
    from capital_provider import get_net_liquidation  # noqa: F401 — just import check
    assert callable(get_net_liquidation)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
