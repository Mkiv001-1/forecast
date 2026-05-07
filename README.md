# Trading Robot - Python версия

Торговый робот на Python с клиент-серверной архитектурой, генерирующий торговые прогнозы с использованием AI-моделей через OpenRouter.

## Структура проекта

```
forecast/
├── scripts/
│   ├── core/                 # Базовый функционал
│   │   ├── forecast_runner.py      # Основной цикл торгового робота
│   │   ├── forecast_engine.py      # Генерация прогнозов через AI + R/R валидация
│   │   ├── multi_model_forecaster.py # Мульти-модельный прогноз (N моделей × M методов)
│   │   ├── consensus.py            # Консенсус-агрегация + медиана + фильтр аномалий
│   │   ├── consensus_evaluator.py  # Оценка консенсус-прогнозов постфактум
│   │   ├── consensus_recalc.py   # Ретроспективный пересчет консенсуса
│   │   ├── market_regime.py      # Детекция рыночного режима (ADX + MA)
│   │   ├── indicators.py         # Технические индикаторы (RSI, MACD, BB, ATR, ADX, OBV, Stoch RSI)
│   │   ├── data_loader.py        # Загрузка исторических данных (yfinance, Alpha Vantage, Finnhub)
│   │   ├── sqlite_manager.py     # SQLite хранилище (WAL-режим)
│   │   ├── unified_logs_manager.py # Управление таблицей logs
│   │   ├── actuals_evaluator.py  # Оценка прошлых прогнозов по High/Low
│   │   ├── scheduler.py          # Централизованный планировщик задач + heartbeat
│   │   ├── capital_provider.py   # Источник капитала из IB
│   │   ├── position_sizer.py     # Расчет размера позиции (risk-based)
│   │   ├── order_manager.py      # Bracket-ордера + атомарность + откат
│   │   ├── ib_gateway_client.py # Интеграция с Interactive Brokers
│   │   ├── circuit_breaker.py    # Защита от сбоев OpenRouter
│   │   └── model_performance_tracker.py # EMA-веса моделей
│   │
│   ├── server/               # FastAPI сервер
│   │   ├── api.py            # REST API эндпоинты
│   │   ├── config.py         # Конфигурация сервера
│   │   ├── robot.py          # Фоновый запуск робота
│   │   └── main.py           # Точка входа сервера
│   │
│   ├── client/               # GUI клиент (PyQt6)
│   │   ├── main.py           # Точка входа клиента
│   │   ├── gui_main.py       # Главное окно приложения
│   │   ├── api_client.py     # Клиент для API
│   │   └── config.py         # Конфигурация клиента
│   │
│   └── shared/               # Общие модели данных
│       └── models.py
│
├── scripts/server/ini/       # Конфигурации сервера
│   ├── server_config.ini.example
│   └── server_config.ini
│
├── scripts/client/ini/       # Конфигурации клиента
│   ├── client_config.ini.example
│   └── client_config.ini
│
├── trading_robot.db          # SQLite база данных
├── README.md                 # Этот файл
├── run_server.bat            # Запуск сервера (Windows)
├── run_client.bat            # Запуск клиента (Windows)
├── requirements_server.txt   # Зависимости сервера
└── requirements_client.txt   # Зависимости клиента
```

## Обзор системы

Система состоит из двух компонентов:

1. **Сервер (FastAPI)** — предоставляет REST API для управления торговым роботом, хранит данные в SQLite, генерирует прогнозы.
2. **Клиент (PyQt6 GUI)** — графический интерфейс для мониторинга и управления роботом через API.

## Функционал

- **Загрузка данных**: Исторические цены через yfinance / Alpha Vantage / Finnhub
- **Технический анализ**: MA, RSI, MACD, ATR, Bollinger Bands, ADX, OBV, Stoch RSI
- **AI-прогнозы**: 6 методов анализа через OpenRouter (Claude, GPT, Gemini, DeepSeek и др.)
- **Хранение данных**: SQLite для всех результатов
- **Мульти-модельный подход**: Комбинирование прогнозов нескольких AI-моделей
- **Консенсус-алгоритм**: Взвешенная агрегация сигналов
- **Оценка результатов**: Автоматическая оценка точности прошлых прогнозов и консенсуса
- **Риск-менеджмент**: Стоп-лосс, R/R-фильтр, расчет позиции от NetLiquidation
- **Исполнение ордеров**: Bracket-ордера (Entry + Take Profit + Stop Loss) в IB
- **Мониторинг**: Circuit breaker, heartbeat, планировщик задач

