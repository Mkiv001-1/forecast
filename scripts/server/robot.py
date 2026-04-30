"""Robot runner — wraps main_excel.py business logic in a background thread."""

import sys
import os
import threading
import logging
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CORE_DIR = os.path.join(_PROJECT_ROOT, "scripts", "core")


def _add_root_to_path():
    for _p in [_PROJECT_ROOT, _CORE_DIR]:
        if _p not in sys.path:
            sys.path.insert(0, _p)


class RobotRunner:
    """Runs trading robot tasks in a background thread and captures log output."""

    STATUS_IDLE = "idle"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_ERROR = "error"

    def __init__(self, db_file: str):
        self.excel_file = db_file  # kept for API compat
        self.db_file = db_file
        self._status = self.STATUS_IDLE
        self._message: Optional[str] = None
        self._started_at: Optional[datetime] = None
        self._finished_at: Optional[datetime] = None
        self._log_lines: List[str] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    @property
    def status(self) -> str:
        return self._status

    @property
    def message(self) -> Optional[str]:
        return self._message

    @property
    def started_at(self) -> Optional[str]:
        return self._started_at.isoformat() if self._started_at else None

    @property
    def finished_at(self) -> Optional[str]:
        return self._finished_at.isoformat() if self._finished_at else None

    @property
    def duration_sec(self) -> Optional[float]:
        if self._started_at and self._finished_at:
            return (self._finished_at - self._started_at).total_seconds()
        return None

    def get_log_lines(self) -> List[str]:
        with self._lock:
            return list(self._log_lines)

    def _add_log(self, line: str):
        with self._lock:
            self._log_lines.append(line)
        logger.info(line)

    def _run(self, mode: str):
        self._status = self.STATUS_RUNNING
        self._started_at = datetime.now()
        self._finished_at = None
        with self._lock:
            self._log_lines = []

        try:
            _add_root_to_path()
            os.chdir(_PROJECT_ROOT)

            import sys as _sys
            # Drop cached modules so fresh code is loaded from disk each run
            for _mod in list(_sys.modules.keys()):
                if any(x in _mod for x in ('main_excel', 'unified_logs_manager', 'actuals_evaluator')):
                    _sys.modules.pop(_mod, None)

            from scripts.core.sqlite_manager import SQLiteManager
            from scripts.core.main_excel import run_trading_bot, evaluate_past_forecasts

            self._add_log(f"[{datetime.now().strftime('%H:%M:%S')}] Starting mode: {mode}")
            self._add_log(f"[{datetime.now().strftime('%H:%M:%S')}] DB: {self.db_file}")

            db_manager = SQLiteManager(self.db_file)

            if mode == "forecast":
                self._add_log(f"[{datetime.now().strftime('%H:%M:%S')}] Running forecast generation...")
                run_trading_bot(db_file=self.db_file)
                self._add_log(f"[{datetime.now().strftime('%H:%M:%S')}] Forecast generation complete.")

            elif mode == "evaluate":
                self._add_log(f"[{datetime.now().strftime('%H:%M:%S')}] Running evaluation of past forecasts...")
                evaluate_past_forecasts(db_manager)
                self._add_log(f"[{datetime.now().strftime('%H:%M:%S')}] Evaluation complete.")

            elif mode == "full":
                self._add_log(f"[{datetime.now().strftime('%H:%M:%S')}] Running full cycle (evaluate + forecast)...")
                evaluate_past_forecasts(db_manager)
                self._add_log(f"[{datetime.now().strftime('%H:%M:%S')}] Evaluation done, starting forecast...")
                run_trading_bot(db_file=self.db_file)
                self._add_log(f"[{datetime.now().strftime('%H:%M:%S')}] Full cycle complete.")

            else:
                raise ValueError(f"Unknown mode: {mode}")

            self._status = self.STATUS_DONE
            self._message = f"Completed: {mode}"

        except Exception as exc:
            self._status = self.STATUS_ERROR
            self._message = str(exc)
            self._add_log(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: {exc}")
            logger.exception(f"Robot error in mode={mode}")

        finally:
            self._finished_at = datetime.now()
            elapsed = self.duration_sec
            self._add_log(f"[{datetime.now().strftime('%H:%M:%S')}] Duration: {elapsed:.1f}s")

    def start(self, mode: str) -> bool:
        """Start a robot run. Returns False if already running."""
        if self._status == self.STATUS_RUNNING:
            return False
        self._thread = threading.Thread(target=self._run, args=(mode,), daemon=True)
        self._thread.start()
        return True
