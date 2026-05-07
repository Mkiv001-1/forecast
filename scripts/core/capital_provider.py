"""
Capital provider — single source of truth for available trading capital.

Priority hierarchy:
  1. Live IB account (PREFERRED_ACCOUNT_TYPE = 'live')
  2. Paper IB account (fallback)
  3. MANUAL_CAPITAL_OVERRIDE config value

Staleness check: if the last IB sync is older than CAPITAL_STALENESS_MINUTES,
a forced refresh is triggered before returning the value.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_FALLBACK_CAPITAL_CACHE: dict = {}


def _get_config(db_manager, key: str, default: str = "") -> str:
    try:
        v = db_manager.get_config_value(key)
        return v if v is not None else default
    except Exception:
        return default


def _staleness_minutes(db_manager) -> int:
    try:
        return int(_get_config(db_manager, "CAPITAL_STALENESS_MINUTES", "15"))
    except ValueError:
        return 15


def _preferred_type(db_manager) -> str:
    return _get_config(db_manager, "PREFERRED_ACCOUNT_TYPE", "live").lower()


def _manual_override(db_manager) -> Optional[float]:
    raw = _get_config(db_manager, "MANUAL_CAPITAL_OVERRIDE", "")
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return None


def _is_stale(last_sync_str: str, staleness_minutes: int) -> bool:
    if not last_sync_str:
        return True
    try:
        last_sync = datetime.fromisoformat(last_sync_str.replace("Z", "+00:00"))
        if last_sync.tzinfo is None:
            last_sync = last_sync.replace(tzinfo=timezone.utc)
        threshold = datetime.now(tz=timezone.utc) - timedelta(minutes=staleness_minutes)
        return last_sync < threshold
    except Exception:
        return True


def _refresh_ib_sync(db_manager) -> bool:
    """Trigger a synchronous IB account refresh. Returns True on success."""
    try:
        from ib_gateway_client import sync_accounts_with_ib
        sync_accounts_with_ib(db_manager)
        return True
    except Exception as e:
        logger.warning(f"CAPITAL_SOURCE_STALE: IB refresh failed: {e}")
        return False


def _get_account_from_db(db_manager, preferred_type: str) -> Optional[dict]:
    """Query accounts table for the best available account."""
    try:
        import sqlite3
        with sqlite3.connect(db_manager.db_file) as con:
            con.row_factory = sqlite3.Row
            # Prefer requested type
            row = con.execute(
                "SELECT * FROM accounts WHERE LOWER(type) = ? ORDER BY net_liquidation DESC LIMIT 1",
                (preferred_type,)
            ).fetchone()
            if row:
                return dict(row)
            # Fallback: any account
            row = con.execute(
                "SELECT * FROM accounts ORDER BY net_liquidation DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"capital_provider: DB read error: {e}")
        return None


def _write_last_sync(db_manager, account_id: str) -> None:
    """Write current UTC time to accounts.last_sync for the given account."""
    try:
        import sqlite3
        now = datetime.now(tz=timezone.utc).isoformat()
        with sqlite3.connect(db_manager.db_file) as con:
            con.execute(
                "UPDATE accounts SET last_sync = ? WHERE account_id = ?",
                (now, account_id)
            )
            con.commit()
    except Exception as e:
        logger.warning(f"capital_provider: could not write last_sync: {e}")


def get_net_liquidation(db_manager) -> float:
    """
    Return the best available NetLiquidation value.

    Hierarchy:
      1. MANUAL_CAPITAL_OVERRIDE (if set and > 0)
      2. IB live account (refreshed if stale)
      3. IB paper account (refreshed if stale)
      4. Cached last-known value (with CAPITAL_SOURCE_STALE warning)
    """
    manual = _manual_override(db_manager)
    if manual is not None:
        logger.debug(f"capital_provider: using MANUAL_CAPITAL_OVERRIDE = {manual}")
        return manual

    staleness = _staleness_minutes(db_manager)
    preferred = _preferred_type(db_manager)

    account = _get_account_from_db(db_manager, preferred)
    if account:
        last_sync = account.get("last_sync", "")
        if _is_stale(last_sync, staleness):
            logger.info("capital_provider: IB data stale, triggering refresh…")
            refreshed = _refresh_ib_sync(db_manager)
            if refreshed:
                # Re-read after refresh
                account = _get_account_from_db(db_manager, preferred)
                if account:
                    _write_last_sync(db_manager, account["account_id"])
            else:
                logger.warning("CAPITAL_SOURCE_STALE: using cached IB value")
        net_liq = float(account.get("net_liquidation") or 0)
        if net_liq > 0:
            _FALLBACK_CAPITAL_CACHE["net_liquidation"] = net_liq
            logger.debug(
                f"capital_provider: NetLiquidation={net_liq:,.2f} "
                f"from account {account.get('account_id')} (type={account.get('type')})"
            )
            return net_liq

    # Last-resort: cache
    cached = _FALLBACK_CAPITAL_CACHE.get("net_liquidation")
    if cached:
        logger.warning(f"CAPITAL_SOURCE_STALE: returning cached NetLiquidation={cached:,.2f}")
        return float(cached)

    logger.error("capital_provider: no capital source available, returning 0")
    return 0.0


async def get_net_liquidation_async(db_manager) -> float:
    """Async wrapper for use in FastAPI endpoints."""
    import asyncio
    return await asyncio.get_event_loop().run_in_executor(None, get_net_liquidation, db_manager)