## Методы анализа (6 AI-методов)

Каждый метод анализирует рынок с уникального ракурса и имеет свой горизонт оценки. AI-модель получает специализированные индикаторы для каждого метода.

| Метод | Описание | Ключевые индикаторы | Горизонт (часов) | Триггер оценки |
|-------|----------|---------------------|------------------|---------------|
| `momentum_trend` | Тренд и импульс | MA20/50/200, EMA9/21, ADX, MACD, RSI, OBV | 24 | `both` |
| `price_action` | Позиция в Bollinger Bands | BB upper/lower, Stoch RSI, ценовая динамика 5д/20д | 8 | `price_level` |
| `relative_strength` | Относительная сила | RSI, ADX, динамика 5/10/20/50д, объемный коэффициент | 48 | `time` |
| `volatility` | Волатильность и пробои | ATR, BB ширина, RSI, ADX | 4 | `price_level` |
| `mean_reversion` | Возврат к среднему | Отклонение от MA20/50, RSI, Stoch RSI | 72 | `price_level` |
| `volume_breakout` | Объемный импульс | Объем (текущий vs средний), OBV тренд, ATR, ADX | 2 | `price_level` |

### Детали методов

**Momentum Trend** (`momentum_trend`, 24ч)
- Выравнивание скользящих средних (MA20 vs MA50 vs MA200)
- ADX для определения силы тренда (>25 = сильный)
- MACD histogram для импульса
- Рекомендуется для трендовых рынков

**Price Action** (`price_action`, 8ч)
- Позиция цены внутри Bollinger Bands
- Stochastic RSI для перекупленности/перепроданности
- Свечные паттерны и уровни поддержки/сопротивления
- Короткий горизонт — подходит для быстрых входов

**Relative Strength** (`relative_strength`, 48ч)
- Сравнение динамики актива с рынком (SPY)
- Объемный анализ (текущий объем vs 20-дневный средний)
- Долгосрочная перспектива (2 дня)

**Volatility Breakout** (`volatility`, 4ч)
- ATR как процент от цены (волатильность)
- Ширина Bollinger Bands
- Пробой волатильностного конверта
- Очень короткий горизонт — скальпинг

**Mean Reversion** (`mean_reversion`, 72ч)
- Отклонение цены от MA20/MA50 в процентах
- Дивергенции RSI
- Долгий горизонт — позиционная торговля

**Volume Breakout** (`volume_breakout`, 2ч)
- Аномальный объем (коэффициент к среднему)
- OBV тренд (накопление/распределение)
- Самый короткий горизонт — импульсный вход

## Обработка прогнозов и консенсус

### Поток данных

```
┌─────────────────────────────────────────────────────────────────┐
│  1. ГЕНЕРАЦИЯ ПРОГНОЗОВ (N моделей × M методов)                  │
│     ├── Каждая AI-модель получает все 6 методов                │
│     ├── Индикаторы подбираются под метод (специализация)       │
│     └── Результат: JSON с confidence, side, target, stop, TIF │
├─────────────────────────────────────────────────────────────────┤
│  2. ВАЛИДАЦИЯ ОТДЕЛЬНЫХ ПРОГНОЗОВ                              │
│     ├── R/R фильтр: минимум 1.5 (target - entry) / (entry - stop)│
│     ├── Логичность стопа (для LONG: стоп < цены)               │
│     └── Отсеивание с confidence < 50%                          │
├─────────────────────────────────────────────────────────────────┤
│  3. АГРЕГАЦИЯ В КОНСЕНСУС                                       │
│     ├── Группировка по тикеру                                  │
│     ├── Фильтр аномалий: отклонение target > 15% от цены      │
│     ├── Калибровка confidence: raw × (ema_accuracy / 0.5)     │
│     ├── Вес прогноза: calibrated_confidence × win_rate × ema  │
│     └── Expected Value: (confidence/100) × (reward/risk)      │
├─────────────────────────────────────────────────────────────────┤
│  4. РАСЧЕТ ИТОГОВОГО СИГНАЛА                                   │
│     ├── Медиана target_price среди LONG/SHORT                  │
│     ├── Медиана stop_loss среди того же направления            │
│     ├── Если 40%+ веса на противоположном направлении → NEUTRAL│
│     └── Expected Value < 0.5 → сигнал отклоняется (NEUTRAL)    │
└─────────────────────────────────────────────────────────────────┘
```

