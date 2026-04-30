"""
SQLite storage manager — drop-in replacement for ExcelManager.
Public interface is identical so all other modules work unchanged.
"""

import os
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import pandas as pd

logger = logging.getLogger(__name__)

_CREATE_TABLES = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS settings (
    ticker  TEXT PRIMARY KEY,
    active  INTEGER DEFAULT 0,
    comment TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS providers (
    name        TEXT PRIMARY KEY,
    type        TEXT    DEFAULT 'ai',
    base_url    TEXT    DEFAULT '',
    api_key     TEXT    DEFAULT '',
    model       TEXT    DEFAULT '',
    temperature REAL    DEFAULT 0.2,
    max_tokens  INTEGER DEFAULT 2000,
    rate_limit  INTEGER DEFAULT 60,
    active      INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS config (
    key         TEXT PRIMARY KEY,
    value       TEXT DEFAULT '',
    description TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS logs (
    id                TEXT PRIMARY KEY,
    forecast_date     TEXT,
    created_at        TEXT,
    ticker            TEXT,
    method            TEXT,
    model             TEXT,
    confidence        REAL,
    side              TEXT,
    entry_price       REAL,
    entry_conditions  TEXT,
    exit_target       TEXT,
    exit_stop         TEXT,
    position_size     TEXT DEFAULT '',
    rationale         TEXT,
    forecast_prompt   TEXT,
    prompt_response   TEXT,
    status            TEXT DEFAULT 'NEW',
    horizon_days      INTEGER DEFAULT 1,
    actual_date       TEXT,
    actual_open       REAL,
    actual_close      REAL,
    actual_high       REAL,
    actual_low        REAL,
    entry_triggered   INTEGER,
    target_hit        INTEGER,
    stop_hit          INTEGER,
    pnl_pct           REAL,
    direction_correct INTEGER,
    exit_successful   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_logs_ticker  ON logs(ticker);
CREATE INDEX IF NOT EXISTS idx_logs_status  ON logs(status);
CREATE INDEX IF NOT EXISTS idx_logs_date    ON logs(forecast_date);

CREATE TABLE IF NOT EXISTS price_data (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker  TEXT NOT NULL,
    date    TEXT NOT NULL,
    open    REAL,
    high    REAL,
    low     REAL,
    close   REAL,
    volume  INTEGER,
    UNIQUE(ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_price_ticker ON price_data(ticker);

CREATE TABLE IF NOT EXISTS indicators (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT NOT NULL,
    date         TEXT NOT NULL,
    price        REAL,
    ma20         REAL, ma50  REAL, ma200 REAL,
    ema9         REAL, ema21 REAL,
    rsi14        REAL, stoch_rsi REAL,
    atr14        REAL, adx14    REAL,
    macd         REAL, macd_signal REAL, macd_hist REAL,
    bb_upper     REAL, bb_lower   REAL, bb_middle  REAL,
    obv          REAL,
    change_5d    REAL, change_10d REAL, change_20d REAL, change_50d REAL,
    volume_avg_20 REAL, volume_current INTEGER,
    market_regime TEXT,
    UNIQUE(ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_ind_ticker ON indicators(ticker);

CREATE TABLE IF NOT EXISTS prompts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT,
    ticker      TEXT,
    method      TEXT,
    prompt_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_prompts_ticker ON prompts(ticker);

CREATE TABLE IF NOT EXISTS consensus (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT,
    ticker          TEXT,
    signal          TEXT,
    confidence      REAL,
    methods_long    TEXT,
    methods_short   TEXT,
    methods_neutral TEXT,
    rationale       TEXT
);
CREATE INDEX IF NOT EXISTS idx_cons_ticker ON consensus(ticker);

CREATE TABLE IF NOT EXISTS model_catalog (
    model_id    TEXT PRIMARY KEY,
    name        TEXT DEFAULT '',
    provider    TEXT DEFAULT '',
    context_len INTEGER DEFAULT 0,
    input_price REAL DEFAULT 0,
    output_price REAL DEFAULT 0,
    updated_at  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS prompt_templates (
    method      TEXT PRIMARY KEY,
    prompt_text TEXT DEFAULT '',
    updated_at  TEXT DEFAULT ''
);
"""

_DEFAULT_CONFIG = [
    ("OPENROUTER_API_KEY",   "",           "OpenRouter API key (https://openrouter.ai)"),
    ("ALPHA_VANTAGE_API_KEY","",           "Alpha Vantage API key"),
    ("DATA_SOURCE",          "yfinance",   "Primary price data source: yfinance | alpha_vantage | finnhub"),
    ("SCHEDULE_FORECAST",    "21:30",      "Daily forecast time UTC (HH:MM), empty = disabled"),
    ("SCHEDULE_EVALUATE",    "14:30",      "Daily evaluation time UTC (HH:MM), empty = disabled"),
    ("MAX_RISK_PER_TRADE",   "2.0",        "Max risk per trade % of account"),
    ("MAX_POSITIONS",        "3",          "Max simultaneous open positions"),
    ("DEFAULT_HORIZON_DAYS", "1",          "Default forecast horizon in trading days"),
]

_DEFAULT_PROVIDERS = [
    ("claude-sonnet",   "ai",   "https://openrouter.ai/api/v1", "", "anthropic/claude-sonnet-4",           0.2, 2000, 60, 1),
    ("gpt-4o",          "ai",   "https://openrouter.ai/api/v1", "", "openai/gpt-4o",                       0.2, 2000, 60, 1),
    ("deepseek-v3",     "ai",   "https://openrouter.ai/api/v1", "", "deepseek/deepseek-chat-v3-0324",       0.2, 2000, 60, 1),
    ("gemini-flash",    "ai",   "https://openrouter.ai/api/v1", "", "google/gemini-2.5-flash-preview",      0.2, 2000, 60, 1),
    ("sonar-pro",       "ai",   "https://openrouter.ai/api/v1", "", "perplexity/sonar-pro",                 0.2, 2000, 60, 1),
    ("alpha_vantage",   "data", "",                             "", "",                                     0.0,    0,  5, 1),
    ("yfinance",        "data", "",                             "", "",                                     0.0,    0, 60, 1),
    ("finnhub",         "data", "",                             "", "",                                     0.0,    0, 60, 0),
]

_DEFAULT_CATALOG = [
    # (model_id, name, provider, context_len, input_$/1M, output_$/1M)
    ("anthropic/claude-sonnet-4",           "Claude Sonnet 4",             "Anthropic",  200000, 3.00,  15.00),
    ("anthropic/claude-3.5-sonnet",          "Claude 3.5 Sonnet",           "Anthropic",  200000, 3.00,  15.00),
    ("anthropic/claude-3.5-haiku",           "Claude 3.5 Haiku",            "Anthropic",  200000, 0.80,   4.00),
    ("anthropic/claude-3-opus",              "Claude 3 Opus",               "Anthropic",  200000, 15.00, 75.00),
    ("openai/gpt-4.1",                       "GPT-4.1",                     "OpenAI",     128000, 2.00,   8.00),
    ("openai/gpt-4o",                        "GPT-4o",                      "OpenAI",     128000, 2.50,  10.00),
    ("openai/gpt-4o-mini",                   "GPT-4o Mini",                 "OpenAI",     128000, 0.15,   0.60),
    ("openai/o3",                            "o3",                          "OpenAI",     200000, 10.00, 40.00),
    ("openai/o3-mini",                       "o3-mini",                     "OpenAI",     200000, 1.10,   4.40),
    ("openai/o4-mini",                       "o4-mini",                     "OpenAI",     200000, 1.10,   4.40),
    ("deepseek/deepseek-chat-v3-0324",        "DeepSeek V3 0324",            "DeepSeek",   65536,  0.27,   1.10),
    ("deepseek/deepseek-chat",               "DeepSeek Chat",               "DeepSeek",   65536,  0.27,   1.10),
    ("deepseek/deepseek-r1",                 "DeepSeek R1",                 "DeepSeek",   65536,  0.55,   2.19),
    ("deepseek/deepseek-r1-distill-llama-70b","DeepSeek R1 Distill 70B",    "DeepSeek",   65536,  0.23,   0.69),
    ("google/gemini-2.5-pro-preview",         "Gemini 2.5 Pro",             "Google",    1000000, 1.25,  10.00),
    ("google/gemini-2.5-flash-preview",       "Gemini 2.5 Flash",           "Google",    1000000, 0.15,   0.60),
    ("google/gemini-2.0-flash-001",           "Gemini 2.0 Flash",           "Google",    1000000, 0.10,   0.40),
    ("perplexity/sonar-pro",                 "Sonar Pro",                   "Perplexity",  200000, 3.00,  15.00),
    ("perplexity/sonar",                     "Sonar",                       "Perplexity",  127072, 1.00,   1.00),
    ("perplexity/sonar-reasoning-pro",        "Sonar Reasoning Pro",        "Perplexity",  127072, 8.00,  40.00),
    ("meta-llama/llama-4-maverick",           "Llama 4 Maverick",           "Meta",       524288, 0.17,   0.60),
    ("meta-llama/llama-3.3-70b-instruct",     "Llama 3.3 70B",              "Meta",       131072, 0.12,   0.30),
    ("mistralai/mistral-large-2411",          "Mistral Large 24.11",        "Mistral",    131072, 2.00,   6.00),
    ("mistralai/mistral-small-3.1-24b-instruct","Mistral Small 3.1 24B",   "Mistral",    131072, 0.10,   0.30),
    ("x-ai/grok-3-mini-beta",                "Grok 3 Mini",                 "xAI",        131072, 0.30,   0.50),
    ("x-ai/grok-2-1212",                     "Grok 2",                      "xAI",        131072, 2.00,  10.00),
    ("qwen/qwen-2.5-72b-instruct",            "Qwen 2.5 72B",               "Qwen",        32768, 0.13,   0.40),
    ("qwen/qwq-32b",                         "QwQ 32B",                     "Qwen",        32768, 0.15,   0.60),
]

_DEFAULT_SETTINGS = [
    ("NASDAQ:NVDA", 1, "Nvidia - AI chips"),
    ("NASDAQ:TSLA", 0, "Tesla"),
    ("NASDAQ:AAPL", 0, "Apple"),
]

_DEFAULT_PROMPT_TEMPLATES = [
    ("momentum_trend", """Сделай торговый прогноз для {ticker} на {forecast_date} (горизонт {horizon} дней).
Рыночный режим: {market_regime} (ADX={adx:.1f})
{market_context}
ТЕХНИЧЕСКИЕ ДАННЫЕ:
- Цена: ${price:.2f} | MA20: ${ma20:.2f} | MA50: ${ma50:.2f} | MA200: ${ma200:.2f}
- EMA9: ${ema9:.2f} | EMA21: ${ema21:.2f}
- RSI(14): {rsi:.1f} | MACD hist: {macd_hist:+.2f} | ADX: {adx:.1f}
- OBV: {obv_trend} | Динамика: 5д={change_5d:+.1f}% 20д={change_20d:+.1f}%

МЕТОД: MOMENTUM TREND
Определи направление тренда и силу импульса. Используй выравнивание MA и ADX.
Тренд сильный если ADX>25, бычий если EMA9>EMA21 и цена выше MA50.
{history}
{footer}"""),

    ("price_action", """Сделай торговый прогноз для {ticker} на {forecast_date} (горизонт {horizon} дней).
Рыночный режим: {market_regime} (ADX={adx:.1f})
{market_context}
ТЕХНИЧЕСКИЕ ДАННЫЕ:
- Цена: ${price:.2f} | BB верх: ${bb_upper:.2f} | BB низ: ${bb_lower:.2f} | Позиция: {bb_pos:.0f}%
- Stoch RSI: {stoch_rsi:.2f} | RSI: {rsi:.1f} | ATR: {atr_pct:.1f}%
- Динамика: 5д={change_5d:+.1f}% 20д={change_20d:+.1f}%

МЕТОД: PRICE ACTION
Оцени уровни поддержки/сопротивления и перекупленность/перепроданность.
Цена у верхней BB (>80%) = зона сопротивления, у нижней (<20%) = поддержка.
{history}
{footer}"""),

    ("relative_strength", """Сделай торговый прогноз для {ticker} на {forecast_date} (горизонт {horizon} дней).
Рыночный режим: {market_regime} (ADX={adx:.1f})
{market_context}
ТЕХНИЧЕСКИЕ ДАННЫЕ:
- Цена: ${price:.2f} | RSI: {rsi:.1f} | ADX: {adx:.1f}
- Динамика: 5д={change_5d:+.1f}% 10д={change_10d:+.1f}% 20д={change_20d:+.1f}% 50д={change_50d:+.1f}%
- Объём: {volume_current} ({vol_ratio:.1f}x среднего)

МЕТОД: RELATIVE STRENGTH
Оцени относительную силу актива vs рынок и устойчивость тренда.
{history}
{footer}"""),

    ("volatility", """Сделай торговый прогноз для {ticker} на {forecast_date} (горизонт {horizon} дней).
Рыночный режим: {market_regime} (ADX={adx:.1f})
{market_context}
ТЕХНИЧЕСКИЕ ДАННЫЕ:
- Цена: ${price:.2f} | ATR: ${atr:.2f} ({atr_pct:.1f}%) | ADX: {adx:.1f}
- BB: [{bb_lower:.2f} — {bb_upper:.2f}] ширина {bb_width:.2f} | RSI: {rsi:.1f}
- Динамика: 5д={change_5d:+.1f}% 20д={change_20d:+.1f}%

МЕТОД: VOLATILITY BREAKOUT
Оцени режим волатильности и риск пробоя Bollinger Bands.
Сжатие BB (ширина <3% цены) предшествует пробою.
{history}
{footer}"""),

    ("mean_reversion", """Сделай торговый прогноз для {ticker} на {forecast_date} (горизонт {horizon} дней).
Рыночный режим: {market_regime} (ADX={adx:.1f})
{market_context}
ТЕХНИЧЕСКИЕ ДАННЫЕ:
- Цена: ${price:.2f} | MA20: ${ma20:.2f} (откл. {ma20_dev:+.1f}%) | MA50: ${ma50:.2f}
- RSI: {rsi:.1f} | Stoch RSI: {stoch_rsi:.2f} | MACD hist: {macd_hist:+.2f}
- Динамика: 5д={change_5d:+.1f}% 20д={change_20d:+.1f}%

МЕТОД: MEAN REVERSION
Оцени вероятность возврата цены к MA20/MA50. Ищи дивергенции RSI и цены.
Лучший сигнал: RSI<30 + цена ниже MA20 на >5% (oversold bounce).
{history}
{footer}"""),

    ("volume_breakout", """Сделай торговый прогноз для {ticker} на {forecast_date} (горизонт {horizon} дней).
Рыночный режим: {market_regime} (ADX={adx:.1f})
{market_context}
ТЕХНИЧЕСКИЕ ДАННЫЕ:
- Цена: ${price:.2f} | Объём: {volume_current} ({vol_ratio:.1f}x среднего)
- OBV тренд: {obv_trend} | ATR: {atr_pct:.1f}% | ADX: {adx:.1f}
- Динамика: 5д={change_5d:+.1f}% 20д={change_20d:+.1f}%

МЕТОД: VOLUME BREAKOUT
Оцени силу объёмного импульса и вероятность пробоя ключевого уровня.
Объём >2x среднего при движении цены = подтверждённый пробой.
{history}
{footer}"""),
]


# ---------------------------------------------------------------------------
# Table name alias: keep Excel sheet names working
# ---------------------------------------------------------------------------
_SHEET_TO_TABLE = {
    "Settings":   "settings",
    "Providers":  "providers",
    "Config":     "config",
    "Logs":       "logs",
    "PriceData":  "price_data",
    "Indicators": "indicators",
    "Prompts":    "prompts",
    "Consensus":  "consensus",
    # legacy
    "Log":        "logs",
    "Forecasts":  "logs",
}


def _tbl(sheet_name: str) -> str:
    return _SHEET_TO_TABLE.get(sheet_name, sheet_name.lower())


class SQLiteManager:
    """SQLite-backed storage. Drop-in replacement for ExcelManager."""

    def __init__(self, db_file: str = "trading_robot.db"):
        self.db_file = os.path.abspath(db_file)
        self._init_db()

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def _init_db(self):
        """Create tables and seed defaults if DB is new."""
        is_new = not os.path.exists(self.db_file)
        con = self._connect()
        try:
            con.executescript(_CREATE_TABLES)
            con.commit()
            if is_new:
                self._seed_defaults(con)
                con.commit()
                logger.info(f"Created new database: {self.db_file}")
            else:
                logger.info(f"Using existing database: {self.db_file}")
        finally:
            con.close()

    def _seed_defaults(self, con: sqlite3.Connection):
        con.executemany(
            "INSERT OR IGNORE INTO config(key, value, description) VALUES (?,?,?)",
            _DEFAULT_CONFIG,
        )
        con.executemany(
            "INSERT OR IGNORE INTO providers(name,type,base_url,api_key,model,temperature,max_tokens,rate_limit,active) VALUES (?,?,?,?,?,?,?,?,?)",
            _DEFAULT_PROVIDERS,
        )
        con.executemany(
            "INSERT OR IGNORE INTO settings(ticker,active,comment) VALUES (?,?,?)",
            _DEFAULT_SETTINGS,
        )
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        con.executemany(
            "INSERT OR IGNORE INTO model_catalog(model_id,name,provider,context_len,input_price,output_price,updated_at) VALUES (?,?,?,?,?,?,?)",
            [(r[0], r[1], r[2], r[3], r[4], r[5], ts) for r in _DEFAULT_CATALOG],
        )
        con.executemany(
            "INSERT OR IGNORE INTO prompt_templates(method, prompt_text, updated_at) VALUES (?,?,?)",
            [(r[0], r[1], ts) for r in _DEFAULT_PROMPT_TEMPLATES],
        )

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_file, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    # ------------------------------------------------------------------
    # Generic read/write (ExcelManager-compatible interface)
    # ------------------------------------------------------------------

    def read_sheet(self, sheet_name: str) -> pd.DataFrame:
        """Read entire table as DataFrame."""
        table = _tbl(sheet_name)
        try:
            with self._connect() as con:
                df = pd.read_sql_query(f"SELECT * FROM {table}", con)
            logger.debug(f"Read {len(df)} rows from '{table}'")
            return df
        except Exception as e:
            logger.error(f"Error reading '{table}': {e}")
            return pd.DataFrame()

    def append_to_sheet(self, sheet_name: str, data) -> bool:
        """Insert one row (dict) or many rows (list[dict] / DataFrame)."""
        table = _tbl(sheet_name)
        try:
            if isinstance(data, dict):
                rows = [data]
            elif isinstance(data, list):
                rows = data
            elif isinstance(data, pd.DataFrame):
                rows = data.to_dict("records")
            else:
                rows = [data]

            if not rows:
                return True

            cols = list(rows[0].keys())
            placeholders = ", ".join(["?"] * len(cols))
            col_str = ", ".join(cols)
            sql = f"INSERT OR IGNORE INTO {table} ({col_str}) VALUES ({placeholders})"

            with self._connect() as con:
                for row in rows:
                    values = [_py_val(row.get(c)) for c in cols]
                    con.execute(sql, values)
            logger.debug(f"Appended {len(rows)} rows to '{table}'")
            return True
        except Exception as e:
            logger.error(f"Error appending to '{table}': {e}")
            return False

    def update_row_by_id(self, sheet_name: str, row_id: Any, update_data: dict) -> bool:
        """Update a row by its 'id' column."""
        table = _tbl(sheet_name)
        try:
            set_parts = ", ".join([f"{k} = ?" for k in update_data.keys()])
            values = [_py_val(v) for v in update_data.values()] + [str(row_id)]
            sql = f"UPDATE {table} SET {set_parts} WHERE id = ?"
            with self._connect() as con:
                con.execute(sql, values)
            return True
        except Exception as e:
            logger.error(f"Error updating row {row_id} in '{table}': {e}")
            return False

    def upsert_row(self, sheet_name: str, data: dict) -> bool:
        """Insert or replace a row."""
        table = _tbl(sheet_name)
        try:
            cols = list(data.keys())
            placeholders = ", ".join(["?"] * len(cols))
            col_str = ", ".join(cols)
            sql = f"INSERT OR REPLACE INTO {table} ({col_str}) VALUES ({placeholders})"
            values = [_py_val(data[c]) for c in cols]
            with self._connect() as con:
                con.execute(sql, values)
            return True
        except Exception as e:
            logger.error(f"Error upserting to '{table}': {e}")
            return False

    def clear_sheet(self, sheet_name: str, keep_headers: bool = True) -> bool:
        """Delete all rows from table (keep_headers is a no-op for SQLite)."""
        table = _tbl(sheet_name)
        try:
            with self._connect() as con:
                con.execute(f"DELETE FROM {table}")
            logger.info(f"Cleared '{table}'")
            return True
        except Exception as e:
            logger.error(f"Error clearing '{table}': {e}")
            return False

    # ------------------------------------------------------------------
    # Domain helpers (ExcelManager-compatible)
    # ------------------------------------------------------------------

    def get_settings(self) -> List[str]:
        """Return list of active ticker symbols."""
        try:
            with self._connect() as con:
                rows = con.execute(
                    "SELECT ticker FROM settings WHERE active = 1"
                ).fetchall()
            return [r["ticker"] for r in rows]
        except Exception as e:
            logger.error(f"Error getting settings: {e}")
            return []

    def get_cached_price_data(self, ticker: str, days: int = 250):
        """Return cached price data as list of dicts (newest first)."""
        try:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            with self._connect() as con:
                rows = con.execute(
                    "SELECT * FROM price_data WHERE ticker=? AND date>=? ORDER BY date DESC",
                    (ticker, cutoff),
                ).fetchall()
            if not rows:
                return None
            result = []
            for r in rows:
                result.append({
                    "ticker": r["ticker"],
                    "date":   datetime.strptime(r["date"], "%Y-%m-%d"),
                    "open":   float(r["open"]),
                    "high":   float(r["high"]),
                    "low":    float(r["low"]),
                    "close":  float(r["close"]),
                    "volume": int(r["volume"]),
                })
            logger.info(f"Loaded {len(result)} cached days for {ticker}")
            return result
        except Exception as e:
            logger.error(f"Error loading cached price data: {e}")
            return None

    def save_price_data(self, price_data: list, ticker: str = None) -> bool:
        """Upsert price data records."""
        if not price_data:
            return True
        try:
            sql = """
                INSERT OR REPLACE INTO price_data (ticker, date, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """
            rows = []
            for rec in price_data:
                date_val = rec.get("date") or rec.get("Date")
                if hasattr(date_val, "strftime"):
                    date_str = date_val.strftime("%Y-%m-%d")
                else:
                    date_str = str(date_val)[:10]
                rec_ticker = rec.get("ticker") or rec.get("Ticker") or ticker or ""
                rows.append((
                    rec_ticker,
                    date_str,
                    float(rec.get("open") or rec.get("Open") or 0),
                    float(rec.get("high") or rec.get("High") or 0),
                    float(rec.get("low")  or rec.get("Low")  or 0),
                    float(rec.get("close") or rec.get("Close") or 0),
                    int(rec.get("volume") or rec.get("Volume") or 0),
                ))
            with self._connect() as con:
                con.executemany(sql, rows)
            logger.info(f"Saved {len(rows)} price records")
            return True
        except Exception as e:
            logger.error(f"Error saving price data: {e}")
            return False

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def get_config_value(self, key: str, default: str = "") -> str:
        """Get a single config value."""
        try:
            with self._connect() as con:
                row = con.execute(
                    "SELECT value FROM config WHERE key=?", (key,)
                ).fetchone()
            return row["value"] if row else default
        except Exception as e:
            logger.error(f"Error reading config key '{key}': {e}")
            return default

    def set_config_value(self, key: str, value: str) -> bool:
        """Set a config value."""
        try:
            with self._connect() as con:
                con.execute(
                    "INSERT OR REPLACE INTO config(key, value) VALUES (?, ?)",
                    (key, value),
                )
            return True
        except Exception as e:
            logger.error(f"Error setting config key '{key}': {e}")
            return False

    # ------------------------------------------------------------------
    # Model catalog
    # ------------------------------------------------------------------

    def get_model_catalog(self, provider: str = None) -> pd.DataFrame:
        """Return model_catalog as DataFrame, optionally filtered by provider."""
        try:
            where = "WHERE provider = ?" if provider else ""
            params = (provider,) if provider else ()
            with self._connect() as con:
                return pd.read_sql_query(
                    f"SELECT * FROM model_catalog {where} ORDER BY provider, name",
                    con, params=params,
                )
        except Exception as e:
            logger.error(f"Error reading model_catalog: {e}")
            return pd.DataFrame()

    def refresh_model_catalog(self, api_key: str) -> int:
        """
        Fetch the full model list from OpenRouter /models and upsert into model_catalog.
        Returns number of models upserted, or -1 on error.
        """
        import requests
        try:
            resp = requests.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            resp.raise_for_status()
            models = resp.json().get("data", [])
            if not models:
                logger.warning("OpenRouter /models returned empty list")
                return 0
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rows = []
            for m in models:
                mid = m.get("id", "")
                if not mid:
                    continue
                pricing = m.get("pricing", {})
                try:
                    inp = float(pricing.get("prompt", 0)) * 1_000_000
                    out = float(pricing.get("completion", 0)) * 1_000_000
                except (TypeError, ValueError):
                    inp = out = 0.0
                ctx = m.get("context_length") or 0
                rows.append((mid, m.get("name", mid),
                             mid.split("/")[0].capitalize(),
                             int(ctx), inp, out, ts))
            with self._connect() as con:
                con.executemany(
                    "INSERT OR REPLACE INTO model_catalog"
                    "(model_id,name,provider,context_len,input_price,output_price,updated_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    rows,
                )
            logger.info(f"Model catalog refreshed: {len(rows)} models")
            return len(rows)
        except Exception as e:
            logger.error(f"Error refreshing model catalog: {e}")
            return -1

    def get_model_ids(self) -> list:
        """Return sorted list of model_id strings from catalog."""
        try:
            with self._connect() as con:
                cur = con.execute(
                    "SELECT model_id FROM model_catalog ORDER BY provider, name"
                )
                return [r[0] for r in cur.fetchall()]
        except Exception as e:
            logger.error(f"Error reading model ids: {e}")
            return []

    # ------------------------------------------------------------------
    # Prompt templates (global, per-method)
    # ------------------------------------------------------------------

    def get_prompt_template(self, method: str) -> Optional[str]:
        """Return the template text for a method, or None if not found."""
        try:
            with self._connect() as con:
                cur = con.execute(
                    "SELECT prompt_text FROM prompt_templates WHERE method=?", (method,)
                )
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error(f"Error reading prompt template for '{method}': {e}")
            return None

    def get_all_prompt_templates(self) -> dict:
        """Return {method: prompt_text} for all methods."""
        try:
            with self._connect() as con:
                cur = con.execute(
                    "SELECT method, prompt_text FROM prompt_templates ORDER BY method"
                )
                return {r[0]: r[1] for r in cur.fetchall()}
        except Exception as e:
            logger.error(f"Error reading prompt templates: {e}")
            return {}

    def save_prompt_template(self, method: str, prompt_text: str) -> bool:
        """Upsert a prompt template for a method."""
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._connect() as con:
                con.execute(
                    "INSERT OR REPLACE INTO prompt_templates(method, prompt_text, updated_at)"
                    " VALUES (?,?,?)",
                    (method, prompt_text, ts),
                )
            return True
        except Exception as e:
            logger.error(f"Error saving prompt template for '{method}': {e}")
            return False

    def reset_prompt_template(self, method: str) -> bool:
        """Reset a prompt template to its built-in default."""
        default = next((r[1] for r in _DEFAULT_PROMPT_TEMPLATES if r[0] == method), None)
        if default is None:
            return False
        return self.save_prompt_template(method, default)

    # ------------------------------------------------------------------
    # Prompt helpers (log)
    # ------------------------------------------------------------------

    def save_prompt(self, ticker: str, method: str, prompt_text: str, date: str = None) -> bool:
        """Save a prompt record."""
        try:
            ts = date or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._connect() as con:
                con.execute(
                    "INSERT INTO prompts(date, ticker, method, prompt_text) VALUES (?,?,?,?)",
                    (ts, ticker, method, prompt_text),
                )
            return True
        except Exception as e:
            logger.error(f"Error saving prompt: {e}")
            return False

    def get_prompts(self, ticker: str = None, method: str = None,
                    date_from: str = None, date_to: str = None,
                    limit: int = 200) -> pd.DataFrame:
        """Fetch prompts with optional filters."""
        try:
            where = []
            params = []
            if ticker:
                where.append("ticker = ?"); params.append(ticker)
            if method:
                where.append("method = ?"); params.append(method)
            if date_from:
                where.append("date >= ?"); params.append(date_from)
            if date_to:
                where.append("date <= ?"); params.append(date_to)
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            sql = f"SELECT * FROM prompts {clause} ORDER BY date DESC LIMIT {limit}"
            with self._connect() as con:
                return pd.read_sql_query(sql, con, params=params)
        except Exception as e:
            logger.error(f"Error reading prompts: {e}")
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # Consensus helper
    # ------------------------------------------------------------------

    def save_consensus(self, data: dict) -> bool:
        cols = list(data.keys())
        placeholders = ", ".join(["?"] * len(cols))
        col_str = ", ".join(cols)
        sql = f"INSERT OR REPLACE INTO consensus ({col_str}) VALUES ({placeholders})"
        try:
            values = [_py_val(data[c]) for c in cols]
            with self._connect() as con:
                con.execute(sql, values)
            return True
        except Exception as e:
            logger.error(f"Error saving consensus: {e}")
            return False

    # ------------------------------------------------------------------
    # Price / Indicator queries
    # ------------------------------------------------------------------

    def get_price_data(self, ticker: str = None, date_from: str = None,
                       date_to: str = None, limit: int = 500) -> pd.DataFrame:
        try:
            where = []
            params = []
            if ticker:
                where.append("ticker = ?"); params.append(ticker)
            if date_from:
                where.append("date >= ?"); params.append(date_from)
            if date_to:
                where.append("date <= ?"); params.append(date_to)
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            sql = f"SELECT * FROM price_data {clause} ORDER BY ticker, date DESC LIMIT {limit}"
            with self._connect() as con:
                return pd.read_sql_query(sql, con, params=params)
        except Exception as e:
            logger.error(f"Error reading price_data: {e}")
            return pd.DataFrame()

    def get_indicators(self, ticker: str = None, limit: int = 200) -> pd.DataFrame:
        try:
            where = []
            params = []
            if ticker:
                where.append("ticker = ?"); params.append(ticker)
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            sql = f"SELECT * FROM indicators {clause} ORDER BY ticker, date DESC LIMIT {limit}"
            with self._connect() as con:
                return pd.read_sql_query(sql, con, params=params)
        except Exception as e:
            logger.error(f"Error reading indicators: {e}")
            return pd.DataFrame()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _py_val(v):
    """Convert numpy/pandas types to plain Python for SQLite."""
    if v is None:
        return None
    try:
        import math
        if isinstance(v, float) and math.isnan(v):
            return None
    except Exception:
        pass
    try:
        import numpy as np
        if isinstance(v, np.integer):
            return int(v)
        if isinstance(v, np.floating):
            return float(v)
        if isinstance(v, np.bool_):
            return bool(v)
    except ImportError:
        pass
    try:
        import pandas as pd
        if pd.isna(v):
            return None
    except Exception:
        pass
    return v
