"""
Модуль оценки консенсусных прогнозов.

Логика:
1. Выбирает записи consensus WHERE eval_status = 'PENDING' AND eval_target_date <= now
2. Загружает фактические данные (price_data) за eval_target_date
3. Определяет entry_price_actual (close бара на дату консенсуса)
4. Рассчитывает target_hit, stop_hit, direction_correct, pnl_pct, r_multiple
5. Обновляет запись: eval_status = 'EVALUATED' | 'NO_DATA'
"""

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


def _to_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        v = float(val)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _compute_r_multiple(pnl_pct: float, entry: float, stop: float, signal: str) -> Optional[float]:
    """R-multiple = PnL / distance_to_stop (both in %)."""
    try:
        if not entry or not stop or entry <= 0 or stop <= 0:
            return None
        risk_pct = abs(entry - stop) / entry * 100
        if risk_pct == 0:
            return None
        return round(pnl_pct / risk_pct, 3)
    except Exception:
        return None


def evaluate_consensus_records(db_manager) -> int:
    """Evaluate all PENDING consensus records whose eval_target_date has passed.

    Returns:
        int: number of records evaluated
    """
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with db_manager._connect() as con:
            df = pd.read_sql_query(
                """
                SELECT * FROM consensus
                WHERE eval_status IN ('PENDING', 'NO_DATA')
                  AND eval_target_date IS NOT NULL
                  AND eval_target_date != ''
                  AND eval_target_date <= ?
                ORDER BY eval_target_date ASC
                """,
                con,
                params=[now_str],
            )

        if df.empty:
            logger.info("consensus_evaluator: no pending records ready for evaluation")
            return 0

        total = len(df)
        logger.info(f"consensus_evaluator: starting evaluation of {total} pending records")

        evaluated = 0
        errors = 0
        no_data = 0

        for idx, row in df.iterrows():
            rec_id    = int(row["id"])
            ticker    = str(row["ticker"] or "")
            signal    = str(row["signal"] or "NEUTRAL").upper()
            cons_date = str(row["date"] or "")
            eval_date = str(row["eval_target_date"] or "")
            target_price = _to_float(row.get("target_price"))
            stop_loss    = _to_float(row.get("stop_loss"))
            entry_limit_price = _to_float(row.get("entry_limit_price"))
            horizon_hours = row.get("horizon_hours")
            if horizon_hours is not None:
                try:
                    horizon_hours = int(horizon_hours)
                except (TypeError, ValueError):
                    horizon_hours = None

            logger.info(f"consensus_evaluator: [{idx+1}/{total}] processing id={rec_id} {ticker} {signal} target_date={eval_date}")

            try:
                result_status = _evaluate_one(
                    db_manager=db_manager,
                    rec_id=rec_id,
                    ticker=ticker,
                    signal=signal,
                    cons_date=cons_date,
                    eval_target_date=eval_date,
                    target_price=target_price,
                    stop_loss=stop_loss,
                    entry_limit_price=entry_limit_price,
                    horizon_hours=horizon_hours,
                )
                if result_status == "NO_DATA":
                    no_data += 1
                    logger.info(f"consensus_evaluator: [{idx+1}/{total}] id={rec_id} → NO_DATA (no price data)")
                elif result_status == "NO_DATA_INTRADAY":
                    no_data += 1
                    logger.info(f"consensus_evaluator: [{idx+1}/{total}] id={rec_id} → NO_DATA_INTRADAY (intraday horizon needs intraday bars)")
                elif result_status == "PENDING":
                    logger.info(f"consensus_evaluator: [{idx+1}/{total}] id={rec_id} → PENDING (data not yet available)")
                else:
                    evaluated += 1
                    logger.info(f"consensus_evaluator: [{idx+1}/{total}] id={rec_id} → EVALUATED")
            except Exception as e:
                errors += 1
                logger.error(f"consensus_evaluator: [{idx+1}/{total}] error evaluating id={rec_id}: {e}")

        logger.info(f"consensus_evaluator: completed. evaluated={evaluated}/{total}, no_data={no_data}, errors={errors}")
        return evaluated

    except Exception as e:
        logger.error(f"consensus_evaluator: top-level error: {e}")
        return 0


