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
    ForecastLog, TickerSetting, ProviderSetting,
    LogsResponse, TickersResponse, ProvidersResponse,
    TickerCreate, TickerUpdate, ProviderUpdate,
    RunResponse, HealthResponse,
    ConfigParam, ConfigResponse,
    PromptRecord, PromptsResponse,
    PriceRecord, PriceDataResponse,
    IndicatorRecord, IndicatorsResponse,
    ConsensusRecord, ConsensusResponse,
    SystemLogResponse,
)

logger = logging.getLogger(__name__)


class ForecastApiClient:
    """Synchronous HTTP client for the Forecast server."""

    def __init__(self, server_url: str, api_key: str, timeout: int = 30):
        self.server_url = server_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"X-API-Key": api_key})

    def _get(self, path: str, params: dict = None):
        url = f"{self.server_url}{path}"
        resp = self._session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, json: dict = None):
        url = f"{self.server_url}{path}"
        resp = self._session.post(url, json=json, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, json: dict = None):
        url = f"{self.server_url}{path}"
        resp = self._session.put(url, json=json, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def health(self) -> HealthResponse:
        data = self._session.get(f"{self.server_url}/health", timeout=self.timeout).json()
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
    ) -> IndicatorsResponse:
        params = {"limit": limit}
        if ticker: params["ticker"] = ticker
        data = self._get("/indicators", params=params)
        return IndicatorsResponse(**data)

    def get_consensus(
        self,
        ticker: Optional[str] = None,
        limit: int = 100,
    ) -> ConsensusResponse:
        params = {"limit": limit}
        if ticker: params["ticker"] = ticker
        data = self._get("/consensus", params=params)
        return ConsensusResponse(**data)

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
