"""FastAPI endpoints for Forecast Trading Robot server."""

import os
import sys
import logging
from datetime import datetime
from typing import List, Optional
from contextlib import asynccontextmanager

import pandas as pd

from fastapi import FastAPI, HTTPException, Query, Security, Depends, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader

_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_SERVER_DIR)
_PROJECT_ROOT = os.path.dirname(_SCRIPTS_DIR)
for _p in [_PROJECT_ROOT, _SCRIPTS_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.shared.models import (
    ForecastLog, TickerSetting, ProviderSetting, IBConfigRecord, IBConfigResponse,
    LogsResponse, TickersResponse, ProvidersResponse,
    TickerUpdate, TickerCreate, ProviderUpdate,
    RunResponse, HealthResponse,
    ConfigParam, ConfigResponse,
    PromptRecord, PromptsResponse,
    PriceRecord, PriceDataResponse,
    IndicatorRecord, IndicatorsResponse,
    ConsensusRecord, ConsensusResponse,
    PositionRecord, PortfolioResponse,
    AccountRecord, AccountsResponse,
    SystemLogResponse, OrderSubmitRequest, OrderSubmitResponse,
    ForecastRunLink, ForecastRunRecord, ForecastRunsResponse, ForecastRunDetailResponse,
)
from scripts.core.config import CONFIDENCE_THRESHOLD
from scripts.server.config import ServerConfig
from scripts.server.robot import RobotRunner

logger = logging.getLogger(__name__)

_config: ServerConfig = None
_runner: RobotRunner = None

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_config() -> ServerConfig:
    return _config


def verify_api_key(api_key: str = Security(API_KEY_HEADER)):
    if not _config:
        raise HTTPException(status_code=503, detail="Server not initialized")
    if not api_key or api_key != _config.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    return api_key


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _runner
    _config = ServerConfig()
    _runner = RobotRunner(_config.db_file)
    logger.info(f"Server started on {_config.host}:{_config.port}")
    logger.info(f"DB file: {_config.db_file}")
    logger.info(f"API Key: {_config.api_key}")

    # Mark any runs stuck as 'running' from a previous server session as failed
    try:
        import sqlite3 as _sqlite3
        with _sqlite3.connect(_config.db_file) as _con:
            _rows = _con.execute(
                "UPDATE forecast_runs SET status='failed', completed_at=started_at "
                "WHERE status='running' AND completed_at IS NULL"
            ).rowcount
        if _rows:
            logger.info(f"startup: marked {_rows} stuck forecast_run(s) as failed")
    except Exception as e:
        logger.warning(f"startup: cleanup stuck runs failed: {e}")

    # Start background task scheduler
    try:
        _ensure_paths()
        from scripts.core.scheduler import start_scheduler
        db = _get_db_manager()
        await start_scheduler(db)
        logger.info("Scheduler started")
    except Exception as e:
        logger.warning(f"Scheduler startup failed (non-fatal): {e}")

    yield

    # Stop scheduler on shutdown
    try:
        from scripts.core.scheduler import stop_scheduler
        await stop_scheduler()
        logger.info("Scheduler stopped")
    except Exception as e:
        logger.warning(f"Scheduler shutdown error: {e}")

    logger.info("Server shutting down")


app = FastAPI(
    title="Forecast Trading Robot API",
    description="API for forecast trading robot management",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


_CORE_DIR = os.path.join(_PROJECT_ROOT, "scripts", "core")


def _ensure_paths():
    for _p in [_PROJECT_ROOT, _CORE_DIR]:
        if _p not in sys.path:
            sys.path.insert(0, _p)


def _get_db_manager():
    """Get SQLiteManager pointing at the configured database."""
    _ensure_paths()
    from scripts.core.sqlite_manager import SQLiteManager
    return SQLiteManager(_config.db_file)


def _get_data_manager():
    """Legacy alias — all callers use SQLiteManager now."""
    return _get_db_manager()


def _clean_record(record: dict) -> dict:
    """Convert NaN / numpy types to plain Python for JSON serialisation."""
    import math
    out = {}
    for k, v in record.items():
        if v is None:
            out[k] = None
            continue
        try:
            import numpy as np
            if isinstance(v, np.integer):
                out[k] = int(v); continue
            if isinstance(v, np.floating):
                out[k] = None if math.isnan(float(v)) else float(v); continue
            if isinstance(v, np.bool_):
                out[k] = bool(v); continue
        except ImportError:
            pass
        if isinstance(v, float) and math.isnan(v):
            out[k] = None
        else:
            out[k] = v
    return out


def _clean_records(records: list) -> list:
    """Apply _clean_record to a list of dicts."""
    return [_clean_record(r) for r in records]


@app.get("/health", response_model=HealthResponse)
async def health_check():
    db_exists = os.path.exists(_config.db_file) if _config else False
    return HealthResponse(
        status="ok",
        db_file=_config.db_file if _config else None,
        db_exists=db_exists,
        server=f"{_config.host}:{_config.port}" if _config else None,
    )


@app.get("/logs", response_model=LogsResponse, dependencies=[Depends(verify_api_key)])
async def get_logs(
    ticker: Optional[str] = None,
    method: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(500, ge=1, le=5000),
):
    try:
        em = _get_data_manager()
        df = em.read_sheet("Logs")
        if df.empty:
            return LogsResponse(items=[], total=0)

        if ticker:
            df = df[df["ticker"].astype(str).str.upper() == ticker.upper()]
        if method:
            df = df[df["method"].astype(str).str.lower() == method.lower()]
        if status:
            df = df[df["status"].astype(str).str.upper() == status.upper()]
        if date_from:
            df["_fd"] = pd.to_datetime(df["forecast_date"], errors="coerce")
            df = df[df["_fd"] >= pd.to_datetime(date_from)]
            df = df.drop(columns=["_fd"])
        if date_to:
            df["_fd"] = pd.to_datetime(df["forecast_date"], errors="coerce")
            df = df[df["_fd"] <= pd.to_datetime(date_to)]
            df = df.drop(columns=["_fd"])

        df = df.sort_values("created_at", ascending=False).head(limit)
        df = df.where(df.notna(), None)
        records = df.to_dict("records")
        items = [ForecastLog(**_safe_row(r)) for r in records]
        return LogsResponse(items=items, total=len(items))
    except Exception as e:
        logger.exception("Error reading logs")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/logs/{log_id}", response_model=ForecastLog, dependencies=[Depends(verify_api_key)])
async def get_log(log_id: str):
    try:
        em = _get_data_manager()
        df = em.read_sheet("Logs")
        if df.empty:
            raise HTTPException(status_code=404, detail="Not found")
        row = df[df["id"].astype(str) == log_id]
        if row.empty:
            raise HTTPException(status_code=404, detail="Log entry not found")
        row = row.where(row.notna(), None)
        return ForecastLog(**_safe_row(row.iloc[0].to_dict()))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error reading log entry")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tickers", response_model=TickersResponse, dependencies=[Depends(verify_api_key)])
async def get_tickers():
    try:
        em = _get_data_manager()
        df = em.read_sheet("Settings")
        if df.empty:
            return TickersResponse(items=[])
        df = df.where(df.notna(), None)
        items = [TickerSetting(**_safe_row(r)) for r in df.to_dict("records")]
        return TickersResponse(items=items)
    except Exception as e:
        logger.exception("Error reading tickers")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/tickers/{ticker}", dependencies=[Depends(verify_api_key)])
async def delete_ticker(ticker: str):
    try:
        em = _get_db_manager()
        with em._connect() as con:
            con.execute("DELETE FROM settings WHERE ticker = ?", (ticker,))
        return {"deleted": ticker}
    except Exception as e:
        logger.exception("Error deleting ticker")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tickers", response_model=TickerSetting, dependencies=[Depends(verify_api_key)])
async def add_ticker(body: TickerCreate):
    try:
        em = _get_db_manager()
        df = em.read_sheet("Settings")
        if not df.empty and body.ticker in df["ticker"].astype(str).values:
            raise HTTPException(status_code=409, detail="Ticker already exists")
        new_row = {"ticker": body.ticker, "active": body.active, "comment": body.comment or ""}
        em.append_to_sheet("Settings", new_row)
        return TickerSetting(**new_row)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error adding ticker")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/tickers/{ticker}", response_model=TickerSetting, dependencies=[Depends(verify_api_key)])