def _load_price_data_from_db(db_manager, ticker: str, date_from: str) -> list:
    """Load price bars from price_data table for a ticker, starting from date_from."""
    try:
        with db_manager._connect() as con:
            rows = con.execute(
                "SELECT date, open, high, low, close, volume FROM price_data "
                "WHERE ticker = ? AND date >= ? ORDER BY date",
                (ticker, date_from),
            ).fetchall()
        result = []
        for r in rows:
            result.append({
                "date":   r[0],
                "open":   r[1],
                "high":   r[2],
                "low":    r[3],
                "close":  r[4],
                "volume": r[5],
            })
        return result
    except Exception as e:
        logger.warning(f"consensus_evaluator: db price_data query failed for {ticker}: {e}")
        return []


def _evaluate_one(
    db_manager,
    rec_id: int,
    ticker: str,
    signal: str,
    cons_date: str,
    eval_target_date: str,
    target_price: Optional[float],
    stop_loss: Optional[float],
    entry_limit_price: Optional[float] = None,
    horizon_hours: Optional[int] = None,
) -> str:
    """Evaluate one consensus record and persist results. Returns status: 'EVALUATED' or 'NO_DATA'."""
    # --- Intraday check: daily bars insufficient for horizon < 24h ---
    if horizon_hours and horizon_hours < 24:
        logger.warning(
            f"consensus_evaluator: id={rec_id} intraday horizon ({horizon_hours}h) — "
            f"cannot evaluate with daily bars only, need intraday data"
        )
        _save_eval(db_manager, rec_id, eval_status="NO_DATA_INTRADAY")
        return "NO_DATA_INTRADAY"

    # --- Load price data around eval_target_date ---
    try:
        target_dt = datetime.fromisoformat(eval_target_date[:19])
    except Exception:
        logger.warning(f"consensus_evaluator: bad eval_target_date '{eval_target_date}' for id={rec_id}")
        _save_eval(db_manager, rec_id, eval_status="NO_DATA")
        return "NO_DATA"

    # Date window: from consensus date (or 30 days before target)
    cons_date_str = cons_date[:10] if cons_date else ""
    if cons_date_str:
        date_from = cons_date_str
    else:
        from datetime import timedelta
        date_from = (target_dt - timedelta(days=30)).strftime("%Y-%m-%d")

    # Try DB cache first (fast, no network)
    price_data = _load_price_data_from_db(db_manager, ticker, date_from)

    # Fallback to fetch_price_data if DB has no data
    if not price_data:
        today = datetime.now().date()
        days_back = (today - target_dt.date()).days + 14
        days_needed = max(days_back, 30)
        try:
            from data_loader import fetch_price_data
            price_data = fetch_price_data(ticker, days=days_needed, db_manager=db_manager)
        except Exception as e:
            logger.warning(f"consensus_evaluator: fetch_price_data failed for {ticker}: {e}")

    if not price_data:
        logger.warning(f"consensus_evaluator: no price data for {ticker} id={rec_id}")
        _save_eval(db_manager, rec_id, eval_status="NO_DATA")
        return "NO_DATA"

    # --- Find bar for eval_target_date ---
    target_date_str = target_dt.strftime("%Y-%m-%d")
    cons_date_only = cons_date[:10] if cons_date else ""
    actual_bar = _find_bar(price_data, target_date_str)

    # If no exact bar and target date is already in the past, use nearest available bar
    # (handles weekends, holidays — next/prev trading day)
    if actual_bar is None and target_dt.date() <= datetime.now().date():
        actual_bar = _find_nearest_bar_on_or_after(price_data, target_date_str)
        if actual_bar is not None:
            logger.info(
                f"consensus_evaluator: no exact bar for {ticker} on {target_date_str}, "
                f"using nearest: {actual_bar['date']}"
            )

    # Validate: actual_bar must be strictly after consensus date
    # If the only available bar is on/before cons_date, the evaluation data hasn't arrived yet
    if actual_bar is not None and cons_date_only:
        actual_bar_date = actual_bar["date"]
        if hasattr(actual_bar_date, "strftime"):
            actual_bar_date = actual_bar_date.strftime("%Y-%m-%d")
        else:
            actual_bar_date = str(actual_bar_date)[:10]
        if actual_bar_date <= cons_date_only:
            logger.info(
                f"consensus_evaluator: id={rec_id} {ticker} actual_bar={actual_bar_date} "
                f"is not after cons_date={cons_date_only}, keeping PENDING"
            )
            _save_eval(db_manager, rec_id, eval_status="PENDING")
            return "PENDING"

    if actual_bar is None:
        logger.warning(f"consensus_evaluator: no bar for {ticker} on {target_date_str} (target in future or no data)")
        _save_eval(db_manager, rec_id, eval_status="NO_DATA")
        return "NO_DATA"

    actual_close = float(actual_bar["close"])
    actual_high  = float(actual_bar["high"])
    actual_low   = float(actual_bar["low"])
    actual_open  = float(actual_bar["open"])
    actual_date  = str(actual_bar["date"])
    if hasattr(actual_bar["date"], "strftime"):
        actual_date = actual_bar["date"].strftime("%Y-%m-%d")

    # --- entry_price_actual: price used for PnL calculation ---
    # Priority: 1) entry_limit_price from consensus (real trading base)
    #           2) close of consensus date (historical fallback)
    #           3) last bar before eval_target_date (data availability fallback)
    #           4) actual_close (last resort)
    entry_price_actual: Optional[float] = None

    # 1) Use entry_limit_price from consensus as primary base (matches real trading)
    if entry_limit_price and entry_limit_price > 0:
        entry_price_actual = entry_limit_price
        logger.debug(f"consensus_evaluator: id={rec_id} using entry_limit_price={entry_limit_price} as base")
    elif cons_date:
        try:
            cons_date_str = cons_date[:10]
            entry_bar = _find_bar(price_data, cons_date_str, fallback_nearest=False)
            if entry_bar:
                entry_price_actual = float(entry_bar["close"])
                logger.debug(f"consensus_evaluator: id={rec_id} using cons_date close={entry_price_actual}")
        except Exception:
            pass

    # Fallback: last bar strictly before eval_target_date
    if entry_price_actual is None:
        entry_bar = _find_last_bar_before(price_data, target_date_str)
        if entry_bar:
            entry_price_actual = float(entry_bar["close"])
            logger.debug(f"consensus_evaluator: id={rec_id} using last bar before target={entry_price_actual}")

    # Use entry_price_actual as reference; fallback to actual_close
    entry = entry_price_actual or actual_close

    # --- target_hit / stop_hit ---
    target_hit = False
    stop_hit   = False

    if signal == "LONG":
        if target_price:
            target_hit = actual_high >= target_price
        if stop_loss:
            stop_hit = actual_low <= stop_loss
    elif signal == "SHORT":
        if target_price:
            target_hit = actual_low <= target_price
        if stop_loss:
            stop_hit = actual_high >= stop_loss

    # --- First Hit Analysis (when both hit on same day) ---
    # Determine which level was hit first using distance from open
    first_hit = None  # 'target' | 'stop' | None
    if target_hit and stop_hit and actual_open:
        # Calculate distance from open to target and stop
        if signal == "LONG":
            dist_to_target = abs(target_price - actual_open) if target_price else None
            dist_to_stop = abs(stop_loss - actual_open) if stop_loss else None
        else:  # SHORT
            dist_to_target = abs(actual_open - target_price) if target_price else None
            dist_to_stop = abs(actual_open - stop_loss) if stop_loss else None
        
        if dist_to_target and dist_to_stop:
            # The level closer to open was hit first (conservatively)
            if dist_to_target < dist_to_stop:
                first_hit = "target"
            elif dist_to_stop < dist_to_target:
                first_hit = "stop"
            else:
                # Equal distance - ambiguous, default to stop (conservative)
                first_hit = "stop"
    elif target_hit and not stop_hit:
        first_hit = "target"
    elif stop_hit and not target_hit:
        first_hit = "stop"

    # Stop priority: if stop triggered → failure
    # direction_correct based on actual_close vs entry
    direction_correct = False
    if signal == "LONG":
        direction_correct = actual_close > entry
    elif signal == "SHORT":
        direction_correct = actual_close < entry

    # pnl_pct: use stop price if stop triggered, otherwise actual_close
    if stop_hit and stop_loss:
        exit_price = stop_loss
    else:
        exit_price = actual_close

    pnl_pct = 0.0
    if entry and entry > 0 and exit_price and exit_price > 0:
        if signal == "LONG":
            pnl_pct = round((exit_price - entry) / entry * 100, 2)
        elif signal == "SHORT":
            pnl_pct = round((entry - exit_price) / entry * 100, 2)

    r_multiple = _compute_r_multiple(pnl_pct, entry, stop_loss, signal) if stop_loss else None

    _save_eval(
        db_manager=db_manager,
        rec_id=rec_id,
        eval_status="EVALUATED",
        actual_date=actual_date,
        actual_open=actual_open,
        actual_close=actual_close,
        actual_high=actual_high,
        actual_low=actual_low,
        entry_price_actual=entry_price_actual,
        target_hit=int(target_hit),
        stop_hit=int(stop_hit),
        first_hit=first_hit,
        direction_correct=int(direction_correct),
        pnl_pct=pnl_pct,
        r_multiple=r_multiple,
    )
    logger.info(
        f"consensus_evaluator: id={rec_id} {ticker} {signal} → "
        f"dir={direction_correct} target_hit={target_hit} stop_hit={stop_hit} first_hit={first_hit} "
        f"pnl={pnl_pct:.2f}% R={r_multiple}"
    )
    return "EVALUATED"


