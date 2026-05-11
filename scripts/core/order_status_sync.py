"""Manual IB order status synchronization helpers.

Syncs IB order statuses into local orders/trades tables in an idempotent way.
"""

from __future__ import annotations

import logging
import sqlite3
import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


_IB_TO_INTERNAL = {
    "APIPENDING": "SUBMITTED",
    "SUBMITTED": "SUBMITTED",
    "PENDINGSUBMIT": "SUBMITTED",
    "PRESUBMITTED": "SUBMITTED",
    "PENDINGCANCEL": "SUBMITTED",
    "APICANCELLED": "CANCELLED",
    "APICANCELED": "CANCELLED",
    "FILLED": "FILLED",
    "CANCELLED": "CANCELLED",
    "CANCELED": "CANCELLED",
    "INACTIVE": "REJECTED",
}

_TERMINAL_ORDER_STATUSES = {"FILLED", "FILLED_ENTRY", "CANCELLED", "REJECTED", "EXPIRED"}


def _now_utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_iso_utc(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _map_target_status(ib_status: str, order_role: str) -> Optional[str]:
    mapped = _IB_TO_INTERNAL.get((ib_status or "").upper())
    if mapped is None:
        return None
    if mapped == "FILLED" and (order_role or "").upper() == "ENTRY":
        return "FILLED_ENTRY"
    return mapped


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _parse_order_ref_parts(order_ref: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for token in str(order_ref or "").split("|"):
        if "=" not in token:
            continue
        k, v = token.split("=", 1)
        key = k.strip().lower()
        value = v.strip()
        if key and value:
            parts[key] = value
    return parts


def _extract_trade_uid_from_order_ref(order_ref: str) -> str:
    return _parse_order_ref_parts(order_ref).get("tid", "")


def _calculate_latency_ms(submitted_at: str, filled_at: str) -> Optional[int]:
    submitted = _parse_iso_utc(submitted_at)
    filled = _parse_iso_utc(filled_at)
    if submitted is None or filled is None:
        return None
    delta = (filled - submitted).total_seconds() * 1000
    if delta < 0:
        return None
    return int(delta)


def _table_has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:
        return False
    return any(str(r[1]) == column for r in rows)


def _fetch_statuses_with_event_loop(host: str, port: int, client_id: int) -> list:
    """Fetch IB statuses ensuring a loop exists in the current worker thread.

    ib_insync/eventkit accesses asyncio loop policy during import/init, so
    executor threads need an explicitly attached event loop.
    """
    created_loop: Optional[asyncio.AbstractEventLoop] = None
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.get_event_loop()
            except RuntimeError:
                created_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(created_loop)

        from scripts.core.ib_gateway_client import fetch_open_order_statuses

        return fetch_open_order_statuses(host=host, port=port, client_id=client_id)
    finally:
        if created_loop is not None:
            try:
                created_loop.close()
            finally:
                asyncio.set_event_loop(None)


def _update_trade_on_entry_fill(con: sqlite3.Connection, ib_parent_id: int, fill_price: Optional[float], filled_at: str) -> int:
    if not ib_parent_id or fill_price is None:
        return 0
    row = con.execute(
        "SELECT id FROM trades WHERE ib_parent_id=? ORDER BY id DESC LIMIT 1",
        (ib_parent_id,),
    ).fetchone()
    if not row:
        return 0
    con.execute(
        "UPDATE trades SET entry_price=?, entry_filled_at=?, updated_at=? WHERE id=?",
        (fill_price, filled_at, _now_utc_iso(), row["id"]),
    )
    return 1


def _get_trade_id_for_parent(con: sqlite3.Connection, ib_parent_id: int) -> Optional[int]:
    if not ib_parent_id:
        return None
    row = con.execute(
        "SELECT id FROM trades WHERE ib_parent_id=? ORDER BY id DESC LIMIT 1",
        (ib_parent_id,),
    ).fetchone()
    if not row:
        return None
    try:
        return int(row["id"])
    except Exception:
        return None


def _log_status_transaction(
    db_manager,
    *,
    con: Optional[sqlite3.Connection] = None,
    event_type: str = "ORDER_STATUS_UPDATE",
    operation_status: str = "APPLIED",
    event_source: str,
    ticker: str,
    trade_uid: str,
    ib_order_id: int,
    ib_perm_id: int,
    ib_parent_id: int,
    order_id: int,
    trade_id: Optional[int],
    status_before: str,
    status_after: str,
    response_payload: dict,
) -> None:
    payload_json = json.dumps(response_payload, ensure_ascii=True, separators=(",", ":"), default=str)

    # Prefer writing through the caller's active transaction/connection to avoid
    # nested writer connections that can deadlock with SQLite WAL.
    if con is not None:
        try:
            con.execute(
                """
                INSERT INTO ib_order_transactions (
                    occurred_at, event_source, event_type, operation_status,
                    status_before, status_after, ticker, trade_uid,
                    ib_order_id, ib_perm_id, ib_parent_id,
                    order_id, trade_id, consensus_id, log_id,
                    request_payload_json, response_payload_json,
                    error_message, latency_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    _now_utc_iso(),
                    event_source,
                    event_type,
                    operation_status,
                    status_before,
                    status_after,
                    ticker,
                    trade_uid,
                    ib_order_id,
                    ib_perm_id,
                    ib_parent_id,
                    order_id,
                    trade_id,
                    None,
                    "",
                    "",
                    payload_json,
                    "",
                    None,
                ),
            )
            return
        except Exception as e:
            logger.warning(f"_log_status_transaction in-transaction write failed: {e}")

    if not hasattr(db_manager, "log_ib_transaction"):
        return
    try:
        db_manager.log_ib_transaction(
            occurred_at=_now_utc_iso(),
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
            request_payload_json="",
            response_payload_json=payload_json,
            error_message="",
            latency_ms=None,
        )
    except Exception as e:
        logger.warning(f"_log_status_transaction failed: {e}")


def _calculate_realized_pnl(signal: str, qty: float, entry: float, exit_price: float) -> float:
    if (signal or "").upper() == "SHORT":
        return (entry - exit_price) * qty
    return (exit_price - entry) * qty


def _find_order_for_status_row(
    con: sqlite3.Connection,
    status_row: dict[str, Any],
    *,
    has_order_trade_uid_col: bool,
    has_order_ib_perm_id_col: bool,
) -> Optional[sqlite3.Row]:
    ib_order_id = int(status_row.get("ib_order_id") or 0)
    if ib_order_id <= 0:
        return None

    select_cols = "id, ticker, ib_parent_id, order_role, status, submitted_at"
    if has_order_trade_uid_col:
        select_cols += ", trade_uid"
    else:
        select_cols += ", '' AS trade_uid"

    ib_perm_id = int(status_row.get("ib_perm_id") or 0)
    if has_order_ib_perm_id_col and ib_perm_id > 0:
        row = con.execute(
            f"SELECT {select_cols} FROM orders "
            "WHERE ib_order_id=? AND ib_perm_id=? ORDER BY id DESC LIMIT 1",
            (ib_order_id, ib_perm_id),
        ).fetchone()
        if row is not None:
            return row

    trade_uid = _extract_trade_uid_from_order_ref(str(status_row.get("order_ref") or ""))
    if has_order_trade_uid_col and trade_uid:
        row = con.execute(
            f"SELECT {select_cols} FROM orders "
            "WHERE ib_order_id=? AND trade_uid=? ORDER BY id DESC LIMIT 1",
            (ib_order_id, trade_uid),
        ).fetchone()
        if row is not None:
            return row

    # Transitional fallback: match by ib_order_id alone.
    # Handles orders submitted before ib_perm_id/trade_uid tracking was added
    # (local rows have ib_perm_id=0/NULL and no order_ref with tid=).
    return con.execute(
        f"SELECT {select_cols} FROM orders WHERE ib_order_id=? ORDER BY id DESC LIMIT 1",
        (ib_order_id,),
    ).fetchone()


def _update_trade_on_exit_fill(
    con: sqlite3.Connection,
    ib_parent_id: int,
    order_role: str,
    fill_price: Optional[float],
    filled_at: str,
) -> int:
    if not ib_parent_id or fill_price is None:
        return 0
    trade = con.execute(
        "SELECT id, signal, quantity, entry_price, status FROM trades WHERE ib_parent_id=? ORDER BY id DESC LIMIT 1",
        (ib_parent_id,),
    ).fetchone()
    if not trade:
        return 0

    if str(trade["status"] or "").upper() == "CLOSED":
        return 0

    entry_price = _safe_float(trade["entry_price"])
    qty = _safe_float(trade["quantity"]) or 0.0
    realized_pnl: Optional[float] = None
    if entry_price is not None and qty > 0:
        realized_pnl = _calculate_realized_pnl(str(trade["signal"] or ""), qty, entry_price, fill_price)

    close_reason = "TAKE_PROFIT" if (order_role or "").upper() == "TAKE_PROFIT" else "STOP_LOSS"
    con.execute(
        "UPDATE trades "
        "SET status='CLOSED', exit_price=?, exit_filled_at=?, close_reason=?, realized_pnl=?, updated_at=? "
        "WHERE id=?",
        (fill_price, filled_at, close_reason, realized_pnl, _now_utc_iso(), trade["id"]),
    )
    return 1


def _cancel_sibling_exit_orders(
    con: sqlite3.Connection,
    *,
    parent_id: int,
    filled_order_id: int,
    ticker: str,
    event_source: str,
    db_manager,
) -> int:
    if not parent_id or not filled_order_id:
        return 0

    has_trade_uid_col = _table_has_column(con, "orders", "trade_uid")
    has_ib_perm_id_col = _table_has_column(con, "orders", "ib_perm_id")
    select_cols = "id, ib_order_id, ib_parent_id, status"
    if has_ib_perm_id_col:
        select_cols += ", ib_perm_id"
    else:
        select_cols += ", 0 AS ib_perm_id"
    if has_trade_uid_col:
        select_cols += ", trade_uid"
    else:
        select_cols += ", '' AS trade_uid"

    siblings = con.execute(
        f"""
        SELECT {select_cols}
        FROM orders
        WHERE ib_parent_id=?
          AND id<>?
          AND UPPER(order_role) IN ('TAKE_PROFIT','STOP_LOSS')
          AND UPPER(status) IN ('QUEUED','SUBMITTED')
        """,
        (parent_id, filled_order_id),
    ).fetchall()
    if not siblings:
        return 0

    updated = 0
    for sibling in siblings:
        con.execute(
            "UPDATE orders SET status='CANCELLED' WHERE id=?",
            (int(sibling["id"]),),
        )
        _log_status_transaction(
            db_manager,
            con=con,
            event_type="ORDER_STATUS_UPDATE",
            operation_status="INFERRED",
            event_source=event_source,
            ticker=ticker,
            trade_uid=str(sibling["trade_uid"] or ""),
            ib_order_id=int(sibling["ib_order_id"] or 0),
            ib_perm_id=int(sibling["ib_perm_id"] or 0),
            ib_parent_id=int(sibling["ib_parent_id"] or 0),
            order_id=int(sibling["id"]),
            trade_id=_get_trade_id_for_parent(con, int(sibling["ib_parent_id"] or 0)),
            status_before=str(sibling["status"] or ""),
            status_after="CANCELLED",
            response_payload={"reason": "sibling_exit_filled"},
        )
        updated += 1

    return updated


def _sync_consensus_from_trade(con: sqlite3.Connection, trade_id: Optional[int], stage: str = "update") -> int:
    """Sync consensus actual fields from filled trade data.
    
    Args:
        con: SQLite connection (within active transaction)
        trade_id: ID of the trade record to sync from
        stage: "entry_fill" | "exit_fill" for logging context
    
    Returns:
        1 if consensus updated, 0 otherwise
    """
    if not trade_id:
        return 0
    
    # Load trade record
    trade = con.execute(
        """
        SELECT id, consensus_id, signal, quantity, entry_price, stop_loss, 
               target_price, exit_price, close_reason, realized_pnl
        FROM trades WHERE id=?
        """,
        (trade_id,),
    ).fetchone()
    
    if not trade:
        return 0
    
    consensus_id = trade["consensus_id"]
    if not consensus_id:
        return 0  # Trade not linked to consensus
    
    # Load consensus record to get current values
    consensus = con.execute(
        "SELECT id, signal FROM consensus WHERE id=?",
        (consensus_id,),
    ).fetchone()
    
    if not consensus:
        return 0
    
    # --- Calculate metrics ---
    entry_price_actual = _safe_float(trade["entry_price"])
    exit_price = _safe_float(trade["exit_price"])
    stop_loss = _safe_float(trade["stop_loss"])
    target_price = _safe_float(trade["target_price"])
    signal = str(trade["signal"] or "NEUTRAL").upper()
    close_reason = str(trade["close_reason"] or "")
    quantity = _safe_float(trade["quantity"]) or 0.0
    realized_pnl = _safe_float(trade["realized_pnl"])
    
    # target_hit: did price reach target in the correct direction?
    target_hit = 0
    if entry_price_actual and exit_price and target_price:
        if signal == "LONG":
            target_hit = 1 if exit_price >= target_price else 0
        elif signal == "SHORT":
            target_hit = 1 if exit_price <= target_price else 0
    
    # stop_hit: was stop-loss triggered?
    stop_hit = 1 if close_reason == "STOP_LOSS" else 0
    
    # pnl_pct: percentage P&L from entry to exit
    pnl_pct = None
    if entry_price_actual and entry_price_actual > 0 and exit_price is not None and exit_price > 0:
        if signal == "LONG":
            pnl_pct = round((exit_price - entry_price_actual) / entry_price_actual * 100, 2)
        elif signal == "SHORT":
            pnl_pct = round((entry_price_actual - exit_price) / entry_price_actual * 100, 2)
    
    # r_multiple: risk-reward ratio (PnL % / Risk %)
    r_multiple = None
    if pnl_pct is not None and stop_loss and entry_price_actual and entry_price_actual > 0:
        risk_pct = abs(entry_price_actual - stop_loss) / entry_price_actual * 100
        if risk_pct > 0:
            r_multiple = round(pnl_pct / risk_pct, 3)
    
    # exit_successful: 1 if target hit without stop, 0 if stopped, None if still open
    exit_successful = None
    if target_hit and not stop_hit:
        exit_successful = 1
    elif stop_hit:
        exit_successful = 0
    
    # Build update dict only for fields that have computed values
    updates = {
        "eval_status": "EVALUATED",
        "updated_at": _now_utc_iso(),
    }
    
    if entry_price_actual is not None:
        updates["entry_price_actual"] = entry_price_actual
    
    updates["target_hit"] = target_hit
    updates["stop_hit"] = stop_hit
    
    if pnl_pct is not None:
        updates["pnl_pct"] = pnl_pct
    
    if r_multiple is not None:
        updates["r_multiple"] = r_multiple
    
    if exit_successful is not None:
        updates["exit_successful"] = exit_successful
    
    if realized_pnl is not None:
        updates["realized_pnl"] = realized_pnl
    
    # Execute update
    if len(updates) > 2:  # More than just eval_status and updated_at
        set_clause = ", ".join(f"{k}=?" for k in updates)
        con.execute(
            f"UPDATE consensus SET {set_clause} WHERE id=?",
            list(updates.values()) + [consensus_id],
        )
        logger.info(
            f"consensus_sync: id={consensus_id} {signal} → "
            f"entry_actual={entry_price_actual} target_hit={target_hit} "
            f"stop_hit={stop_hit} pnl={pnl_pct}% R={r_multiple}"
        )
        return 1
    
    return 0


def sync_orders_with_ib(
    db_manager,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 14,
    source: str = "manual",
) -> dict:
    """Run one-shot synchronization from IB open trades into local orders/trades."""
    summary = {
        "ok": True,
        "scanned": 0,
        "updated_orders": 0,
        "updated_trades": 0,
        "updated_consensus": 0,
        "errors": [],
        "mapping_warnings": [],
        "synced_at": _now_utc_iso(),
    }

    try:
        statuses = _fetch_statuses_with_event_loop(host=host, port=port, client_id=client_id)
    except Exception as e:
        summary["ok"] = False
        summary["errors"].append(f"fetch_error:{e}")
        return summary
    summary["scanned"] = len(statuses)

    if not statuses:
        return summary

    event_source = "sync_scheduler" if str(source).lower() == "scheduler" else "sync_manual"

    try:
        with sqlite3.connect(db_manager.db_file) as con:
            con.row_factory = sqlite3.Row
            has_order_trade_uid_col = _table_has_column(con, "orders", "trade_uid")
            has_order_ib_perm_id_col = _table_has_column(con, "orders", "ib_perm_id")
            for status_row in statuses:
                try:
                    ib_order_id = int(status_row.get("ib_order_id") or 0)
                    ib_perm_id = int(status_row.get("ib_perm_id") or 0)
                    trade_uid = _extract_trade_uid_from_order_ref(str(status_row.get("order_ref") or ""))
                    if ib_order_id <= 0:
                        continue

                    order = _find_order_for_status_row(
                        con,
                        status_row,
                        has_order_trade_uid_col=has_order_trade_uid_col,
                        has_order_ib_perm_id_col=has_order_ib_perm_id_col,
                    )
                    if order is None:
                        msg = f"mapping_error:ib_order_id={ib_order_id};ib_perm_id={ib_perm_id};trade_uid={trade_uid}"
                        summary["mapping_warnings"].append(msg)
                        logger.warning(f"order_status_sync: {msg}")
                        continue

                    ib_status = str(status_row.get("status") or "")
                    target_status = _map_target_status(ib_status, str(order["order_role"] or ""))
                    if target_status is None:
                        continue

                    current_status = str(order["status"] or "")
                    if current_status in _TERMINAL_ORDER_STATUSES and target_status == "SUBMITTED":
                        _log_status_transaction(
                            db_manager,
                            con=con,
                            event_type="ORDER_STATUS_CHECK",
                            operation_status="NO_CHANGE",
                            event_source=event_source,
                            ticker=str(order["ticker"] or ""),
                            trade_uid=str(order["trade_uid"] or ""),
                            ib_order_id=ib_order_id,
                            ib_perm_id=ib_perm_id,
                            ib_parent_id=int(order["ib_parent_id"] or 0),
                            order_id=int(order["id"]),
                            trade_id=_get_trade_id_for_parent(con, int(order["ib_parent_id"] or 0)),
                            status_before=current_status,
                            status_after=target_status,
                            response_payload=status_row,
                        )
                        continue

                    avg_fill = _safe_float(status_row.get("avg_fill_price"))
                    last_update = str(status_row.get("last_update") or "")
                    filled_at = last_update or _now_utc_iso()

                    updates: dict[str, Any] = {}
                    if current_status != target_status:
                        updates["status"] = target_status

                    if target_status in ("FILLED", "FILLED_ENTRY"):
                        updates["filled_at"] = filled_at
                        if avg_fill is not None and avg_fill > 0:
                            updates["filled_price"] = avg_fill
                        latency = _calculate_latency_ms(str(order["submitted_at"] or ""), filled_at)
                        if latency is not None:
                            updates["execution_latency_ms"] = latency

                    if updates:
                        set_clause = ", ".join(f"{k}=?" for k in updates)
                        con.execute(
                            f"UPDATE orders SET {set_clause} WHERE id=?",
                            list(updates.values()) + [order["id"]],
                        )
                        summary["updated_orders"] += 1
                        _log_status_transaction(
                            db_manager,
                            con=con,
                            event_type="ORDER_STATUS_UPDATE",
                            operation_status="APPLIED",
                            event_source=event_source,
                            ticker=str(order["ticker"] or ""),
                            trade_uid=str(order["trade_uid"] or ""),
                            ib_order_id=ib_order_id,
                            ib_perm_id=ib_perm_id,
                            ib_parent_id=int(order["ib_parent_id"] or 0),
                            order_id=int(order["id"]),
                            trade_id=_get_trade_id_for_parent(con, int(order["ib_parent_id"] or 0)),
                            status_before=current_status,
                            status_after=target_status,
                            response_payload=status_row,
                        )
                    else:
                        _log_status_transaction(
                            db_manager,
                            con=con,
                            event_type="ORDER_STATUS_CHECK",
                            operation_status="NO_CHANGE",
                            event_source=event_source,
                            ticker=str(order["ticker"] or ""),
                            trade_uid=str(order["trade_uid"] or ""),
                            ib_order_id=ib_order_id,
                            ib_perm_id=ib_perm_id,
                            ib_parent_id=int(order["ib_parent_id"] or 0),
                            order_id=int(order["id"]),
                            trade_id=_get_trade_id_for_parent(con, int(order["ib_parent_id"] or 0)),
                            status_before=current_status,
                            status_after=target_status,
                            response_payload=status_row,
                        )

                    if target_status == "FILLED_ENTRY":
                        if _update_trade_on_entry_fill(
                            con,
                            int(order["ib_parent_id"] or 0),
                            avg_fill,
                            filled_at,
                        ):
                            summary["updated_trades"] += 1
                            trade_id = _get_trade_id_for_parent(con, int(order["ib_parent_id"] or 0))
                            summary["updated_consensus"] += _sync_consensus_from_trade(con, trade_id, "entry_fill")
                    elif target_status == "FILLED":
                        if _update_trade_on_exit_fill(
                            con,
                            int(order["ib_parent_id"] or 0),
                            str(order["order_role"] or ""),
                            avg_fill,
                            filled_at,
                        ):
                            summary["updated_trades"] += 1
                            trade_id = _get_trade_id_for_parent(con, int(order["ib_parent_id"] or 0))
                            summary["updated_consensus"] += _sync_consensus_from_trade(con, trade_id, "exit_fill")
                        
                        if str(order["order_role"] or "").upper() in {"TAKE_PROFIT", "STOP_LOSS"}:
                            summary["updated_orders"] += _cancel_sibling_exit_orders(
                                con,
                                parent_id=int(order["ib_parent_id"] or 0),
                                filled_order_id=int(order["id"]),
                                ticker=str(order["ticker"] or ""),
                                event_source=event_source,
                                db_manager=db_manager,
                            )
                except Exception as row_error:
                    summary["errors"].append(f"row_error:{row_error}")

    except Exception as e:
        logger.error(f"order_status_sync failed: {e}")
        summary["ok"] = False
        summary["errors"].append(str(e))

    summary["synced_at"] = _now_utc_iso()
    if summary["errors"]:
        summary["ok"] = False
    if summary["mapping_warnings"]:
        logger.warning(
            f"order_status_sync: {len(summary['mapping_warnings'])} IB order(s) could not be matched "
            f"to local records (orphaned): {'; '.join(summary['mapping_warnings'])}"
        )
    return summary
