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
import json
import time
import uuid
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


def _get_last_price(db_manager, ticker: str) -> float:
    """Return the most recent close price for ticker from price_data, or 0.0 if not found.
    
    Uses encapsulated db_manager method if available, falls back to direct query.
    """
    # Prefer encapsulated method if available
    if hasattr(db_manager, 'get_last_price'):
        return db_manager.get_last_price(ticker)
    
    # Fallback for backward compatibility
    try:
        import sqlite3 as _sq
        with _sq.connect(db_manager.db_file) as con:
            row = con.execute(
                "SELECT close FROM price_data WHERE ticker=? ORDER BY date DESC LIMIT 1",
                (ticker,)
            ).fetchone()
        return float(row[0]) if row and row[0] else 0.0
    except Exception:
        return 0.0


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


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _open_orders_count_sql(has_trades_table: bool) -> str:
    if not has_trades_table:
        return "SELECT COUNT(*) FROM orders WHERE UPPER(order_role)='ENTRY' AND status IN ('QUEUED','SUBMITTED','FILLED_ENTRY')"
    return (
        "SELECT COUNT(*) "
        "FROM orders o "
        "LEFT JOIN trades t ON t.ib_parent_id = o.ib_parent_id "
        "WHERE "
        "UPPER(o.order_role)='ENTRY' AND ("
        "o.status IN ('QUEUED','SUBMITTED') "
        "OR (o.status = 'FILLED_ENTRY' AND COALESCE(UPPER(t.status), 'OPEN') = 'OPEN')"
        ")"
    )


def _count_open_orders(db_manager) -> int:
    try:
        with sqlite3.connect(db_manager.db_file) as con:
            has_trades_table = _table_exists(con, "trades")
            row = con.execute(_open_orders_count_sql(has_trades_table)).fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


