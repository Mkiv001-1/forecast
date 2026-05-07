"""HTTP client for the Forecast Trading Robot API."""

import logging
from typing import List, Optional
import requests

_SCRIPTS_DIR = None
try:
    import os, sys
    _SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
except Exception:
    pass

from scripts.shared.models import (
    ForecastLog, TickerSetting, ProviderSetting, IBConfigRecord, IBConfigResponse,
    LogsResponse, TickersResponse, ProvidersResponse,
    TickerCreate, TickerUpdate, ProviderUpdate,
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

logger = logging.getLogger(__name__)


class ForecastApiClient:
    """Synchronous HTTP client for the Forecast server."""

    def __init__(self, server_url: str, api_key: str, timeout: int = 8):
        self.server_url = server_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"X-API-Key": api_key})

    def _get(self, path: str, params: dict = None):
        url = f"{self.server_url}{path}"
        resp = self._session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, json: dict = None, params: dict = None):
        url = f"{self.server_url}{path}"
        resp = self._session.post(url, json=json, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, json: dict = None):
        url = f"{self.server_url}{path}"
        resp = self._session.put(url, json=json, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, json: dict = None):
        url = f"{self.server_url}{path}"
        resp = self._session.patch(url, json=json, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def health(self, timeout: Optional[int] = None) -> HealthResponse:
        req_timeout = timeout if timeout is not None else self.timeout
        data = self._session.get(f"{self.server_url}/health", timeout=req_timeout).json()
        return HealthResponse(**data)

    def get_logs(
        self,
        ticker: Optional[str] = None,
        method: Optional[str] = None,
        status: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 500,
    ) -> List[ForecastLog]:
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if method:
            params["method"] = method
        if status:
            params["status"] = status
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        data = self._get("/logs", params=params)
        return [ForecastLog(**item) for item in data.get("items", [])]

    def get_log(self, log_id: str) -> ForecastLog:
        data = self._get(f"/logs/{log_id}")
        return ForecastLog(**data)

    def get_tickers(self) -> List[TickerSetting]:
        data = self._get("/tickers")
        return [TickerSetting(**item) for item in data.get("items", [])]

    def add_ticker(self, ticker: str, active: int = 1, comment: str = "") -> TickerSetting:
        data = self._post("/tickers", json={"ticker": ticker, "active": active, "comment": comment})
        return TickerSetting(**data)

    def update_ticker(self, ticker: str, active: int, comment: str = "") -> TickerSetting:
        data = self._put(f"/tickers/{ticker}", json={"active": active, "comment": comment})
        return TickerSetting(**data)

    def get_providers(self) -> List[ProviderSetting]:
        data = self._get("/providers")
        return [ProviderSetting(**item) for item in data.get("items", [])]

    def update_provider(self, name: str, **kwargs) -> ProviderSetting:
        data = self._put(f"/providers/{name}", json={k: v for k, v in kwargs.items() if v is not None})
        return ProviderSetting(**data)

    def run_forecast(self) -> RunResponse:
        data = self._post("/run/forecast")
        return RunResponse(**data)

    def run_evaluate(self) -> RunResponse:
        data = self._post("/run/evaluate")
        return RunResponse(**data)

    def run_full(self) -> RunResponse:
        data = self._post("/run/full")
        return RunResponse(**data)

    def run_price_data(self) -> RunResponse:
        data = self._post("/run/price-data")
        return RunResponse(**data)

    def run_status(self) -> RunResponse:
        data = self._get("/run/status")
        return RunResponse(**data)

    def _delete(self, path: str):
        url = f"{self.server_url}{path}"
        resp = self._session.delete(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def delete_ticker(self, ticker: str) -> dict:
        return self._delete(f"/tickers/{ticker}")

    def delete_provider(self, name: str) -> dict:
        return self._delete(f"/providers/{name}")

    def get_logs_response(
        self,
        ticker: Optional[str] = None,
        method: Optional[str] = None,
        status: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 500,
    ) -> LogsResponse:
        params = {"limit": limit}
        if ticker: params["ticker"] = ticker
        if method: params["method"] = method
        if status: params["status"] = status
        if date_from: params["date_from"] = date_from
        if date_to: params["date_to"] = date_to
        data = self._get("/logs", params=params)
        return LogsResponse(**data)

    def get_config(self) -> ConfigResponse:
        data = self._get("/config")
        return ConfigResponse(**data)

    def set_config(self, key: str, value: str) -> None:
        """Convenience wrapper: PUT /config/{key}."""
        self._put(f"/config/{key}", json={"key": key, "value": value, "description": ""})

    def get(self, path: str, params: dict = None) -> dict:
        """Generic GET — returns raw dict. Used by new tabs (trades, ib-log, etc.)."""
        return self._get(path, params=params)

    def update_config(self, key: str, body: ConfigParam) -> ConfigParam:
        data = self._put(f"/config/{key}", json=body.model_dump())
        return ConfigParam(**data)

    def get_prompts(
        self,
        ticker: Optional[str] = None,
        method: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 200,
    ) -> PromptsResponse:
        params = {"limit": limit}
        if ticker: params["ticker"] = ticker
        if method: params["method"] = method
        if date_from: params["date_from"] = date_from
        if date_to: params["date_to"] = date_to
        data = self._get("/prompts", params=params)
        return PromptsResponse(**data)

    def get_price_data(
        self,
        ticker: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 500,
    ) -> PriceDataResponse:
        params = {"limit": limit}
        if ticker: params["ticker"] = ticker
        if date_from: params["date_from"] = date_from
        if date_to: params["date_to"] = date_to
        data = self._get("/price-data", params=params)
        return PriceDataResponse(**data)

    def get_indicators(
        self,
        ticker: Optional[str] = None,
        limit: int = 200,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> IndicatorsResponse:
        params = {"limit": limit}
        if ticker: params["ticker"] = ticker
        if date_from: params["date_from"] = date_from
        if date_to: params["date_to"] = date_to
        data = self._get("/indicators", params=params)
        return IndicatorsResponse(**data)

    def get_consensus(
        self,
        ticker: Optional[str] = None,
        limit: int = 100,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> ConsensusResponse:
        params = {"limit": limit}
        if ticker: params["ticker"] = ticker
        if date_from: params["date_from"] = date_from
        if date_to: params["date_to"] = date_to
        data = self._get("/consensus", params=params)
        return ConsensusResponse(**data)

    def evaluate_consensus(self) -> dict:
        """Trigger evaluation of pending consensus records."""
        return self._post("/consensus/evaluate")

    def recalculate_consensus(self, date_from: str = None, date_to: str = None, force: bool = False) -> dict:
        """Recalculate consensus from historical forecast logs."""
        params = {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        if force:
            params["force"] = "true"
        return self._post("/consensus/recalculate", params=params)

    def activate_consensus(self, consensus_id: int) -> dict:
        """Trigger trade placement for one consensus record."""
        return self._post(f"/consensus/{int(consensus_id)}/activate")

    def preview_consensus_trade(self, consensus_id: int) -> dict:
        """Fetch trade preview details for one consensus record."""
        return self._get(f"/consensus/{int(consensus_id)}/preview-trade")

    def get_system_log(
        self,
        lines: int = 200,
        level: Optional[str] = None,
    ) -> SystemLogResponse:
        params = {"lines": lines}
        if level: params["level"] = level
        data = self._get("/system-log", params=params)
        return SystemLogResponse(**data)

    def get_model_catalog(self, provider: Optional[str] = None) -> dict:
        params = {}
        if provider:
            params["provider"] = provider
        return self._get("/model-catalog", params=params)

    def refresh_model_catalog(self) -> dict:
        return self._post("/model-catalog/refresh", {})

    def get_prompt_templates(self) -> dict:
        return self._get("/prompt-templates")

    def save_prompt_template(self, method: str, prompt_text: str) -> dict:
        return self._put(f"/prompt-templates/{method}", {"prompt_text": prompt_text})

    def reset_prompt_template(self, method: str) -> dict:
        return self._post(f"/prompt-templates/{method}/reset", {})

    def get_accounts(self, broker: Optional[str] = None) -> AccountsResponse:
        params = {}
        if broker:
            params["broker"] = broker
        data = self._get("/accounts", params=params)
        return AccountsResponse(**data)

    def sync_accounts(self, host: str = "127.0.0.1", port: int = 7497, client_id: int = 1, type: str = "paper") -> dict:
        return self._post("/accounts/sync", json={}, params={"host": host, "port": port, "client_id": client_id, "type": type})

    def get_portfolio(self, account: Optional[str] = None) -> PortfolioResponse:
        params = {}
        if account:
            params["account"] = account
        data = self._get("/portfolio", params=params)
        return PortfolioResponse(**data)

    def sync_portfolio(self, host: str = "127.0.0.1", port: int = 7497, client_id: int = 1, type: str = "paper") -> dict:
        return self._post("/portfolio/sync", json={}, params={"host": host, "port": port, "client_id": client_id, "type": type})

    def test_ib_connection(self, host: str = "127.0.0.1", port: int = 7497, client_id: int = 1) -> dict:
        """Test IB Gateway connection and return detailed logs."""
        return self._get("/ib/test-connection", params={"host": host, "port": port, "client_id": client_id})

    def get_ib_configs(self) -> IBConfigResponse:
        """Get all IB Gateway connection configurations."""
        data = self._get("/ib-config")
        return IBConfigResponse(**data)

    def get_ib_config(self, config_id: int) -> IBConfigRecord:
        """Get specific IB Gateway configuration."""
        data = self._get(f"/ib-config/{config_id}")
        return IBConfigRecord(**data)

    def create_ib_config(self, config: IBConfigRecord) -> IBConfigRecord:
        """Create new IB Gateway configuration."""
        data = self._post("/ib-config", json=config.model_dump())
        return IBConfigRecord(**data)

    def update_ib_config(self, config_id: int, config: IBConfigRecord) -> IBConfigRecord:
        """Update IB Gateway configuration."""
        data = self._put(f"/ib-config/{config_id}", json=config.model_dump())
        return IBConfigRecord(**data)

    def delete_ib_config(self, config_id: int) -> dict:
        """Delete IB Gateway configuration."""
        return self._delete(f"/ib-config/{config_id}")

    def get_scheduler_tasks(self) -> list:
        """Return all rows from scheduled_tasks."""
        data = self._get("/scheduler/tasks")
        return data.get("items", [])

    def set_task_active(self, name: str, active: int) -> dict:
        """Enable (1) or disable (0) a scheduled task."""
        return self._patch(f"/scheduler/tasks/{name}/active", json={"active": active})

    def get_heartbeat_history(self, limit: int = 20) -> list:
        """Return recent heartbeat_log entries."""
        data = self._get("/heartbeat/history", params={"limit": limit})
        return data.get("items", [])

    def get_orders(
        self,
        ticker: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 200,
        include_test: Optional[bool] = None,
        test_only: Optional[bool] = None,
    ) -> list:
        """Return orders from the server."""
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        if include_test is not None:
            params["include_test"] = include_test
        if test_only is not None:
            params["test_only"] = test_only
        data = self._get("/orders", params=params)
        return data.get("items", [])

    def get_trades(
        self,
        ticker: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 200,
        include_test: Optional[bool] = None,
        test_only: Optional[bool] = None,
    ) -> list:
        """Return trades from the server."""
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        if include_test is not None:
            params["include_test"] = include_test
        if test_only is not None:
            params["test_only"] = test_only
        data = self._get("/trades", params=params)
        return data.get("trades", [])

    def cancel_order(self, order_id: int) -> dict:
        """Cancel an IB order by DB id."""
        return self._post(f"/orders/{order_id}/cancel")

    def get_tickets(
        self,
        ticker: Optional[str] = None,
        status: Optional[str] = None,
        portfolio: Optional[int] = None,
        limit: int = 500,
    ) -> list:
        """Return tickets list."""
        params: dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        if portfolio is not None:
            params["portfolio"] = portfolio
        data = self._get("/tickets", params=params)
        return data.get("items", [])

    def create_ticket(self, ticker: str, **kwargs) -> dict:
        """Create a new ticket."""
        return self._post("/tickets", {"ticker": ticker, **kwargs})

    def update_ticket(self, ticket_id: int, **kwargs) -> dict:
        """Update ticket fields."""
        return self._patch(f"/tickets/{ticket_id}", kwargs)

    def delete_ticket(self, ticket_id: int) -> dict:
        """Delete a ticket."""
        return self._delete(f"/tickets/{ticket_id}")

    def create_method(self, method: str, timeframe_hours: int = 24,
                      trigger: str = "both", execute: str = "yes") -> dict:
        """Create a new method configuration."""
        return self._post("/method-config", json={
            "method": method,
            "timeframe_hours": timeframe_hours,
            "trigger": trigger,
            "execute": execute,
        })

    def get_method_configs(self) -> list:
        """Return all method configurations."""
        data = self._get("/method-config")
        return data.get("items", [])

    def get_method_config(self, method: str) -> dict:
        """Return detailed configuration for a specific method."""
        return self._get(f"/method-config/{method}")

    def update_method_execute(self, method: str, execute: str) -> dict:
        """Update execute flag for a method ('yes' or 'no')."""
        return self._put(f"/method-config/{method}/execute", json={"execute": execute})

    def update_provider_execute(self, provider: str, execute: str) -> dict:
        """Update execute flag for a provider ('yes' or 'no')."""
        return self._put(f"/providers/{provider}/execute", json={"execute": execute})

    def get_forecast_runs(self, limit: int = 50) -> dict:
        """Return list of forecast runs with aggregated stats."""
        return self._get("/forecast-runs", params={"limit": limit})

    def get_forecast_run(self, run_id: int) -> dict:
        """Return details of a specific forecast run including links."""
        return self._get(f"/forecast-runs/{run_id}")
