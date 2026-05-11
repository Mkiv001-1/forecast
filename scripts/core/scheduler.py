"""
Centralized async task scheduler.

Registers named tasks with schedules, runs them as asyncio.Tasks,
tracks status in the scheduled_tasks table, and provides a heartbeat.

Integration: call start_scheduler(db_manager) from FastAPI lifespan startup,
             call stop_scheduler() on shutdown.
"""

import asyncio
import concurrent.futures
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Callable, Dict, Optional, Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scheduler state (encapsulated in class to avoid global state)
# ---------------------------------------------------------------------------

class SchedulerState:
    """Encapsulated scheduler state to avoid global variables."""
    
    def __init__(self):
        self.tasks: Dict[str, asyncio.Task] = {}
        self.db_manager = None
        self.running = False
        self.task_running: Dict[str, bool] = {}  # overlap guard per task name
        self.thread_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
    
    def reset(self):
        """Reset state for clean shutdown/restart."""
        self.tasks.clear()
        self.task_running.clear()
        self.db_manager = None
        self.running = False
        if self.thread_pool:
            self.thread_pool.shutdown(wait=False, cancel_futures=True)
            self.thread_pool = None


# Module-level singleton instance
_state = SchedulerState()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _cfg(key: str, default: str = "") -> str:
    if _state.db_manager is None:
        return default
    try:
        v = _state.db_manager.get_config_value(key)
        return v if v is not None else default
    except Exception:
        return default


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(_cfg(key, str(default)))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# DB helpers (using encapsulated db_manager methods)
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _upsert_task(name: str, updates: dict) -> None:
    """Upsert scheduled task using db_manager method."""
    if _state.db_manager is None:
        return
    _state.db_manager.upsert_scheduled_task(name, updates)


def _increment_counters(name: str, success: bool, error_msg: str = "") -> None:
    """Increment task counters using db_manager method."""
    if _state.db_manager is None:
        return
    _state.db_manager.increment_task_counters(name, success, error_msg)


# ---------------------------------------------------------------------------
# Task runner
# ---------------------------------------------------------------------------

async def _run_task_loop(
    name: str,
    coro_factory: Callable,
    interval_seconds: float,
    max_retries: int = 2,
    run_on_start: bool = False,
) -> None:
    """Repeatedly call coro_factory() every interval_seconds, with retries."""
    logger.info(f"scheduler: task '{name}' started (interval={interval_seconds}s)")
    _state.task_running[name] = False

    # Calculate initial sleep: if not run_on_start, check last_run_at from DB
    # so that after restart we don't wait a full interval if the task is overdue.
    initial_sleep = interval_seconds
    if not run_on_start and _state.db_manager:
        last_run_str = _state.db_manager.get_scheduled_task_last_run(name)
        if last_run_str:
            try:
                last_run = datetime.fromisoformat(last_run_str)
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(tz=timezone.utc) - last_run).total_seconds()
                remaining = interval_seconds - elapsed
                if remaining <= 0:
                    initial_sleep = 0.0
                    logger.info(f"scheduler: '{name}' overdue by {-remaining:.0f}s, running immediately")
                else:
                    initial_sleep = remaining
                    logger.info(f"scheduler: '{name}' resuming, next run in {remaining:.0f}s")
            except Exception:
                pass
    elif run_on_start:
        initial_sleep = 0.0

    if initial_sleep > 0:
        await asyncio.sleep(initial_sleep)

    while _state.running:
        if _state.task_running.get(name):
            logger.warning(f"scheduler: '{name}' previous run still in progress, skipping interval")
            await asyncio.sleep(interval_seconds)
            continue
        _state.task_running[name] = True
        attempt = 0
        success = False
        last_error = ""
        try:
            while attempt <= max_retries and not success:
                try:
                    await coro_factory()
                    success = True
                except Exception as e:
                    last_error = str(e)
                    attempt += 1
                    if attempt <= max_retries:
                        wait = 2 ** attempt
                        logger.warning(
                            f"scheduler: '{name}' attempt {attempt}/{max_retries} failed: {e}. "
                            f"Retrying in {wait}s"
                        )
                        await asyncio.sleep(wait)
                    else:
                        logger.error(f"scheduler: '{name}' failed after {max_retries} retries: {e}")
        finally:
            _state.task_running[name] = False

        _increment_counters(name, success, last_error)
        await asyncio.sleep(interval_seconds)