async def update_ticker(ticker: str, body: TickerUpdate):
    try:
        em = _get_db_manager()
        df = em.read_sheet("Settings")
        if df.empty:
            raise HTTPException(status_code=404, detail="Settings sheet empty")
        mask = df["ticker"].astype(str) == ticker
        if not mask.any():
            raise HTTPException(status_code=404, detail=f"Ticker {ticker} not found")
        em.upsert_row("settings", {
            "ticker":  ticker,
            "active":  body.active,
            "comment": body.comment or "",
        })
        return TickerSetting(ticker=ticker, active=body.active, comment=body.comment)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error updating ticker")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/providers", response_model=ProvidersResponse, dependencies=[Depends(verify_api_key)])
async def get_providers():
    try:
        em = _get_db_manager()
        df = em.read_sheet("Providers")
        if df.empty:
            return ProvidersResponse(items=[])
        df = df.where(df.notna(), None)
        items = [ProviderSetting(**_safe_row(r)) for r in df.to_dict("records")]
        return ProvidersResponse(items=items)
    except Exception as e:
        logger.exception("Error reading providers")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/providers/{name}", response_model=ProviderSetting, dependencies=[Depends(verify_api_key)])
async def update_provider(name: str, body: ProviderUpdate):
    try:
        em = _get_db_manager()
        # Build upsert payload — merge existing row with updates
        with em._connect() as con:
            cur = con.execute("SELECT * FROM providers WHERE name = ?", (name,))
            row = cur.fetchone()
        if row:
            existing = dict(row)
        else:
            existing = {"name": name, "type": "ai",
                        "base_url": "https://openrouter.ai/api/v1",
                        "api_key": "", "model": "",
                        "temperature": 0.2, "max_tokens": 2000,
                        "rate_limit": 60, "active": 1}
        if body.api_key is not None:   existing["api_key"]     = body.api_key
        if body.model is not None:     existing["model"]       = body.model
        if body.temperature is not None: existing["temperature"] = body.temperature
        if body.max_tokens is not None: existing["max_tokens"]  = body.max_tokens
        if body.rate_limit is not None: existing["rate_limit"]  = body.rate_limit
        if body.active is not None:    existing["active"]      = body.active
        em.upsert_row("providers", existing)
        return ProviderSetting(**_safe_row(existing))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error updating provider")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/run/forecast", response_model=RunResponse, dependencies=[Depends(verify_api_key)])
async def run_forecast():
    if not _runner.start("forecast"):
        raise HTTPException(status_code=409, detail="Robot is already running")
    return RunResponse(status="running", message="Forecast started", started_at=_runner.started_at)


@app.post("/run/evaluate", response_model=RunResponse, dependencies=[Depends(verify_api_key)])
async def run_evaluate():
    if not _runner.start("evaluate"):
        raise HTTPException(status_code=409, detail="Robot is already running")
    return RunResponse(status="running", message="Evaluation started", started_at=_runner.started_at)


@app.post("/run/full", response_model=RunResponse, dependencies=[Depends(verify_api_key)])
async def run_full():
    if not _runner.start("full"):
        raise HTTPException(status_code=409, detail="Robot is already running")
    return RunResponse(status="running", message="Full cycle started", started_at=_runner.started_at)


@app.post("/run/price-data", response_model=RunResponse, dependencies=[Depends(verify_api_key)])
async def run_price_data():
    if not _runner.start("price_data"):
        raise HTTPException(status_code=409, detail="Robot is already running")
    return RunResponse(status="running", message="Price data update started", started_at=_runner.started_at)


@app.get("/run/status", response_model=RunResponse, dependencies=[Depends(verify_api_key)])
async def run_status():
    return RunResponse(
        status=_runner.status,
        message=_runner.message,
        started_at=_runner.started_at,
        finished_at=_runner.finished_at,
        duration_sec=_runner.duration_sec,
        log_lines=_runner.get_log_lines(),
    )


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------

@app.get("/config", response_model=ConfigResponse, dependencies=[Depends(verify_api_key)])
async def get_config_all():
    try:
        em = _get_db_manager()
        df = em.read_sheet("Config")
        if df.empty:
            return ConfigResponse(items=[])
        df = df.where(df.notna(), None)
        items = [ConfigParam(**_safe_row(r)) for r in df.to_dict("records")]
        return ConfigResponse(items=items)
    except Exception as e:
        logger.exception("Error reading config")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/config/{key}", response_model=ConfigParam, dependencies=[Depends(verify_api_key)])
async def update_config(key: str, body: ConfigParam):
    try:
        em = _get_db_manager()
        value = body.value or ""
        _validate_config_value(key, value)
        em.set_config_value(key, value)
        logger.info(f"[API] Config updated: {key} = {value!r}")
        return ConfigParam(key=key, value=value, description=body.description)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error updating config")
        raise HTTPException(status_code=500, detail=str(e))


def _validate_config_value(key: str, value: str) -> None:
    """Raise HTTPException(400) if the value is out of acceptable range."""
    _FLOAT_RANGES = {
        # key: (min_inclusive, max_inclusive)
        "DEFAULT_RISK_PCT":          (0.0001, 0.5),
        "MAX_POSITION_PCT":          (0.001,  1.0),
        "MAX_SECTOR_EXPOSURE_PCT":   (0.01,   1.0),
        "MAX_SECTOR_HARD_LIMIT_PCT": (0.01,   1.0),
        "SECTOR_OVERWEIGHT_FACTOR":  (0.01,   1.0),
        "RISK_PERCENT_ON_STOP":      (0.1,   10.0),  # percent, not fraction
    }
    _BOOL_KEYS = {"LIVE_TRADING_CONFIRMED", "USE_STOP_LIMIT", "ALLOW_EXTENDED_HOURS",
                  "AUTO_BLOCK_ON_ROLLBACK_FAIL", "OPENROUTER_FREE_ONLY"}
    _CHOICE_KEYS = {
        "RISK_MODE":           {"percent_of_capital", "percent_of_portfolio_on_stop"},
        "IB_CAPITAL_FAILSAFE": {"manual_only", "deny"},
        "ORDER_MODE":          {"disabled", "paper", "live"},
        "PREFERRED_ACCOUNT_TYPE": {"live", "paper"},
    }

    if key in _FLOAT_RANGES:
        lo, hi = _FLOAT_RANGES[key]
        try:
            v = float(value)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail=f"{key}: expected a number, got {value!r}")
        if not (lo <= v <= hi):
            raise HTTPException(status_code=400, detail=f"{key}: value {v} out of range [{lo}, {hi}]")

    elif key in _BOOL_KEYS:
        if value.lower() not in ("true", "false", ""):
            raise HTTPException(status_code=400, detail=f"{key}: expected 'true' or 'false', got {value!r}")

    elif key in _CHOICE_KEYS:
        choices = _CHOICE_KEYS[key]
        if value and value not in choices:
            raise HTTPException(status_code=400, detail=f"{key}: must be one of {sorted(choices)}, got {value!r}")

    elif key == "MANUAL_CAPITAL_OVERRIDE":
        if value.strip():
            try:
                v = float(value)
                if v <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail=f"{key}: must be a positive number or empty")

    elif key == "RISK_ACCOUNT_ID":
        # No format constraint — just disallow whitespace-only
        if value != value.strip():
            raise HTTPException(status_code=400, detail=f"{key}: must not have leading/trailing whitespace")



