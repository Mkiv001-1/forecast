"""IB Gateway client — sync wrapper around ib_insync to fetch portfolio positions and account balances."""

import asyncio
import functools
import logging
import threading
from datetime import datetime
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


_TERMINAL_IB_STATUSES = {"FILLED", "CANCELLED", "CANCELED", "INACTIVE"}
_CANCELING_IB_STATUSES = {"APICANCELLED", "APICANCELED"}


def _safe_float(val) -> Optional[float]:
    try:
        return float(val) if val is not None else 0.0
    except Exception:
        return 0.0


def _safe_int(val) -> int:
    try:
        return int(val) if val is not None else 0
    except Exception:
        return 0


def _normalize_contract_ticker(contract: Any) -> str:
    symbol = str(getattr(contract, "symbol", "") or "").strip()
    exchange = str(getattr(contract, "exchange", "") or "").strip()
    primary_exchange = str(getattr(contract, "primaryExchange", "") or "").strip()

    if not symbol:
        return ""
    if exchange.upper() == "SMART" and primary_exchange:
        return f"{primary_exchange}:{symbol}"
    if exchange:
        return f"{exchange}:{symbol}"
    return symbol


def _client_id_candidates(preferred: int) -> list[int]:
    ids: list[int] = []
    for candidate in (preferred, preferred + 100, preferred + 200):
        if 0 <= int(candidate) <= 999 and int(candidate) not in ids:
            ids.append(int(candidate))
    return ids or [int(preferred)]


def _connect_with_client_id_fallback(ib, host: str, port: int, client_id: int, timeout: int | float) -> int:
    last_error: Exception | None = None
    for candidate in _client_id_candidates(int(client_id)):
        try:
            ib.connect(host, port, clientId=candidate, timeout=timeout)
            return candidate
        except Exception as e:
            last_error = e
            if "client id is already in use" in str(e).lower():
                logger.warning(f"[IB] clientId {candidate} is busy, retrying with fallback id")
                continue
            raise
    if last_error is not None:
        raise last_error
    raise RuntimeError("IB connect failed: no client_id candidates")


def _extract_trade_last_update(trade: Any) -> str:
    log_items = getattr(trade, "log", None) or []
    if not log_items:
        return ""
    timestamp = getattr(log_items[-1], "time", "")
    return timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)


def _trade_to_status_record(trade: Any) -> Optional[Dict[str, Any]]:
    try:
        order = getattr(trade, "order", None)
        status_obj = getattr(trade, "orderStatus", None)
        order_id = _safe_int(getattr(order, "orderId", 0))
        if order_id <= 0:
            return None
        return {
            "ib_order_id": order_id,
            "ib_perm_id": _safe_int(getattr(order, "permId", 0)),
            "order_ref": str(getattr(order, "orderRef", "") or ""),
            "status": str(getattr(status_obj, "status", "") or ""),
            "avg_fill_price": _safe_float(getattr(status_obj, "avgFillPrice", None)),
            "filled_qty": _safe_float(getattr(status_obj, "filled", None)),
            "last_update": _extract_trade_last_update(trade),
        }
    except Exception:
        return None


def _status_priority(status: str) -> int:
    normalized = str(status or "").upper()
    if normalized in _TERMINAL_IB_STATUSES or normalized in _CANCELING_IB_STATUSES:
        return 3
    if normalized in {"APIPENDING", "PENDINGCANCEL", "PENDINGSUBMIT", "SUBMITTED", "PRESUBMITTED"}:
        return 2
    if normalized:
        return 1
    return 0


def _merge_status_record(records_by_order_id: Dict[int, Dict[str, Any]], record: Optional[Dict[str, Any]]) -> None:
    if not record:
        return
    order_id = _safe_int(record.get("ib_order_id"))
    if order_id <= 0:
        return

    existing = records_by_order_id.get(order_id)
    if existing is None:
        records_by_order_id[order_id] = record
        return

    existing_priority = _status_priority(existing.get("status", ""))
    new_priority = _status_priority(record.get("status", ""))
    if new_priority > existing_priority:
        records_by_order_id[order_id] = record
        return

    if new_priority == existing_priority:
        existing_fill = _safe_float(existing.get("avg_fill_price"))
        new_fill = _safe_float(record.get("avg_fill_price"))
        if (new_fill or 0.0) > (existing_fill or 0.0):
            records_by_order_id[order_id] = record
            return
        if record.get("last_update") and not existing.get("last_update"):
            records_by_order_id[order_id] = record


def _ib_summary_to_dict(summary_list) -> Dict[str, Dict[str, str]]:
    """Convert ib_insync accountSummary list to {account: {tag: value}}."""
    result: Dict[str, Dict[str, str]] = {}
    for av in summary_list:
        acc = getattr(av, 'account', '') or ''
        tag = getattr(av, 'tag', '') or ''
        val = getattr(av, 'value', '') or ''
        if acc and tag:
            result.setdefault(acc, {})[tag] = val
    return result


