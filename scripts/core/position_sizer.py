"""
Position sizer — calculates safe position size for a given signal.

Logic:
  1. risk = NetLiquidation × DEFAULT_RISK_PCT
  2. qty  = risk / |entry - stop_loss|
  3. Clamp to MAX_POSITION_PCT × NetLiquidation / entry
  4. Sector correlation check:
       > MAX_SECTOR_EXPOSURE_PCT  → qty × SECTOR_OVERWEIGHT_FACTOR
       > MAX_SECTOR_HARD_LIMIT_PCT → reject (SKIPPED_SECTOR_OVERWEIGHT)
"""

import logging
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)


def _cfg(db_manager, key: str, default: float) -> float:
    try:
        v = db_manager.get_config_value(key)
        return float(v) if v is not None and str(v).strip() else default
    except Exception:
        return default


def _get_sector_exposure(db_manager, sector: str) -> float:
    """Return total market value of portfolio positions in the given sector."""
    if not sector:
        return 0.0
    try:
        with sqlite3.connect(db_manager.db_file) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT p.market_value, s.sector
                FROM portfolio p
                LEFT JOIN settings s ON UPPER(p.ticker) = UPPER(s.ticker)
                WHERE LOWER(COALESCE(s.sector, '')) = LOWER(?)
                """,
                (sector,)
            ).fetchall()
        return sum(float(r["market_value"] or 0) for r in rows)
    except Exception as e:
        logger.warning(f"position_sizer: sector exposure query failed: {e}")
        return 0.0


def _get_ticker_sector(db_manager, ticker: str) -> str:
    """Lookup sector for a ticker from settings table."""
    try:
        with sqlite3.connect(db_manager.db_file) as con:
            row = con.execute(
                "SELECT sector FROM settings WHERE UPPER(ticker) = UPPER(?)",
                (ticker,)
            ).fetchone()
        return (row[0] or "").strip() if row else ""
    except Exception:
        return ""


def calculate_position(
    ticker: str,
    entry_price: float,
    stop_loss: float,
    db_manager,
    net_liquidation: Optional[float] = None,
) -> dict:
    """
    Calculate safe position size.

    Returns dict with keys:
      quantity        (int, shares)
      risk_amount     (float, USD at risk)
      risk_pct        (float, fraction of NetLiquidation)
      position_value  (float, entry × quantity)
      sector          (str)
      sector_exposure_at_signal (float, existing sector exposure USD)
      status          (str: OK | SKIPPED_SECTOR_OVERWEIGHT | SKIPPED_ZERO_RISK |
                            SKIPPED_INVALID_STOP | SKIPPED_NO_CAPITAL)
    """
    if net_liquidation is None:
        from capital_provider import get_net_liquidation
        net_liquidation = get_net_liquidation(db_manager)

    if net_liquidation <= 0:
        return _reject("SKIPPED_NO_CAPITAL", ticker, 0, 0, "", 0)

    if stop_loss <= 0 or entry_price <= 0:
        return _reject("SKIPPED_INVALID_STOP", ticker, net_liquidation, 0, "", 0)

    stop_distance = abs(entry_price - stop_loss)
    if stop_distance < 0.0001:
        return _reject("SKIPPED_ZERO_RISK", ticker, net_liquidation, 0, "", 0)

    default_risk_pct    = _cfg(db_manager, "DEFAULT_RISK_PCT",          0.01)
    max_position_pct    = _cfg(db_manager, "MAX_POSITION_PCT",          0.05)
    soft_limit_pct      = _cfg(db_manager, "MAX_SECTOR_EXPOSURE_PCT",   0.15)
    hard_limit_pct      = _cfg(db_manager, "MAX_SECTOR_HARD_LIMIT_PCT", 0.25)
    overweight_factor   = _cfg(db_manager, "SECTOR_OVERWEIGHT_FACTOR",  0.5)

    risk_amount = net_liquidation * default_risk_pct
    qty_by_risk = risk_amount / stop_distance

    max_qty_by_position = (net_liquidation * max_position_pct) / entry_price
    qty = min(qty_by_risk, max_qty_by_position)

    # --- Sector check ---
    sector = _get_ticker_sector(db_manager, ticker)
    sector_exposure = _get_sector_exposure(db_manager, sector) if sector else 0.0
    sector_exposure_pct = sector_exposure / net_liquidation if net_liquidation > 0 else 0.0

    if sector_exposure_pct > hard_limit_pct:
        logger.warning(
            f"position_sizer: {ticker} sector '{sector}' "
            f"at {sector_exposure_pct*100:.1f}% > hard limit {hard_limit_pct*100:.0f}% → rejected"
        )
        return _reject("SKIPPED_SECTOR_OVERWEIGHT", ticker, net_liquidation, risk_amount, sector, sector_exposure)

    if sector_exposure_pct > soft_limit_pct:
        logger.info(
            f"position_sizer: {ticker} sector '{sector}' "
            f"at {sector_exposure_pct*100:.1f}% > soft limit → qty × {overweight_factor}"
        )
        qty *= overweight_factor

    qty = max(0, int(qty))
    position_value = qty * entry_price
    actual_risk_pct = risk_amount / net_liquidation

    logger.info(
        f"position_sizer: {ticker} qty={qty} entry={entry_price:.2f} "
        f"stop={stop_loss:.2f} risk={risk_amount:.2f} ({actual_risk_pct*100:.2f}%)"
    )

    return {
        "quantity":                  qty,
        "risk_amount":               round(risk_amount, 2),
        "risk_pct":                  round(actual_risk_pct, 6),
        "position_value":            round(position_value, 2),
        "sector":                    sector,
        "sector_exposure_at_signal": round(sector_exposure, 2),
        "status":                    "OK",
    }


def _reject(reason: str, ticker: str, net_liq: float, risk_amount: float, sector: str, sector_exp: float) -> dict:
    logger.warning(f"position_sizer: {ticker} rejected — {reason}")
    return {
        "quantity":                  0,
        "risk_amount":               round(risk_amount, 2),
        "risk_pct":                  0.0,
        "position_value":            0.0,
        "sector":                    sector,
        "sector_exposure_at_signal": round(sector_exp, 2),
        "status":                    reason,
    }