# ---------------------------------------------------------------------------
# Prompts endpoint
# ---------------------------------------------------------------------------

@app.get("/prompts", response_model=PromptsResponse, dependencies=[Depends(verify_api_key)])
async def get_prompts(
    ticker: Optional[str] = None,
    method: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(200, le=1000),
):
    try:
        em = _get_db_manager()
        df = em.get_prompts(ticker=ticker, method=method, date_from=date_from, date_to=date_to, limit=limit)
        if df.empty:
            return PromptsResponse(items=[], total=0)
        df = df.where(df.notna(), None)
        items = [PromptRecord(**_safe_row(r)) for r in df.to_dict("records")]
        return PromptsResponse(items=items, total=len(items))
    except Exception as e:
        logger.exception("Error reading prompts")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Price data endpoint
# ---------------------------------------------------------------------------

@app.get("/price-data", response_model=PriceDataResponse, dependencies=[Depends(verify_api_key)])
async def get_price_data(
    ticker: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(500, le=5000),
):
    try:
        em = _get_db_manager()
        df = em.get_price_data(ticker=ticker, date_from=date_from, date_to=date_to, limit=limit)
        if df.empty:
            return PriceDataResponse(items=[], total=0)
        df = df.where(df.notna(), None)
        items = [PriceRecord(**_safe_row(r)) for r in df.to_dict("records")]
        return PriceDataResponse(items=items, total=len(items))
    except Exception as e:
        logger.exception("Error reading price data")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Indicators endpoint
# ---------------------------------------------------------------------------

@app.get("/indicators", response_model=IndicatorsResponse, dependencies=[Depends(verify_api_key)])
async def get_indicators(
    ticker: Optional[str] = None,
    limit: int = Query(200, le=2000),
    date_from: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
):
    try:
        em = _get_db_manager()
        df = em.get_indicators(ticker=ticker, limit=limit, date_from=date_from, date_to=date_to)
        if df.empty:
            return IndicatorsResponse(items=[], total=0)
        df = df.where(df.notna(), None)
        items = [IndicatorRecord(**_safe_row(r)) for r in df.to_dict("records")]
        return IndicatorsResponse(items=items, total=len(items))
    except Exception as e:
        logger.exception("Error reading indicators")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Consensus evaluation & recalculation
# ---------------------------------------------------------------------------

@app.post("/consensus/evaluate", dependencies=[Depends(verify_api_key)])
async def evaluate_consensus():
    """Manually trigger evaluation of pending consensus records.

    Returns detailed result with counts of processed records.
    This is a synchronous call - waits for evaluation to complete.
    """
    logger.info("consensus/evaluate: starting manual evaluation request")
    try:
        import sys, os
        core_dir = os.path.join(_PROJECT_ROOT, "scripts", "core")
        for p in [_PROJECT_ROOT, core_dir]:
            if p not in sys.path:
                sys.path.insert(0, p)
        from scripts.core.consensus_evaluator import evaluate_consensus_records
        em = _get_db_manager()

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Check pending records - ready vs not ready
        with em._connect() as con:
            # Ready for evaluation (target date passed)
            cur = con.execute(
                "SELECT COUNT(*) FROM consensus WHERE eval_status = 'PENDING' AND eval_target_date IS NOT NULL AND eval_target_date <= ?",
                (now_str,)
            )
            ready_before = cur.fetchone()[0]
            # Pending but not ready yet (target date in future)
            cur = con.execute(
                "SELECT COUNT(*) FROM consensus WHERE eval_status = 'PENDING' AND eval_target_date IS NOT NULL AND eval_target_date > ?",
                (now_str,)
            )
            not_ready = cur.fetchone()[0]
            # Pending without target date
            cur = con.execute(
                "SELECT COUNT(*) FROM consensus WHERE eval_status = 'PENDING' AND (eval_target_date IS NULL OR eval_target_date = '')"
            )
            no_target = cur.fetchone()[0]

        logger.info(f"consensus/evaluate: ready={ready_before}, not_ready={not_ready}, no_target={no_target}")

        count = evaluate_consensus_records(em)

        # Check results after evaluation
        with em._connect() as con:
            cur = con.execute(
                "SELECT COUNT(*) FROM consensus WHERE eval_status = 'PENDING' AND eval_target_date IS NOT NULL AND eval_target_date <= ?",
                (now_str,)
            )
            ready_after = cur.fetchone()[0]
            cur = con.execute(
                "SELECT COUNT(*) FROM consensus WHERE eval_status = 'EVALUATED'"
            )
            total_evaluated = cur.fetchone()[0]

        logger.info(
            f"consensus/evaluate: completed. processed={count}, "
            f"ready_before={ready_before}, ready_after={ready_after}, "
            f"total_evaluated={total_evaluated}"
        )

        return {
            "status": "completed",
            "message": f"Evaluated {count} consensus records",
            "processed": count,
            "ready_before": ready_before,
            "ready_after": ready_after,
            "not_ready": not_ready,
            "no_target": no_target,
            "total_evaluated": total_evaluated,
        }
    except Exception as e:
        logger.exception(f"consensus/evaluate error: {e}")
        raise HTTPException(status_code=500, detail=f"Evaluation failed: {str(e)}")


# ---------------------------------------------------------------------------
# Consensus recalculate endpoint
# ---------------------------------------------------------------------------

@app.post("/consensus/recalculate", dependencies=[Depends(verify_api_key)])
async def recalculate_consensus(
    date_from: str = Query(None, description="Start date YYYY-MM-DD"),
    date_to: str = Query(None, description="End date YYYY-MM-DD"),
    force: bool = Query(False, description="If true, overwrite EVALUATED records and reset eval fields"),
):
    """Recalculate consensus records from historical forecast logs.

    Groups forecasts by (created_date, ticker), calculates consensus for each group,
    and creates/updates consensus records.
    When force=True, overwrites all records including EVALUATED and resets eval fields.
    """
    logger.info(f"consensus/recalculate: starting date_from={date_from}, date_to={date_to}, force={force}")
    try:
        import sys, os
        core_dir = os.path.join(_PROJECT_ROOT, "scripts", "core")
        for p in [_PROJECT_ROOT, core_dir]:
            if p not in sys.path:
                sys.path.insert(0, p)
        from scripts.core.consensus_recalc import recalculate_consensus
        em = _get_db_manager()

        stats = recalculate_consensus(em, date_from=date_from, date_to=date_to, force=force)

        logger.info(
            f"consensus/recalculate: completed. "
            f"created={stats['created']}, updated={stats['updated']}, "
            f"skipped={stats['skipped']}, errors={stats['errors']}"
        )

        return {
            "status": "completed",
            "message": f"Recalculated {stats['total_groups']} consensus groups",
            "created": stats["created"],
            "updated": stats["updated"],
            "skipped": stats["skipped"],
            "evaluated": stats.get("evaluated", 0),
            "errors": stats["errors"],
            "total_groups": stats["total_groups"],
        }
    except Exception as e:
        logger.exception(f"consensus/recalculate error: {e}")
        raise HTTPException(status_code=500, detail=f"Recalculation failed: {str(e)}")


# ---------------------------------------------------------------------------
# System log endpoint
# ---------------------------------------------------------------------------

