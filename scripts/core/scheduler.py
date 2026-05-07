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
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from typing import Callable, Dict, Optional, Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_tasks: Dict[str, asyncio.Task] = {}
_db_manager = None
_running = False
_task_running: Dict[str, bool] = {}  # overlap guard per task name


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _cfg(key: str, default: str = "") -> str:
    if _db_manager is None:
        return default
    try:
        v = _db_manager.get_config_value(key)
        return v if v is not None else default
    except Exception:
        return default


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(_cfg(key, str(default)))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _upsert_task(name: str, updates: dict) -> None:
    if _db_manager is None:
        return
    try:
        updates["name"] = name
        cols = list(updates.keys())
        ph   = ", ".join(["?"] * len(cols))
        set_parts = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "name")
        sql = (
            f"INSERT INTO scheduled_tasks ({', '.join(cols)}) VALUES ({ph}) "
            f"ON CONFLICT(name) DO UPDATE SET {set_parts}"
        )
        with sqlite3.connect(_db_manager.db_file) as con:
            con.execute(sql, list(updates.values()))
    except Exception as e:
        logger.warning(f"scheduler: _upsert_task {name} failed: {e}")


def _increment_counters(name: str, success: bool, error_msg: str = "") -> None:
    status = "ok" if success else "error"
    try:
        with sqlite3.connect(_db_manager.db_file) as con:
            con.execute(
                """
                UPDATE scheduled_tasks
                SET last_run_at=?, last_run_status=?, last_error=?,
                    run_count = run_count + 1,
                    error_count = error_count + (CASE WHEN ? = 'error' THEN 1 ELSE 0 END)
                WHERE name=?
                """,
                (_now_utc(), status, error_msg, status, name),
            )
    except Exception as e:
        logger.warning(f"scheduler: _increment_counters {name} failed: {e}")


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
    _task_running[name] = False

    # Calculate initial sleep: if not run_on_start, check last_run_at from DB
    # so that after restart we don't wait a full interval if the task is overdue.
    if run_on_start:
        initial_sleep = 0.0
    else:
        initial_sleep = interval_seconds
        if _db_manager:
            try:
                with sqlite3.connect(_db_manager.db_file) as con:
                    row = con.execute(
                        "SELECT last_run_at FROM scheduled_tasks WHERE name=?", (name,)
                    ).fetchone()
                if row and row[0]:
                    from datetime import datetime, timezone
                    last_run_str = row[0]
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
            except Exception:
                pass

    if initial_sleep > 0:
        await asyncio.sleep(initial_sleep)

    while _running:
        if _task_running.get(name):
            logger.warning(f"scheduler: '{name}' previous run still in progress, skipping interval")
            await asyncio.sleep(interval_seconds)
            continue
        _task_running[name] = True
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
            _task_running[name] = False

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
    try:
        if _db_manager:
            with sqlite3.connect(_db_manager.db_file) as con:
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
    try:
        if _db_manager:
            with sqlite3.connect(_db_manager.db_file) as con:
                row = con.execute("SELECT COUNT(*) FROM accounts").fetchone()
            ib_ok = 1 if row and row[0] > 0 else 0
    except Exception as e:
        notes.append(f"ib_err:{e}")

    # Write to heartbeat_log
    try:
        if _db_manager:
            with sqlite3.connect(_db_manager.db_file) as con:
                con.execute(
                    "INSERT INTO heartbeat_log(checked_at, ib_ok, openrouter_ok, sqlite_ok, notes) VALUES (?,?,?,?,?)",
                    (_now_utc(), ib_ok, or_ok, db_ok, "; ".join(notes)),
                )
    except Exception as e:
        logger.warning(f"heartbeat: write failed: {e}")

    logger.debug(f"heartbeat: ib={ib_ok} openrouter={or_ok} sqlite={db_ok}")


async def _order_timeout_task() -> None:
    """Check for bracket groups with missing children after fill."""
    try:
        from order_manager import check_child_timeouts
        check_child_timeouts(_db_manager)
    except Exception as e:
        logger.error(f"scheduler: order_timeout_task error: {e}")


# Thread pool for CPU-bound / blocking robot tasks
_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="scheduler-robot")

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
    
    # Create db_manager and run_id here for proper tracking
    db = SQLiteManager(_db_manager.db_file if _db_manager else None)
    active_tickers = db.get_settings()
    run_id = db.create_forecast_run('scheduler', len(active_tickers))
    
    try:
        run_trading_bot(db_manager=db, run_id=run_id)
    except Exception as e:
        # Mark run as failed
        if run_id:
            db.complete_forecast_run(run_id, status='failed', error_message=str(e))
        raise


def _run_evaluate_sync() -> None:
    """Blocking call to evaluate_past_forecasts — executed in thread pool."""
    _ensure_core_path()
    os.chdir(_PROJECT_ROOT)
    from scripts.core.sqlite_manager import SQLiteManager
    from scripts.core.forecast_runner import evaluate_past_forecasts
    db = SQLiteManager(_db_manager.db_file)
    evaluate_past_forecasts(db)