# ---------------------------------------------------------------------------
# Built-in tasks
# ---------------------------------------------------------------------------

async def _heartbeat_task() -> None:
    """Check IB / OpenRouter / SQLite health and log to heartbeat_log."""
    ib_ok = 0
    or_ok = 0
    db_ok = 0
    notes = []

    # SQLite check
    if _state.db_manager:
        try:
            with _state.db_manager._connect() as con:
                con.execute("SELECT 1")
            db_ok = 1
        except Exception as e:
            notes.append(f"sqlite_err:{e}")

    # OpenRouter circuit-breaker check
    try:
        from circuit_breaker import get_state
        state = get_state()
        or_ok = 1 if state == "CLOSED" else 0
        if state != "CLOSED":
            notes.append(f"openrouter:{state}")
    except Exception:
        or_ok = 1  # If no circuit breaker yet, assume ok

    # IB check: just verify DB has recent accounts
    if _state.db_manager:
        try:
            account_count = _state.db_manager.get_accounts_count()
            ib_ok = 1 if account_count > 0 else 0
        except Exception as e:
            notes.append(f"ib_err:{e}")

    # Write to heartbeat_log
    if _state.db_manager:
        _state.db_manager.log_heartbeat(ib_ok, or_ok, db_ok, "; ".join(notes))

    logger.debug(f"heartbeat: ib={ib_ok} openrouter={or_ok} sqlite={db_ok}")


async def _order_timeout_task() -> None:
    """Check for bracket groups with missing children after fill."""
    try:
        from order_manager import check_child_timeouts
        check_child_timeouts(_state.db_manager)
    except Exception as e:
        logger.error(f"scheduler: order_timeout_task error: {e}")


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ensure_core_path() -> None:
    core_dir = os.path.join(_PROJECT_ROOT, "scripts", "core")
    for p in [_PROJECT_ROOT, core_dir]:
        if p not in sys.path:
            sys.path.insert(0, p)


def _run_forecast_sync() -> None:
    """Blocking call to run_trading_bot — executed in thread pool."""
    _ensure_core_path()
    os.chdir(_PROJECT_ROOT)
    from scripts.core.forecast_runner import run_trading_bot
    from scripts.core.sqlite_manager import SQLiteManager

    db_file = _state.db_manager.db_file if _state.db_manager else None
    db = SQLiteManager(db_file)
    run_id = None
    try:
        active_tickers = db.get_settings()
        run_id = db.create_forecast_run('scheduler', len(active_tickers))
        run_trading_bot(db_manager=db, run_id=run_id)
    except Exception as e:
        if run_id:
            db.complete_forecast_run(run_id, status='failed', error_message=str(e))
        raise


def _run_evaluate_sync() -> None:
    """Blocking call to evaluate_past_forecasts — executed in thread pool."""
    _ensure_core_path()
    os.chdir(_PROJECT_ROOT)
    from scripts.core.sqlite_manager import SQLiteManager
    from scripts.core.forecast_runner import evaluate_past_forecasts
    db_file = _state.db_manager.db_file if _state.db_manager else None
    db = SQLiteManager(db_file)
    evaluate_past_forecasts(db)


async def _scheduled_forecast_task() -> None:
    """Run forecast generation in a thread pool (non-blocking)."""
    loop = asyncio.get_running_loop()
    logger.info("scheduler: starting scheduled forecast run")
    if _state.thread_pool is None:
        raise RuntimeError("scheduler: thread pool is not initialized")
    await loop.run_in_executor(_state.thread_pool, _run_forecast_sync)
    logger.info("scheduler: scheduled forecast run complete")