@app.get("/system-log", response_model=SystemLogResponse, dependencies=[Depends(verify_api_key)])
async def get_system_log(
    lines: int = Query(200, le=2000),
    level: Optional[str] = None,
):
    try:
        log_file = os.path.join(_PROJECT_ROOT, "trading_robot.log")
        if not os.path.exists(log_file):
            return SystemLogResponse(lines=[], total=0)
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        all_lines = [l.rstrip() for l in all_lines]
        if level:
            lvl = level.upper()
            all_lines = [l for l in all_lines if lvl in l]
        result = all_lines[-lines:]
        return SystemLogResponse(lines=result, total=len(result))
    except Exception as e:
        logger.exception("Error reading system log")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Provider management: add / delete
# ---------------------------------------------------------------------------

@app.post("/providers", response_model=ProviderSetting, dependencies=[Depends(verify_api_key)])
async def add_provider(body: ProviderSetting):
    try:
        em = _get_db_manager()
        name = body.get_name()
        if not name:
            raise HTTPException(status_code=422, detail="Provider name is required")
        em.upsert_row("providers", {
            "name":        name,
            "type":        "ai",
            "base_url":    "https://openrouter.ai/api/v1",
            "api_key":     body.get_api_key(),
            "model":       body.model or "",
            "temperature": body.temperature or 0.2,
            "max_tokens":  body.max_tokens or 2000,
            "rate_limit":  body.rate_limit or 60,
            "active":      body.active if body.active is not None else 1,
        })
        return body
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error adding provider")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/providers/{name}", dependencies=[Depends(verify_api_key)])
async def delete_provider(name: str):
    try:
        em = _get_db_manager()
        with em._connect() as con:
            con.execute("DELETE FROM providers WHERE name = ?", (name,))
        return {"deleted": name}
    except Exception as e:
        logger.exception("Error deleting provider")
        raise HTTPException(status_code=500, detail=str(e))


def _safe_row(row: dict) -> dict:
    """Convert NaN / numpy types to plain Python for Pydantic."""
    import math
    result = {}
    for k, v in row.items():
        if v is None:
            result[k] = None
        elif isinstance(v, float) and math.isnan(v):
            result[k] = None
        else:
            try:
                import numpy as np
                if isinstance(v, (np.integer,)):
                    result[k] = int(v)
                elif isinstance(v, (np.floating,)):
                    result[k] = float(v)
                elif isinstance(v, (np.bool_,)):
                    result[k] = bool(v)
                else:
                    result[k] = v
            except ImportError:
                result[k] = v
    return result


# ---------------------------------------------------------------------------
# Prompt templates endpoints
# ---------------------------------------------------------------------------

@app.get("/prompt-templates", dependencies=[Depends(verify_api_key)])
async def get_prompt_templates():
    try:
        em = _get_db_manager()
        templates = em.get_all_prompt_templates()
        return {"templates": templates}
    except Exception as e:
        logger.exception("Error reading prompt templates")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/prompt-templates/{method}", dependencies=[Depends(verify_api_key)])
async def save_prompt_template(method: str, body: dict = Body(...)):
    try:
        em = _get_db_manager()
        text = body.get("prompt_text", "")
        ok = em.save_prompt_template(method, text)
        if not ok:
            raise HTTPException(status_code=500, detail="Failed to save template")
        return {"saved": method}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error saving prompt template")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/prompt-templates/{method}/reset", dependencies=[Depends(verify_api_key)])
async def reset_prompt_template(method: str):
    try:
        em = _get_db_manager()
        ok = em.reset_prompt_template(method)
        if not ok:
            raise HTTPException(status_code=404, detail=f"No default for method '{method}'")
        return {"reset": method}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error resetting prompt template")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Model catalog endpoints
# ---------------------------------------------------------------------------

@app.get("/model-catalog", dependencies=[Depends(verify_api_key)])
async def get_model_catalog(provider: str = Query(None)):
    try:
        em = _get_db_manager()
        df = em.get_model_catalog(provider=provider)
        if df.empty:
            return {"items": [], "total": 0}
        df = df.where(df.notna(), None)
        return {"items": df.to_dict("records"), "total": len(df)}
    except Exception as e:
        logger.exception("Error reading model catalog")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/model-catalog/refresh", dependencies=[Depends(verify_api_key)])
