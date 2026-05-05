# Changelog

Хронология изменений по сессиям разработки.
Формат: `## [ДАТА] Название сессии` → подразделы Added / Fixed / Changed / Debt.

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