def fetch_ib_accounts(host: str = "127.0.0.1", port: int = 7497, client_id: int = 1, timeout: int = 30) -> List[Dict[str, Any]]:
    """Connect to IB Gateway, fetch account balances, disconnect.

    Returns list of dicts ready for the accounts table.
    """
    accounts: List[Dict[str, Any]] = []
    try:
        from ib_insync import IB
    except ImportError:
        logger.error("ib_insync not installed. Run: pip install ib_insync")
        return accounts

    ib = IB()
    try:
        logger.info(f"[IB] Connecting to {host}:{port} with clientId={client_id}, timeout={timeout}s...")
        connected_client_id = _connect_with_client_id_fallback(ib, host, port, client_id, timeout)
        if connected_client_id != client_id:
            logger.info(f"[IB] Connected with fallback clientId={connected_client_id} (requested={client_id})")
        logger.info(f"[IB] Connected successfully")
        logger.info(f"Connected to IB Gateway {host}:{port}")

        managed = ib.managedAccounts()
        logger.info(f"Managed accounts: {managed}")

        for acc_id in managed:
            try:
                summary = ib.accountSummary(acc_id)
                data = _ib_summary_to_dict(summary).get(acc_id, {})
            except Exception as e:
                logger.warning(f"Could not fetch accountSummary for {acc_id}: {e}")
                data = {}

            accounts.append({
                'broker': 'ibkr',
                'account_id': acc_id or '',
                'name': '',
                'account_type': data.get('AccountType', ''),
                'base_currency': data.get('Currency', 'USD'),
                'buying_power': _safe_float(data.get('BuyingPower')),
                'net_liquidation': _safe_float(data.get('NetLiquidation')),
                'available_funds': _safe_float(data.get('AvailableFunds')),
                'cash': _safe_float(data.get('CashBalance')),
                'maintenance_margin': _safe_float(data.get('MaintMarginReq')),
                'last_update': datetime.now().isoformat(),
            })

        logger.info(f"Fetched {len(accounts)} accounts from IB")
    except Exception as e:
        logger.error(f"IB Gateway accounts error: {e}")
        raise
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
                logger.info("Disconnected from IB Gateway")
        except Exception:
            pass

    return accounts


def fetch_ib_positions(host: str = "127.0.0.1", port: int = 7497, client_id: int = 1) -> List[Dict[str, Any]]:
    """Connect to IB Gateway, fetch portfolio positions, disconnect.

    Returns list of dicts ready to upsert into portfolio table.
    """
    positions: List[Dict[str, Any]] = []
    try:
        from ib_insync import IB
    except ImportError:
        logger.error("ib_insync not installed. Run: pip install ib_insync")
        return positions

    ib = IB()
    try:
        connected_client_id = _connect_with_client_id_fallback(ib, host, port, client_id, 10)
        if connected_client_id != client_id:
            logger.info(f"[IB] positions fallback clientId={connected_client_id} (requested={client_id})")
        logger.info(f"Connected to IB Gateway {host}:{port}")

        for pos in ib.portfolio():  # noqa: E501
            contract = pos.contract
            ticker = _normalize_contract_ticker(contract)
            con_id = _safe_int(getattr(contract, 'conId', None))

            positions.append({
                'ticker': ticker,
                'account': pos.account or '',
                'broker': 'ibkr',
                'quantity': _safe_float(pos.position),
                'avg_cost': _safe_float(getattr(pos, 'averageCost', None)),
                'market_price': _safe_float(getattr(pos, 'marketPrice', None)),
                'market_value': _safe_float(getattr(pos, 'marketValue', None)),
                'unrealized_pnl': _safe_float(getattr(pos, 'unrealizedPNL', None)),
                'realized_pnl': _safe_float(getattr(pos, 'realizedPNL', None)),
                'currency': getattr(contract, 'currency', 'USD') or 'USD',
                'asset_type': getattr(contract, 'secType', 'STK') or 'STK',
                'sector': '',
                'last_update': datetime.now().isoformat(),
                'con_id': con_id,
            })

        logger.info(f"Fetched {len(positions)} positions from IB")
    except Exception as e:
        logger.error(f"IB Gateway positions error: {e}")
        raise
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
                logger.info("Disconnected from IB Gateway")
        except Exception:
            pass

    return positions


def sync_accounts_with_ib(db_manager, host: str = "127.0.0.1", port: int = 7497, client_id: int = 1, type: str = "paper") -> bool:
    """Fetch account balances from IB and store them in SQLite accounts table."""
    try:
        accounts = fetch_ib_accounts(host, port, client_id)
        if not accounts:
            logger.warning("No accounts fetched from IB")
            return False

        db_manager.clear_sheet('Accounts')
        for acc in accounts:
            acc['type'] = type  # 'paper' or 'live'
            db_manager.upsert_row('Accounts', acc)

        logger.info(f"Synced {len(accounts)} accounts to accounts table")
        return True
    except Exception as e:
        logger.error(f"Accounts sync error: {e}")
        return False


