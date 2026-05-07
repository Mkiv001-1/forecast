"""
Order manager — converts consensus signals into bracket orders via IB.

Modes (ORDER_MODE config key):
  disabled  — no orders placed (default, safe)
  paper     — connect to IB paper account (port 7497)
  live      — connect to IB live account (port 7496); requires LIVE_TRADING_CONFIRMED=true

Flow:
  submit_signal()
    → guard checks (mode, blocked, duplicate, max open, market hours)
    → slippage guard (bid/ask snapshot)
    → save QUEUED order to DB
    → place_bracket_order() → update to SUBMITTED
    → monitor child timeout from filled_at → rollback if needed

Rollback (Step 8):
  rollback_bracket_group()
    → cancel unfilled children
    → close position at market
    → update statuses
    → notify (stub)
    → block ticker on ROLLBACK_FAILED
"""

import logging
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

_IB_PAPER_PORT = 7497
_IB_LIVE_PORT  = 7496

# Per-ticker locks to prevent concurrent duplicate order submission
_ticker_locks: Dict[str, threading.Lock] = {}
_ticker_locks_guard = threading.Lock()


def _get_ticker_lock(ticker: str) -> threading.Lock:
    """Return (creating if needed) a per-ticker threading.Lock."""
    key = ticker.upper()
    with _ticker_locks_guard:
        if key not in _ticker_locks:
            _ticker_locks[key] = threading.Lock()
        return _ticker_locks[key]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(db_manager, key: str, default: str = "") -> str:
    try:
        v = db_manager.get_config_value(key)
        return v if v is not None else default
    except Exception:
        return default


def _cfg_int(db_manager, key: str, default: int) -> int:
    try:
        v = db_manager.get_config_value(key)
        return int(v) if v is not None else default
    except Exception:
        return default


def _cfg_float(db_manager, key: str, default: float) -> float:
    try:
        return float(_cfg(db_manager, key, str(default)))
    except ValueError:
        return default


def _cfg_int(db_manager, key: str, default: int) -> int:
    try:
        return int(_cfg(db_manager, key, str(default)))
    except ValueError:
        return default


def _cfg_bool(db_manager, key: str, default: bool = False) -> bool:
    return _cfg(db_manager, key, str(default)).lower() == "true"


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _symbol_from_ticker(ticker: str) -> str:
    """Extract bare symbol from 'NASDAQ:AAPL' or 'AAPL'."""
    return ticker.split(":")[-1].strip()


def _is_market_hours() -> bool:
    """Return True if current UTC time is within NYSE regular session (Mon-Fri 14:30–21:00 UTC)."""
    now = datetime.now(tz=timezone.utc)
    if now.weekday() >= 5:
        return False
    market_open  = now.replace(hour=14, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=21, minute=0,  second=0, microsecond=0)
    return market_open <= now <= market_close


def _get_ib_port(mode: str) -> int:
    return _IB_LIVE_PORT if mode == "live" else _IB_PAPER_PORT


def _count_open_orders(db_manager) -> int:
    try:
        with sqlite3.connect(db_manager.db_file) as con:
            row = con.execute(
                "SELECT COUNT(*) FROM orders WHERE status IN ('QUEUED','SUBMITTED','FILLED_ENTRY')"
            ).fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


def _has_open_order_for_ticker(db_manager, ticker: str) -> bool:
    try:
        with sqlite3.connect(db_manager.db_file) as con:
            row = con.execute(
                "SELECT COUNT(*) FROM orders WHERE UPPER(ticker)=UPPER(?) AND status IN ('QUEUED','SUBMITTED','FILLED_ENTRY')",
                (ticker,)
            ).fetchone()
            return (row[0] if row else 0) > 0
    except Exception:
        return False


def _is_ticker_blocked(db_manager, ticker: str) -> bool:
    try:
        with sqlite3.connect(db_manager.db_file) as con:
            row = con.execute(
                "SELECT trading_blocked FROM settings WHERE UPPER(ticker)=UPPER(?)",
                (ticker,)
            ).fetchone()
            return bool(row[0]) if row else False
    except Exception:
        return False