async def _scheduled_forecast_task() -> None:
    """Run forecast generation in a thread pool (non-blocking)."""
    loop = asyncio.get_event_loop()
    logger.info("scheduler: starting scheduled forecast run")
    await loop.run_in_executor(_thread_pool, _run_forecast_sync)
    logger.info("scheduler: scheduled forecast run complete")


async def _scheduled_evaluate_task() -> None:
    """Run evaluation of past forecasts in a thread pool (non-blocking)."""
    loop = asyncio.get_event_loop()
    logger.info("scheduler: starting scheduled evaluate run")
    await loop.run_in_executor(_thread_pool, _run_evaluate_sync)
    logger.info("scheduler: scheduled evaluate run complete")


def _run_consensus_evaluate_sync() -> None:
    """Blocking call to evaluate_consensus_records — executed in thread pool."""
    _ensure_core_path()
    os.chdir(_PROJECT_ROOT)
    from scripts.core.sqlite_manager import SQLiteManager
    from scripts.core.consensus_evaluator import evaluate_consensus_records
    db = SQLiteManager(_db_manager.db_file)
    count = evaluate_consensus_records(db)
    logger.info(f"scheduler: consensus_evaluate completed, {count} records evaluated")


async def _scheduled_consensus_evaluate_task() -> None:
    """Run evaluation of consensus records in a thread pool (non-blocking)."""
    loop = asyncio.get_event_loop()
    logger.info("scheduler: starting scheduled consensus evaluate run")
    await loop.run_in_executor(_thread_pool, _run_consensus_evaluate_sync)
    logger.info("scheduler: scheduled consensus evaluate run complete")


def _run_price_data_update_sync() -> None:
    """Fetch and save fresh price data for all active tickers."""
    _ensure_core_path()
    os.chdir(_PROJECT_ROOT)
    from scripts.core.sqlite_manager import SQLiteManager
    from scripts.core.data_loader import fetch_price_data
    db = SQLiteManager(_db_manager.db_file)
    tickers = db.get_active_tickers() if hasattr(db, "get_active_tickers") else []
    if not tickers:
        # fallback: read from settings table directly
        import sqlite3 as _sq
        with _sq.connect(_db_manager.db_file) as con:
            tickers = [r[0] for r in con.execute("SELECT ticker FROM settings WHERE active=1").fetchall()]
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
    loop = asyncio.get_event_loop()
    logger.info("scheduler: starting price_data update")
    await loop.run_in_executor(_thread_pool, _run_price_data_update_sync)
    logger.info("scheduler: price_data update complete")


async def _expire_queued_orders_task() -> None:
    """Expire QUEUED orders older than ORDER_QUEUE_MAX_AGE_HOURS."""
    max_age_hours = _cfg_int("ORDER_QUEUE_MAX_AGE_HOURS", 24)
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    try:
        if _db_manager:
            with sqlite3.connect(_db_manager.db_file) as con:
                cur = con.execute(
                    "UPDATE orders SET status='EXPIRED' WHERE status='QUEUED' AND created_at < ?",
                    (cutoff,)
                )
                if cur.rowcount:
                    logger.info(f"scheduler: expired {cur.rowcount} QUEUED orders older than {max_age_hours}h")
    except Exception as e:
        logger.error(f"scheduler: expire_queued_orders error: {e}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def start_scheduler(db_manager) -> None:
    """Start all scheduler tasks. Call from FastAPI lifespan."""
    global _db_manager, _running
    _db_manager = db_manager
    _running = True

    max_retries = _cfg_int("SCHEDULER_MAX_RETRIES", 2)
    forecast_interval = _cfg_int("FORECAST_INTERVAL_MINUTES", 60) * 60
    evaluate_interval = _cfg_int("EVALUATE_INTERVAL_MINUTES", 120) * 60
    price_data_interval = _cfg_int("PRICE_DATA_INTERVAL_MINUTES", 60) * 60

    task_specs = [
        ("heartbeat",              _heartbeat_task,                    30,                   True),
        ("order_timeout_check",    _order_timeout_task,                15,                   False),
        ("expire_queued_orders",   _expire_queued_orders_task,         300,                  False),
        ("update_price_data",      _scheduled_price_data_task,         price_data_interval,  False),
        ("scheduled_forecast",     _scheduled_forecast_task,           forecast_interval,    False),
        ("scheduled_evaluate",     _scheduled_evaluate_task,           evaluate_interval,    False),
        ("consensus_evaluate",     _scheduled_consensus_evaluate_task, evaluate_interval,    False),
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
        _tasks[name] = task
        logger.info(f"scheduler: registered task '{name}' every {interval}s")

    logger.info(f"scheduler: started with {len(_tasks)} tasks")


async def stop_scheduler() -> None:
    """Cancel all scheduler tasks. Call from FastAPI lifespan shutdown."""
    global _running
    _running = False
    for name, task in _tasks.items():
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info(f"scheduler: task '{name}' stopped")
    _tasks.clear()
    logger.info("scheduler: all tasks stopped")


def get_task_status() -> Dict[str, Any]:
    """Return current status of all registered tasks."""
    result = {}
    for name, task in _tasks.items():
        result[name] = {
            "running": not task.done(),
            "cancelled": task.cancelled(),
            "exception": str(task.exception()) if task.done() and not task.cancelled() and task.exception() else None,
        }
    return result