async def refresh_model_catalog():
    try:
        em = _get_db_manager()
        api_key = em.get_config_value("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            raise HTTPException(status_code=400, detail="OPENROUTER_API_KEY not configured")
        count = em.refresh_model_catalog(api_key)
        if count < 0:
            raise HTTPException(status_code=502, detail="Failed to fetch models from OpenRouter")
        return {"refreshed": count}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error refreshing model catalog")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Accounts endpoints
# ---------------------------------------------------------------------------

@app.get("/accounts", response_model=AccountsResponse, dependencies=[Depends(verify_api_key)])
async def get_accounts(broker: str = Query(None)):
    try:
        em = _get_db_manager()
        df = em.read_sheet('Accounts')
        if df.empty:
            return AccountsResponse(items=[], total=0)
        if broker:
            df = df[df.get('broker', '') == broker]
        df = df.where(df.notna(), None)
        items = [AccountRecord(**row) for row in df.to_dict('records')]
        return AccountsResponse(items=items, total=len(items))
    except Exception as e:
        logger.exception("Error reading accounts")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/accounts/sync", dependencies=[Depends(verify_api_key)])
async def sync_accounts(
    host: str = Query("127.0.0.1"),
    port: int = Query(7497),
    client_id: int = Query(1, ge=0, le=999),
    type: str = Query("paper"),
):
    try:
        from scripts.core.ib_gateway_client import sync_accounts_with_ib_async
        em = _get_db_manager()
        ok = await sync_accounts_with_ib_async(em, host=host, port=port, client_id=client_id, type=type)
        if not ok:
            raise HTTPException(status_code=502, detail="IB Gateway returned no accounts. Ensure TWS/Gateway is running and API is enabled on port " + str(port))
        return {"synced": True, "client_id": client_id, "type": type}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error syncing accounts")
        raise HTTPException(status_code=502, detail=f"Cannot connect to IB Gateway at {host}:{port} — {e}")


# ---------------------------------------------------------------------------
# Portfolio endpoints
# ---------------------------------------------------------------------------

@app.get("/portfolio", response_model=PortfolioResponse, dependencies=[Depends(verify_api_key)])
async def get_portfolio(account: str = Query(None)):
    try:
        em = _get_db_manager()
        df = em.read_sheet('Portfolio')
        if df.empty:
            return PortfolioResponse(items=[], total=0)
        if account:
            df = df[df.get('account', '') == account]
        df = df.where(df.notna(), None)
        items = [PositionRecord(**row) for row in df.to_dict('records')]
        return PortfolioResponse(items=items, total=len(items))
    except Exception as e:
        logger.exception("Error reading portfolio")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/portfolio/sync", dependencies=[Depends(verify_api_key)])
async def sync_portfolio(
    host: str = Query("127.0.0.1"),
    port: int = Query(7497),
    client_id: int = Query(1, ge=0, le=999),
    type: str = Query("paper"),
):
    try:
        from scripts.core.ib_gateway_client import sync_portfolio_with_ib_async
        from datetime import datetime, timezone
        em = _get_db_manager()
        ok = await sync_portfolio_with_ib_async(em, host=host, port=port, client_id=client_id, type=type)
        if not ok:
            raise HTTPException(status_code=502, detail="IB Gateway returned no positions. Ensure TWS/Gateway is running and API is enabled on port " + str(port))
        synced_at = datetime.now(tz=timezone.utc).isoformat()
        em.set_config_value("LAST_PORTFOLIO_SYNC_AT", synced_at)
        return {"synced": True, "client_id": client_id, "type": type, "synced_at": synced_at}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error syncing portfolio")
        raise HTTPException(status_code=502, detail=f"Cannot connect to IB Gateway at {host}:{port} — {e}")


@app.get("/ib/test-connection", dependencies=[Depends(verify_api_key)])
async def test_ib_connection_endpoint(
    host: str = Query("127.0.0.1"),
    port: int = Query(7497),
    client_id: int = Query(1, ge=0, le=999),
):
    """Test IB Gateway connection and return detailed logs."""
    from scripts.core.ib_gateway_client import test_ib_connection_async
    result = await test_ib_connection_async(host=host, port=port, client_id=client_id)
    return result


@app.get("/ib/positions/{con_id}/status", dependencies=[Depends(verify_api_key)])
async def get_ib_position_status(
    con_id: int,
    host: str = Query("127.0.0.1"),
    port: int = Query(7497),
    client_id: int = Query(1, ge=0, le=999),
):
    """Fetch live status for a single IB position identified by con_id."""
    try:
        from scripts.core.ib_gateway_client import fetch_ib_position_status_by_con_id
        import asyncio

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: fetch_ib_position_status_by_con_id(
                con_id=int(con_id),
                host=host,
                port=int(port),
                client_id=int(client_id),
            ),
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Error fetching IB position status")
        raise HTTPException(status_code=502, detail=f"Cannot fetch IB position status: {e}")


@app.get("/ib/orders/{ib_order_id}/status", dependencies=[Depends(verify_api_key)])
async def get_ib_order_status(
    ib_order_id: int,
    host: str = Query("127.0.0.1"),
    port: int = Query(7497),
    client_id: int = Query(14, ge=0, le=999),
):
    """Fetch live status for a single IB order identified by ib_order_id."""
    try:
        from scripts.core.ib_gateway_client import fetch_ib_order_status_by_order_id
        import asyncio

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: fetch_ib_order_status_by_order_id(
                order_id=int(ib_order_id),
                host=host,
                port=int(port),
                client_id=int(client_id),
            ),
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Error fetching IB order status")
        raise HTTPException(status_code=502, detail=f"Cannot fetch IB order status: {e}")


# ---------------------------------------------------------------------------
# IB Config endpoints
# ---------------------------------------------------------------------------

@app.get("/ib-config", response_model=IBConfigResponse, dependencies=[Depends(verify_api_key)])
async def get_ib_configs():
    """Get all IB Gateway connection configurations."""
    try:
        em = _get_db_manager()
        df = em.read_sheet('IBConfig')
        if df.empty:
            return IBConfigResponse(items=[], total=0)
        df = df.where(df.notna(), None)
        items = [IBConfigRecord(**_safe_row(r)) for r in df.to_dict('records')]
        return IBConfigResponse(items=items, total=len(items))
    except Exception as e:
        logger.exception("Error reading IB configs")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/ib-config/{config_id}", response_model=IBConfigRecord, dependencies=[Depends(verify_api_key)])
async def get_ib_config(config_id: int):
    """Get specific IB Gateway configuration by ID."""
    try:
        em = _get_db_manager()
        with em._connect() as con:
            cur = con.execute("SELECT * FROM ib_config WHERE id = ?", (config_id,))
            row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="IB config not found")
        return IBConfigRecord(**_safe_row(dict(row)))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error reading IB config")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ib-config", response_model=IBConfigRecord, dependencies=[Depends(verify_api_key)])
async def create_ib_config(body: IBConfigRecord):
    """Create new IB Gateway configuration."""
    try:
        em = _get_db_manager()
        row = {
            "name": body.name,
            "host": body.host,
            "port": body.port,
            "client_id": body.client_id,
            "type": body.type,
            "active": body.active,
        }
        em.upsert_row("ib_config", row)
        return body
    except Exception as e:
        logger.exception("Error creating IB config")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/ib-config/{config_id}", response_model=IBConfigRecord, dependencies=[Depends(verify_api_key)])
async def update_ib_config(config_id: int, body: IBConfigRecord):
    """Update IB Gateway configuration."""
    try:
        em = _get_db_manager()
        with em._connect() as con:
            cur = con.execute("SELECT * FROM ib_config WHERE id = ?", (config_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="IB config not found")
        row = {
            "id": config_id,
            "name": body.name,
            "host": body.host,
            "port": body.port,
            "client_id": body.client_id,
            "type": body.type,
            "active": body.active,
        }
        em.upsert_row("ib_config", row)
        return body
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error updating IB config")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/ib-config/{config_id}", dependencies=[Depends(verify_api_key)])
async def delete_ib_config(config_id: int):
    """Delete IB Gateway configuration."""
    try:
        em = _get_db_manager()
        with em._connect() as con:
            con.execute("DELETE FROM ib_config WHERE id = ?", (config_id,))
        return {"deleted": config_id}
    except Exception as e:
        logger.exception("Error deleting IB config")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# IB Order Types endpoints
# ---------------------------------------------------------------------------

@app.get("/ib-order-types", dependencies=[Depends(verify_api_key)])
async def get_ib_order_types(active_only: bool = Query(False)):
    """Get IB order types. If active_only=True, return only enabled types."""
    try:
        em = _get_db_manager()
        df = em.get_order_types(active_only=active_only)
        if df.empty:
            return {"items": [], "total": 0}
        df = df.where(df.notna(), None)
        return {"items": df.to_dict("records"), "total": len(df)}
    except Exception as e:
        logger.exception("Error reading IB order types")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/ib-order-types/{order_type_code}/active", dependencies=[Depends(verify_api_key)])
async def set_ib_order_type_active(order_type_code: str, body: dict = Body(...)):
    """Enable/disable an order type."""
    try:
        em = _get_db_manager()
        active = body.get("active", True)
        ok = em.set_order_type_active(order_type_code, active)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Order type '{order_type_code}' not found")
        return {"order_type_code": order_type_code, "active": active}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error updating IB order type")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ib-order-types/reset", dependencies=[Depends(verify_api_key)])