async def _scheduled_evaluate_task() -> None:
    """Run evaluation of past forecasts in a thread pool (non-blocking)."""
    loop = asyncio.get_running_loop()
    logger.info("scheduler: starting scheduled evaluate run")
    if _state.thread_pool is None:
        raise RuntimeError("scheduler: thread pool is not initialized")
    await loop.run_in_executor(_state.thread_pool, _run_evaluate_sync)
    logger.info("scheduler: scheduled evaluate run complete")


def _run_consensus_evaluate_sync() -> None:
    """Blocking call to evaluate_consensus_records — executed in thread pool."""
    _ensure_core_path()
    os.chdir(_PROJECT_ROOT)
    from scripts.core.sqlite_manager import SQLiteManager
    from scripts.core.consensus_evaluator import evaluate_consensus_records
    db_file = _state.db_manager.db_file if _state.db_manager else None
    db = SQLiteManager(db_file)
    count = evaluate_consensus_records(db)
    logger.info(f"scheduler: consensus_evaluate completed, {count} records evaluated")


async def _scheduled_consensus_evaluate_task() -> None:
    """Run evaluation of consensus records in a thread pool (non-blocking)."""
    loop = asyncio.get_running_loop()
    logger.info("scheduler: starting scheduled consensus evaluate run")
    if _state.thread_pool is None:
        raise RuntimeError("scheduler: thread pool is not initialized")
    await loop.run_in_executor(_state.thread_pool, _run_consensus_evaluate_sync)
    logger.info("scheduler: scheduled consensus evaluate run complete")


def _run_price_data_update_sync() -> None:
    """Fetch and save fresh price data for all active tickers."""
    _ensure_core_path()
    os.chdir(_PROJECT_ROOT)
    from scripts.core.sqlite_manager import SQLiteManager
    from scripts.core.data_loader import fetch_price_data
    db_file = _state.db_manager.db_file if _state.db_manager else None
    db = SQLiteManager(db_file)
    tickers = db.get_active_tickers() if hasattr(db, "get_active_tickers") else []
    if not tickers:
        # fallback: read from settings table directly using encapsulated method
        tickers = db.get_active_tickers_direct()
    if not tickers:
        logger.warning("scheduler: price_data_update — no active tickers")
        return
    updated = 0
    for ticker in tickers:
        try:
            data = fetch_price_data(ticker, days=30, db_manager=db)
            if data:
                db.save_price_data(data, ticker=ticker)
                updated += 1
                logger.info(f"scheduler: price_data updated for {ticker} ({len(data)} bars)")
            else:
                logger.warning(f"scheduler: price_data_update — no data returned for {ticker}")
        except Exception as e:
            logger.error(f"scheduler: price_data_update error for {ticker}: {e}")
    logger.info(f"scheduler: price_data_update done. updated={updated}/{len(tickers)}")


async def _scheduled_price_data_task() -> None:
    """Refresh price data for all active tickers in a thread pool (non-blocking)."""
    loop = asyncio.get_running_loop()
    logger.info("scheduler: starting price_data update")
    if _state.thread_pool is None:
        raise RuntimeError("scheduler: thread pool is not initialized")
    await loop.run_in_executor(_state.thread_pool, _run_price_data_update_sync)
    logger.info("scheduler: price_data update complete")


