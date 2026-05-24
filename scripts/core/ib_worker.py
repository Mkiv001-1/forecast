"""
IB Worker — single-threaded serialized queue for all IB Gateway calls.

All server-side IB interactions go through `ib_request(op, **kwargs)`.
The worker runs in one dedicated thread (ThreadPoolExecutor(max_workers=1))
so IB Gateway never sees more than one active client_id from the server.

Usage (from async context):
    from scripts.core.ib_worker import ib_request
    result = await ib_request("place_bracket", symbol="AAPL", ...)

Usage (from sync thread, e.g. order_manager running in executor):
    from scripts.core.ib_worker import ib_request_sync
    result = ib_request_sync("place_bracket", symbol="AAPL", ...)

Lifecycle (FastAPI lifespan):
    await start_ib_worker(db_manager)
    ...
    await stop_ib_worker()
"""

import asyncio
import concurrent.futures
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0
_IB_CLIENT_ID = 20  # single client_id used by the worker


# ---------------------------------------------------------------------------
# Worker state
# ---------------------------------------------------------------------------

@dataclass
class _WorkerState:
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    loop: Optional[asyncio.AbstractEventLoop] = None
    task: Optional[asyncio.Task] = None
    db_manager: Any = None
    running: bool = False
    _mock_handler: Optional[Any] = None  # for testing


_state = _WorkerState()


# ---------------------------------------------------------------------------
# Request envelope
# ---------------------------------------------------------------------------

@dataclass
class _IBRequest:
    op: str
    kwargs: Dict[str, Any]
    future: asyncio.Future


# ---------------------------------------------------------------------------
# Operation dispatch
# ---------------------------------------------------------------------------

def _execute_op(op: str, kwargs: Dict[str, Any]) -> Any:
    """Execute a single IB operation synchronously. Runs inside worker thread."""
    if _state._mock_handler is not None:
        return _state._mock_handler(op, kwargs)

    if op == "place_bracket":
        from ib_gateway_client import place_bracket_order_safe
        return place_bracket_order_safe(**kwargs)

    if op == "cancel_order":
        from ib_gateway_client import cancel_order_safe
        return cancel_order_safe(**kwargs)

    if op == "close_position":
        from ib_gateway_client import close_position_market_safe
        return close_position_market_safe(**kwargs)

    if op == "fetch_orders":
        from ib_gateway_client import _run_with_isolated_loop, fetch_open_order_statuses
        return _run_with_isolated_loop(fetch_open_order_statuses, **kwargs)

    if op == "fetch_positions":
        from ib_gateway_client import _run_with_isolated_loop, fetch_ib_positions
        return _run_with_isolated_loop(fetch_ib_positions, **kwargs)

    if op == "get_bid_ask":
        from ib_gateway_client import get_bid_ask_spread_safe
        return get_bid_ask_spread_safe(**kwargs)

    if op == "test_connection":
        from ib_gateway_client import _run_with_isolated_loop, test_ib_connection
        return _run_with_isolated_loop(test_ib_connection, **kwargs)

    if op == "sync_orders":
        from scripts.core.order_status_sync import sync_orders_with_ib
        db = kwargs.pop("db_manager")
        return sync_orders_with_ib(db, **kwargs)

    if op == "sync_accounts":
        import asyncio as _asyncio
        from ib_gateway_client import sync_accounts_with_ib_async
        db = kwargs.pop("db_manager")
        try:
            loop = _asyncio.new_event_loop()
            result = loop.run_until_complete(sync_accounts_with_ib_async(db, **kwargs))
            loop.close()
            return result
        except Exception as e:
            logger.error(f"ib_worker: sync_accounts failed: {e}")
            return False

    if op == "sync_portfolio":
        import asyncio as _asyncio
        from ib_gateway_client import sync_portfolio_with_ib_async
        db = kwargs.pop("db_manager")
        try:
            loop = _asyncio.new_event_loop()
            result = loop.run_until_complete(sync_portfolio_with_ib_async(db, **kwargs))
            loop.close()
            return result
        except Exception as e:
            logger.error(f"ib_worker: sync_portfolio failed: {e}")
            return False

    if op == "fetch_position_status":
        from ib_gateway_client import _run_with_isolated_loop, fetch_ib_position_status_by_con_id
        return _run_with_isolated_loop(fetch_ib_position_status_by_con_id, **kwargs)

    if op == "fetch_order_status":
        from ib_gateway_client import _run_with_isolated_loop, fetch_ib_order_status_by_order_id
        return _run_with_isolated_loop(fetch_ib_order_status_by_order_id, **kwargs)

    if op == "snapshot_portfolio":
        import asyncio as _asyncio
        from ib_gateway_client import snapshot_portfolio_history_async
        db = kwargs.pop("db_manager")
        try:
            loop = _asyncio.new_event_loop()
            result = loop.run_until_complete(snapshot_portfolio_history_async(db, **kwargs))
            loop.close()
            return result
        except Exception as e:
            logger.error(f"ib_worker: snapshot_portfolio failed: {e}")
            return 0

    raise ValueError(f"ib_worker: unknown op '{op}'")