async def reset_ib_order_types():
    """Reset order types to defaults."""
    try:
        em = _get_db_manager()
        ok = em.reset_order_types()
        if not ok:
            raise HTTPException(status_code=500, detail="Failed to reset order types")
        return {"reset": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error resetting IB order types")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Capital & consensus endpoints (ред. 2)
# ---------------------------------------------------------------------------

@app.get("/capital", dependencies=[Depends(verify_api_key)])
async def get_capital():
    """Return available NetLiquidation from IB or manual override."""
    try:
        _ensure_paths()
        from scripts.core.capital_provider import get_net_liquidation_async
        em = _get_db_manager()
        net_liq = await get_net_liquidation_async(em)
        return {"net_liquidation": net_liq}
    except Exception as e:
        logger.exception("Error fetching capital")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/consensus", dependencies=[Depends(verify_api_key)])
async def get_consensus(ticker: Optional[str] = None, limit: int = Query(50, ge=1, le=500)):
    """Return recent consensus records, optionally filtered by ticker."""
    try:
        import sqlite3
        em = _get_db_manager()
        with em._connect() as con:
            if ticker:
                rows = con.execute(
                    "SELECT * FROM consensus WHERE UPPER(ticker)=UPPER(?) ORDER BY date DESC LIMIT ?",
                    (ticker, limit)
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM consensus ORDER BY date DESC LIMIT ?",
                    (limit,)
                ).fetchall()
        return {"items": [dict(r) for r in rows], "total": len(rows)}
    except Exception as e:
        logger.exception("Error fetching consensus")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Forecast Run tracking endpoints
# ---------------------------------------------------------------------------

@app.get("/forecast-runs", dependencies=[Depends(verify_api_key)])
async def get_forecast_runs(limit: int = Query(50, ge=1, le=200)):
    """Return recent forecast runs with aggregated statistics."""
    try:
        em = _get_db_manager()
        df = em.get_forecast_runs(limit=limit)
        items = _clean_records(df.to_dict('records')) if not df.empty else []
        return {"items": items, "total": len(items)}
    except Exception as e:
        logger.exception("Error fetching forecast runs")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/forecast-runs/{run_id}", dependencies=[Depends(verify_api_key)])
async def get_forecast_run_detail(run_id: int):
    """Return detailed info for a specific forecast run including all links."""
    try:
        em = _get_db_manager()

        run = em.get_forecast_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail=f"Forecast run {run_id} not found")

        links_df = em.get_forecast_run_links(run_id)
        links = _clean_records(links_df.to_dict('records')) if not links_df.empty else []

        consensus = []
        try:
            with em._connect() as con:
                rows = con.execute(
                    "SELECT * FROM consensus WHERE run_id = ? ORDER BY date DESC",
                    (run_id,)
                ).fetchall()
                consensus = _clean_records([dict(r) for r in rows])
        except Exception:
            pass

        return {
            "run": _clean_record(run) if isinstance(run, dict) else run,
            "links": links,
            "consensus": consensus if consensus else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error fetching forecast run {run_id}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Order management endpoints
# ---------------------------------------------------------------------------

@app.get("/orders", dependencies=[Depends(verify_api_key)])
async def get_orders(
    ticker: Optional[str] = None,
    status: Optional[str] = None,
    include_test: bool = Query(True, description="Include test rows (is_test=1)"),
    test_only: bool = Query(False, description="Return only test rows (is_test=1)"),
    limit: int = Query(100, ge=1, le=1000),
):
    """Return orders from the orders table."""
    try:
        em = _get_db_manager()
        with em._connect() as con:
            clauses = []
            params: list = []
            if ticker:
                clauses.append("UPPER(ticker)=UPPER(?)")
                params.append(ticker)
            if status:
                clauses.append("UPPER(status)=UPPER(?)")
                params.append(status)
            if test_only:
                clauses.append("COALESCE(is_test, 0)=1")
            elif not include_test:
                clauses.append("COALESCE(is_test, 0)=0")
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            rows = con.execute(
                f"SELECT * FROM orders {where} ORDER BY created_at DESC LIMIT ?",
                params
            ).fetchall()
        return {"items": [dict(r) for r in rows], "total": len(rows)}
    except Exception as e:
        logger.exception("Error fetching orders")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/orders/{order_id}/cancel", dependencies=[Depends(verify_api_key)])
async def cancel_order_endpoint(order_id: int):
    """Cancel an IB order by DB id."""
    try:
        _ensure_paths()
        em = _get_db_manager()
        with em._connect() as con:
            row = con.execute(
                "SELECT ib_order_id, account_type FROM orders WHERE id=?", (order_id,)
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Order not found")
        ib_id, mode = row["ib_order_id"], row["account_type"]
        port = 7496 if mode == "live" else 7497
        from scripts.core.ib_gateway_client import cancel_order
        import asyncio
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: cancel_order(ib_id, port=port)
        )
        return {"cancelled": ok, "order_id": order_id, "ib_order_id": ib_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error cancelling order")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/orders/sync", dependencies=[Depends(verify_api_key)])
async def sync_orders_from_ib(
    host: str = Query("127.0.0.1"),
    port: Optional[int] = Query(None),
    client_id: int = Query(14),
):
    """Manually synchronize order statuses from IB into local orders/trades."""
    try:
        _ensure_paths()
        em = _get_db_manager()

        resolved_port = port
        if resolved_port is None:
            order_mode = str(em.get_config_value("ORDER_MODE") or "paper").lower()
            resolved_port = 7496 if order_mode == "live" else 7497

        from scripts.core.order_status_sync import sync_orders_with_ib
        import asyncio

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: sync_orders_with_ib(
                em,
                host=host,
                port=int(resolved_port),
                client_id=int(client_id),
                source="manual",
            ),
        )

        if bool(result.get("ok", False)):
            synced_at = str(result.get("synced_at", "") or "")
            if synced_at:
                em.set_config_value("LAST_ORDERS_SYNC_AT", synced_at)

        return result
    except Exception as e:
        logger.exception("Error syncing orders from IB")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/orders/submit", response_model=OrderSubmitResponse, dependencies=[Depends(verify_api_key)])
