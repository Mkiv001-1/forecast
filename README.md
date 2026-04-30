# Trading Robot - Python версия

Торговый робот на Python с клиент-серверной архитектурой, генерирующий торговые прогнозы с использованием AI-моделей через OpenRouter.

## Структура проекта

```
forecast/
├── scripts/
│   ├── core/                 # Базовый функционал
│   │   ├── main_excel.py     # Основной цикл торгового робота
│   │   ├── indicators.py     # Технические индикаторы (RSI, MACD, Bollinger Bands и др.)
│   │   ├── forecast_engine.py # Генерация прогнозов через AI
│   │   ├── multi_model_forecaster.py # Мульти-модельный прогноз
│   │   ├── consensus.py      # Консенсус-агрегация прогнозов
│   │   ├── market_regime.py  # Детекция рыночного режима
│   │   ├── market_context.py # Контекст рынка
│   │   ├── data_loader.py    # Загрузка исторических данных
│   │   ├── sqlite_manager.py # SQLite хранилище (замена Excel)
│   │   ├── unified_logs_manager.py # Управление таблицей Logs
│   │   ├── actuals_evaluator.py # Оценка прошлых прогнозов
│   │   └── ...
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
- **Оценка результатов**: Автоматическая оценка точности прошлых прогнозов

## Методы анализа (6 AI-моделей)

1. `momentum_trend` — Анализ тренда и импульса
2. `price_action` — Анализ позиции в Bollinger Bands
3. `relative_strength` — Сравнение с рынком
4. `volatility` — Оценка волатильности по ATR
5. `mean_reversion` — Анализ отклонения от среднего
6. `volume_breakout` — Анализ объемов и пробоев

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
python scripts/core/main_excel.py --init

# Тестовый запуск на одном тикере
python scripts/core/main_excel.py --test NASDAQ:NVDA

# Обычный запуск
python scripts/core/main_excel.py

# Оценка предыдущих прогнозов
python scripts/core/main_excel.py --evaluate

# Очистка данных
python scripts/core/main_excel.py --clear
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
- `GET /health` — проверка здоровья сервера
- `GET /system-log` — чтение системного лога

### Прогнозы
- `POST /run/forecast` — запуск прогнозирования
- `POST /run/evaluate` — оценка прошлых прогнозов
- `POST /run/full` — полный цикл
- `GET /run/status` — статус выполнения

### Данные
- `GET /logs` — список прогнозов (Logs)
- `GET /indicators` — технические индикаторы
- `GET /price-data` — исторические цены
- `GET /consensus` — консенсус-прогнозы

### Настройки
- `GET /tickers` — список тикеров
- `POST /tickers` — добавить тикер
- `PUT /tickers/{ticker}` — обновить тикер
- `DELETE /tickers/{ticker}` — удалить тикер

### AI Модели
- `GET /providers` — список провайдеров
- `PUT /providers/{name}` — обновить провайдера
- `GET /model-catalog` — каталог моделей OpenRouter
- `POST /model-catalog/refresh` — обновить список моделей

### Конфигурация
- `GET /config` — все параметры
- `PUT /config/{key}` — обновить параметр

### Prompt Templates
- `GET /prompt-templates` — все шаблоны
- `PUT /prompt-templates/{method}` — сохранить шаблон
- `POST /prompt-templates/{method}/reset` — сбросить шаблон

## Структура базы данных SQLite

- **settings** — список тикеров (ticker, active, comment)
- **price_data** — исторические цены (ticker, date, open, high, low, close, volume)
- **indicators** — рассчитанные индикаторы
- **logs** — единая таблица прогнозов и оценок
- **consensus** — консенсус-прогнозы
- **config** — параметры конфигурации
- **providers** — настройки AI-провайдеров
- **prompts** — сохраненные промпты
- **model_catalog** — каталог моделей OpenRouter
- **prompt_templates** — шаблоны промптов по методам

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
- `gspread`, `oauth2client` — работа с Google Sheets (legacy)
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