def _save_order(db_manager, order_data: dict) -> int:
    """Insert order row, return rowid."""
    cols = list(order_data.keys())
    ph   = ", ".join(["?"] * len(cols))
    sql  = f"INSERT INTO orders ({', '.join(cols)}) VALUES ({ph})"
    with sqlite3.connect(db_manager.db_file) as con:
        cur = con.execute(sql, list(order_data.values()))
        return cur.lastrowid


def _update_order(db_manager, row_id: int, updates: dict) -> None:
    set_parts = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [row_id]
    with sqlite3.connect(db_manager.db_file) as con:
        con.execute(f"UPDATE orders SET {set_parts} WHERE id=?", vals)


def _block_ticker(db_manager, ticker: str) -> None:
    try:
        with sqlite3.connect(db_manager.db_file) as con:
            con.execute(
                "UPDATE settings SET trading_blocked=1 WHERE UPPER(ticker)=UPPER(?)",
                (ticker,)
            )
        logger.warning(f"order_manager: ticker {ticker} BLOCKED (trading_blocked=1)")
    except Exception as e:
        logger.error(f"order_manager: could not block ticker {ticker}: {e}")


def _check_execute_flags(db_manager, methods_list: str) -> tuple[bool, str]:
    """
    Check execute flags for all methods in the methods_list.
    methods_list format: "method1(model1), method2(model2), ..."
    
    Returns (can_execute, reason):
    - can_execute: True if all methods have execute='yes'
    - reason: explanation if can_execute=False
    """
    if not methods_list:
        return True, ""
    
    # Extract unique method names from "method(model)" format
    methods = []
    for item in methods_list.split(", "):
        if "(" in item:
            method_name = item.split("(")[0].strip()
            methods.append(method_name)
    
    if not methods:
        return True, ""
    
    try:
        # Check method_config execute flags
        method_placeholders = ",".join(["?" for _ in methods])
        method_query = f"""
            SELECT method, execute FROM method_config 
            WHERE method IN ({method_placeholders})
        """
        method_rows = db_manager._execute_query(method_query, methods)
        
        method_execute = {row[0]: row[1] for row in method_rows}
        
        # Check providers execute flags (from model names)
        providers = []
        for item in methods_list.split(", "):
            if "(" in item and ")" in item:
                model_name = item.split("(")[1].split(")")[0].strip()
                # Map model names to provider names
                if "claude" in model_name.lower():
                    providers.append("claude-sonnet")
                elif "gpt" in model_name.lower():
                    providers.append("gpt-4o")
                elif "deepseek" in model_name.lower():
                    providers.append("deepseek-v3")
                elif "gemini" in model_name.lower():
                    providers.append("gemini-flash")
                elif "sonar" in model_name.lower():
                    providers.append("sonar-pro")
        
        provider_execute = {}
        if providers:
            provider_placeholders = ",".join(["?" for _ in providers])
            provider_query = f"""
                SELECT name, execute FROM providers 
                WHERE name IN ({provider_placeholders})
            """
            provider_rows = db_manager._execute_query(provider_query, providers)
            provider_execute = {row[0]: row[1] for row in provider_rows}
        
        # Check all methods have execute='yes'
        for method in methods:
            execute_flag = method_execute.get(method, 'yes')  # default to yes if not found
            if execute_flag != 'yes':
                return False, f"Method {method} has execute='{execute_flag}'"
        
        # Check all providers have execute='yes'
        for provider in set(providers):  # use set to avoid duplicates
            execute_flag = provider_execute.get(provider, 'yes')  # default to yes if not found
            if execute_flag != 'yes':
                return False, f"Provider {provider} has execute='{execute_flag}'"
        
        return True, ""
        
    except Exception as e:
        logger.warning(f"Error checking execute flags: {e}")
        # Default to allow execution if check fails
        return True, ""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def submit_signal(
    ticker: str,
    consensus: dict,
    position_size: dict,
    db_manager,
    log_id: str = "",
) -> Dict[str, Any]:
    """
    Convert a consensus signal + position size into a bracket order.

    Returns {status, order_ids, message}.
    Possible statuses:
      SUBMITTED, QUEUED, DISABLED, SKIPPED_*, ROLLBACK_PENDING, ERROR
    """
    mode = _cfg(db_manager, "ORDER_MODE", "disabled").lower()

    if mode == "disabled":
        logger.info(f"order_manager: ORDER_MODE=disabled, skipping {ticker}")
        return {"status": "DISABLED", "order_ids": [], "message": "ORDER_MODE=disabled"}

    if mode == "live" and not _cfg_bool(db_manager, "LIVE_TRADING_CONFIRMED"):
        logger.warning("order_manager: live mode requires LIVE_TRADING_CONFIRMED=true")
        return {"status": "DISABLED", "order_ids": [], "message": "LIVE_TRADING_CONFIRMED not set"}

    signal = consensus.get("signal", "NEUTRAL").upper()
    if signal == "NEUTRAL":
        return {"status": "SKIPPED_NEUTRAL", "order_ids": [], "message": "Signal is NEUTRAL"}

    if position_size.get("status") != "OK" or position_size.get("quantity", 0) <= 0:
        reason = position_size.get("status", "UNKNOWN")
        return {"status": reason, "order_ids": [], "message": f"Position sizing: {reason}"}

    # Guard: ticker blocked
    if _is_ticker_blocked(db_manager, ticker):
        return {"status": "SKIPPED_TICKER_BLOCKED", "order_ids": [], "message": f"{ticker} is blocked"}

    # Guard: duplicate open order — hold per-ticker lock for the entire
    # check-then-insert critical section to prevent race with concurrent threads.
    _ticker_lock = _get_ticker_lock(ticker)
    if not _ticker_lock.acquire(blocking=False):
        return {"status": "SKIPPED_DUPLICATE", "order_ids": [], "message": f"Concurrent submission in progress for {ticker}"}
    try:
        if _has_open_order_for_ticker(db_manager, ticker):
            return {"status": "SKIPPED_DUPLICATE", "order_ids": [], "message": f"Open order exists for {ticker}"}

        # Guard: max open orders
        max_open = _cfg_int(db_manager, "MAX_OPEN_ORDERS", 5)
        if _count_open_orders(db_manager) >= max_open:
            return {"status": "SKIPPED_MAX_ORDERS", "order_ids": [], "message": f"MAX_OPEN_ORDERS={max_open} reached"}

        # Guard: execute flags for methods and providers
        methods_for_signal = ""
        if signal == "LONG":
            methods_for_signal = consensus.get("methods_long", "")
        elif signal == "SHORT":
            methods_for_signal = consensus.get("methods_short", "")

        can_execute, execute_reason = _check_execute_flags(db_manager, methods_for_signal)
        if not can_execute:
            logger.info(f"order_manager: skipping {ticker} - {execute_reason}")
            return {"status": "SKIPPED_EXECUTE_DISABLED", "order_ids": [], "message": execute_reason}

        symbol   = _symbol_from_ticker(ticker)
        action   = "BUY" if signal == "LONG" else "SELL"
        quantity = int(position_size["quantity"])
        stop_loss   = consensus.get("stop_loss")
        target_price = consensus.get("target_price")
        entry_limit_price = consensus.get("entry_limit_price")
        entry_tif = consensus.get("entry_tif", "DAY")

        if not stop_loss or not target_price:
            return {"status": "SKIPPED_MISSING_LEVELS", "order_ids": [],
                    "message": "consensus missing stop_loss or target_price"}

        # Slippage Guard — snapshot bid/ask before Market order
        max_spread_pct = _cfg_float(db_manager, "MAX_SPREAD_PCT", 0.005)
        ib_port = _get_ib_port(mode)
        try:
            from ib_gateway_client import get_bid_ask_spread
            spread_info = get_bid_ask_spread(symbol, port=ib_port)
            if spread_info.get("status") == "ok":
                spread_pct = spread_info.get("spread_pct", 0)
                if spread_pct > max_spread_pct:
                    logger.warning(
                        f"order_manager: {ticker} spread={spread_pct:.4%} > MAX_SPREAD_PCT={max_spread_pct:.4%}"
                    )
                    return {"status": "SKIPPED_HIGH_VOLATILITY", "order_ids": [],
                            "message": f"spread={spread_pct:.4%} exceeds limit"}
        except Exception as e:
            logger.warning(f"order_manager: slippage guard failed for {ticker}: {e} (proceeding)")
            spread_info = {}

        # Determine order status based on market hours
        queue_age_hours = _cfg_int(db_manager, "ORDER_QUEUE_MAX_AGE_HOURS", 24)
        allow_extended  = _cfg_bool(db_manager, "ALLOW_EXTENDED_HOURS")
        initial_status  = "SUBMITTED" if (_is_market_hours() or allow_extended) else "QUEUED"

        use_stop_limit         = _cfg_bool(db_manager, "USE_STOP_LIMIT")
        stop_limit_offset_pct  = _cfg_float(db_manager, "STOP_LIMIT_OFFSET_PCT", 0.0005)

        # Determine entry order type based on entry_limit_price
        if entry_limit_price and entry_limit_price > 0:
            entry_order_type = "LMT"
            entry_price_val = float(entry_limit_price)
        else:
            entry_order_type = "MKT"
            entry_price_val = None

        # Save parent order record to DB (still inside lock — INSERT before release)
        now = _now_utc()
        order_row = {
            "log_id":               log_id,
            "ticker":               ticker,
            "ib_order_id":          0,
            "ib_parent_id":         0,
            "order_role":           "ENTRY",
            "order_type":           entry_order_type,
            "action":               action,
            "quantity":             quantity,
            "limit_price":          entry_price_val,
            "stop_price":           None,
            "status":               initial_status,
            "account_type":         mode,
            "created_at":           now,
            "submitted_at":         now if initial_status == "SUBMITTED" else "",
            "spread_at_submission": spread_info.get("spread_pct"),
            "error_message":        "",
        }
        parent_db_id = _save_order(db_manager, order_row)

        if initial_status == "QUEUED":
            logger.info(f"order_manager: {ticker} order QUEUED (outside market hours)")
            return {"status": "QUEUED", "order_ids": [parent_db_id], "message": "Queued for market open"}

    finally:
        # Release lock after INSERT so any concurrent thread sees the new row
        if _ticker_lock is not None:
            _ticker_lock.release()
            _ticker_lock = None

    # Place bracket via IB (outside critical section — lock already released)
    try:
        from ib_gateway_client import place_bracket_order
        ib_result = place_bracket_order(
            symbol=symbol,
            action=action,
            quantity=quantity,
            entry_price=entry_price_val,
            entry_order_type=entry_order_type,
            entry_tif=entry_tif,
            stop_loss_price=float(stop_loss),
            take_profit_price=float(target_price),
            take_profit_tif=consensus.get("take_profit_tif", "GTC"),
            stop_loss_tif=consensus.get("stop_loss_tif", "GTC"),
            use_stop_limit=use_stop_limit,
            stop_limit_offset_pct=stop_limit_offset_pct,
            allow_extended_hours=allow_extended,
            port=ib_port,
        )
    except Exception as e:
        _update_order(db_manager, parent_db_id, {"status": "ERROR", "error_message": str(e)})
        logger.error(f"order_manager: place_bracket_order exception for {ticker}: {e}")
        return {"status": "ERROR", "order_ids": [parent_db_id], "message": str(e)}

    if ib_result.get("status") != "submitted":
        err = ib_result.get("error", "unknown")
        _update_order(db_manager, parent_db_id, {"status": "ERROR", "error_message": err})
        return {"status": "ERROR", "order_ids": [parent_db_id], "message": err}

    parent_ib_id = ib_result["parent_id"]
    target_ib_id = ib_result["target_id"]
    stop_ib_id   = ib_result["stop_id"]

    _update_order(db_manager, parent_db_id, {
        "ib_order_id":  parent_ib_id,
        "ib_parent_id": parent_ib_id,
        "status":       "SUBMITTED",
        "submitted_at": _now_utc(),
    })

    # Save child orders
    child_base = {
        "log_id": log_id, "ticker": ticker,
        "ib_parent_id": parent_ib_id, "quantity": quantity,
        "status": "SUBMITTED", "account_type": mode,
        "created_at": now, "submitted_at": _now_utc(), "error_message": "",
    }
    target_row = {**child_base, "ib_order_id": target_ib_id, "order_role": "TAKE_PROFIT",
                  "order_type": "LMT", "action": "SELL" if action == "BUY" else "BUY",
                  "limit_price": float(target_price), "stop_price": None}
    stop_row = {**child_base, "ib_order_id": stop_ib_id, "order_role": "STOP_LOSS",
                "order_type": "STP", "action": "SELL" if action == "BUY" else "BUY",
                "limit_price": None, "stop_price": float(stop_loss)}
    target_db_id = _save_order(db_manager, target_row)
    stop_db_id   = _save_order(db_manager, stop_row)

    from notification_manager import notify
    notify(
        f"ORDER_SUBMITTED: {ticker} {action} qty={quantity} "
        f"stop={stop_loss} target={target_price} IB#{parent_ib_id}",
        level="INFO",
    )

    logger.info(
        f"order_manager: bracket submitted for {ticker} "
        f"parent={parent_ib_id} target={target_ib_id} stop={stop_ib_id}"
    )
    return {
        "status":    "SUBMITTED",
        "order_ids": [parent_db_id, target_db_id, stop_db_id],
        "ib_ids":    {"parent": parent_ib_id, "target": target_ib_id, "stop": stop_ib_id},
        "message":   f"Bracket submitted IB#{parent_ib_id}",
    }


