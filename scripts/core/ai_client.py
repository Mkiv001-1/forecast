"""
Universal AI client supporting both OpenRouter (cloud) and LM Studio (local).
"""

import json
import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Cloud: OpenRouter
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Local: LM Studio
LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"

_APP_URL = "https://forecast-robot.local"


class RateLimitError(Exception):
    """Raised when OpenRouter returns 429 and caller should back off or skip."""
    def __init__(self, model: str, retry_after: int = 30):
        self.model = model
        self.retry_after = retry_after
        super().__init__(f"Rate limited: {model} (retry after {retry_after}s)")


class AIClient:
    """Calls AI models via OpenRouter or LM Studio local server."""

    def __init__(self, api_key: str, base_url: str = OPENROUTER_URL, model_prefix: str = ""):
        self.base_url = base_url
        self.model_prefix = model_prefix

        if base_url == LM_STUDIO_URL:
            # LM Studio local server — no API key needed, use dummy
            self.api_key = "sk-local-dummy"
            logger.info(f"AIClient configured for LM Studio local server at {base_url}")
        else:
            # OpenRouter cloud — require real API key
            if not api_key:
                raise ValueError("OpenRouter API key is required")
            self.api_key = api_key
            logger.info(f"AIClient configured for OpenRouter cloud")

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": _APP_URL,
            "Content-Type": "application/json",
        })

    def call(
        self,
        model: str,
        user_prompt: str,
        system_prompt: str = "You are a professional algorithmic trader. Respond only with valid JSON.",
        temperature: float = 0.2,
        max_tokens: int = 2000,
        max_retries: int = 3,
    ) -> str:
        """
        Call a model via OpenRouter or LM Studio.
        For LM Studio: model should be the exact ID from /v1/models endpoint.
        For OpenRouter: model should be provider/model format (e.g. anthropic/claude-3-opus-20240229).
        Returns the assistant message content string.
        Raises on unrecoverable error after max_retries.
        """
        # LM Studio models may have a prefix; strip it if present
        display_model = model
        if self.model_prefix and model.startswith(self.model_prefix):
            display_model = model[len(self.model_prefix):]

        payload = {
            "model": display_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens":  max_tokens,
        }

        # Circuit-breaker guard
        try:
            from circuit_breaker import is_open
            if is_open():
                raise RuntimeError(f"circuit_breaker: {self.base_url} OPEN — {model} call rejected")
        except ImportError:
            pass  # circuit_breaker not yet available

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                logger.debug(f"[{model}] attempt {attempt}/{max_retries}")
                response = self._session.post(
                    self.base_url,
                    json=payload,
                    timeout=120,
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                logger.debug(f"[{model}] OK, {len(content)} chars")
                try:
                    from circuit_breaker import record_success
                    record_success()
                except ImportError:
                    pass
                return content

            except requests.exceptions.Timeout as e:
                logger.warning(f"[{model}] timeout on attempt {attempt}: {e}")
                last_error = e
                try:
                    from circuit_breaker import record_failure
                    record_failure()
                except ImportError:
                    pass
                time.sleep(5 * attempt)

            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                body = {}
                try:
                    body = e.response.json()
                except Exception:
                    pass
                logger.error(f"[{model}] HTTP {status} on attempt {attempt}: {body}")
                last_error = e
                if status in (401, 402, 403, 404):
                    break  # non-retryable billing/auth errors — do NOT trip circuit breaker
                if status == 429:
                    retry_after = 30
                    try:
                        retry_after = int(
                            body.get("error", {}).get("metadata", {})
                            .get("retry_after_seconds", 30)
                        )
                    except Exception:
                        pass
                    logger.warning(f"[{model}] rate-limited (retry_after={retry_after}s) — skipping")
                    raise RateLimitError(model, retry_after)
                else:
                    try:
                        from circuit_breaker import record_failure
                        record_failure()
                    except ImportError:
                        pass
                    time.sleep(5 * attempt)

            except RateLimitError:
                raise  # propagate immediately — do not retry

            except Exception as e:
                logger.error(f"[{model}] error on attempt {attempt}: {e}")
                last_error = e
                try:
                    from circuit_breaker import record_failure
                    record_failure()
                except ImportError:
                    pass
                time.sleep(5 * attempt)

        raise RuntimeError(f"All {max_retries} attempts failed for model '{model}': {last_error}")


def get_ai_client(db_manager, base_url: str = OPENROUTER_URL, model_prefix: str = "") -> Optional[AIClient]:
    """
    Create AIClient using the API key from the database config.
    If base_url is LM_STUDIO_URL, no API key is required (local server).
    Returns None if the key is not configured (for OpenRouter cloud).
    """
    if base_url == LM_STUDIO_URL:
        # LM Studio local server — no API key needed
        logger.info("Creating AIClient for LM Studio local server")
        return AIClient(api_key="", base_url=base_url, model_prefix=model_prefix)

    # OpenRouter cloud — require real API key
    api_key = db_manager.get_config_value("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        logger.warning("OPENROUTER_API_KEY is not configured in DB config table")
        return None
    return AIClient(api_key=api_key, base_url=base_url, model_prefix=model_prefix)


def get_active_ai_models(db_manager, base_url: str = OPENROUTER_URL) -> list:
    """
    Return list of active AI model dicts from providers table.
    Each dict: {name, model, temperature, max_tokens, rate_limit}
    If OPENROUTER_FREE_ONLY=true, appends :free suffix to model IDs.
    For LM Studio local server, models are read from the providers table
    with model IDs as-is (e.g. 'qwen3.5-9b').
    """
    try:
        free_only = db_manager.get_config_value("OPENROUTER_FREE_ONLY", "false").strip().lower() == "true"
        df = db_manager.read_sheet("Providers")
        if df.empty:
            return []
        ai_df = df[(df["type"] == "ai") & (df["active"].astype(int) == 1)]
        if ai_df.empty:
            return []
        result = []
        for _, row in ai_df.iterrows():
            model_id = str(row.get("model", ""))
            if free_only and model_id and not model_id.endswith(":free"):
                model_id = model_id + ":free"
            result.append({
                "name":        str(row.get("name", "")),
                "model":       model_id,
                "temperature": float(row.get("temperature", 0.2)),
                "max_tokens":  int(row.get("max_tokens", 2000)),
                "rate_limit":  int(row.get("rate_limit", 60)),
            })
        if free_only:
            logger.info(f"OPENROUTER_FREE_ONLY=true — using :free model variants")
        return result
    except Exception as e:
        logger.error(f"Error reading AI models: {e}")
        return []
