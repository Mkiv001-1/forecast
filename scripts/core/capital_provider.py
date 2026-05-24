"""
Capital provider — single source of truth for available trading capital.

Priority hierarchy (updated to use Portfolio History):
  1. MANUAL_CAPITAL_OVERRIDE (if set and > 0)
  2. Portfolio History data (refreshed if stale)
  3. Cached last-known value (with CAPITAL_SOURCE_STALE warning)

Portfolio History approach:
  - Uses portfolio_history table with row_type='summary' and ticker='__ACCOUNT_SUMMARY__'
  - Staleness check based on timestamp in portfolio_history
  - Forced refresh from IB if data is stale
  - Fallback to cached value if refresh fails
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

_FALLBACK_CAPITAL_CACHE: dict = {}


class CapitalUnavailableError(RuntimeError):
    """Raised when capital cannot be obtained and failsafe denies fallback."""


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
    """Trigger a synchronous IB portfolio refresh. Returns True on success."""
    try:
        from ib_gateway_client import sync_portfolio_with_ib_safe
        sync_portfolio_with_ib_safe(db_manager)
        return True
    except Exception as e:
        logger.warning(f"CAPITAL_SOURCE_STALE: IB refresh failed: {e}")
        return False


def _get_capital_from_portfolio_history(db_manager) -> Optional[dict]:
    """Query portfolio_history table for the most recent account summary."""
    try:
        import sqlite3
        with sqlite3.connect(db_manager.db_file) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                """
                SELECT net_liquidation, buying_power, available_funds, 
                       cash, maintenance_margin, timestamp
                FROM portfolio_history 
                WHERE row_type = 'summary' 
                AND ticker = '__ACCOUNT_SUMMARY__'
                ORDER BY timestamp DESC 
                LIMIT 1
                """
            ).fetchone()
            
            if row and row["net_liquidation"]:
                return dict(row)
            else:
                return None
    except Exception as e:
        logger.error(f"capital_provider: portfolio_history read error: {e}")
        return None


def get_net_liquidation(db_manager) -> float:
    """
    Return the best available NetLiquidation value using Portfolio History.

    Hierarchy:
      1. MANUAL_CAPITAL_OVERRIDE (if set and > 0)
      2. Portfolio History data (refreshed if stale)
      3. Cached last-known value (with CAPITAL_SOURCE_STALE warning)
    """
    manual = _manual_override(db_manager)
    if manual is not None:
        logger.debug(f"capital_provider: using MANUAL_CAPITAL_OVERRIDE = {manual}")
        return manual

    staleness = _staleness_minutes(db_manager)

    capital_data = _get_capital_from_portfolio_history(db_manager)
    if capital_data:
        last_sync = capital_data.get("timestamp", "")
        if _is_stale(last_sync, staleness):
            logger.info("capital_provider: Portfolio History data stale, triggering refresh…")
            refreshed = _refresh_ib_sync(db_manager)
            if refreshed:
                # Re-read after refresh
                capital_data = _get_capital_from_portfolio_history(db_manager)
            else:
                logger.warning("CAPITAL_SOURCE_STALE: using cached Portfolio History value")
        
        net_liq = float(capital_data.get("net_liquidation") or 0)
        if net_liq > 0:
            _FALLBACK_CAPITAL_CACHE["net_liquidation"] = net_liq
            logger.debug(
                f"capital_provider: NetLiquidation={net_liq:,.2f} "
                f"from portfolio_history (timestamp={capital_data.get('timestamp')})"
            )
            return net_liq

    # Last-resort: cache
    cached = _FALLBACK_CAPITAL_CACHE.get("net_liquidation")
    if cached:
        logger.warning(f"CAPITAL_SOURCE_STALE: returning cached NetLiquidation={cached:,.2f}")
        return float(cached)

    logger.error("capital_provider: no capital source available, returning 0")
    return 0.0


def get_portfolio_net_liquidation(db_manager) -> Tuple[float, str]:
    """
    Return NetLiquidation and source info for portfolio risk mode.
    
    Uses Portfolio History as the primary source.
    """
    capital_data = _get_capital_from_portfolio_history(db_manager)
    
    if capital_data:
        net_liq = float(capital_data.get("net_liquidation") or 0)
        if net_liq > 0:
            return net_liq, "portfolio_history"
    
    # Fallback to manual override
    manual = _manual_override(db_manager)
    if manual is not None:
        return manual, "manual_override"
    
    # No capital available
    raise CapitalUnavailableError("No capital data available in Portfolio History or manual override")


def get_capital_details(db_manager) -> dict:
    """
    Return detailed capital information from Portfolio History.

    Includes all capital fields for reporting and analysis.
    """
    capital_data = _get_capital_from_portfolio_history(db_manager)

    if capital_data:
        return {
            'net_liquidation': float(capital_data.get('net_liquidation', 0)),
            'buying_power': float(capital_data.get('buying_power', 0)),
            'available_funds': float(capital_data.get('available_funds', 0)),
            'cash': float(capital_data.get('cash', 0)),
            'maintenance_margin': float(capital_data.get('maintenance_margin', 0)),
            'timestamp': capital_data.get('timestamp'),
            'source': 'portfolio_history'
        }
    else:
        return {
            'net_liquidation': 0.0,
            'buying_power': 0.0,
            'available_funds': 0.0,
            'cash': 0.0,
            'maintenance_margin': 0.0,
            'timestamp': None,
            'source': 'none'
        }


def get_capital(db_manager) -> dict:
    """
    Backward-compatible capital payload for legacy callers (e.g. order_manager).

    Returns:
      {
        "status": "OK" | "UNAVAILABLE" | "ERROR",
        "net_liquidation": float,
        "source": str,
        "message": str,
      }
    """
    try:
        net_liq = float(get_net_liquidation(db_manager) or 0.0)
        if net_liq > 0:
            return {
                "status": "OK",
                "net_liquidation": net_liq,
                "source": "portfolio_history",
                "message": "",
            }
        return {
            "status": "UNAVAILABLE",
            "net_liquidation": 0.0,
            "source": "none",
            "message": "no_capital_source",
        }
    except CapitalUnavailableError as e:
        return {
            "status": "UNAVAILABLE",
            "net_liquidation": 0.0,
            "source": "none",
            "message": str(e),
        }
    except Exception as e:
        logger.error(f"capital_provider.get_capital failed: {e}")
        return {
            "status": "ERROR",
            "net_liquidation": 0.0,
            "source": "none",
            "message": str(e),
        }
