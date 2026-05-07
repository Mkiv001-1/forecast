"""IB Gateway client — sync wrapper around ib_insync to fetch portfolio positions and account balances."""

import asyncio
import functools
import logging
import threading
from datetime import datetime
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


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
        ib.connect(host, port, clientId=client_id, timeout=timeout)
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
        ib.connect(host, port, clientId=client_id, timeout=10)
        logger.info(f"Connected to IB Gateway {host}:{port}")

        for pos in ib.portfolio():  # noqa: E501
            contract = pos.contract
            ticker = f"{contract.exchange}:{contract.symbol}" if hasattr(contract, 'exchange') and contract.exchange else contract.symbol
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
        if not positions:
            logger.warning("No positions fetched from IB")
            return False

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
    try:
        ib.connect(host, port, clientId=client_id, timeout=15)
        order = Order()
        order.orderId = order_id
        ib.cancelOrder(order)
        ib.sleep(1)
        logger.info(f"[IB] cancel_order: orderId={order_id} sent")
        return True
    except Exception as e:
        logger.error(f"[IB] cancel_order {order_id} failed: {e}")
        return False
    finally:
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
        ib.connect(host, port, clientId=client_id, timeout=15)
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