### Алгоритм консенсуса

**Шаг 1: Фильтрация прогнозов**
- `CONSENSUS_MAX_DEVIATION` = 15% — отсеиваем галлюцинации LLM (target далеко от текущей цены)
- Проверка execute-флагов: метод и провайдер должны иметь `execute='yes'`

**Шаг 2: Калибровка уверенности**
```
calibration_factor = ema_accuracy / 0.5  # baseline 50%
calibrated_confidence = raw_confidence × calibration_factor
# ограничение: 0.5 ≤ calibration_factor ≤ 1.5
```
Пример: модель с ema_accuracy=0.7 корректирует 60% → 84%

**Шаг 3: Расчет веса**
```
final_weight = calibrated_confidence × win_rate × ema_accuracy
```
- `win_rate` — историческая точность метода
- `ema_accuracy` — текущая точность модели (EMA с α=0.2)

**Шаг 4: Expected Value фильтр**
```
expected_r = (confidence / 100) × (reward / risk)
```
- Если `expected_r < 0.5` → сигнал превращается в NEUTRAL
- 70% × R/R 3.0 = 2.1 → ✅ LONG
- 60% × R/R 0.3 = 0.18 → ❌ NEUTRAL

**Шаг 5: Агрегация уровней**
- Итоговый `target_price` — медиана всех target доминирующего направления
- Итоговый `stop_loss` — медиана всех stop того же направления
- `horizon_hours` — медиана timeframe_hours методов в консенсусе

**Шаг 6: Проверка разногласий**
- Если >40% суммарного веса приходится на альтернативное направление → `high_model_disagreement=true` → сигнал NEUTRAL

### Сохранение и оценка

**Forecast Run Tracking**
- Каждый запуск прогнозирования получает `run_id`
- Все прогнозы сохраняются в `forecast_run_links` с полным snapshot весов
- Позволяет анализировать постфактум: какие методы давали лучшие веса

**Оценка постфактум**
- `eval_target_date = consensus_date + horizon_hours`
- При достижении даты: проверяем `price_data` за eval_target_date
- `target_hit` — High >= target (LONG) или Low <= target (SHORT)
- `stop_hit` — Low <= stop (LONG) или High >= stop (SHORT)
- **Приоритет стопа**: если оба уровня достигнуты в один день — засчитывается stop (консервативная оценка)
- Расчет: `pnl_pct`, `r_multiple`, `direction_correct`

## Детекция рыночного режима

На основе ADX и выравнивания скользящих средних определяется текущий режим:
- **STRONG_UPTREND** — сильный бычий тренд (используются momentum, relative_strength, volume_breakout)
- **STRONG_DOWNTREND** — сильный медвежий тренд (momentum, relative_strength)
- **WEAK_TREND** — слабый тренд (все методы)
- **RANGING** — боковик (mean_reversion, price_action, volatility)

## Установка и настройка

### 1. Клонирование репозитория

```bash
cd D:\Git\forecast
```

### 2. Запуск сервера

```bash
# Windows
run_server.bat

# Или вручную
cd scripts\server
python main.py
```

Сервер запустится на `http://0.0.0.0:8000`. API ключ будет сгенерирован автоматически и сохранён в `server_config.ini`.

### 3. Запуск клиента

```bash
# Windows
run_client.bat
```

### 4. Конфигурация (INI-файлы)

**Сервер** (`scripts/server/ini/server_config.ini`):
```ini
[server]
host = 0.0.0.0
port = 8000

[data]
excel_file = trading_robot.db

[security]
api_key = ваш-api-ключ
```

**Клиент** (`scripts/client/ini/client_config.ini`):
```ini
[server]
url = http://localhost:8000
api_key = ваш-api-ключ
```

### 5. Настройка провайдеров AI

По умолчанию в базе уже настроены несколько моделей через OpenRouter. Для добавления своего API ключа:

