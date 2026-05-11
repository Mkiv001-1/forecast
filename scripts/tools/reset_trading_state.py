"""Full reset of order/trade execution state (IB + SQLite).

What it does:
1) Cancels all non-terminal open IB orders.
2) Closes all open IB positions with market orders.
3) Clears orders/trades in SQLite and resets consensus execution fields.

What it does NOT do:
- Does not remove consensus rows.
- Does not remove forecast logs.

Usage:
  python scripts/tools/reset_trading_state.py
  python scripts/tools/reset_trading_state.py --dry-run
  python scripts/tools/reset_trading_state.py --ib-only
  python scripts/tools/reset_trading_state.py --db-only
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORE_DIR = os.path.join(PROJECT_ROOT, "scripts", "core")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

from scripts.server.config import ServerConfig
from scripts.core.sqlite_manager import SQLiteManager
from ib_gateway_client import (
    cancel_order_safe,
    close_position_market_safe,
    fetch_ib_positions,
    fetch_open_order_statuses,
)


TERMINAL_ORDER_STATUSES = {
    "FILLED",
    "CANCELLED",
    "CANCELED",
    "INACTIVE",
    "APICANCELLED",
    "APICANCELED",
}


def _resolve_db_file(explicit_db_file: str) -> str:
    if explicit_db_file:
        return os.path.abspath(explicit_db_file)
    cfg = ServerConfig()
    return cfg.db_file


def _resolve_port(explicit_port: int, order_mode: str) -> int:
    if explicit_port:
        return explicit_port
    return 7496 if str(order_mode).lower() == "live" else 7497


def _symbol_from_ticker(ticker: str) -> str:
    raw = str(ticker or "").strip()
    if not raw:
        return ""
    if ":" in raw:
        return raw.split(":")[-1].strip()
    return raw


def _collect_open_order_ids(host: str, port: int, client_id: int) -> List[int]:
    statuses = fetch_open_order_statuses(host=host, port=port, client_id=client_id)
    order_ids: List[int] = []
    for row in statuses:
        status = str(row.get("status", "")).upper()
        order_id = int(row.get("ib_order_id") or 0)
        if order_id <= 0:
            continue
        if status in TERMINAL_ORDER_STATUSES:
            continue
        order_ids.append(order_id)
    # de-duplicate while preserving order
    return list(dict.fromkeys(order_ids))


def _collect_open_positions(host: str, port: int, client_id: int) -> List[Dict[str, Any]]:
    positions = fetch_ib_positions(host=host, port=port, client_id=client_id)
    open_positions: List[Dict[str, Any]] = []
    for row in positions:
        qty = float(row.get("quantity") or 0.0)
        if abs(qty) <= 1e-9:
            continue
        symbol = _symbol_from_ticker(str(row.get("ticker") or ""))
        if not symbol:
            continue
        open_positions.append({
            "symbol": symbol,
            "quantity": qty,
            "account": str(row.get("account") or "").strip(),
            "ticker": str(row.get("ticker") or ""),
        })
    return open_positions


def reset_ib_state(
    host: str,
    port: int,
    client_id: int,
    dry_run: bool,
    allow_extended_hours: bool,
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "ok": False,
        "orders_found": 0,
        "orders_cancel_attempted": 0,
        "orders_cancel_ok": 0,
        "orders_cancel_failed": 0,
        "positions_found": 0,
        "positions_close_attempted": 0,
        "positions_close_submitted": 0,
        "positions_close_failed": 0,
        "remaining_open_orders": 0,
        "remaining_open_positions": 0,
        "errors": [],
    }

    try:
        open_order_ids = _collect_open_order_ids(host=host, port=port, client_id=client_id)
        summary["orders_found"] = len(open_order_ids)

        open_positions = _collect_open_positions(host=host, port=port, client_id=client_id)
        summary["positions_found"] = len(open_positions)

        if dry_run:
            summary["remaining_open_orders"] = len(open_order_ids)
            summary["remaining_open_positions"] = len(open_positions)
            summary["ok"] = True
            return summary

        for idx, order_id in enumerate(open_order_ids):
            summary["orders_cancel_attempted"] += 1
            ok = cancel_order_safe(
                order_id=order_id,
                host=host,
                port=port,
                client_id=client_id + 10 + idx,
            )
            if ok:
                summary["orders_cancel_ok"] += 1
            else:
                summary["orders_cancel_failed"] += 1

        for idx, pos in enumerate(open_positions):
            summary["positions_close_attempted"] += 1
            result = close_position_market_safe(
                symbol=pos["symbol"],
                quantity=float(pos["quantity"]),
                account=pos["account"],
                allow_extended_hours=allow_extended_hours,
                host=host,
                port=port,
                client_id=client_id + 100 + idx,
            )
            if str(result.get("status", "")).lower() == "submitted":
                summary["positions_close_submitted"] += 1
            else:
                summary["positions_close_failed"] += 1
                summary["errors"].append(
                    f"close failed for {pos['ticker']} ({pos['quantity']}): {result.get('error')}"
                )

        remaining_orders = _collect_open_order_ids(host=host, port=port, client_id=client_id + 200)
        remaining_positions = _collect_open_positions(host=host, port=port, client_id=client_id + 201)

        summary["remaining_open_orders"] = len(remaining_orders)
        summary["remaining_open_positions"] = len(remaining_positions)
        summary["ok"] = (
            summary["orders_cancel_failed"] == 0
            and summary["positions_close_failed"] == 0
            and summary["remaining_open_orders"] == 0
            and summary["remaining_open_positions"] == 0
        )
        if summary["remaining_open_orders"] > 0:
            summary["errors"].append(f"remaining_open_orders={summary['remaining_open_orders']}")
        if summary["remaining_open_positions"] > 0:
            summary["errors"].append(f"remaining_open_positions={summary['remaining_open_positions']}")

        return summary
    except Exception as e:
        summary["errors"].append(str(e))
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reset order/trade state in IB and SQLite while keeping consensus and forecasts"
    )
    parser.add_argument("--db-file", default="", help="Path to SQLite DB (default from server config)")
    parser.add_argument("--host", default="127.0.0.1", help="IB host")
    parser.add_argument("--port", type=int, default=0, help="IB port (default by ORDER_MODE)")
    parser.add_argument("--client-id", type=int, default=311, help="Base IB client id")
    parser.add_argument("--allow-extended-hours", action="store_true", help="Use outsideRth for close orders")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without changing IB/DB")
    parser.add_argument("--ib-only", action="store_true", help="Run only IB reset")
    parser.add_argument("--db-only", action="store_true", help="Run only DB reset")
    parser.add_argument(
        "--no-reset-eval-status",
        action="store_true",
        help="Do not force consensus.eval_status='PENDING' during DB reset",
    )
    args = parser.parse_args()

    if args.ib_only and args.db_only:
        print("Error: --ib-only and --db-only are mutually exclusive")
        return 2

    db_file = _resolve_db_file(args.db_file)
    db = SQLiteManager(db_file)

    order_mode = str(db.get_config_value("ORDER_MODE", "paper") or "paper").lower()
    ib_port = _resolve_port(args.port, order_mode)

    print("=== Trading State Reset ===")
    print(f"DB: {db_file}")
    print(f"ORDER_MODE: {order_mode}")
    print(f"IB endpoint: {args.host}:{ib_port}")
    print(f"Dry run: {args.dry_run}")
    print(f"Modes: ib_only={args.ib_only}, db_only={args.db_only}")

    ib_summary: Dict[str, Any] = {"ok": True}
    db_summary: Dict[str, Any] = {"ok": True}

    if not args.db_only:
        ib_summary = reset_ib_state(
            host=args.host,
            port=ib_port,
            client_id=args.client_id,
            dry_run=args.dry_run,
            allow_extended_hours=args.allow_extended_hours,
        )
        print("IB summary:")
        for key in [
            "orders_found",
            "orders_cancel_attempted",
            "orders_cancel_ok",
            "orders_cancel_failed",
            "positions_found",
            "positions_close_attempted",
            "positions_close_submitted",
            "positions_close_failed",
            "remaining_open_orders",
            "remaining_open_positions",
        ]:
            print(f"  {key}: {ib_summary.get(key)}")
        if ib_summary.get("errors"):
            print("  errors:")
            for err in ib_summary.get("errors", []):
                print(f"    - {err}")

    if not args.ib_only:
        if args.dry_run:
            print("DB summary:")
            print("  dry-run: DB reset skipped")
        else:
            db_summary = db.reset_orders_and_trades_state(
                reset_eval_status=not args.no_reset_eval_status
            )
            print("DB summary:")
            for key in [
                "deleted_ib_transactions",
                "deleted_orders",
                "deleted_trades",
                "updated_consensus",
            ]:
                print(f"  {key}: {db_summary.get(key)}")
            if db_summary.get("errors"):
                print("  errors:")
                for err in db_summary.get("errors", []):
                    print(f"    - {err}")

    success = bool(ib_summary.get("ok", True)) and bool(db_summary.get("ok", True))
    if args.dry_run:
        print("Dry-run completed.")
        return 0

    if success:
        print("Reset completed successfully.")
        return 0

    print("Reset completed with errors.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
