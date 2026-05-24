# Context
PyQt6 GUI + FastAPI. Python 3.12, SQLite WAL, OpenRouter AI. N models × M methods → consensus → bracket orders via IB Gateway.

# Standards
- Python 3.12 + type hints. PEP8.
- No new deps without justification.
- Small named functions. Warn before API changes.

# Architecture
- Core → Server → Client layers. No new layers without need.
- New features: create `docs/features/<name>.md` first, then code.

# Testing
- pytest. Deterministic tests. Explicit error handling.

# Response Format
1. 2-5 bullet overview.
2. Code blocks with paths, relevant parts only.
3. Remaining tasks (if any).
Russian text, English identifiers/code.

# Avoid
- Rewriting: `forecast_runner.py`, `consensus.py`, `order_manager.py`.
- SQLite changes without `migrate.py`.
- Raw SQL outside `sqlite_manager.py`.
- Changing consensus/EMA weights without discussion.

# Knowledge Persistence
- **Сохранение рассуждений:** Все нетривиальные рассуждения, анализ проблем, архитектурные решения и выводы фиксировать в Markdown-файлы
- **Локация:**
  - `docs/features/<name>.md` — описания фич и архитектурные решения
  - `docs/archives/YYYY-MM-DD-<topic>.md` — исторические чат-сессии и рассуждения
- **Формат файлов:**
  - Front matter: дата, тема, связанные модули/файлы
  - Структура: Проблема → Рассуждение → Решение → Выводы/Action Items
  - Код в фenced blocks с путями (как в ответах Cascade)
- **Качество:** Файл должен быть самодостаточен — понятен без контекста чата, пригоден для включения в промпт
- **Использование:** При новых запросах проверять сохранённые знания на релевантность, цитировать при необходимости
- **Обязательно сохранять:**
  - Разбор неочевидных багов
  - Архитектурные trade-off и обоснование
  - Нетривиальные причины отказа от альтернатив
  - Контекст, необходимый для продолжения работы в новой сессии