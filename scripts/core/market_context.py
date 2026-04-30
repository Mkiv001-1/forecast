"""
Market context loader: fetches SPY/VIX to enrich forecast prompts.
"""

import logging

logger = logging.getLogger(__name__)


def fetch_market_context(db_manager=None) -> dict:
    """
    Load SPY and VIX recent data to provide market context in prompts.
    Returns a dict with keys: spy_change_1d, spy_change_5d, vix_current.
    Falls back to empty values on any error.
    """
    ctx = {
        "spy_change_1d": None,
        "spy_change_5d": None,
        "vix_current":   None,
    }
    try:
        from data_loader import fetch_price_data_yfinance
        spy = fetch_price_data_yfinance("SPY", days=10)
        if spy and len(spy) >= 2:
            ctx["spy_change_1d"] = round(
                (spy[-1]["close"] - spy[-2]["close"]) / spy[-2]["close"] * 100, 2
            )
        if spy and len(spy) >= 6:
            ctx["spy_change_5d"] = round(
                (spy[-1]["close"] - spy[-6]["close"]) / spy[-6]["close"] * 100, 2
            )
    except Exception as e:
        logger.debug(f"Could not load SPY: {e}")

    try:
        from data_loader import fetch_price_data_yfinance
        vix = fetch_price_data_yfinance("^VIX", days=5)
        if vix:
            ctx["vix_current"] = round(vix[-1]["close"], 2)
    except Exception as e:
        logger.debug(f"Could not load VIX: {e}")

    return ctx


def format_market_context(ctx: dict) -> str:
    """Format market context dict for inclusion in a prompt."""
    parts = []
    if ctx.get("spy_change_1d") is not None:
        parts.append(f"S&P 500 (1d): {ctx['spy_change_1d']:+.2f}%")
    if ctx.get("spy_change_5d") is not None:
        parts.append(f"S&P 500 (5d): {ctx['spy_change_5d']:+.2f}%")
    if ctx.get("vix_current") is not None:
        level = "high" if ctx["vix_current"] > 25 else ("low" if ctx["vix_current"] < 15 else "normal")
        parts.append(f"VIX: {ctx['vix_current']:.1f} ({level} volatility)")
    return ", ".join(parts) if parts else "Market context unavailable"