async def submit_order_manual(body: OrderSubmitRequest):
    """Manually submit order for ticker based on latest consensus."""
    try:
        _ensure_paths()
        em = _get_db_manager()

        # 1. Get latest consensus
        with em._connect() as con:
            row = con.execute(
                "SELECT * FROM consensus WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                (body.ticker,)
            ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail=f"No consensus found for {body.ticker}")

        consensus = dict(row)
        consensus_id = consensus.get("id")
        signal = consensus.get("signal", "NEUTRAL")
        confidence = consensus.get("confidence", 0.0)

        # 2. Validate signal
        if signal not in ("LONG", "SHORT"):
            return OrderSubmitResponse(
                status="SKIPPED_NEUTRAL",
                order_ids=[],
                message=f"Signal is {signal}, not LONG/SHORT",
                consensus_signal=signal,
                confidence=confidence
            )

        if confidence < CONFIDENCE_THRESHOLD:
            return OrderSubmitResponse(
                status="SKIPPED_LOW_CONFIDENCE",
                order_ids=[],
                message=f"Confidence {confidence:.1f}% < {CONFIDENCE_THRESHOLD}%",
                consensus_signal=signal,
                confidence=confidence
            )

        # 3. Override with request params if provided
        if body.stop_loss is not None:
            consensus["stop_loss"] = body.stop_loss
        if body.target_price is not None:
            consensus["target_price"] = body.target_price
        if body.entry_limit_price is not None:
            consensus["entry_limit_price"] = body.entry_limit_price

        # 4. Check required levels
        if not consensus.get("stop_loss") or not consensus.get("target_price"):
            return OrderSubmitResponse(
                status="SKIPPED_MISSING_LEVELS",
                order_ids=[],
                message="Missing stop_loss or target_price",
                consensus_signal=signal,
                confidence=confidence
            )

        # 5. Get capital and calculate position
        from scripts.core.capital_provider import get_capital
        from scripts.core.position_sizer import calculate_position
        from scripts.core.order_manager import submit_signal

        capital = get_capital(em)
        if capital["status"] != "OK":
            return OrderSubmitResponse(
                status="SKIPPED_NO_CAPITAL",
                order_ids=[],
                message=f"Capital error: {capital['status']}",
                consensus_signal=signal,
                confidence=confidence
            )

        # Get current price from latest price_data
        with em._connect() as con:
            price_row = con.execute(
                "SELECT close FROM price_data WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                (body.ticker,)
            ).fetchone()
        current_price = price_row["close"] if price_row else consensus.get("target_price", 0)

        position = calculate_position(
            ticker=body.ticker,
            entry_price=current_price,
            stop_loss=consensus["stop_loss"],
            db_manager=em,
            net_liquidation=capital["net_liquidation"]
        )

        if position["status"] != "OK" or position["quantity"] <= 0:
            return OrderSubmitResponse(
                status=position.get("status", "INVALID_POSITION"),
                order_ids=[],
                message=f"Position sizing failed: {position.get('status', 'unknown')}",
                consensus_signal=signal,
                confidence=confidence
            )

        # Override quantity if provided
        if body.quantity is not None and body.quantity > 0:
            position["quantity"] = body.quantity

        # 6. Submit order
        log_id = em.get_latest_forecast_log_id(body.ticker)
        result = submit_signal(
            ticker=body.ticker,
            consensus=consensus,
            position_size=position,
            db_manager=em,
            log_id=log_id or "",
            consensus_id=int(consensus_id) if consensus_id is not None else None,
            event_source="submit_manual",
        )

        return OrderSubmitResponse(
            status=result["status"],
            order_ids=result.get("order_ids", []),
            message=result.get("message", ""),
            consensus_signal=signal,
            confidence=confidence
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error submitting order")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Trades endpoints
# ---------------------------------------------------------------------------

@app.get("/trades", dependencies=[Depends(verify_api_key)])
async def get_trades(
    trade_id: Optional[int] = None,
    status: Optional[str] = None,
    ticker: Optional[str] = None,
    include_test: bool = Query(True, description="Include test rows (is_test=1)"),
    test_only: bool = Query(False, description="Return only test rows (is_test=1)"),
    limit: int = 200,
):
    """Return trades list from the trades table."""
    try:
        _ensure_paths()
        em = _get_db_manager()
        where_parts = []
        params = []
        if trade_id is not None:
            where_parts.append("id = ?")
            params.append(int(trade_id))
        if status:
            where_parts.append("status = ?")
            params.append(status)
        if ticker:
            where_parts.append("UPPER(ticker) = UPPER(?)")
            params.append(ticker)
        if test_only:
            where_parts.append("COALESCE(is_test, 0)=1")
        elif not include_test:
            where_parts.append("COALESCE(is_test, 0)=0")
        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        with em._connect() as con:
            rows = con.execute(
                f"SELECT * FROM trades {where_sql} ORDER BY id DESC LIMIT ?",
                params + [limit],
            ).fetchall()
        items = [dict(r) for r in rows]
        return {"items": items, "trades": items, "total": len(items)}
    except Exception as e:
        logger.exception("Error fetching trades")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/ib-log", dependencies=[Depends(verify_api_key)])
async def get_ib_log(ticker: Optional[str] = None, limit: int = 500):
    """Return recent IB Gateway audit log entries."""
    try:
        _ensure_paths()
        em = _get_db_manager()
        where_sql = "WHERE ticker = ?" if ticker else ""
        params = [ticker] if ticker else []
        with em._connect() as con:
            rows = con.execute(
                f"SELECT * FROM ib_gateway_log {where_sql} ORDER BY id DESC LIMIT ?",
                params + [limit],
            ).fetchall()
        return {"log": [dict(r) for r in rows]}
    except Exception as e:
        logger.exception("Error fetching ib_gateway_log")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/ib-transactions", dependencies=[Depends(verify_api_key)])
async def get_ib_transactions(
    ticker: Optional[str] = None,
    ib_order_id: Optional[int] = None,
    ib_parent_id: Optional[int] = None,
    event_source: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = Query(500, ge=1, le=5000),
):
    """Return rows from ib_order_transactions with optional filters."""
    try:
        em = _get_db_manager()
        df = em.get_ib_transactions(
            ticker=ticker,
            ib_order_id=ib_order_id,
            ib_parent_id=ib_parent_id,
            event_source=event_source,
            event_type=event_type,
            limit=limit,
        )
        if df.empty:
            return {"items": [], "total": 0}
        df = df.where(df.notna(), None)
        items = [_clean_record(r) for r in df.to_dict("records")]
        return {"items": items, "total": len(items)}
    except Exception as e:
        logger.exception("Error fetching ib_order_transactions")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/consensus/{consensus_id}/activate", dependencies=[Depends(verify_api_key)])
async def activate_consensus(consensus_id: int):
    """Manually trigger order activation for a specific consensus record."""
    try:
        _ensure_paths()
        em = _get_db_manager()
        from scripts.core.order_manager import activate_consensus_order
        result = activate_consensus_order(consensus_id, em)
        return result
    except Exception as e:
        logger.exception("Error activating consensus order")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/consensus/{consensus_id}/preview-trade", dependencies=[Depends(verify_api_key)])
async def preview_consensus_trade(consensus_id: int):
    """Return trade preview details (including calculated quantity) for confirmation popup."""
    try:
        _ensure_paths()
        em = _get_db_manager()
        from scripts.core.order_manager import preview_consensus_order
        return preview_consensus_order(consensus_id, em)
    except Exception as e:
        logger.exception("Error previewing consensus trade")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Scheduler & circuit breaker status endpoints
# ---------------------------------------------------------------------------

@app.get("/scheduler/status", dependencies=[Depends(verify_api_key)])
async def get_scheduler_status():
    """Return running status of scheduler tasks."""
    try:
        _ensure_paths()
        from scripts.core.scheduler import get_task_status
        return {"tasks": get_task_status()}
    except Exception as e:
        return {"tasks": {}, "error": str(e)}


@app.get("/scheduler/tasks", dependencies=[Depends(verify_api_key)])
async def get_scheduler_tasks():
    """Return all rows from scheduled_tasks table."""
    try:
        em = _get_db_manager()
        with em._connect() as con:
            rows = con.execute(
                "SELECT * FROM scheduled_tasks ORDER BY name"
            ).fetchall()
        return {"items": [dict(r) for r in rows], "total": len(rows)}
    except Exception as e:
        logger.exception("Error fetching scheduler tasks")
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/scheduler/tasks/{name}/active", dependencies=[Depends(verify_api_key)])
async def set_task_active(name: str, body: dict = Body(...)):
    """Enable or disable a scheduled task (is_active 0 or 1)."""
    try:
        active = int(bool(body.get("active", 1)))
        em = _get_db_manager()
        with em._connect() as con:
            cur = con.execute(
                "UPDATE scheduled_tasks SET is_active=? WHERE name=?",
                (active, name)
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail=f"Task '{name}' not found")
        return {"name": name, "is_active": active}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error updating task active state")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/circuit-breaker/status", dependencies=[Depends(verify_api_key)])
async def get_circuit_breaker_status():
    """Return OpenRouter circuit breaker state."""
    try:
        _ensure_paths()
        from scripts.core.circuit_breaker import status as cb_status
        return cb_status()
    except Exception as e:
        return {"state": "UNKNOWN", "error": str(e)}


@app.post("/circuit-breaker/reset", dependencies=[Depends(verify_api_key)])
async def reset_circuit_breaker():
    """Manually reset circuit breaker to CLOSED."""
    try:
        _ensure_paths()
        from scripts.core.circuit_breaker import reset as cb_reset
        cb_reset()
        return {"state": "CLOSED", "reset": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Method config endpoints
# ---------------------------------------------------------------------------

@app.get("/method-config", dependencies=[Depends(verify_api_key)])
async def get_method_config():
    """Return method configuration (timeframe_hours, trigger, active)."""
    try:
        em = _get_db_manager()
        with em._connect() as con:
            rows = con.execute("SELECT * FROM method_config ORDER BY method").fetchall()
        return {"items": [dict(r) for r in rows], "total": len(rows)}
    except Exception as e:
        logger.exception("Error fetching method_config")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/method-config", dependencies=[Depends(verify_api_key)])
async def create_method_config(body: dict = Body(...)):
    """Create a new method with its config and empty prompt template."""
    try:
        method = body.get("method", "").strip()
        if not method:
            raise HTTPException(status_code=400, detail="method name is required")
        timeframe_hours = int(body.get("timeframe_hours", 24))
        trigger = body.get("trigger", "both")
        if trigger not in ("both", "time", "price_level"):
            raise HTTPException(status_code=400, detail="trigger must be 'both', 'time', or 'price_level'")
        execute = body.get("execute", "yes")
        if execute not in ("yes", "no"):
            execute = "yes"

        em = _get_db_manager()
        ts = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with em._connect() as con:
            existing = con.execute(
                "SELECT 1 FROM method_config WHERE method=?", (method,)
            ).fetchone()
            if existing:
                raise HTTPException(status_code=409, detail=f"Method '{method}' already exists")
            con.execute(
                "INSERT INTO method_config(method, timeframe_hours, trigger, active, execute) VALUES (?,?,?,1,?)",
                (method, timeframe_hours, trigger, execute),
            )
            con.execute(
                "INSERT OR IGNORE INTO prompt_templates(method, prompt_text, updated_at) VALUES (?,?,?)",
                (method, "", ts),
            )
        logger.info(f"Created new method: {method}")
        return {"method": method, "timeframe_hours": timeframe_hours, "trigger": trigger, "execute": execute}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error creating method_config")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/method-config/{method}", dependencies=[Depends(verify_api_key)])