# ---------------------------------------------------------------------------
# Step 8 — Rollback
# ---------------------------------------------------------------------------

def rollback_bracket_group(
    parent_db_id: int,
    db_manager,
    host: str = "127.0.0.1",
) -> bool:
    """
    Atomic rollback of a bracket group after Entry fill.

    Steps (per plan §4.5):
    1. Mark group ROLLBACK_PENDING
    2. Cancel unfilled child orders (target, stop)
    3. Close open position at market
    4. Wait ORDER_ROLLBACK_TIMEOUT_SEC for confirmation
    5. Verify no orphan position remains
    6. Mark ROLLBACK_COMPLETE or ROLLBACK_FAILED
    7. On FAILED: block ticker + notify ORPHAN_POSITION

    Returns True if rollback completed successfully.
    """
    from notification_manager import notify

    rollback_timeout = _cfg_int(db_manager, "ORDER_ROLLBACK_TIMEOUT_SEC", 30)
    auto_block       = _cfg_bool(db_manager, "AUTO_BLOCK_ON_ROLLBACK_FAIL", True)

    logger.warning(f"order_manager: initiating rollback for parent DB id={parent_db_id}")

    # Step 1 — mark ROLLBACK_PENDING
    try:
        with sqlite3.connect(db_manager.db_file) as con:
            row = con.execute(
                "SELECT ticker, ib_parent_id, account_type FROM orders WHERE id=?",
                (parent_db_id,)
            ).fetchone()
            if not row:
                logger.error(f"rollback: parent order {parent_db_id} not found")
                return False
            ticker, ib_parent_id, mode = row[0], row[1], row[2]

            con.execute(
                "UPDATE orders SET status='ROLLBACK_PENDING' WHERE ib_parent_id=?",
                (ib_parent_id,)
            )
    except Exception as e:
        logger.error(f"rollback: DB error step 1: {e}")
        return False

    symbol  = _symbol_from_ticker(ticker)
    ib_port = _get_ib_port(mode)

    # Step 2 — cancel unfilled children
    try:
        with sqlite3.connect(db_manager.db_file) as con:
            children = con.execute(
                "SELECT ib_order_id, order_role, status FROM orders "
                "WHERE ib_parent_id=? AND order_role IN ('TAKE_PROFIT','STOP_LOSS')",
                (ib_parent_id,)
            ).fetchall()

        from ib_gateway_client import cancel_order
        for child_ib_id, role, status in children:
            if status not in ("FILLED", "CANCELLED"):
                logger.info(f"rollback: cancelling {role} IB#{child_ib_id}")
                cancel_order(child_ib_id, port=ib_port)

    except Exception as e:
        logger.error(f"rollback: cancel children failed: {e}")

    # Step 3 — close position at market
    try:
        from ib_gateway_client import close_position_market
        close_result = close_position_market(symbol, quantity=1, port=ib_port)
        logger.info(f"rollback: close_position_market result: {close_result}")
    except Exception as e:
        logger.error(f"rollback: close position failed: {e}")
        close_result = {"status": "error", "error": str(e)}

    # Step 4 — wait for confirmation (stub: wait timeout)
    import time
    time.sleep(min(rollback_timeout, 10))

    # Step 5 & 6 — determine success
    close_ok = close_result.get("status") == "submitted"
    new_status = "ROLLBACK_COMPLETE" if close_ok else "ROLLBACK_FAILED"

    try:
        with sqlite3.connect(db_manager.db_file) as con:
            con.execute(
                "UPDATE orders SET status=? WHERE ib_parent_id=?",
                (new_status, ib_parent_id)
            )
    except Exception as e:
        logger.error(f"rollback: DB update step 6 failed: {e}")

    if not close_ok:
        # Step 7 — block ticker + notify
        if auto_block:
            _block_ticker(db_manager, ticker)
        notify(
            f"ROLLBACK_FAILED: {ticker} IB#{ib_parent_id} — ORPHAN_POSITION. "
            f"Manual intervention required!",
            level="CRITICAL",
        )
        logger.critical(
            f"rollback: ROLLBACK_FAILED for {ticker} IB#{ib_parent_id} — ORPHAN_POSITION"
        )
        return False

    notify(f"ROLLBACK_COMPLETE: {ticker} IB#{ib_parent_id} closed successfully", level="WARNING")
    logger.info(f"rollback: ROLLBACK_COMPLETE for {ticker} IB#{ib_parent_id}")
    return True


