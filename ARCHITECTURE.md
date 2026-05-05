# Architecture & Technical Decisions

Живой документ. Обновляется при каждом значимом архитектурном решении.
Цель: дать следующей сессии контекст «почему так, а не иначе».

---

## Общая архитектура

```
Client (PyQt6 GUI)
    │  HTTP / REST
    ▼
Server (FastAPI, port 8000)
    │
    ├── scripts/core/          ← бизнес-логика
    │   ├── main_excel.py      ← главный цикл
    │   ├── forecast_engine.py ← промпты → OpenRouter → парсинг
    │   ├── multi_model_forecaster.py ← запуск N моделей × M методов
    │   ├── consensus.py       ← агрегация сигналов
    │   ├── market_regime.py   ← ADX + MA → режим рынка
    │   ├── indicators.py      ← технические индикаторы
    │   ├── data_loader.py     ← yfinance / Alpha Vantage / Finnhub
    │   ├── sqlite_manager.py  ← ORM-обёртка над SQLite
    │   ├── unified_logs_manager.py ← управление таблицей logs
    │   └── actuals_evaluator.py    ← оценка прошлых прогнозов
    │
    └── trading_robot.db       ← SQLite (единое хранилище)
```

---

## Ключевые решения

### SQLite вместо Excel
**Решение:** основное хранилище — `trading_robot.db` (SQLite), Excel используется только для экспорта/отчётов.  
**Причина:** Excel не поддерживает параллельный доступ; SQLite надёжнее при фоновых запусках сервера.

### OpenRouter как единая точка доступа к AI
**Решение:** все AI-запросы идут через OpenRouter (`openai`-совместимый API).  
**Причина:** позволяет переключать модели (Claude, GPT-4, Gemini, DeepSeek и др.) без изменения кода.  
**Конфиг:** ключ хранится в таблице `config` (ключ `OPENROUTER_API_KEY`), не в `.env`.

### Мульти-модельный подход (N моделей × M методов)
**Решение:** `multi_model_forecaster.py` запускает каждый метод анализа на каждой активной AI-модели.  
**Методы:** `momentum_trend`, `price_action`, `relative_strength`, `volatility`, `mean_reversion`, `volume_breakout`.  
**Консенсус:** взвешенная агрегация в `consensus.py`, результат в таблице `consensus`.

### Детекция рыночного режима
**Решение:** `market_regime.py` определяет режим по ADX + выравниванию MA → выбирает подмножество методов.  
**Режимы:** `STRONG_UPTREND`, `STRONG_DOWNTREND`, `WEAK_TREND`, `RANGING`.

### Оценка прогнозов (статусы записей logs)
**Решение:** записи в `logs` имеют статус `NEW` → после оценки становятся `EVALUATED`.  
**Логика:** `actuals_evaluator.py` берёт записи `NEW` старше 3 часов, загружает фактические цены, считает PnL и точность направления.

### Prompt Templates
**Решение:** шаблоны промптов хранятся в таблице `prompt_templates` (SQLite), редактируются через API.  
**Дефолты:** `_DEFAULT_PROMPT_TEMPLATES` в `sqlite_manager.py`.

---

## Таблицы SQLite

| Таблица | Назначение |
|---|---|
| `settings` | Тикеры (ticker, active, comment) |
| `price_data` | Исторические OHLCV (250 дней) |
| `indicators` | Рассчитанные техиндикаторы |
| `logs` | Все прогнозы + оценки (статус NEW/EVALUATED) |
| `consensus` | Агрегированные консенсус-прогнозы |
| `config` | Параметры конфигурации (ключи AI, настройки) |
| `providers` | Настройки AI-провайдеров |
| `prompts` | Сохранённые промпты |
| `model_catalog` | Каталог моделей OpenRouter |
| `prompt_templates` | Шаблоны промптов по методам |

---

## REST API (FastAPI, порт 8000)

Аутентификация: заголовок `X-API-Key`.  
Ключевые группы эндпоинтов: `/run/*`, `/logs`, `/indicators`, `/consensus`, `/tickers`, `/providers`, `/config`, `/prompt-templates`.

---

## Известные нюансы и ограничения

- `~$trading_robot.xlsx` — временный файл Excel (Lock-файл Office), не коммитится
- `.client.pid` / `.server.pid` — PID-файлы для управления процессами через bat-скрипты
- `venv_client/` и `venv_server/` — раздельные виртуальные окружения для клиента и сервера
- Google Sheets (`gspread`) — legacy-зависимость, активно не используется

---

## Технический долг

*(Заполняется по мере обнаружения)*

---
