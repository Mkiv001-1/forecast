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
                'account_type': '',
                'base_currency': 'USD',
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


def sync_accounts_with_ib(excel_manager, host: str = "127.0.0.1", port: int = 7497, client_id: int = 1) -> bool:
    """Fetch account balances from IB and store them in SQLite accounts table."""
    try:
        accounts = fetch_ib_accounts(host, port, client_id)
        if not accounts:
            logger.warning("No accounts fetched from IB")
            return False

        excel_manager.clear_sheet('Accounts')
        for acc in accounts:
            excel_manager.upsert_row('Accounts', acc)

        logger.info(f"Synced {len(accounts)} accounts to accounts table")
        return True
    except Exception as e:
        logger.error(f"Accounts sync error: {e}")
        return False


def sync_portfolio_with_ib(excel_manager, host: str = "127.0.0.1", port: int = 7497, client_id: int = 1) -> bool:
    """Fetch positions AND accounts from IB and store them in SQLite."""
    try:
        ok1 = sync_accounts_with_ib(excel_manager, host, port, client_id)
        if not ok1:
            logger.warning("Account sync returned no data, continuing with positions")

        positions = fetch_ib_positions(host, port, client_id)
        if not positions:
            logger.warning("No positions fetched from IB")
            return False

        for pos in positions:
            excel_manager.upsert_row('Portfolio', pos)

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

async def sync_accounts_with_ib_async(excel_manager, host: str = "127.0.0.1", port: int = 7497, client_id: int = 1) -> bool:
    """Async version that runs sync_accounts_with_ib in a thread to avoid event loop conflicts."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, 
        functools.partial(_sync_accounts_wrapped, excel_manager, host, port, client_id)
    )


async def sync_portfolio_with_ib_async(excel_manager, host: str = "127.0.0.1", port: int = 7497, client_id: int = 1) -> bool:
    """Async version that runs sync_portfolio_with_ib in a thread to avoid event loop conflicts."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, 
        functools.partial(_sync_portfolio_wrapped, excel_manager, host, port, client_id)
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