def _run_intraday_update_sync() -> None:
    """Fetch and save fresh hourly bars for all active tickers."""
    _ensure_core_path()
    os.chdir(_PROJECT_ROOT)
    from scripts.core.sqlite_manager import SQLiteManager
    from scripts.core.data_loader import fetch_intraday_yfinance
    db_file = _state.db_manager.db_file if _state.db_manager else None
    db = SQLiteManager(db_file)
    tickers = db.get_active_tickers() if hasattr(db, "get_active_tickers") else []
    if not tickers:
        tickers = db.get_active_tickers_direct()
    if not tickers:
        logger.warning("scheduler: intraday_update — no active tickers")
        return
    updated = 0
    for ticker in tickers:
        try:
            bars = fetch_intraday_yfinance(ticker, days=60, interval="1h")
            if bars:
                db.save_intraday_data(bars, ticker=ticker, interval="1h")
                updated += 1
                logger.info(f"scheduler: intraday updated for {ticker} ({len(bars)} bars)")
            else:
                logger.warning(f"scheduler: intraday_update — no data returned for {ticker}")
        except Exception as e:
            logger.error(f"scheduler: intraday_update error for {ticker}: {e}")
    logger.info(f"scheduler: intraday_update done. updated={updated}/{len(tickers)}")


async def _scheduled_intraday_task() -> None:
    """Refresh hourly intraday bars for all active tickers (non-blocking)."""
    loop = asyncio.get_running_loop()
    logger.info("scheduler: starting intraday update")
    if _state.thread_pool is None:
        raise RuntimeError("scheduler: thread pool is not initialized")
    await loop.run_in_executor(_state.thread_pool, _run_intraday_update_sync)
    logger.info("scheduler: intraday update complete")


async def _expire_queued_orders_task() -> None:
    """Expire QUEUED orders older than ORDER_QUEUE_MAX_AGE_HOURS."""
    max_age_hours = _cfg_int("ORDER_QUEUE_MAX_AGE_HOURS", 24)
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    try:
        if _state.db_manager:
            expired = _state.db_manager.expire_queued_orders(cutoff)
            if expired:
                logger.info(f"scheduler: expired {expired} QUEUED orders older than {max_age_hours}h")
    except Exception as e:
        logger.error(f"scheduler: expire_queued_orders error: {e}")


def _run_process_pending_orders_sync() -> None:
    """Process all consensus records in PENDING_ORDER state — activate or expire them."""
    _ensure_core_path()
    os.chdir(_PROJECT_ROOT)
    from scripts.core.sqlite_manager import SQLiteManager
    from scripts.core.order_manager import activate_consensus_order
    db_file = _state.db_manager.db_file if _state.db_manager else None
    db = SQLiteManager(db_file)
    
    rows = db.get_pending_consensus_orders()
    if not rows:
        return

    logger.info(f"scheduler: pending_orders — processing {len(rows)} candidates")
    for row_id, ticker in rows:
        try:
            result = activate_consensus_order(row_id, db)
            logger.debug(f"scheduler: pending_orders consensus={row_id} {ticker} → {result['status']}: {result.get('message','')}")
        except Exception as e:
            logger.error(f"scheduler: pending_orders error for consensus={row_id} {ticker}: {e}")


async def _scheduled_pending_orders_task() -> None:
    """Activate pending consensus orders (non-blocking)."""
    loop = asyncio.get_running_loop()
    if _state.thread_pool is None:
        raise RuntimeError("scheduler: thread pool is not initialized")
    await loop.run_in_executor(_state.thread_pool, _run_process_pending_orders_sync)


def _run_order_status_sync_sync() -> None:
    """Sync local orders/trades statuses from IB as a scheduler task."""
    _ensure_core_path()
    os.chdir(_PROJECT_ROOT)
    from scripts.core.sqlite_manager import SQLiteManager
    from scripts.core.order_status_sync import sync_orders_with_ib

    db_file = _state.db_manager.db_file if _state.db_manager else None
    db = SQLiteManager(db_file)

    order_mode = str(db.get_config_value("ORDER_MODE") or "paper").lower()
    port = 7496 if order_mode == "live" else 7497
    client_id = 14

    result = sync_orders_with_ib(
        db,
        host="127.0.0.1",
        port=int(port),
        client_id=int(client_id),
        source="scheduler",
    )
    if not bool(result.get("ok", False)):
        errs = result.get("errors", [])
        raise RuntimeError("; ".join(str(e) for e in errs) or "order status sync failed")


