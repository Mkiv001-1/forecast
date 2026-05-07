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
    ├── scripts/core/              ← бизнес-логика
    │   ├── scheduler.py            ← централизованный планировщик + heartbeat
    │   ├── forecast_runner.py     ← главный цикл
    │   ├── forecast_engine.py     ← промпты → OpenRouter → R/R валидация
    │   ├── multi_model_forecaster.py ← запуск N моделей × M методов
    │   ├── consensus.py           ← медианная агрегация + фильтр аномалий
    │   ├── consensus_evaluator.py ← оценка консенсуса постфактум
    │   ├── consensus_recalc.py    ← ретроспективный пересчет
    │   ├── market_regime.py       ← ADX + MA → режим рынка
    │   ├── indicators.py          ← технические индикаторы
    │   ├── data_loader.py         ← yfinance / Alpha Vantage / Finnhub
    │   ├── sqlite_manager.py      ← ORM-обёртка над SQLite (WAL + WriteQueue)
    │   ├── unified_logs_manager.py ← управление таблицей logs
    │   ├── actuals_evaluator.py   ← оценка по High/Low + приоритет стопа
    │   ├── capital_provider.py    ← источник капитала из IB
    │   ├── position_sizer.py      ← расчет позиции от NetLiquidation
    │   ├── order_manager.py       ← bracket-ордера + атомарность
    │   ├── ib_gateway_client.py   ← IB Gateway: позиции + балансы + спред
    │   ├── circuit_breaker.py     ← защита от сбоев OpenRouter
    │   └── model_performance_tracker.py ← EMA-веса моделей
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

### IB Gateway интеграция
**Решение:** `ib_gateway_client.py` — sync-обёртка над `ib_insync` для получения позиций и балансов из Interactive Brokers.  
**Подключение:** IB Gateway (localhost:7497 для paper, :7496 для live) по протоколу TWS API.  
**Функции:**
- `fetch_ib_accounts()` — балансы счетов (NetLiquidation, BuyingPower, AvailableFunds)
- `fetch_ib_positions()` — позиции портфеля (quantity, avg_cost, market_value, unrealized_pnl)
- `test_ib_connection()` — диагностика подключения с детальными логами
- Асинхронные обёртки для FastAPI (`*_async`) с изоляцией event loop через threads

**Хранение:** таблицы `accounts` и `portfolio` в SQLite, поле `type` = 'paper' или 'live'.  
**GUI:** вкладка "IB" для теста подключения и ручной синхронизации.

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
| `accounts` | Счета IB (балансы, buying power, тип paper/live) |
| `portfolio` | Позиции IB (количество, стоимость, unrealized PnL) |
| `ib_config` | Настройки подключения к IB Gateway |
| `orders` | Ордера: Entry, Take Profit, Stop Loss; полный жизненный цикл |
| `scheduled_tasks` | Реестр задач планировщика |
| `method_config` | Параметры методов: timeframe_hours, execute |
| `heartbeat_log` | Служебные записи для проверки SQLite |
| `forecast_runs` | Аудит запусков прогнозирования |
| `forecast_run_links` | Связь прогнозов с весами |

---

## REST API (FastAPI, порт 8000)

Аутентификация: заголовок `X-API-Key`.  
Ключевые группы эндпоинтов: `/run/*`, `/logs`, `/indicators`, `/consensus`, `/tickers`, `/providers`, `/config`, `/prompt-templates`, `/ib/*` (test-connection, sync, accounts, portfolio, config).

---

## Бизнес-логика

### Основной рабочий цикл

```
1. Загрузка тикеров → 2. Получение данных → 3. Расчёт индикаторов
         ↓
4. Определение режима рынка → 5. Генерация прогнозов (N моделей × M методов)
         ↓
6. Агрегация консенсуса → 7. Сохранение в logs → 8. Оценка результатов (через 3ч)
```

### Компоненты бизнес-логики

**`main_excel.py`** — оркестратор процесса
- Загружает активные тикеры из `settings`
- Последовательно вызывает: `data_loader` → `indicators` → `market_regime` → `multi_model_forecaster`
- Сохраняет результаты в `logs` и `consensus`

**`forecast_engine.py`** — генерация прогнозов
- Формирует промпт по шаблону метода + данные тикера
- Отправляет в OpenRouter с таймаутом и retry-логикой
- Парсит ответ: извлекает `action` (LONG/SHORT/HOLD), `target_price`, `confidence`, `reasoning`
- Обрабатывает ошибки модели (fallback на HOLD при невалидном ответе)