# ---------------------------------------------------------------------------
# Worker loop (runs as asyncio Task)
# ---------------------------------------------------------------------------

async def _worker_loop() -> None:
    """Drain the queue, execute each request in a thread executor."""
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="ib-worker"
    )
    loop = asyncio.get_running_loop()
    logger.info("ib_worker: started")

    try:
        while _state.running:
            try:
                req: _IBRequest = await asyncio.wait_for(_state.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if req.future.cancelled():
                _state.queue.task_done()
                continue

            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(executor, _execute_op, req.op, req.kwargs),
                    timeout=_DEFAULT_TIMEOUT,
                )
                if not req.future.done():
                    req.future.set_result(result)
            except asyncio.TimeoutError:
                logger.error(f"ib_worker: op='{req.op}' timed out after {_DEFAULT_TIMEOUT}s")
                if not req.future.done():
                    req.future.set_exception(
                        TimeoutError(f"IB op '{req.op}' timed out after {_DEFAULT_TIMEOUT}s")
                    )
            except Exception as e:
                logger.error(f"ib_worker: op='{req.op}' failed: {e}")
                if not req.future.done():
                    req.future.set_exception(e)
            finally:
                _state.queue.task_done()
    finally:
        executor.shutdown(wait=False)
        logger.info("ib_worker: stopped")


# ---------------------------------------------------------------------------
# Public lifecycle API
# ---------------------------------------------------------------------------

async def start_ib_worker(db_manager: Any = None) -> None:
    """Start the IB worker. Call from FastAPI lifespan startup."""
    if _state.running:
        logger.warning("ib_worker: already running, ignoring start")
        return
    _state.db_manager = db_manager
    _state.running = True
    _state.queue = asyncio.Queue()
    _state.loop = asyncio.get_running_loop()
    _state.task = asyncio.create_task(_worker_loop(), name="ib-worker")
    logger.info("ib_worker: task created")


async def stop_ib_worker() -> None:
    """Stop the IB worker. Call from FastAPI lifespan shutdown."""
    _state.running = False
    if _state.task and not _state.task.done():
        try:
            await asyncio.wait_for(_state.task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _state.task.cancel()
    _state.task = None
    _state.loop = None
    logger.info("ib_worker: shutdown complete")


# ---------------------------------------------------------------------------
# Public request API
# ---------------------------------------------------------------------------

async def ib_request(op: str, timeout: float = _DEFAULT_TIMEOUT, **kwargs) -> Any:
    """Submit an IB operation and await the result (non-blocking).

    Raises TimeoutError if the operation exceeds `timeout` seconds.
    Raises RuntimeError if the worker is not running.
    """
    if not _state.running or _state.loop is None:
        raise RuntimeError("ib_worker is not running — call start_ib_worker() first")

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    await _state.queue.put(_IBRequest(op=op, kwargs=kwargs, future=future))
    return await asyncio.wait_for(future, timeout=timeout + 2.0)


def ib_request_sync(op: str, timeout: float = _DEFAULT_TIMEOUT, **kwargs) -> Any:
    """Submit an IB operation from a synchronous (non-async) thread.

    Uses run_coroutine_threadsafe to bridge into the worker's event loop.
    Blocks the calling thread until the result is available.
    Raises RuntimeError if worker is not running.
    """
    if not _state.running or _state.loop is None:
        raise RuntimeError("ib_worker is not running — call start_ib_worker() first")

    future = asyncio.run_coroutine_threadsafe(
        ib_request(op, timeout=timeout, **kwargs),
        _state.loop,
    )
    return future.result(timeout=timeout + 5.0)


# ---------------------------------------------------------------------------
# Testing helpers
# ---------------------------------------------------------------------------

def set_mock_handler(handler: Optional[Any]) -> None:
    """Set a mock handler for testing. handler(op, kwargs) -> result.
    Pass None to clear.
    """
    _state._mock_handler = handler


def is_running() -> bool:
    """Return True if the worker is active."""
    return _state.running