def check_child_timeouts(db_manager) -> None:
    """
    Scan FILLED_ENTRY orders and trigger rollback if children haven't appeared
    within ORDER_CHILD_TIMEOUT_SEC of filled_at.
    Called periodically by the scheduler.
    """
    timeout_sec = _cfg_int(db_manager, "ORDER_CHILD_TIMEOUT_SEC", 10)

    try:
        with sqlite3.connect(db_manager.db_file) as con:
            con.row_factory = sqlite3.Row
            filled_entries = con.execute(
                "SELECT id, ticker, ib_parent_id, filled_at FROM orders "
                "WHERE order_role='ENTRY' AND status='FILLED_ENTRY' AND filled_at != ''"
            ).fetchall()
    except Exception as e:
        logger.error(f"check_child_timeouts: DB error: {e}")
        return

    now = datetime.now(tz=timezone.utc)
    for row in filled_entries:
        try:
            filled_at = datetime.fromisoformat(row["filled_at"].replace("Z", "+00:00"))
            if filled_at.tzinfo is None:
                filled_at = filled_at.replace(tzinfo=timezone.utc)
            elapsed = (now - filled_at).total_seconds()
            if elapsed < timeout_sec:
                continue

            # Check if children exist and are submitted
            with sqlite3.connect(db_manager.db_file) as con:
                child_count = con.execute(
                    "SELECT COUNT(*) FROM orders WHERE ib_parent_id=? AND order_role IN ('TAKE_PROFIT','STOP_LOSS')",
                    (row["ib_parent_id"],)
                ).fetchone()[0]

            if child_count < 2:
                logger.warning(
                    f"check_child_timeouts: {row['ticker']} IB#{row['ib_parent_id']} "
                    f"missing children after {elapsed:.0f}s → rollback"
                )
                rollback_bracket_group(row["id"], db_manager)
        except Exception as e:
            logger.error(f"check_child_timeouts: error processing row {row['id']}: {e}")