def _find_last_bar_before(price_data: list, date_str: str) -> Optional[dict]:
    """Return the last price bar whose date is strictly before date_str (YYYY-MM-DD)."""
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return None
    candidates = []
    for rec in price_data:
        d = rec["date"]
        if hasattr(d, "date"):
            d = d.date()
        elif isinstance(d, str):
            try:
                d = datetime.strptime(d[:10], "%Y-%m-%d").date()
            except Exception:
                continue
        if d < target:
            candidates.append((d, rec))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def _find_nearest_bar_on_or_after(price_data: list, date_str: str) -> Optional[dict]:
    """Return the nearest bar on or after date_str. Falls back to last bar before if none found after."""
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return None

    def _to_date(r):
        d = r["date"]
        if hasattr(d, "date"):
            return d.date()
        elif isinstance(d, str):
            try:
                return datetime.strptime(d[:10], "%Y-%m-%d").date()
            except Exception:
                return None
        return None

    on_or_after = [(d, r) for r in price_data if (d := _to_date(r)) is not None and d >= target]
    if on_or_after:
        on_or_after.sort(key=lambda x: x[0])
        return on_or_after[0][1]

    # Fallback: last bar before target date
    return _find_last_bar_before(price_data, date_str)


def _find_bar(price_data: list, date_str: str, fallback_nearest: bool = False) -> Optional[dict]:
    """Find price bar for given date_str (YYYY-MM-DD). Optionally return nearest."""
    for rec in price_data:
        d = rec["date"]
        d_str = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
        if d_str == date_str:
            return rec

    if not fallback_nearest or not price_data:
        return None

    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()

        def _dist(r):
            d = r["date"]
            if hasattr(d, "date"):
                d = d.date()
            elif isinstance(d, str):
                d = datetime.strptime(d[:10], "%Y-%m-%d").date()
            return abs((d - target).days)

        return min(price_data, key=_dist)
    except Exception:
        return None


def _save_eval(db_manager, rec_id: int, eval_status: str, **kwargs) -> None:
    """Persist evaluation results back to consensus table."""
    updates = {"eval_status": eval_status}
    updates.update(kwargs)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [rec_id]
    try:
        with db_manager._connect() as con:
            con.execute(f"UPDATE consensus SET {set_clause} WHERE id = ?", values)
        logger.debug(f"consensus_evaluator: saved eval id={rec_id} status={eval_status}")
    except Exception as e:
        logger.error(f"consensus_evaluator: failed to save eval for id={rec_id}: {e}")