async def _scheduled_order_status_sync_task() -> None:
    """Run IB order status synchronization in a thread pool (non-blocking)."""
    loop = asyncio.get_running_loop()
    if _state.thread_pool is None:
        raise RuntimeError("scheduler: thread pool is not initialized")
    await loop.run_in_executor(_state.thread_pool, _run_order_status_sync_sync)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def start_scheduler(db_manager) -> None:
    """Start all scheduler tasks. Call from FastAPI lifespan."""
    # Reset any previous state
    _state.reset()
    
    _state.db_manager = db_manager
    _state.running = True

    max_workers = _cfg_int("SCHEDULER_MAX_WORKERS", 4)
    if max_workers < 1:
        max_workers = 4
    _state.thread_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="scheduler-robot",
    )
    logger.info(f"scheduler: thread pool initialized with max_workers={max_workers}")

    max_retries = _cfg_int("SCHEDULER_MAX_RETRIES", 2)
    forecast_interval = _cfg_int("FORECAST_INTERVAL_MINUTES", 240) * 60
    evaluate_interval = _cfg_int("EVALUATE_INTERVAL_MINUTES", 120) * 60
    price_data_interval = _cfg_int("PRICE_DATA_INTERVAL_MINUTES", 60) * 60
    intraday_interval = _cfg_int("INTRADAY_UPDATE_INTERVAL_MINUTES", 60) * 60
    pending_orders_interval = _cfg_int("PENDING_ORDERS_INTERVAL_MINUTES", 1) * 60
    order_status_sync_interval = _cfg_int("ORDER_STATUS_SYNC_INTERVAL_SECONDS", 60)

    task_specs = [
        ("heartbeat",              _heartbeat_task,                    30,                     True),
        ("order_timeout_check",    _order_timeout_task,                15,                     False),
        ("expire_queued_orders",   _expire_queued_orders_task,         300,                    False),
        ("process_pending_orders", _scheduled_pending_orders_task,     pending_orders_interval, False),
        ("sync_order_statuses",    _scheduled_order_status_sync_task,   order_status_sync_interval, False),
        ("update_price_data",      _scheduled_price_data_task,         price_data_interval,    False),
        ("update_intraday",        _scheduled_intraday_task,           intraday_interval,      False),
        ("scheduled_forecast",     _scheduled_forecast_task,           forecast_interval,      False),
        ("scheduled_evaluate",     _scheduled_evaluate_task,           evaluate_interval,      False),
        ("consensus_evaluate",     _scheduled_consensus_evaluate_task, evaluate_interval,      False),
    ]

    for name, factory, interval, run_on_start in task_specs:
        _upsert_task(name, {
            "schedule_type":  "interval",
            "schedule_value": str(interval),
            "is_active":      1,
            "max_duration_sec": interval * 2,
        })

        task = asyncio.create_task(
            _run_task_loop(name, factory, interval, max_retries, run_on_start),
            name=name,
        )
        _state.tasks[name] = task
        logger.info(f"scheduler: registered task '{name}' every {interval}s")

    logger.info(f"scheduler: started with {len(_state.tasks)} tasks")


async def stop_scheduler() -> None:
    """Cancel all scheduler tasks. Call from FastAPI lifespan shutdown."""
    _state.running = False
    for name, task in _state.tasks.items():
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info(f"scheduler: task '{name}' stopped")
    _state.tasks.clear()

    if _state.thread_pool is not None:
        _state.thread_pool.shutdown(wait=False, cancel_futures=True)
        _state.thread_pool = None

    logger.info("scheduler: all tasks stopped")


def get_task_status() -> Dict[str, Any]:
    """Return current status of all registered tasks."""
    result = {}
    for name, task in _state.tasks.items():
        result[name] = {
            "running": not task.done(),
            "cancelled": task.cancelled(),
            "exception": str(task.exception()) if task.done() and not task.cancelled() and task.exception() else None,
        }
    return result