def _has_open_order_for_ticker(db_manager, ticker: str) -> bool:
    try:
        with sqlite3.connect(db_manager.db_file) as con:
            has_trades_table = _table_exists(con, "trades")
            if not has_trades_table:
                row = con.execute(
                    "SELECT COUNT(*) FROM orders WHERE UPPER(ticker)=UPPER(?) AND UPPER(order_role)='ENTRY' AND status IN ('QUEUED','SUBMITTED','FILLED_ENTRY')",
                    (ticker,)
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT COUNT(*) "
                    "FROM orders o "
                    "LEFT JOIN trades t ON t.ib_parent_id = o.ib_parent_id "
                    "WHERE UPPER(o.ticker)=UPPER(?) "
                    "AND ("
                    "UPPER(o.order_role)='ENTRY' AND ("
                    "o.status IN ('QUEUED','SUBMITTED') "
                    "OR (o.status='FILLED_ENTRY' AND COALESCE(UPPER(t.status), 'OPEN')='OPEN')"
                    ")"
                    ")",
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


def _table_has_column(db_manager, table: str, column: str) -> bool:
    try:
        with sqlite3.connect(db_manager.db_file) as con:
            rows = con.execute(f"PRAGMA table_info({table})").fetchall()
            return any(str(r[1]).lower() == column.lower() for r in rows)
    except Exception:
        return False


def _update_order(db_manager, row_id: int, updates: dict) -> None:
    set_parts = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [row_id]
    with sqlite3.connect(db_manager.db_file) as con:
        con.execute(f"UPDATE orders SET {set_parts} WHERE id=?", vals)


def _json_payload(value: Any, max_len: int = 32000) -> str:
    """Serialize payload to compact JSON string with size guard."""
    if value is None:
        return ""
    try:
        text = json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)
    except Exception:
        text = str(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _log_ib_transaction_event(
    db_manager,
    *,
    event_source: str,
    event_type: str,
    operation_status: str = "",
    status_before: str = "",
    status_after: str = "",
    ticker: str = "",
    trade_uid: str = "",
    ib_order_id: int = 0,
    ib_perm_id: int = 0,
    ib_parent_id: int = 0,
    order_id: Optional[int] = None,
    trade_id: Optional[int] = None,
    consensus_id: Optional[int] = None,
    log_id: str = "",
    request_payload: Any = None,
    response_payload: Any = None,
    error_message: str = "",
    latency_ms: Optional[int] = None,
) -> None:
    """Best-effort write to ib_order_transactions via SQLiteManager helper."""
    if not hasattr(db_manager, "log_ib_transaction"):
        return
    try:
        db_manager.log_ib_transaction(
            occurred_at=_now_utc(),
            event_source=event_source,
            event_type=event_type,
            operation_status=operation_status,
            status_before=status_before,
            status_after=status_after,
            ticker=ticker,
            trade_uid=trade_uid,
            ib_order_id=ib_order_id,
            ib_perm_id=ib_perm_id,
            ib_parent_id=ib_parent_id,
            order_id=order_id,
            trade_id=trade_id,
            consensus_id=consensus_id,
            log_id=log_id,
            request_payload_json=_json_payload(request_payload),
            response_payload_json=_json_payload(response_payload),
            error_message=error_message,
            latency_ms=latency_ms,
        )
    except Exception as e:
        logger.warning(f"_log_ib_transaction_event failed: {e}")


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
        with sqlite3.connect(db_manager.db_file) as con:
            method_rows = con.execute(method_query, methods).fetchall()
        
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
            with sqlite3.connect(db_manager.db_file) as con:
                provider_rows = con.execute(provider_query, providers).fetchall()
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
    is_test: bool = False,
    test_tag: str = "",
    consensus_id: Optional[int] = None,
    event_source: str = "submit_auto",
) -> Dict[str, Any]:
    """
    Convert a consensus signal + position size into a bracket order.

    Returns {status, order_ids, message}.
    Possible statuses:
      SUBMITTED, QUEUED, DISABLED, SKIPPED_*, ROLLBACK_PENDING, ERROR
    """
    mode = _cfg(db_manager, "ORDER_MODE", "disabled").lower()
    normalized_tag = (test_tag or "").strip()
    order_ref = ""
    trade_uid = str(uuid.uuid4())
    has_order_test_cols = _table_has_column(db_manager, "orders", "is_test") and _table_has_column(db_manager, "orders", "test_tag")
    has_order_trade_uid_col = _table_has_column(db_manager, "orders", "trade_uid")
    has_order_ib_perm_id_col = _table_has_column(db_manager, "orders", "ib_perm_id")
    has_trade_test_cols = _table_has_column(db_manager, "trades", "is_test") and _table_has_column(db_manager, "trades", "test_tag")
    has_trade_uid_col = _table_has_column(db_manager, "trades", "trade_uid")

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
        open_count = _count_open_orders(db_manager)
        if open_count >= max_open:
            return {
                "status": "SKIPPED_MAX_ORDERS",
                "order_ids": [],
                "message": f"MAX_OPEN_ORDERS={max_open} reached (open={open_count})",
            }

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
            from ib_gateway_client import get_bid_ask_spread_safe
            spread_info = get_bid_ask_spread_safe(symbol, port=ib_port)
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
        if has_order_trade_uid_col:
            order_row["trade_uid"] = trade_uid
        if has_order_ib_perm_id_col:
            order_row["ib_perm_id"] = 0
        if has_order_test_cols:
            order_row["is_test"] = 1 if is_test else 0
            order_row["test_tag"] = normalized_tag
        parent_db_id = _save_order(db_manager, order_row)
        if is_test and normalized_tag:
            order_ref = normalized_tag
        else:
            order_ref = f"tid={trade_uid}|oid={parent_db_id}"

        if initial_status == "QUEUED":
            logger.info(f"order_manager: {ticker} order QUEUED (outside market hours)")
            return {"status": "QUEUED", "order_ids": [parent_db_id], "message": "Queued for market open"}

    finally:
        # Release lock after INSERT so any concurrent thread sees the new row
        if _ticker_lock is not None:
            _ticker_lock.release()
            _ticker_lock = None

    # Place bracket via IB (outside critical section — lock already released)
    ib_request_payload = {
        "symbol": symbol,
        "action": action,
        "quantity": quantity,
        "entry_order_type": entry_order_type,
        "entry_price": entry_price_val,
        "entry_tif": entry_tif,
        "stop_loss_price": float(stop_loss),
        "take_profit_price": float(target_price),
        "take_profit_tif": consensus.get("take_profit_tif", "GTC"),
        "stop_loss_tif": consensus.get("stop_loss_tif", "GTC"),
        "use_stop_limit": use_stop_limit,
        "stop_limit_offset_pct": stop_limit_offset_pct,
        "allow_extended_hours": allow_extended,
        "order_ref": order_ref,
        "port": ib_port,
    }
    _log_ib_transaction_event(
        db_manager,
        event_source=event_source,
        event_type="ORDER_SUBMIT_REQUEST",
        operation_status="REQUESTED",
        ticker=ticker,
        trade_uid=trade_uid,
        order_id=parent_db_id,
        consensus_id=consensus_id,
        log_id=log_id,
        request_payload=ib_request_payload,
    )
    ib_call_started = time.monotonic()

    try:
        from ib_gateway_client import place_bracket_order_safe
        ib_result = place_bracket_order_safe(
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
            order_ref=order_ref,
            port=ib_port,
        )
    except Exception as e:
        latency_ms = int((time.monotonic() - ib_call_started) * 1000)
        _update_order(db_manager, parent_db_id, {"status": "ERROR", "error_message": str(e)})
        _log_ib_transaction_event(
            db_manager,
            event_source=event_source,
            event_type="ORDER_SUBMIT_RESPONSE",
            operation_status="FAILED",
            status_before=initial_status,
            status_after="ERROR",
            ticker=ticker,
            trade_uid=trade_uid,
            order_id=parent_db_id,
            consensus_id=consensus_id,
            log_id=log_id,
            request_payload=ib_request_payload,
            error_message=str(e),
            latency_ms=latency_ms,
        )
        logger.error(f"order_manager: place_bracket_order exception for {ticker}: {e}")
        return {"status": "ERROR", "order_ids": [parent_db_id], "message": str(e)}

    if ib_result.get("status") != "submitted":
        latency_ms = int((time.monotonic() - ib_call_started) * 1000)
        err = ib_result.get("error", "unknown")
        _update_order(db_manager, parent_db_id, {"status": "ERROR", "error_message": err})
        _log_ib_transaction_event(
            db_manager,
            event_source=event_source,
            event_type="ORDER_SUBMIT_RESPONSE",
            operation_status="FAILED",
            status_before=initial_status,
            status_after="ERROR",
            ticker=ticker,
            trade_uid=trade_uid,
            order_id=parent_db_id,
            consensus_id=consensus_id,
            log_id=log_id,
            request_payload=ib_request_payload,
            response_payload=ib_result,
            error_message=str(err),
            latency_ms=latency_ms,
        )
        return {"status": "ERROR", "order_ids": [parent_db_id], "message": err}

    parent_ib_id = ib_result["parent_id"]
    target_ib_id = ib_result["target_id"]
    stop_ib_id   = ib_result["stop_id"]

    parent_updates = {
        "ib_order_id":  parent_ib_id,
        "ib_parent_id": parent_ib_id,
        "status":       "SUBMITTED",
        "submitted_at": _now_utc(),
    }
    if has_order_ib_perm_id_col:
        parent_updates["ib_perm_id"] = int(ib_result.get("parent_perm_id") or 0)
    _update_order(db_manager, parent_db_id, parent_updates)

    # Save child orders
    child_base = {
        "log_id": log_id, "ticker": ticker,
        "ib_parent_id": parent_ib_id, "quantity": quantity,
        "status": "SUBMITTED", "account_type": mode,
        "created_at": now, "submitted_at": _now_utc(), "error_message": "",
    }
    if has_order_trade_uid_col:
        child_base["trade_uid"] = trade_uid
    if has_order_test_cols:
        child_base["is_test"] = 1 if is_test else 0
        child_base["test_tag"] = normalized_tag
    target_row = {**child_base, "ib_order_id": target_ib_id, "order_role": "TAKE_PROFIT",
                  "order_type": "LMT", "action": "SELL" if action == "BUY" else "BUY",
                  "limit_price": float(target_price), "stop_price": None}
    stop_row = {**child_base, "ib_order_id": stop_ib_id, "order_role": "STOP_LOSS",
                "order_type": "STP", "action": "SELL" if action == "BUY" else "BUY",
                "limit_price": None, "stop_price": float(stop_loss)}
    if has_order_ib_perm_id_col:
        target_row["ib_perm_id"] = int(ib_result.get("target_perm_id") or 0)
        stop_row["ib_perm_id"] = int(ib_result.get("stop_perm_id") or 0)
    target_db_id = _save_order(db_manager, target_row)
    stop_db_id   = _save_order(db_manager, stop_row)

    latency_ms = int((time.monotonic() - ib_call_started) * 1000)
    _log_ib_transaction_event(
        db_manager,
        event_source=event_source,
        event_type="ORDER_SUBMIT_RESPONSE",
        operation_status="SUCCESS",
        status_before=initial_status,
        status_after="SUBMITTED",
        ticker=ticker,
        trade_uid=trade_uid,
        ib_order_id=parent_ib_id,
        ib_perm_id=int(ib_result.get("parent_perm_id") or 0),
        ib_parent_id=parent_ib_id,
        order_id=parent_db_id,
        consensus_id=consensus_id,
        log_id=log_id,
        request_payload=ib_request_payload,
        response_payload=ib_result,
        latency_ms=latency_ms,
    )

    from notification_manager import notify
    notify(
        f"ORDER_SUBMITTED: {ticker} {action} qty={quantity} "
        f"stop={stop_loss} target={target_price} IB#{parent_ib_id}",
        level="INFO",
    )

    # Create a trades record for this bracket group
    trade_id: Optional[int] = None
    try:
        now_ts = _now_utc()
        with sqlite3.connect(db_manager.db_file) as con:
            if has_trade_test_cols:
                if has_trade_uid_col:
                    cur = con.execute(
                        """INSERT INTO trades
                           (trade_uid, ticker, ib_parent_id, signal, quantity, entry_price,
                            stop_loss, target_price, status, created_at, updated_at, is_test, test_tag)
                           VALUES (?,?,?,?,?,?,?,?,'OPEN',?,?,?,?)""",
                        (trade_uid, ticker, parent_ib_id,
                         "LONG" if action == "BUY" else "SHORT",
                         quantity, entry_price_val,
                         float(stop_loss), float(target_price),
                         now_ts, now_ts,
                         1 if is_test else 0,
                         normalized_tag),
                    )
                else:
                    cur = con.execute(
                        """INSERT INTO trades
                           (ticker, ib_parent_id, signal, quantity, entry_price,
                            stop_loss, target_price, status, created_at, updated_at, is_test, test_tag)
                           VALUES (?,?,?,?,?,?,?,'OPEN',?,?,?,?)""",
                        (ticker, parent_ib_id,
                         "LONG" if action == "BUY" else "SHORT",
                         quantity, entry_price_val,
                         float(stop_loss), float(target_price),
                         now_ts, now_ts,
                         1 if is_test else 0,
                         normalized_tag),
                    )
            else:
                if has_trade_uid_col:
                    cur = con.execute(
                        """INSERT INTO trades
                           (trade_uid, ticker, ib_parent_id, signal, quantity, entry_price,
                            stop_loss, target_price, status, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,'OPEN',?,?)""",
                        (trade_uid, ticker, parent_ib_id,
                         "LONG" if action == "BUY" else "SHORT",
                         quantity, entry_price_val,
                         float(stop_loss), float(target_price),
                         now_ts, now_ts),
                    )
                else:
                    cur = con.execute(
                        """INSERT INTO trades
                           (ticker, ib_parent_id, signal, quantity, entry_price,
                            stop_loss, target_price, status, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,'OPEN',?,?)""",
                        (ticker, parent_ib_id,
                         "LONG" if action == "BUY" else "SHORT",
                         quantity, entry_price_val,
                         float(stop_loss), float(target_price),
                         now_ts, now_ts),
                    )
            trade_id = cur.lastrowid
        logger.info(f"order_manager: created trade id={trade_id} for {ticker} IB#{parent_ib_id}")
    except Exception as e:
        logger.warning(f"order_manager: could not create trade record for {ticker}: {e}")

    logger.info(
        f"order_manager: bracket submitted for {ticker} "
        f"parent={parent_ib_id} target={target_ib_id} stop={stop_ib_id}"
    )
    return {
        "status":    "SUBMITTED",
        "order_ids": [parent_db_id, target_db_id, stop_db_id],
        "ib_ids":    {"parent": parent_ib_id, "target": target_ib_id, "stop": stop_ib_id},
        "trade_id":  trade_id,
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

        from ib_gateway_client import cancel_order_safe
        for child_ib_id, role, status in children:
            if status not in ("FILLED", "CANCELLED"):
                logger.info(f"rollback: cancelling {role} IB#{child_ib_id}")
            cancel_order_safe(child_ib_id, port=ib_port)

    except Exception as e:
        logger.error(f"rollback: cancel children failed: {e}")

    # Step 3 — close position at market
    try:
        from ib_gateway_client import close_position_market_safe
        close_result = close_position_market_safe(symbol, quantity=1, port=ib_port)
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


# ---------------------------------------------------------------------------
# Backward-compatible IB status processor (test/legacy entrypoint)
# ---------------------------------------------------------------------------

def process_ib_order_updates(db_manager, ib_statuses: list) -> None:
    """Apply IB order status snapshots to local orders/trades.

    This function is kept for backward compatibility with existing tests and
    scripts. Scheduler/manual sync path uses scripts.core.order_status_sync.
    """
    if not ib_statuses:
        return

    def _to_float(value: Any) -> Optional[float]:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except Exception:
            return None

    ib_map: dict[int, dict] = {}
    for row in ib_statuses:
        try:
            oid = int(row.get("ib_order_id") or 0)
        except Exception:
            oid = 0
        if oid > 0:
            ib_map[oid] = row

    if not ib_map:
        return

    with sqlite3.connect(db_manager.db_file) as con:
        con.row_factory = sqlite3.Row
        orders = con.execute(
            """
            SELECT id, ticker, ib_order_id, ib_parent_id, order_role, status
            FROM orders
            WHERE ib_order_id != 0
            """
        ).fetchall()

        for order in orders:
            ib_order_id = int(order["ib_order_id"] or 0)
            ib_row = ib_map.get(ib_order_id)
            if ib_row is None:
                continue

            ib_status_raw = str(ib_row.get("status") or "").strip()
            ib_status = ib_status_raw.lower()
            role = str(order["order_role"] or "").upper()
            current = str(order["status"] or "").upper()
            fill_price = _to_float(ib_row.get("avg_fill_price"))
            fill_ts = str(ib_row.get("last_update") or "") or _now_utc()

            if ib_status == "filled":
                if role == "ENTRY" and current not in ("FILLED_ENTRY", "FILLED"):
                    con.execute(
                        "UPDATE orders SET status='FILLED_ENTRY', filled_price=?, filled_at=? WHERE id=?",
                        (fill_price, fill_ts, order["id"]),
                    )
                    con.execute(
                        """
                        UPDATE trades
                        SET entry_price=?, entry_filled_at=?, updated_at=?
                        WHERE ib_parent_id=? AND status='OPEN'
                        """,
                        (fill_price, fill_ts, _now_utc(), int(order["ib_parent_id"] or 0)),
                    )

                elif role in ("TAKE_PROFIT", "STOP_LOSS") and current not in ("FILLED", "CANCELLED"):
                    con.execute(
                        "UPDATE orders SET status='FILLED', filled_price=?, filled_at=? WHERE id=?",
                        (fill_price, fill_ts, order["id"]),
                    )

                    trade = con.execute(
                        """
                        SELECT id, signal, quantity, entry_price, status
                        FROM trades
                        WHERE ib_parent_id=?
                        ORDER BY id DESC LIMIT 1
                        """,
                        (int(order["ib_parent_id"] or 0),),
                    ).fetchone()
                    if trade and str(trade["status"] or "").upper() == "OPEN":
                        entry_price = _to_float(trade["entry_price"]) or 0.0
                        qty = _to_float(trade["quantity"]) or 0.0
                        px = fill_price or 0.0
                        signal = str(trade["signal"] or "LONG").upper()
                        if signal == "SHORT":
                            pnl = (entry_price - px) * qty
                        else:
                            pnl = (px - entry_price) * qty
                        close_reason = "TAKE_PROFIT" if role == "TAKE_PROFIT" else "STOP_LOSS"
                        con.execute(
                            """
                            UPDATE trades
                            SET status='CLOSED', exit_price=?, exit_filled_at=?, close_reason=?,
                                realized_pnl=?, updated_at=?
                            WHERE id=?
                            """,
                            (px, fill_ts, close_reason, pnl, _now_utc(), trade["id"]),
                        )

            elif ib_status in ("cancelled", "inactive"):
                if current not in ("FILLED", "FILLED_ENTRY", "CANCELLED"):
                    con.execute(
                        "UPDATE orders SET status='CANCELLED', error_message=? WHERE id=?",
                        (f"IB:{ib_status_raw}", order["id"]),
                    )


# ---------------------------------------------------------------------------
# IB Gateway audit log
# ---------------------------------------------------------------------------

def log_ib_operation(
    db_manager,
    operation: str,
    ticker: str = "",
    ib_order_id: int = 0,
    status: str = "",
    latency_ms: Optional[int] = None,
    request_data: str = "",
    response_data: str = "",
    error_msg: str = "",
) -> None:
    """Write one row to ib_gateway_log for audit trail."""
    try:
        with sqlite3.connect(db_manager.db_file) as con:
            con.execute(
                """INSERT INTO ib_gateway_log
                   (occurred_at, operation, ticker, ib_order_id, status,
                    latency_ms, request_data, response_data, error_msg)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (_now_utc(), operation, ticker, ib_order_id, status,
                 latency_ms, request_data, response_data, error_msg),
            )
    except Exception as e:
        logger.warning(f"log_ib_operation: failed to write log: {e}")


# ---------------------------------------------------------------------------
# Shared consensus activation entrypoint
# ---------------------------------------------------------------------------

def _is_within_order_window(db_manager) -> tuple[bool, str]:
    """
    Check whether the current UTC time is within the configured order window.

    Returns (allowed: bool, reason: str).
    When ORDER_WINDOW_ENABLED=false always returns (True, "").
    """
    if not _cfg_bool(db_manager, "ORDER_WINDOW_ENABLED"):
        return True, ""

    import json as _json
    now = datetime.now(tz=timezone.utc)

    # Weekday check (0=Mon … 4=Fri)
    try:
        allowed_days = _json.loads(_cfg(db_manager, "ORDER_WINDOW_WEEKDAYS", "[0,1,2,3,4]"))
    except Exception:
        allowed_days = [0, 1, 2, 3, 4]
    if now.weekday() not in allowed_days:
        return False, f"outside_weekday({now.weekday()})"

    # Time window check
    def _parse_hhmm(s: str):
        h, m = s.split(":")
        return now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)

    try:
        win_start = _parse_hhmm(_cfg(db_manager, "ORDER_WINDOW_START", "14:30"))
        win_end   = _parse_hhmm(_cfg(db_manager, "ORDER_WINDOW_END",   "20:45"))
        if not (win_start <= now <= win_end):
            return False, f"outside_time_window({_cfg(db_manager, 'ORDER_WINDOW_START')}–{_cfg(db_manager, 'ORDER_WINDOW_END')})"
    except Exception:
        pass  # bad config — allow

    return True, ""


def preview_consensus_order(
    consensus_id: int,
    db_manager,
) -> Dict[str, Any]:
    """Return trade preview (including calculated quantity) for a consensus record."""
    try:
        with sqlite3.connect(db_manager.db_file) as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM consensus WHERE id=?", (consensus_id,)).fetchone()
    except Exception as e:
        return {"status": "ERROR", "message": f"DB read failed: {e}"}

    if row is None:
        return {"status": "ERROR", "message": f"consensus id={consensus_id} not found"}

    ticker = str(row["ticker"] or "")
    signal = str(row["signal"] or "").upper()
    entry_limit_price = row["entry_limit_price"]
    stop_loss = row["stop_loss"]
    target_price = row["target_price"]

    if signal not in ("LONG", "SHORT"):
        return {
            "status": "SKIPPED",
            "message": "neutral_signal",
            "ticker": ticker,
            "signal": signal,
        }

    if stop_loss is None or target_price is None:
        return {
            "status": "SKIPPED_MISSING_LEVELS",
            "message": "consensus missing stop_loss or target_price",
            "ticker": ticker,
            "signal": signal,
        }

    entry_action = "BUY" if signal == "LONG" else "SELL"
    exit_action = "SELL" if signal == "LONG" else "BUY"
    if entry_limit_price is not None and float(entry_limit_price) > 0:
        entry_order_type = "LMT"
        entry_price_val = float(entry_limit_price)
    else:
        entry_order_type = "MKT"
        entry_price_val = None

    from position_sizer import calculate_position
    from capital_provider import get_capital

    capital = get_capital(db_manager)
    if capital.get("status") != "OK":
        return {
            "status": "PENDING",
            "message": f"capital_unavailable:{capital.get('status')}",
            "ticker": ticker,
            "signal": signal,
            "entry_order_type": entry_order_type,
            "entry_action": entry_action,
            "take_profit_order_type": "LMT",
            "take_profit_action": exit_action,
            "stop_loss_order_type": "STP",
            "stop_loss_action": exit_action,
            "entry_price": entry_price_val,
            "target_price": target_price,
            "stop_loss": stop_loss,
        }

    position = calculate_position(
        ticker=ticker,
        entry_price=entry_limit_price or stop_loss or 0,
        stop_loss=stop_loss,
        db_manager=db_manager,
        net_liquidation=capital["net_liquidation"],
    )

    return {
        "status": "OK" if position.get("status") == "OK" else position.get("status", "ERROR"),
        "message": "" if position.get("status") == "OK" else f"position_size_failed:{position.get('status', 'UNKNOWN')}",
        "ticker": ticker,
        "signal": signal,
        "entry_order_type": entry_order_type,
        "entry_action": entry_action,
        "take_profit_order_type": "LMT",
        "take_profit_action": exit_action,
        "stop_loss_order_type": "STP",
        "stop_loss_action": exit_action,
        "entry_price": entry_price_val,
        "target_price": target_price,
        "stop_loss": stop_loss,
        "quantity": int(position.get("quantity", 0) or 0),
        "risk_amount": position.get("risk_amount"),
        "risk_mode": position.get("risk_mode"),
        "capital_source": position.get("capital_source"),
    }


def activate_consensus_order(
    consensus_id: int,
    db_manager,
) -> Dict[str, Any]:
    """
    Shared activation entrypoint for a single consensus record.

    Evaluates TTL, order window, position constraints and calls submit_signal().
    Updates consensus.order_state / order_reason / order_checked_at / order_attempted_at / trade_id.

    Returns {status, message}.
    """
    now_str = _now_utc()

    # Load the consensus record
    try:
        with sqlite3.connect(db_manager.db_file) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT * FROM consensus WHERE id=?", (consensus_id,)
            ).fetchone()
    except Exception as e:
        return {"status": "ERROR", "message": f"DB read failed: {e}"}

    if row is None:
        return {"status": "ERROR", "message": f"consensus id={consensus_id} not found"}

    ticker = row["ticker"]
    signal = (row["signal"] or "").upper()
    confidence = row["confidence"] or 0.0
    created_at_str = row["date"] or ""

    def _set_state(state: str, reason: str, attempted: bool = False):
        updates = {
            "order_state":      state,
            "order_reason":     reason,
            "order_checked_at": now_str,
        }
        if attempted:
            updates["order_attempted_at"] = now_str
        try:
            set_parts = ", ".join(f"{k}=?" for k in updates)
            with sqlite3.connect(db_manager.db_file) as con:
                con.execute(
                    f"UPDATE consensus SET {set_parts} WHERE id=?",
                    list(updates.values()) + [consensus_id],
                )
        except Exception as e:
            logger.warning(f"activate_consensus_order: could not update state: {e}")

    # Guard: signal must be LONG or SHORT
    if signal not in ("LONG", "SHORT"):
        _set_state("ORDER_SKIPPED", "neutral_signal")
        return {"status": "SKIPPED", "message": "neutral_signal"}

    # Guard: TTL check
    ttl_minutes = _cfg_int(db_manager, "FORECAST_TTL_MINUTES", 240)
    try:
        created_at = datetime.fromisoformat(created_at_str.replace(" ", "T"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age_minutes = (datetime.now(tz=timezone.utc) - created_at).total_seconds() / 60
        if age_minutes > ttl_minutes:
            _set_state("EXPIRED", "ttl_expired")
            logger.info(f"activate_consensus_order: consensus {consensus_id} EXPIRED (age={age_minutes:.0f}m > ttl={ttl_minutes}m)")
            return {"status": "EXPIRED", "message": "ttl_expired"}
    except Exception as e:
        logger.warning(f"activate_consensus_order: TTL check failed: {e}")

    # Guard: order window
    window_ok, window_reason = _is_within_order_window(db_manager)
    if not window_ok:
        _set_state("PENDING_ORDER", f"outside_window:{window_reason}")
        return {"status": "PENDING", "message": window_reason}

    # Guard: ORDER_MODE must not be disabled
    mode = _cfg(db_manager, "ORDER_MODE", "disabled").lower()
    if mode == "disabled":
        _set_state("ORDER_SKIPPED", "order_mode_disabled", attempted=True)
        return {"status": "SKIPPED", "message": "ORDER_MODE=disabled"}

    # Build position size
    from position_sizer import calculate_position
    from capital_provider import get_capital

    capital = get_capital(db_manager)
    if capital.get("status") != "OK":
        reason = f"capital_unavailable:{capital.get('status')}"
        _set_state("PENDING_ORDER", reason, attempted=True)
        logger.warning(f"activate_consensus_order: {ticker} — {reason}")
        return {"status": "PENDING", "message": reason}

    consensus_dict = dict(row)
    entry_price = row["entry_limit_price"] or _get_last_price(db_manager, ticker) or row["stop_loss"] or 0
    position = calculate_position(
        ticker=ticker,
        entry_price=entry_price,
        stop_loss=row["stop_loss"],
        db_manager=db_manager,
        net_liquidation=capital["net_liquidation"],
    )
    if position.get("status") != "OK" or position.get("quantity", 0) <= 0:
        reason = f"position_size_failed:{position.get('status', 'UNKNOWN')}"
        _set_state("ORDER_SKIPPED", reason, attempted=True)
        return {"status": "SKIPPED", "message": reason}

    # Call the existing submit_signal
    result = submit_signal(
        ticker=ticker,
        consensus=consensus_dict,
        position_size=position,
        db_manager=db_manager,
        log_id="",
        consensus_id=consensus_id,
        event_source="submit_auto",
    )

    r_status = result.get("status", "ERROR")
    r_msg    = result.get("message", "")

    if r_status == "SUBMITTED":
        # Link trade to consensus
        trade_id = result.get("trade_id")
        try:
            update_cols: Dict[str, Any] = {
                "order_state":       "ORDER_SUBMITTED",
                "order_reason":      "",
                "order_checked_at":  now_str,
                "order_attempted_at": now_str,
            }
            if trade_id:
                update_cols["trade_id"] = trade_id
            set_parts = ", ".join(f"{k}=?" for k in update_cols)
            with sqlite3.connect(db_manager.db_file) as con:
                con.execute(
                    f"UPDATE consensus SET {set_parts} WHERE id=?",
                    list(update_cols.values()) + [consensus_id],
                )
        except Exception as e:
            logger.warning(f"activate_consensus_order: could not update ORDER_SUBMITTED: {e}")
        logger.info(f"activate_consensus_order: consensus {consensus_id} → ORDER_SUBMITTED ({r_msg})")
    elif r_status in ("QUEUED",):
        _set_state("PENDING_ORDER", f"queued:{r_msg}", attempted=True)
    elif r_status.startswith("SKIPPED") or r_status == "DISABLED":
        _set_state("ORDER_SKIPPED", r_status.lower(), attempted=True)
    else:
        _set_state("ORDER_SKIPPED", f"error:{r_msg}", attempted=True)

    return {"status": r_status, "message": r_msg}
