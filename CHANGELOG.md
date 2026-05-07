# Changelog

Хронология изменений по сессиям разработки.
Формат: `## [ДАТА] Название сессии` → подразделы Added / Fixed / Changed / Debt.

---

## [2026-05-05] GUI Consensus Tab + Bugfix

### Added
- Новый таб **Consensus** в GUI клиент (@`gui_main.py:531-825`) для отображения агрегированных сигналов
- Таблица консенсуса с колонками: Date, Ticker, Signal, Conf%, Methods Long/Short/Neutral, Target, Stop, Entry, Disagree
- Панель деталей с rationale и полной информацией о методах
- Цветовая индикация сигналов (LONG/SHORT/NEUTRAL)
- Фильтр по тикеру и сигналу
- Поля `target_price`, `stop_loss`, `entry_limit_price`, `high_model_disagreement` в `ConsensusRecord` (@`shared/models.py`)
- Скрипт `scripts/tools/recalculate_consensus.py` для пересчета консенсуса по существующим прогнозам без вызова AI

### Fixed
- **Критический баг:** все консенсус-сигналы были NEUTRAL из-за несовпадения структуры данных
- В `multi_model_forecaster.py:133-145` исправлена структура `all_forecasts` — теперь поля `side`, `confidence`, `exit_target`, `stop_loss` и TIF на верхнем уровне вместо вложенного `forecast`
- Исправлен парсинг `stop_loss` из `exit_stop` — теперь берётся первое число (цена), а не последнее (процент)

### Changed
- Таб **Tickers** переименован иконкой 📈 (было 🎯)
- `SetupWizard` теперь загружает и фильтры Consensus таба

---

## [2026-05-05] Инициализация журнала изменений

### Added
- Создан `CHANGELOG.md` — журнал изменений по сессиям
- Создан `ARCHITECTURE.md` — документ ключевых архитектурных решений

### Notes
- Система уже содержит: клиент-серверную архитектуру (FastAPI + PyQt6), SQLite хранилище, мульти-модельное AI-прогнозирование через OpenRouter, модуль оценки прогнозов (`actuals_evaluator.py`), унифицированный менеджер логов (`unified_logs_manager.py`)
- Предыдущая история изменений до этой даты не зафиксирована в данном файле — см. `git log` для полной истории

---

## [2026-05-05] IB Gateway integration improvements

### Added
- New `IBConfig` table and model for storing IB Gateway connection settings
- API endpoints for IB Config management: GET/POST/PUT/DELETE `/ib-config`
- `type` field (paper/live) to `AccountRecord` and `PositionRecord` models
- `type` parameter to sync endpoints for distinguishing paper/live trading
- Client methods for IB Config management in `api_client.py`

### Fixed
- asyncio event loop conflicts with ib_insync library
- TypeError in `_run_with_loop` wrapper function
- `avgCost` → `averageCost` attribute name in portfolio positions

---

## [2026-05-06] Consensus Evaluation Logging & Improvements

### Added
- **Consensus Evaluation Fields** — 14 новых полей в таблице `consensus`: `horizon_hours`, `eval_target_date`, `eval_status`, `actual_date`, `actual_open/close/high/low`, `entry_price_actual`, `target_hit`, `stop_hit`, `direction_correct`, `pnl_pct`, `r_multiple`
- **Consensus Evaluator** — новый модуль `consensus_evaluator.py` для оценки консенсус-прогнозов постфактум
- **Scheduled Evaluation** — задача `consensus_evaluate` в scheduler для автоматической оценки
- **API Endpoint** — `POST /consensus/evaluate` для ручного запуска оценки
- **GUI Improvements** — новые колонки Eval, Actual Close, Target Hit, Stop Hit, PnL% в ConsensusTab; кнопка "Evaluate Now" с детальным результатом; статистика (total/evaluated/win_rate/avg_pnl/pending)
- **Logging & Progress** — детальное логирование процесса оценки с прогрессом `[idx/total]`; статистика (evaluated/no_data/errors)
- **Default horizon_hours** — консенсус теперь всегда имеет `eval_target_date` (дефолт 24 часа)
- **Recalculate Consensus** — кнопка "🔄 Recalculate" для ретроспективного пересчета консенсуса по историческим прогнозам
- **Forecast Quality Improvements** — 5 улучшений: Forecast Run Tracking, Expected Value Filter, Confidence Calibration, "First Hit" Analysis, ATR Normalization (56 тестов проходят)

### Changed
- GUI: колонка "Eval Date" перемещена на позицию 1 (сразу после Date)
- Endpoint `/consensus/evaluate` теперь синхронный с детальным результатом

### Fixed
- SQL в `evaluate_consensus_records` корректно учитывает `eval_target_date <= now`
- Дефолтный `horizon_hours=24` когда нет данных в method_config

---

<!-- TEMPLATE для новых сессий:

## [YYYY-MM-DD] Краткое название задачи

### Added
- ...

### Fixed
- ...

### Changed
- ...

### Debt / TODO
- ...

-->
