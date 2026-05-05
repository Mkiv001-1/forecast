"""FastAPI endpoints for Forecast Trading Robot server."""

import os
import sys
import logging
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
    SystemLogResponse,
)
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
    yield
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


def _get_excel_manager():
    """Legacy alias — all callers use SQLiteManager now."""
    return _get_db_manager()


@app.get("/health", response_model=HealthResponse)
async def health_check():
    db_exists = os.path.exists(_config.db_file) if _config else False
    return HealthResponse(
        status="ok",
        excel_file=_config.db_file if _config else None,
        excel_exists=db_exists,
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
        em = _get_excel_manager()
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
        em = _get_excel_manager()
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
        em = _get_excel_manager()
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
        em.set_config_value(key, body.value)
        return ConfigParam(key=key, value=body.value, description=body.description)
    except Exception as e:
        logger.exception("Error updating config")
        raise HTTPException(status_code=500, detail=str(e))


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
):
    try:
        em = _get_db_manager()
        df = em.get_indicators(ticker=ticker, limit=limit)
        if df.empty:
            return IndicatorsResponse(items=[], total=0)
        df = df.where(df.notna(), None)
        items = [IndicatorRecord(**_safe_row(r)) for r in df.to_dict("records")]
        return IndicatorsResponse(items=items, total=len(items))
    except Exception as e:
        logger.exception("Error reading indicators")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Consensus endpoint
# ---------------------------------------------------------------------------

@app.get("/consensus", response_model=ConsensusResponse, dependencies=[Depends(verify_api_key)])
async def get_consensus(
    ticker: Optional[str] = None,
    limit: int = Query(100, le=500),
):
    try:
        em = _get_db_manager()
        where = "WHERE ticker = ?" if ticker else ""
        params = [ticker] if ticker else []
        sql = f"SELECT * FROM consensus {where} ORDER BY date DESC LIMIT {limit}"
        with em._connect() as con:
            df = pd.read_sql_query(sql, con, params=params)
        if df.empty:
            return ConsensusResponse(items=[], total=0)
        df = df.where(df.notna(), None)
        items = [ConsensusRecord(**_safe_row(r)) for r in df.to_dict("records")]
        return ConsensusResponse(items=items, total=len(items))
    except Exception as e:
        logger.exception("Error reading consensus")
        raise HTTPException(status_code=500, detail=str(e))


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
        em = _get_db_manager()
        ok = await sync_portfolio_with_ib_async(em, host=host, port=port, client_id=client_id, type=type)
        if not ok:
            raise HTTPException(status_code=502, detail="IB Gateway returned no positions. Ensure TWS/Gateway is running and API is enabled on port " + str(port))
        return {"synced": True, "client_id": client_id, "type": type}
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