def sync_portfolio_with_ib(db_manager, host: str = "127.0.0.1", port: int = 7497, client_id: int = 1, type: str = "paper") -> bool:
    """Fetch positions AND accounts from IB and store them in SQLite."""
    try:
        ok1 = sync_accounts_with_ib(db_manager, host, port, client_id, type)
        if not ok1:
            logger.warning("Account sync returned no data, continuing with positions")

        positions = fetch_ib_positions(host, port, client_id)
        # Keep local portfolio aligned with IB even when portfolio is empty.
        db_manager.clear_sheet('Portfolio')
        if not positions:
            # Empty portfolio is a valid state; do not treat as transport failure.
            logger.info("No positions fetched from IB (empty portfolio)")
            return True

        for pos in positions:
            pos['type'] = type  # 'paper' or 'live'
            db_manager.upsert_row('Portfolio', pos)

        logger.info(f"Synced {len(positions)} positions to portfolio table")
        return True
    except Exception as e:
        logger.error(f"Portfolio sync error: {e}")
        return False


def _run_in_thread(func):
    """Decorator to run function in thread with isolated event loop."""
    def wrapper(*args, **kwargs):
        result = None
        error = None
        
        def target():
            nonlocal result, error
            try:
                # Create fresh event loop for this thread
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result = func(*args, **kwargs)
            except Exception as e:
                error = e
            finally:
                try:
                    loop.close()
                except:
                    pass
        
        thread = threading.Thread(target=target)
        thread.start()
        thread.join(timeout=30)
        
        if error:
            raise error
        return result
    
    return wrapper


def _run_with_isolated_loop(func, *args, **kwargs):
    """Run a callable in a dedicated thread with a fresh asyncio event loop."""
    result = None
    error = None

    def target():
        nonlocal result, error
        loop = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = func(*args, **kwargs)
        except Exception as e:
            error = e
        finally:
            if loop is not None:
                try:
                    loop.close()
                except Exception:
                    pass

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()

    if error is not None:
        raise error
    return result


# Create wrapped versions of sync functions
_sync_accounts_wrapped = _run_in_thread(sync_accounts_with_ib)
_sync_portfolio_wrapped = _run_in_thread(sync_portfolio_with_ib)


# ---------------------------------------------------------------------------
# Async wrappers for use in FastAPI (avoids event loop conflicts)
# ---------------------------------------------------------------------------

async def sync_accounts_with_ib_async(db_manager, host: str = "127.0.0.1", port: int = 7497, client_id: int = 1, type: str = "paper") -> bool:
    """Async version that runs sync_accounts_with_ib in a thread to avoid event loop conflicts."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, 
        functools.partial(_sync_accounts_wrapped, db_manager, host, port, client_id, type)
    )


async def sync_portfolio_with_ib_async(db_manager, host: str = "127.0.0.1", port: int = 7497, client_id: int = 1, type: str = "paper") -> bool:
    """Async version that runs sync_portfolio_with_ib in a thread to avoid event loop conflicts."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, 
        functools.partial(_sync_portfolio_wrapped, db_manager, host, port, client_id, type)
    )