async def update_method_config(method: str, body: dict = Body(...)):
    """Update timeframe_hours / trigger / active for a method."""
    try:
        em = _get_db_manager()
        allowed = {"timeframe_hours", "trigger", "active"}
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            raise HTTPException(status_code=400, detail="No valid fields provided")
        set_parts = ", ".join(f"{k}=?" for k in updates)
        params = list(updates.values()) + [method]
        with em._connect() as con:
            cur = con.execute(
                f"UPDATE method_config SET {set_parts} WHERE method=?", params
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail=f"Method '{method}' not found")
        return {"method": method, "updated": updates}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error updating method_config")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Execute field management endpoints
# ---------------------------------------------------------------------------

@app.put("/method-config/{method}/execute", dependencies=[Depends(verify_api_key)])
async def update_method_execute(method: str, execute: str = Body(..., embed=True)):
    """Update execute flag for a method ('yes' or 'no')."""
    try:
        if execute not in ("yes", "no"):
            raise HTTPException(status_code=400, detail="execute must be 'yes' or 'no'")
        
        em = _get_db_manager()
        with em._connect() as con:
            cur = con.execute(
                "UPDATE method_config SET execute=? WHERE method=?", 
                (execute, method)
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail=f"Method '{method}' not found")
        
        logger.info(f"Updated method {method} execute={execute}")
        return {"method": method, "execute": execute}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error updating method execute")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/providers/{provider}/execute", dependencies=[Depends(verify_api_key)])
async def update_provider_execute(provider: str, execute: str = Body(..., embed=True)):
    """Update execute flag for a provider ('yes' or 'no')."""
    try:
        if execute not in ("yes", "no"):
            raise HTTPException(status_code=400, detail="execute must be 'yes' or 'no'")
        
        em = _get_db_manager()
        with em._connect() as con:
            cur = con.execute(
                "UPDATE providers SET execute=? WHERE name=?", 
                (execute, provider)
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail=f"Provider '{provider}' not found")
        
        logger.info(f"Updated provider {provider} execute={execute}")
        return {"provider": provider, "execute": execute}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error updating provider execute")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/method-config/{method}", dependencies=[Depends(verify_api_key)])
async def get_method_config_detail(method: str):
    """Return detailed configuration for a specific method including execute flag."""
    try:
        em = _get_db_manager()
        with em._connect() as con:
            row = con.execute(
                "SELECT * FROM method_config WHERE method=?", 
                (method,)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Method '{method}' not found")
        return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching method config")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/providers/{provider}", dependencies=[Depends(verify_api_key)])
async def get_provider_detail(provider: str):
    """Return detailed configuration for a specific provider including execute flag."""
    try:
        em = _get_db_manager()
        with em._connect() as con:
            row = con.execute(
                "SELECT * FROM providers WHERE name=?", 
                (provider,)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Provider '{provider}' not found")
        return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching provider config")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Heartbeat log endpoint
# ---------------------------------------------------------------------------

@app.get("/heartbeat/history", dependencies=[Depends(verify_api_key)])
async def get_heartbeat_history(limit: int = Query(50, ge=1, le=500)):
    """Return recent heartbeat log entries."""
    try:
        em = _get_db_manager()
        with em._connect() as con:
            rows = con.execute(
                "SELECT * FROM heartbeat_log ORDER BY checked_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return {"items": [dict(r) for r in rows], "total": len(rows)}
    except Exception as e:
        logger.exception("Error fetching heartbeat history")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tickets endpoints
# ---------------------------------------------------------------------------

@app.get("/tickets", dependencies=[Depends(verify_api_key)])
async def get_tickets(
    ticker: Optional[str] = None,
    status: Optional[str] = None,
    portfolio: Optional[int] = None,
    limit: int = Query(200, ge=1, le=2000),
):
    """Return tickets list with optional filters."""
    try:
        em = _get_db_manager()
        with em._connect() as con:
            clauses = []
            params: list = []
            if ticker:
                clauses.append("UPPER(ticker)=UPPER(?)")
                params.append(ticker)
            if status:
                clauses.append("UPPER(status)=UPPER(?)")
                params.append(status)
            if portfolio is not None:
                clauses.append("portfolio=?")
                params.append(portfolio)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            rows = con.execute(
                f"SELECT * FROM tickets {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return {"items": [dict(r) for r in rows], "total": len(rows)}
    except Exception as e:
        logger.exception("Error fetching tickets")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tickets", dependencies=[Depends(verify_api_key)])
async def create_ticket(body: dict = Body(...)):
    """Create a new ticket. Required: ticker. Optional: action, quantity, price, status, portfolio, notes."""
    try:
        em = _get_db_manager()
        ticker = (body.get("ticker") or "").strip()
        if not ticker:
            raise HTTPException(status_code=422, detail="ticker is required")
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc).isoformat()
        row = {
            "ticker":    ticker,
            "created_at": now,
            "action":    body.get("action", ""),
            "quantity":  float(body.get("quantity") or 0),
            "price":     float(body.get("price") or 0),
            "status":    body.get("status", "NEW"),
            "portfolio": int(body.get("portfolio") or 0),
            "notes":     body.get("notes", ""),
        }
        cols = list(row.keys())
        ph = ", ".join(["?"] * len(cols))
        with em._connect() as con:
            cur = con.execute(
                f"INSERT INTO tickets ({', '.join(cols)}) VALUES ({ph})",
                list(row.values()),
            )
            new_id = cur.lastrowid
        return {"id": new_id, **row}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error creating ticket")
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/tickets/{ticket_id}", dependencies=[Depends(verify_api_key)])
async def update_ticket(ticket_id: int, body: dict = Body(...)):
    """Update ticket fields (status, notes, action, quantity, price, portfolio)."""
    allowed = {"status", "notes", "action", "quantity", "price", "portfolio"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=422, detail="No valid fields to update")
    try:
        em = _get_db_manager()
        with em._connect() as con:
            row = con.execute("SELECT id FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Ticket not found")
            set_parts = ", ".join(f"{k}=?" for k in updates)
            con.execute(
                f"UPDATE tickets SET {set_parts} WHERE id=?",
                list(updates.values()) + [ticket_id],
            )
        return {"updated": True, "id": ticket_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error updating ticket")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/tickets/{ticket_id}", dependencies=[Depends(verify_api_key)])
async def delete_ticket(ticket_id: int):
    """Delete a ticket by id."""
    try:
        em = _get_db_manager()
        with em._connect() as con:
            row = con.execute("SELECT id FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Ticket not found")
            con.execute("DELETE FROM tickets WHERE id=?", (ticket_id,))
        return {"deleted": True, "id": ticket_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error deleting ticket")
        raise HTTPException(status_code=500, detail=str(e))