**`multi_model_forecaster.py`** — масштабирование
- Для каждого активного провайдера (модели) запускает все активные методы анализа
- Параллельные запросы с `asyncio.gather()` и семафором (лимит конкурентности)
- Результат: список сигналов с метаданными модели и метода

**`consensus.py`** — агрегация сигналов
- Группирует сигналы по тикеру
- Взвешенное голосование: `confidence` × `model_reliability_weight`
- Финальный сигнал: доминирующее направление с усреднённой целью

**`market_regime.py`** — адаптация к рынку
- ADX > 25 + цена > MA → трендовые методы (`momentum_trend`, `breakout`)
- ADX > 25 + цена < MA → трендовые методы с акцентом на SHORT
- ADX < 25 + выравнивание MA → контртрендовые (`mean_reversion`, `range_trading`)

**`actuals_evaluator.py`** — обратная связь
- Периодически (каждые 10 мин) проверяет записи `logs` со статусом `NEW` и возрастом > 3 часов
- Загружает фактические цены через `data_loader`
- Рассчитывает: PnL (фактическая прибыль/убыток), точность направления, отклонение цели
- Обновляет запись: статус → `EVALUATED`, сохраняет метрики

### Правила принятия решений

| Условие | Действие |
|---------|----------|
| Консенсус confidence < 0.6 | Отклонить сигнал (не сохранять в logs) |
| Все модели дают HOLD | Нет сигнала, запись не создаётся |
| Разнонаправленные сигналы (LONG vs SHORT) | Выбирается направление с большей суммарной уверенностью |
| PnL оценки < -2% после 3ч | Помечается как неудачный прогноз (для аналитики) |

### Поток данных

```
Yahoo Finance / Alpha Vantage / Finnhub
              ↓
         price_data (SQLite)
              ↓
    indicators.py → indicators (SQLite)
              ↓
    market_regime.py + forecast_engine.py
              ↓
         logs (SQLite) + consensus (SQLite)
              ↓
    actuals_evaluator.py (через 3ч)
              ↓
    Оценка точности → обновление logs
```

---

## Известные нюансы и ограничения

- `~$trading_robot.xlsx` — временный файл Excel (Lock-файл Office), не коммитится
- `.client.pid` / `.server.pid` — PID-файлы для управления процессами через bat-скрипты
- `venv_client/` и `venv_server/` — раздельные виртуальные окружения для клиента и сервера

---

## Ключевые архитектурные принципы

**Атомарность ордеров.** Bracket-группа неделима. При частичном исполнении Entry сначала отменяется остаток лимитной заявки — только потом выставляется закрывающий Market-ордер. Последовательность строго обязательна.

**Приоритет стопа при оценке.** Если за интервал оценки цена касалась и `stop_loss`, и `target_price`, результат всегда фиксируется как убыток. Это инвариант, не конфигурируемый через `config`.

**EMA быстрее реагирует на деградацию.** Веса провайдеров рассчитываются через EMA с α=0.2, а не скользящее среднее, чтобы система адаптировалась к внешним изменениям модели быстрее, чем за 30 дней.

**Heartbeat как защита от «тихих» сбоев.** Потеря связи с IB без активных ордеров незаметна. Heartbeat обнаруживает деградацию за 30 секунд и логирует состояние в `heartbeat_log`. Для QUEUED-ордеров автоматический переход в PAUSED и обратно не реализован — ордера остаются в QUEUED до истечения `ORDER_QUEUE_MAX_AGE_HOURS`.

**Единственный источник капитала.** `NetLiquidation` из IB — единственная база для расчётов. `BuyingPower` не используется как основа.

**`MANUAL_INTERVENTION_REQUIRED` — единственный сценарий для уведомлений.** Все остальные ошибки система обрабатывает автоматически. Уведомление отправляется только когда на счету есть неуправляемый риск (орфан-позиция).

---

## Технический долг

| Дата | Пункт | Статус |
|------|-------|--------|
| 2026-05-06 | **Google Sheets (`gspread`) удалён** — legacy-зависимость с устаревшей oauth2client; код (`save_price_data_to_sheet`) и документация очищены | ✅ Выполнено |

1. **PAUSED статус для ордеров.** Не реализована логика: (a) перевод QUEUED в PAUSED при потере связи с IB, (b) автоматический resubmit в QUEUED при восстановлении, (c) проверка актуальности цен перед resubmit после длительной паузы. Текущее поведение: QUEUED-ордера expire по `ORDER_QUEUE_MAX_AGE_HOURS` (задача `expire_queued_orders` в scheduler.py), ручной resubmit через API `/orders/submit`.

---
