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