1. Зарегистрируйтесь на [OpenRouter](https://openrouter.ai)
2. Получите API ключ
3. Укажите его в `Config` → `OPENROUTER_API_KEY` через API или БД

```bash
# Через API
curl -X PUT http://localhost:8000/config/OPENROUTER_API_KEY \
  -H "X-API-Key: ваш-api-ключ" \
  -d '{"key": "OPENROUTER_API_KEY", "value": "ваш-openrouter-ключ"}'
```

## Использование

### Запуск через API

```bash
# Запуск прогнозирования
curl -X POST http://localhost:8000/run/forecast \
  -H "X-API-Key: ваш-api-ключ"

# Запуск оценки прошлых прогнозов
curl -X POST http://localhost:8000/run/evaluate \
  -H "X-API-Key: ваш-api-ключ"

# Полный цикл (оценка + прогноз)
curl -X POST http://localhost:8000/run/full \
  -H "X-API-Key: ваш-api-ключ"

# Статус выполнения
curl http://localhost:8000/run/status \
  -H "X-API-Key: ваш-api-ключ"
```

### Запуск через командную строку (legacy)

```bash
# Инициализация базы данных
python scripts/core/forecast_runner.py --init

# Тестовый запуск на одном тикере
python scripts/core/forecast_runner.py --test NASDAQ:NVDA

# Обычный запуск
python scripts/core/forecast_runner.py

# Оценка предыдущих прогнозов
python scripts/core/forecast_runner.py --evaluate

# Очистка данных
python scripts/core/forecast_runner.py --clear
```

### Использование через GUI клиент

1. Запустите сервер (`run_server.bat`)
2. Запустите клиент (`run_client.bat`)
3. В интерфейсе:
   - Настройте тикеры во вкладке Settings
   - Запустите прогноз кнопкой «Run Forecast»
   - Просматривайте результаты в логах и статистике

## REST API Эндпоинты

### Системные
- `GET /health` — проверка здоровья сервера + circuit breaker
- `GET /system-log` — чтение системного лога
- `GET /scheduler/status` — статус планировщика
- `GET /circuit-breaker/status` — статус circuit breaker
- `POST /circuit-breaker/reset` — сброс circuit breaker

### Прогнозы
- `POST /run/forecast` — запуск прогнозирования
- `POST /run/evaluate` — оценка прошлых прогнозов
- `POST /run/full` — полный цикл
- `GET /run/status` — статус выполнения

### Forecast Runs (аудит весов)
- `GET /forecast-runs` — список запусков прогнозирования
- `GET /forecast-runs/{id}` — детали запуска со всеми весами

### Данные
- `GET /logs` — список прогнозов (Logs)
- `GET /indicators` — технические индикаторы
- `GET /price-data` — исторические цены
- `GET /consensus` — консенсус-прогнозы
- `POST /consensus/evaluate` — оценить консенсус-записи
- `POST /consensus/recalculate` — пересчитать консенсус ретроспективно

### Настройки
- `GET /tickers` — список тикеров
- `POST /tickers` — добавить тикер
- `PUT /tickers/{ticker}` — обновить тикер
- `DELETE /tickers/{ticker}` — удалить тикер

### AI Модели
- `GET /providers` — список провайдеров
- `PUT /providers/{name}` — обновить провайдера
- `PUT /providers/{name}/execute` — обновить execute флаг
- `GET /model-catalog` — каталог моделей OpenRouter
- `POST /model-catalog/refresh` — обновить список моделей
- `GET /method-config/{method}` — получить конфиг метода
- `PUT /method-config/{method}/execute` — обновить execute флаг метода

### Конфигурация
- `GET /config` — все параметры
- `PUT /config/{key}` — обновить параметр

### Capital & Orders
- `GET /capital` — текущий капитал и источник
- `POST /orders/submit` — выставить ордер по консенсусу
- `GET /orders` — список ордеров
- `POST /orders/{id}/cancel` — отменить ордер

### Prompt Templates
- `GET /prompt-templates` — все шаблоны
- `PUT /prompt-templates/{method}` — сохранить шаблон
- `POST /prompt-templates/{method}/reset` — сбросить шаблон

### IB Gateway
- `POST /ib/test-connection` — тест подключения к IB
- `POST /ib/sync-accounts` — синхронизировать счета
- `POST /ib/sync-portfolio` — синхронизировать позиции
- `GET /ib/accounts` — список счетов
- `GET /ib/portfolio` — позиции портфеля

## Структура базы данных SQLite

### Основные таблицы
- **settings** — список тикеров (ticker, active, comment, sector, trading_blocked)
- **price_data** — исторические цены (ticker, date, open, high, low, close, volume)
- **indicators** — рассчитанные индикаторы
- **logs** — единая таблица прогнозов и оценок (включая stop_loss, R/R, bracket-поля)
- **consensus** — консенсус-прогнозы с полями оценки (target_hit, stop_hit, pnl_pct, r_multiple)
- **config** — параметры конфигурации
- **providers** — настройки AI-провайдеров (ema_accuracy, ema_updated_at, execute)
- **method_config** — параметры методов анализа (timeframe_hours, execute)
- **prompts** — сохраненные промпты
- **prompt_templates** — шаблоны промптов по методам

### IB Integration
- **accounts** — счета IB (net_liquidation, buying_power, available_funds, type)
- **portfolio** — позиции IB (quantity, market_value, unrealized_pnl, type)
- **ib_config** — настройки подключения к IB Gateway

### Orders & Execution
- **orders** — ордера (bracket-группы: entry, take_profit, stop_loss; статусы, execution_latency_ms)

### Audit & Tracking
- **forecast_runs** — аудит запусков прогнозирования
- **forecast_run_links** — связь прогнозов с весами (raw_confidence, win_rate, ema_accuracy, final_weight)
- **scheduled_tasks** — задачи планировщика
- **heartbeat_log** — служебные записи для проверки SQLite

## Workflow

1. Робот загружает список активных тикеров из таблицы `settings`
2. Для каждого тикера:
   - Загружает исторические цены (250 дней)
   - Рассчитывает технические индикаторы
   - Определяет рыночный режим (ADX + MA)
   - Выбирает подходящие методы анализа для режима
   - Для каждого метода × каждой активной AI-модели:
     - Строит промпт с техническими данными
     - Отправляет запрос в OpenRouter
     - Парсит JSON-ответ с прогнозом
     - Сохраняет в таблицу `logs`
3. Рассчитывает консенсус для каждого тикера
4. Сохраняет консенсус в таблицу `consensus`

## Оценка прогнозов (Backtesting)

Процесс `evaluate`:
1. Находит прогнозы со статусом NEW, возрастом > 3 часов
2. Загружает фактические цены на дату прогноза
3. Сравнивает прогноз (LONG/SHORT) с реальным движением
4. Вычисляет PnL и точность направления
5. Обновляет запись статусом EVALUATED

## Зависимости

**Основные:**
- `yfinance`, `alpha_vantage`, `finnhub` — загрузка цен
- `pandas`, `numpy` — обработка данных
- `requests` — HTTP-запросы
- `openai` (через OpenRouter) — AI API

**Сервер:**
- `fastapi`, `uvicorn` — веб-фреймворк
- `sqlalchemy`, `pandas` — БД

**Клиент:**
- `PyQt6` — GUI
- `requests` — API клиент

## Справочник настроек (таблица `config`)

| Ключ | Дефолт | Описание |
|---|---|---|
| `ORDER_MODE` | `disabled` | Режим ордеров: `disabled` / `paper` / `live` |
| `LIVE_TRADING_CONFIRMED` | `false` | Явное подтверждение live-торговли |
| `DEFAULT_RISK_PCT` | 0.01 | Риск на сделку (1% капитала) |
| `MAX_POSITION_PCT` | 0.05 | Максимальная доля одной позиции (5%) |
| `MAX_SECTOR_EXPOSURE_PCT` | 0.15 | Мягкий лимит секторной экспозиции |
| `MAX_SECTOR_HARD_LIMIT_PCT` | 0.25 | Жёсткий лимит — отклонение сигнала |
| `CAPITAL_STALENESS_MINUTES` | 15 | Порог устаревания данных IB |
| `MAX_SPREAD_PCT` | 0.005 | Максимальный допустимый спред (Slippage Guard) |
| `MAX_OPEN_ORDERS` | 5 | Лимит активных ордеров |
| `CONSENSUS_MAX_DEVIATION` | 0.15 | Максимальное отклонение target от цены |
| `MODEL_WEIGHT_EMA_ALPHA` | 0.2 | Коэффициент сглаживания EMA весов |
| `FORECAST_INTERVAL_MINUTES` | 60 | Интервал основного цикла |

## Логирование

Логи пишутся в `trading_robot.log` и выводятся в консоль.

## Разработка

### Добавление нового метода анализа

1. Добавить метод в `_METHOD_HORIZON` в `multi_model_forecaster.py`
2. Добавить инструкции в `get_method_instructions()` в `forecast_engine.py`
3. Добавить метод в `_METHODS_BY_REGIME` в `market_regime.py`
4. Добавить шаблон в `_DEFAULT_PROMPT_TEMPLATES` в `sqlite_manager.py`

## Лицензия

MIT