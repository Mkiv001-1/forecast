"""
Конфигурация торгового робота (legacy constants — используются как fallback).
Основные настройки хранятся в SQLite таблице config и редактируются через GUI.
"""

# Legacy constants kept for backward compatibility.
# Real values are read from SQLiteManager.get_config_value() at runtime.

PPLX_MODEL = 'sonar-pro'
PPLX_TEMPERATURE = 0.2
PPLX_MAX_TOKENS = 2000

DATA_SOURCE = 'yfinance'
ALPHA_VANTAGE_RATE_LIMIT = 5

SPREADSHEET_NAME = 'Trading Robot'