def test_ib_connection(host: str = "127.0.0.1", port: int = 7497, client_id: int = 1) -> Dict[str, Any]:
    """Test connection to IB Gateway with detailed logging.

    Returns dict with connection status, logs, and details.
    """
    logs: List[str] = []
    result = {
        "success": False,
        "host": host,
        "port": port,
        "client_id": client_id,
        "logs": logs,
        "error": None,
        "accounts": [],
        "positions_count": 0,
    }

    def log(msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        entry = f"[{timestamp}] {msg}"
        logs.append(entry)
        logger.info(f"[IB_TEST] {msg}")

    log(f"Starting connection test to {host}:{port} with clientId={client_id}")

    try:
        from ib_insync import IB
        log("ib_insync imported successfully")
    except ImportError as e:
        log(f"ERROR: ib_insync not installed: {e}")
        result["error"] = f"ib_insync not installed: {e}"
        return result

    ib = IB()
    try:
        log(f"Attempting to connect to {host}:{port}...")
        ib.connect(host, port, clientId=client_id, timeout=10)
        log(f"SUCCESS: Connected to IB Gateway {host}:{port}")
        try:
            log(f"Connection info: serverVersion={ib.client.serverVersion()}")
        except:
            pass

        log("Fetching managed accounts...")
        managed = ib.managedAccounts()
        log(f"Managed accounts: {managed}")
        result["accounts"] = list(managed)

        log("Fetching portfolio positions...")
        positions = list(ib.portfolio())
        log(f"Found {len(positions)} positions")
        result["positions_count"] = len(positions)

        for pos in positions[:3]:
            contract = pos.contract
            log(f"  Position: {contract.symbol} @ {pos.position} (value: {getattr(pos, 'marketValue', 'N/A')})")

        if len(positions) > 3:
            log(f"  ... and {len(positions) - 3} more positions")

        log("Connection test completed successfully")
        result["success"] = True

    except Exception as e:
        error_msg = str(e)
        log(f"ERROR: Connection failed: {error_msg}")
        result["error"] = error_msg

    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
                log("Disconnected from IB Gateway")
        except Exception as e:
            log(f"Warning during disconnect: {e}")

    return result


# Create wrapped version after test_ib_connection is defined
_test_connection_wrapped = _run_in_thread(test_ib_connection)


async def test_ib_connection_async(host: str = "127.0.0.1", port: int = 7497, client_id: int = 1) -> Dict[str, Any]:
    """Async version of test_ib_connection."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, 
        functools.partial(_test_connection_wrapped, host, port, client_id)
    )


# ---------------------------------------------------------------------------
# Order placement (Step 7 additions)
# ---------------------------------------------------------------------------

def place_bracket_order(
    symbol: str,
    action: str,
    quantity: float,
    stop_loss_price: float,
    take_profit_price: float,
    entry_price: float = None,
    entry_order_type: str = "MKT",
    entry_tif: str = "DAY",
    take_profit_tif: str = "GTC",
    stop_loss_tif: str = "GTC",
    account: str = "",
    use_stop_limit: bool = False,
    stop_limit_offset_pct: float = 0.0005,
    allow_extended_hours: bool = False,
    order_ref: str = "",
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 10,
) -> Dict[str, Any]:
    """
    Place a bracket order (Entry + Stop + Limit take-profit) via IB.

    Entry can be Market (MKT) or Limit (LMT) based on entry_order_type.

    Returns:
        {parent_id, stop_id, target_id, status, error}
    """
    result = {"parent_id": None, "stop_id": None, "target_id": None, "status": "error", "error": None}
    try:
        from ib_insync import IB, Stock, MarketOrder, StopOrder, StopLimitOrder, LimitOrder
    except ImportError:
        result["error"] = "ib_insync not installed"
        logger.error(result["error"])
        return result

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=15)
        logger.info(f"[IB] place_bracket_order: connected for {symbol} {action} qty={quantity}")

        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)

        oref = ib.client.getReqId()

        # Entry: Market or Limit order
        if entry_order_type.upper() == "LMT" and entry_price is not None and entry_price > 0:
            parent = LimitOrder(action=action, totalQuantity=quantity, lmtPrice=round(entry_price, 2))
            logger.info(f"[IB] Using Limit entry order at {entry_price}")
        else:
            parent = MarketOrder(action=action, totalQuantity=quantity)
            logger.info(f"[IB] Using Market entry order")

        parent.orderId = oref
        parent.transmit = False
        parent.tif = entry_tif
        if order_ref:
            parent.orderRef = f"{order_ref}|role=ENTRY"
        if account:
            parent.account = account
        if allow_extended_hours:
            parent.outsideRth = True

        # Take-profit: Limit
        tp_action = "SELL" if action == "BUY" else "BUY"
        target = LimitOrder(action=tp_action, totalQuantity=quantity, lmtPrice=round(take_profit_price, 2))
        target.orderId = oref + 1
        target.parentId = oref
        target.transmit = False
        target.tif = take_profit_tif
        if order_ref:
            target.orderRef = f"{order_ref}|role=TAKE_PROFIT"
        if account:
            target.account = account

        # Stop-loss: Stop or Stop-Limit
        sl_action = "SELL" if action == "BUY" else "BUY"
        if use_stop_limit:
            offset = stop_loss_price * stop_limit_offset_pct
            lmt = stop_loss_price - offset if action == "BUY" else stop_loss_price + offset
            stop = StopLimitOrder(
                action=sl_action,
                totalQuantity=quantity,
                lmtPrice=round(lmt, 2),
                stopPrice=round(stop_loss_price, 2),
            )
        else:
            stop = StopOrder(action=sl_action, totalQuantity=quantity, stopPrice=round(stop_loss_price, 2))
        stop.orderId = oref + 2
        stop.parentId = oref
        stop.transmit = True  # transmit all 3
        stop.tif = stop_loss_tif
        if order_ref:
            stop.orderRef = f"{order_ref}|role=STOP_LOSS"
        if account:
            stop.account = account

        parent_trade = ib.placeOrder(contract, parent)
        target_trade = ib.placeOrder(contract, target)
        stop_trade   = ib.placeOrder(contract, stop)

        ib.sleep(1)

        result.update({
            "parent_id": parent_trade.order.orderId,
            "target_id": target_trade.order.orderId,
            "stop_id":   stop_trade.order.orderId,
            "parent_perm_id": _safe_int(getattr(parent_trade.order, "permId", 0)),
            "target_perm_id": _safe_int(getattr(target_trade.order, "permId", 0)),
            "stop_perm_id": _safe_int(getattr(stop_trade.order, "permId", 0)),
            "status":    "submitted",
            "error":     None,
        })
        logger.info(
            f"[IB] bracket placed: parent={result['parent_id']} "
            f"target={result['target_id']} stop={result['stop_id']}"
        )

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"[IB] place_bracket_order failed: {e}")
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass

    return result


def cancel_order(
    order_id: int,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 11,
) -> bool:
    """Cancel a single IB order by order_id. Returns True on success."""
    try:
        from ib_insync import IB, Order
    except ImportError:
        logger.error("ib_insync not installed")
        return False

    ib = IB()
    errors_for_order = []

    def _on_error(req_id, error_code, error_string, contract):
        # IB reports many order-level failures asynchronously via errorEvent.
        if int(req_id or 0) == int(order_id):
            errors_for_order.append((int(error_code or 0), str(error_string or "")))

    try:
        ib.connect(host, port, clientId=client_id, timeout=15)
        ib.errorEvent += _on_error

        order = Order()
        order.orderId = order_id
        ib.cancelOrder(order)
        ib.sleep(1.0)

        # Common hard failure codes seen in practice for invalid/not-found order IDs.
        hard_fail_codes = {10147, 201, 202}
        hard_fail = [e for e in errors_for_order if e[0] in hard_fail_codes]
        if hard_fail:
            code, msg = hard_fail[0]
            logger.warning(
                f"[IB] cancel_order: orderId={order_id} failed code={code} msg={msg}"
            )
            return False

        logger.info(f"[IB] cancel_order: orderId={order_id} sent")
        return True
    except Exception as e:
        logger.error(f"[IB] cancel_order {order_id} failed: {e}")
        return False
    finally:
        try:
            ib.errorEvent -= _on_error
        except Exception:
            pass
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass


def close_position_market(
    symbol: str,
    quantity: float,
    account: str = "",
    allow_extended_hours: bool = False,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 12,
) -> Dict[str, Any]:
    """Close (or reduce) a position via a Market order. Returns {order_id, status, error}."""
    result = {"order_id": None, "status": "error", "error": None}
    try:
        from ib_insync import IB, Stock, MarketOrder
    except ImportError:
        result["error"] = "ib_insync not installed"
        return result

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=15)
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)
        action = "SELL" if quantity > 0 else "BUY"
        order = MarketOrder(action=action, totalQuantity=abs(quantity))
        if account:
            order.account = account
        if allow_extended_hours:
            order.outsideRth = True
        trade = ib.placeOrder(contract, order)
        ib.sleep(1)
        result.update({"order_id": trade.order.orderId, "status": "submitted", "error": None})
        logger.info(f"[IB] close_position_market: {symbol} {action} qty={abs(quantity)} orderId={result['order_id']}")
    except Exception as e:
        result["error"] = str(e)
        logger.error(f"[IB] close_position_market {symbol} failed: {e}")
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass
    return result


def get_bid_ask_spread(
    symbol: str,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 13,
    timeout: float = 5.0,
) -> Dict[str, Any]:
    """
    Fetch bid/ask via snapshot (reqMktData snapshot=True).
    Returns {bid, ask, spread, spread_pct, mid, status, error}.
    Compatible with current sync architecture — no persistent subscription.
    """
    result = {"bid": None, "ask": None, "spread": None, "spread_pct": None, "mid": None,
              "status": "error", "error": None}
    try:
        from ib_insync import IB, Stock
    except ImportError:
        result["error"] = "ib_insync not installed"
        return result

    ib = IB()
    try:
        connected_client_id = _connect_with_client_id_fallback(ib, host, port, client_id, 15)
        if connected_client_id != client_id:
            logger.info(f"[IB] spread fallback clientId={connected_client_id} (requested={client_id})")
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)

        ticker = ib.reqMktData(contract, "", snapshot=True)
        ib.sleep(timeout)

        bid = _safe_float(ticker.bid) if ticker.bid and ticker.bid > 0 else None
        ask = _safe_float(ticker.ask) if ticker.ask and ticker.ask > 0 else None

        if bid and ask and ask > bid:
            spread = ask - bid
            mid = (bid + ask) / 2.0
            spread_pct = spread / mid
            result.update({
                "bid": bid, "ask": ask,
                "spread": round(spread, 4),
                "spread_pct": round(spread_pct, 6),
                "mid": round(mid, 4),
                "status": "ok", "error": None,
            })
        else:
            result.update({"status": "no_data", "error": f"bid={bid} ask={ask}"})

        ib.cancelMktData(contract)
        logger.info(f"[IB] get_bid_ask_spread {symbol}: bid={bid} ask={ask} spread_pct={result.get('spread_pct')}")
    except Exception as e:
        result["error"] = str(e)
        logger.error(f"[IB] get_bid_ask_spread {symbol} failed: {e}")
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass
    return result


def place_bracket_order_safe(*args, **kwargs) -> Dict[str, Any]:
    """Safe wrapper for place_bracket_order with isolated event loop."""
    return _run_with_isolated_loop(place_bracket_order, *args, **kwargs)


def sync_accounts_with_ib_safe(*args, **kwargs) -> bool:
    """Safe wrapper for sync_accounts_with_ib with isolated event loop."""
    return _run_with_isolated_loop(sync_accounts_with_ib, *args, **kwargs)


def sync_portfolio_with_ib_safe(*args, **kwargs) -> bool:
    """Safe wrapper for sync_portfolio_with_ib with isolated event loop."""
    return _run_with_isolated_loop(sync_portfolio_with_ib, *args, **kwargs)


def get_bid_ask_spread_safe(*args, **kwargs) -> Dict[str, Any]:
    """Safe wrapper for get_bid_ask_spread with isolated event loop."""
    return _run_with_isolated_loop(get_bid_ask_spread, *args, **kwargs)


def cancel_order_safe(*args, **kwargs) -> bool:
    """Safe wrapper for cancel_order with isolated event loop."""
    return _run_with_isolated_loop(cancel_order, *args, **kwargs)


def close_position_market_safe(*args, **kwargs) -> Dict[str, Any]:
    """Safe wrapper for close_position_market with isolated event loop."""
    return _run_with_isolated_loop(close_position_market, *args, **kwargs)


def fetch_open_order_statuses(
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 14,
    timeout: float = 3.0,
) -> list:
    """
    Fetch status of all currently open/recently-filled orders from IB.

    Returns list of dicts:
      {ib_order_id, status, avg_fill_price, filled_qty, last_update}

    IB status values we care about:
      Submitted, PreSubmitted, Filled, Cancelled, Inactive (rejected)

    Returns [] on any connection error (caller must tolerate empty result).
    """
    records_by_order_id: Dict[int, Dict[str, Any]] = {}
    try:
        from ib_insync import IB
    except ImportError:
        logger.error("ib_insync not installed")
        return []

    ib = IB()
    try:
        connected_client_id = _connect_with_client_id_fallback(ib, host, port, client_id, 15)
        if connected_client_id != client_id:
            logger.info(f"[IB] open orders fallback clientId={connected_client_id} (requested={client_id})")
        trades = ib.openTrades()
        ib.sleep(timeout)

        for t in trades:
            try:
                _merge_status_record(records_by_order_id, _trade_to_status_record(t))
            except Exception as inner:
                logger.warning(f"[IB] fetch_open_order_statuses: skipping trade: {inner}")

        try:
            all_open_trades = ib.reqAllOpenOrders()
            for trade in all_open_trades or []:
                _merge_status_record(records_by_order_id, _trade_to_status_record(trade))
        except Exception as all_open_error:
            logger.info(f"[IB] fetch_open_order_statuses: reqAllOpenOrders unavailable: {all_open_error}")

        try:
            try:
                completed_trades = ib.reqCompletedOrders(apiOnly=False)
            except TypeError:
                completed_trades = ib.reqCompletedOrders()
            for trade in completed_trades or []:
                _merge_status_record(records_by_order_id, _trade_to_status_record(trade))
        except Exception as completed_error:
            logger.info(f"[IB] fetch_open_order_statuses: reqCompletedOrders unavailable: {completed_error}")

        try:
            executions = ib.executions()
            execution_groups: Dict[int, Dict[str, Any]] = {}
            for execution in executions or []:
                order_id = _safe_int(getattr(execution, "orderId", 0))
                if order_id <= 0:
                    continue
                qty = _safe_float(getattr(execution, "shares", 0.0))
                price = _safe_float(getattr(execution, "price", 0.0))
                group = execution_groups.setdefault(
                    order_id,
                    {
                        "filled_qty": 0.0,
                        "weighted_sum": 0.0,
                        "last_update": "",
                        "ib_perm_id": _safe_int(getattr(execution, "permId", 0)),
                    },
                )
                group["filled_qty"] += qty or 0.0
                group["weighted_sum"] += (qty or 0.0) * (price or 0.0)
                exec_time = getattr(execution, "time", "")
                group["last_update"] = exec_time.isoformat() if hasattr(exec_time, "isoformat") else str(exec_time)

            for order_id, group in execution_groups.items():
                filled_qty = group["filled_qty"]
                avg_fill_price = (group["weighted_sum"] / filled_qty) if filled_qty > 0 else 0.0
                _merge_status_record(
                    records_by_order_id,
                    {
                        "ib_order_id": order_id,
                        "ib_perm_id": _safe_int(group.get("ib_perm_id", 0)),
                        "order_ref": "",
                        "status": "Filled",
                        "avg_fill_price": round(avg_fill_price, 6),
                        "filled_qty": filled_qty,
                        "last_update": group["last_update"],
                    },
                )
        except Exception as executions_error:
            logger.info(f"[IB] fetch_open_order_statuses: executions unavailable: {executions_error}")

        records = list(records_by_order_id.values())
        logger.info(f"[IB] fetch_open_order_statuses: got {len(records)} records")
    except Exception as e:
        logger.error(f"[IB] fetch_open_order_statuses failed: {e}")
        records = []
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass
    return records


def fetch_ib_position_status_by_con_id(
    con_id: int,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 1,
) -> Dict[str, Any]:
    """Fetch live status for a specific position by contract id."""
    if con_id <= 0:
        raise ValueError("con_id must be > 0")

    try:
        from ib_insync import IB
    except ImportError as e:
        raise RuntimeError(f"ib_insync not installed: {e}") from e

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=15)
        for pos in ib.portfolio():
            contract = pos.contract
            row_con_id = _safe_int(getattr(contract, "conId", 0))
            if row_con_id != int(con_id):
                continue

            qty = _safe_float(getattr(pos, "position", None))
            status = "OPEN" if abs(qty) > 0 else "FLAT"
            updated_at = datetime.now().isoformat()
            return {
                "found": True,
                "con_id": int(con_id),
                "status": status,
                "position": {
                    "con_id": row_con_id,
                    "symbol": getattr(contract, "symbol", "") or "",
                    "account": getattr(pos, "account", "") or "",
                    "quantity": qty,
                    "avg_cost": _safe_float(getattr(pos, "averageCost", None)),
                    "market_price": _safe_float(getattr(pos, "marketPrice", None)),
                    "market_value": _safe_float(getattr(pos, "marketValue", None)),
                    "unrealized_pnl": _safe_float(getattr(pos, "unrealizedPNL", None)),
                    "realized_pnl": _safe_float(getattr(pos, "realizedPNL", None)),
                    "currency": getattr(contract, "currency", "USD") or "USD",
                    "exchange": getattr(contract, "exchange", "") or "",
                    "updated_at": updated_at,
                },
            }

        return {
            "found": False,
            "con_id": int(con_id),
            "status": "FLAT",
            "position": None,
        }
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass


def fetch_ib_order_status_by_order_id(
    order_id: int,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 14,
    timeout: float = 2.0,
) -> Dict[str, Any]:
    """Fetch live status for a specific IB order id."""
    if order_id <= 0:
        raise ValueError("order_id must be > 0")

    try:
        from ib_insync import IB
    except ImportError as e:
        raise RuntimeError(f"ib_insync not installed: {e}") from e

    ib = IB()
    try:
        connected_client_id = _connect_with_client_id_fallback(ib, host, port, client_id, 15)
        if connected_client_id != client_id:
            logger.info(f"[IB] order status fallback clientId={connected_client_id} (requested={client_id})")

        open_trades = ib.openTrades()
        ib.sleep(timeout)
        for trade in open_trades:
            row_order_id = _safe_int(getattr(trade.order, "orderId", 0))
            if row_order_id != int(order_id):
                continue

            status_obj = getattr(trade, "orderStatus", None)
            log_items = getattr(trade, "log", None) or []
            last_update = ""
            if log_items:
                ts = getattr(log_items[-1], "time", "")
                last_update = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)

            contract = getattr(trade, "contract", None)
            return {
                "found": True,
                "ib_order_id": int(order_id),
                "status": str(getattr(status_obj, "status", "")),
                "source": "openTrades",
                "order": {
                    "ib_order_id": row_order_id,
                    "perm_id": _safe_int(getattr(trade.order, "permId", 0)),
                    "symbol": getattr(contract, "symbol", "") if contract else "",
                    "account": getattr(trade.order, "account", "") or "",
                    "action": getattr(trade.order, "action", "") or "",
                    "order_type": getattr(trade.order, "orderType", "") or "",
                    "total_qty": _safe_float(getattr(trade.order, "totalQuantity", None)),
                    "filled_qty": _safe_float(getattr(status_obj, "filled", None)),
                    "remaining_qty": _safe_float(getattr(status_obj, "remaining", None)),
                    "avg_fill_price": _safe_float(getattr(status_obj, "avgFillPrice", None)),
                    "last_fill_price": _safe_float(getattr(status_obj, "lastFillPrice", None)),
                    "last_update": last_update,
                },
            }

        try:
            all_open_trades = ib.reqAllOpenOrders()
        except Exception:
            all_open_trades = []

        for trade in all_open_trades or []:
            row_order_id = _safe_int(getattr(getattr(trade, "order", None), "orderId", 0))
            if row_order_id != int(order_id):
                continue

            status_obj = getattr(trade, "orderStatus", None)
            contract = getattr(trade, "contract", None)
            return {
                "found": True,
                "ib_order_id": int(order_id),
                "status": str(getattr(status_obj, "status", "")),
                "source": "reqAllOpenOrders",
                "order": {
                    "ib_order_id": row_order_id,
                    "perm_id": _safe_int(getattr(getattr(trade, "order", None), "permId", 0)),
                    "symbol": getattr(contract, "symbol", "") if contract else "",
                    "account": getattr(getattr(trade, "order", None), "account", "") or "",
                    "action": getattr(getattr(trade, "order", None), "action", "") or "",
                    "order_type": getattr(getattr(trade, "order", None), "orderType", "") or "",
                    "total_qty": _safe_float(getattr(getattr(trade, "order", None), "totalQuantity", None)),
                    "filled_qty": _safe_float(getattr(status_obj, "filled", None)),
                    "remaining_qty": _safe_float(getattr(status_obj, "remaining", None)),
                    "avg_fill_price": _safe_float(getattr(status_obj, "avgFillPrice", None)),
                    "last_fill_price": _safe_float(getattr(status_obj, "lastFillPrice", None)),
                    "last_update": _extract_trade_last_update(trade),
                },
            }

        try:
            try:
                completed_trades = ib.reqCompletedOrders(apiOnly=False)
            except TypeError:
                completed_trades = ib.reqCompletedOrders()
        except Exception as completed_error:
            logger.info(f"[IB] fetch_ib_order_status_by_order_id: reqCompletedOrders unavailable: {completed_error}")
            completed_trades = []

        for trade in completed_trades or []:
            row_order_id = _safe_int(getattr(getattr(trade, "order", None), "orderId", 0))
            if row_order_id != int(order_id):
                continue

            status_obj = getattr(trade, "orderStatus", None)
            contract = getattr(trade, "contract", None)
            return {
                "found": True,
                "ib_order_id": int(order_id),
                "status": str(getattr(status_obj, "status", "")),
                "source": "completedOrders",
                "order": {
                    "ib_order_id": row_order_id,
                    "perm_id": _safe_int(getattr(getattr(trade, "order", None), "permId", 0)),
                    "symbol": getattr(contract, "symbol", "") if contract else "",
                    "account": getattr(getattr(trade, "order", None), "account", "") or "",
                    "action": getattr(getattr(trade, "order", None), "action", "") or "",
                    "order_type": getattr(getattr(trade, "order", None), "orderType", "") or "",
                    "total_qty": _safe_float(getattr(getattr(trade, "order", None), "totalQuantity", None)),
                    "filled_qty": _safe_float(getattr(status_obj, "filled", None)),
                    "remaining_qty": _safe_float(getattr(status_obj, "remaining", None)),
                    "avg_fill_price": _safe_float(getattr(status_obj, "avgFillPrice", None)),
                    "last_fill_price": _safe_float(getattr(status_obj, "lastFillPrice", None)),
                    "last_update": _extract_trade_last_update(trade),
                },
            }

        try:
            executions = ib.executions()
        except Exception as executions_error:
            logger.info(f"[IB] fetch_ib_order_status_by_order_id: executions unavailable: {executions_error}")
            executions = []
        matched_execs = [e for e in executions if _safe_int(getattr(e, "orderId", 0)) == int(order_id)]
        if matched_execs:
            total_qty = sum(_safe_float(getattr(e, "shares", 0.0)) for e in matched_execs)
            weighted = sum(
                _safe_float(getattr(e, "shares", 0.0)) * _safe_float(getattr(e, "price", 0.0))
                for e in matched_execs
            )
            avg_fill_price = (weighted / total_qty) if total_qty > 0 else 0.0
            last_exec = matched_execs[-1]
            exec_time = getattr(last_exec, "time", "")
            last_update = exec_time.isoformat() if hasattr(exec_time, "isoformat") else str(exec_time)

            return {
                "found": True,
                "ib_order_id": int(order_id),
                "status": "Filled",
                "source": "executions",
                "order": {
                    "ib_order_id": int(order_id),
                    "perm_id": _safe_int(getattr(last_exec, "permId", 0)),
                    "symbol": getattr(last_exec, "symbol", "") or "",
                    "account": getattr(last_exec, "acctNumber", "") or "",
                    "action": getattr(last_exec, "side", "") or "",
                    "order_type": "",
                    "total_qty": total_qty,
                    "filled_qty": total_qty,
                    "remaining_qty": 0.0,
                    "avg_fill_price": round(avg_fill_price, 6),
                    "last_fill_price": _safe_float(getattr(last_exec, "price", None)),
                    "last_update": last_update,
                },
            }

        return {
            "found": False,
            "ib_order_id": int(order_id),
            "status": "Unknown",
            "source": "none",
            "order": None,
        }
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass
