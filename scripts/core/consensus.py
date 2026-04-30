"""
Consensus engine: aggregates multi-model multi-method forecasts
into a single weighted signal for each ticker.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def calculate_consensus(forecasts: list, method_stats: dict = None) -> dict:
    """
    Aggregate a list of forecast dicts into a consensus signal.

    Each forecast dict must have: side, confidence, method, model.
    method_stats: optional dict {method: {win_rate: float, ...}}

    Returns:
        {signal, confidence, methods_long, methods_short, methods_neutral, rationale}
    """
    if not forecasts:
        return {
            "signal": "NEUTRAL",
            "confidence": 0.0,
            "methods_long": "",
            "methods_short": "",
            "methods_neutral": "",
            "rationale": "No forecasts available",
        }

    weighted_long  = 0.0
    weighted_short = 0.0
    total_weight   = 0.0

    methods_long    = []
    methods_short   = []
    methods_neutral = []

    for f in forecasts:
        side       = str(f.get("side", "NEUTRAL")).upper()
        confidence = float(f.get("confidence", 50)) / 100.0
        method     = str(f.get("method", ""))

        win_rate = 0.5
        if method_stats and method in method_stats:
            win_rate = float(method_stats[method].get("win_rate", 0.5))

        weight = confidence * win_rate
        total_weight += weight

        if side == "LONG":
            weighted_long  += weight
            methods_long.append(f"{method}({f.get('model','?')})")
        elif side == "SHORT":
            weighted_short += weight
            methods_short.append(f"{method}({f.get('model','?')})")
        else:
            methods_neutral.append(f"{method}({f.get('model','?')})")

    if total_weight == 0:
        signal     = "NEUTRAL"
        confidence = 0.0
    elif weighted_long >= weighted_short:
        signal     = "LONG"
        confidence = round(weighted_long / total_weight * 100, 1)
    else:
        signal     = "SHORT"
        confidence = round(weighted_short / total_weight * 100, 1)

    # Require at least 55% confidence and majority direction to avoid noise
    if confidence < 55:
        signal = "NEUTRAL"

    rationale = (
        f"LONG: {len(methods_long)} signals, SHORT: {len(methods_short)} signals, "
        f"NEUTRAL: {len(methods_neutral)} signals. "
        f"Weighted confidence: {confidence:.1f}%"
    )

    logger.info(f"📊 Consensus: {signal} {confidence:.1f}% ({len(forecasts)} forecasts)")

    return {
        "signal":          signal,
        "confidence":      confidence,
        "methods_long":    ", ".join(methods_long),
        "methods_short":   ", ".join(methods_short),
        "methods_neutral": ", ".join(methods_neutral),
        "rationale":       rationale,
    }


def save_consensus(db_manager, ticker: str, consensus: dict) -> bool:
    """Save consensus record to the consensus table."""
    try:
        record = {
            "date":            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ticker":          ticker,
            "signal":          consensus["signal"],
            "confidence":      consensus["confidence"],
            "methods_long":    consensus["methods_long"],
            "methods_short":   consensus["methods_short"],
            "methods_neutral": consensus["methods_neutral"],
            "rationale":       consensus["rationale"],
        }
        return db_manager.save_consensus(record)
    except Exception as e:
        logger.error(f"Error saving consensus for {ticker}: {e}")
        return False
