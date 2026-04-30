"""Main GUI window for Forecast Trading Robot client."""

import os
import sys
import logging
from typing import List, Optional

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QComboBox, QTabWidget, QTextEdit,
    QSplitter, QFrame, QCheckBox, QDialog, QDialogButtonBox,
    QGroupBox, QStatusBar, QSizePolicy, QAbstractItemView,
    QApplication, QListWidget, QListWidgetItem,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt6.QtGui import QColor, QFont, QBrush

_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from scripts.shared.models import ForecastLog, TickerSetting, ProviderSetting
from scripts.client.api_client import ForecastApiClient
from scripts.client.config import ClientConfig

logger = logging.getLogger(__name__)

_SIDE_COLORS = {
    "LONG": QColor("#c8e6c9"),
    "SHORT": QColor("#ffcdd2"),
    "NEUTRAL": QColor("#f5f5f5"),
}
_EVALUATED_DIM = 40

METHODS = [
    "momentum_trend", "price_action", "relative_strength",
    "volatility", "mean_reversion", "volume_breakout",
]

STATUSES = ["NEW", "EVALUATED", "ERROR"]


class TextDialog(QDialog):
    """Read-only dialog to show a large text block."""

    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 600)
        layout = QVBoxLayout(self)
        edit = QTextEdit()
        edit.setReadOnly(True)
        edit.setPlainText(text or "(empty)")
        edit.setFont(QFont("Consolas", 9))
        layout.addWidget(edit)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)


class AddTickerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Ticker")
        self.setFixedWidth(360)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Ticker (e.g. NASDAQ:NVDA):"))
        self.ticker_edit = QLineEdit()
        self.ticker_edit.setPlaceholderText("NASDAQ:NVDA")
        layout.addWidget(self.ticker_edit)
        layout.addWidget(QLabel("Comment:"))
        self.comment_edit = QLineEdit()
        layout.addWidget(self.comment_edit)
        self.active_cb = QCheckBox("Active")
        self.active_cb.setChecked(True)
        layout.addWidget(self.active_cb)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_data(self):
        return {
            "ticker": self.ticker_edit.text().strip().upper(),
            "active": 1 if self.active_cb.isChecked() else 0,
            "comment": self.comment_edit.text().strip(),
        }


class StatusPoller(QThread):
    """Background thread that polls /run/status every 2 seconds."""
    status_updated = pyqtSignal(object)

    def __init__(self, api: ForecastApiClient):
        super().__init__()
        self.api = api
        self._active = True

    def run(self):
        import time
        while self._active:
            try:
                resp = self.api.run_status()
                self.status_updated.emit(resp)
                if resp.status in ("idle", "done", "error"):
                    self._active = False
                    break
            except Exception as e:
                logger.warning(f"Status poll error: {e}")
            time.sleep(2)

    def stop(self):
        self._active = False


class ForecastsTab(QWidget):
    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._logs: List[ForecastLog] = []
        self._visible: List[ForecastLog] = []
        self._current_log: Optional[ForecastLog] = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # Filter bar
        filter_frame = QFrame()
        filter_frame.setFrameShape(QFrame.Shape.StyledPanel)
        fl = QHBoxLayout(filter_frame)
        fl.setSpacing(6)

        fl.addWidget(QLabel("Ticker:"))
        self.ticker_combo = QComboBox()
        self.ticker_combo.setMinimumWidth(140)
        self.ticker_combo.addItem("ALL", "")
        fl.addWidget(self.ticker_combo)

        fl.addWidget(QLabel("Method:"))
        self.method_combo = QComboBox()
        self.method_combo.setMinimumWidth(160)
        self.method_combo.addItem("ALL", "")
        for m in METHODS:
            self.method_combo.addItem(m, m)
        fl.addWidget(self.method_combo)

        fl.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(130)
        self.model_combo.addItem("ALL", "")
        self.model_combo.currentIndexChanged.connect(self._populate_table)
        fl.addWidget(self.model_combo)

        fl.addWidget(QLabel("Status:"))
        self.status_combo = QComboBox()
        self.status_combo.setMinimumWidth(110)
        self.status_combo.addItem("ALL", "")
        for s in STATUSES:
            self.status_combo.addItem(s, s)
        fl.addWidget(self.status_combo)

        fl.addWidget(QLabel("From:"))
        self.date_from = QLineEdit()
        self.date_from.setPlaceholderText("YYYY-MM-DD")
        self.date_from.setMaximumWidth(110)
        fl.addWidget(self.date_from)

        fl.addWidget(QLabel("To:"))
        self.date_to = QLineEdit()
        self.date_to.setPlaceholderText("YYYY-MM-DD")
        self.date_to.setMaximumWidth(110)
        fl.addWidget(self.date_to)

        self.search_btn = QPushButton("🔍 Search")
        self.search_btn.clicked.connect(self.load_logs)
        fl.addWidget(self.search_btn)

        self.refresh_btn = QPushButton("↺ Refresh")
        self.refresh_btn.clicked.connect(self.load_logs)
        fl.addWidget(self.refresh_btn)

        fl.addStretch()
        layout.addWidget(filter_frame)

        # Splitter: table top, details bottom
        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter, 1)

        # Table
        table_w = QWidget()
        tl = QVBoxLayout(table_w)
        tl.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget()
        cols = ["Created", "Forecast Date", "Ticker", "Method", "Model", "Side", "Conf%", "Status", "Dir✓", "Tgt✓", "Stop✓", "PnL%"]
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.setAlternatingRowColors(False)
        tl.addWidget(self.table)

        self.count_label = QLabel("Found: 0")
        tl.addWidget(self.count_label)
        splitter.addWidget(table_w)

        # Details panel
        details_w = QWidget()
        dl = QVBoxLayout(details_w)
        dl.setContentsMargins(4, 4, 4, 4)
        dl.setSpacing(4)

        # Header row
        hdr_layout = QHBoxLayout()
        self.d_id = QLabel("")
        self.d_id.setFont(QFont("", 9, QFont.Weight.Bold))
        hdr_layout.addWidget(QLabel("ID:"))
        hdr_layout.addWidget(self.d_id)
        hdr_layout.addSpacing(20)
        hdr_layout.addWidget(QLabel("Ticker:"))
        self.d_ticker = QLabel("")
        self.d_ticker.setFont(QFont("", 9, QFont.Weight.Bold))
        hdr_layout.addWidget(self.d_ticker)
        hdr_layout.addSpacing(20)
        hdr_layout.addWidget(QLabel("Created:"))
        self.d_date = QLabel("")
        hdr_layout.addWidget(self.d_date)
        hdr_layout.addSpacing(10)
        hdr_layout.addWidget(QLabel("→"))
        self.d_fdate = QLabel("")
        self.d_fdate.setToolTip("Forecast target date")
        hdr_layout.addWidget(self.d_fdate)
        hdr_layout.addSpacing(20)
        hdr_layout.addWidget(QLabel("Method:"))
        self.d_method = QLabel("")
        hdr_layout.addWidget(self.d_method)
        hdr_layout.addSpacing(20)
        hdr_layout.addWidget(QLabel("Model:"))
        self.d_model = QLabel("")
        hdr_layout.addWidget(self.d_model)
        hdr_layout.addSpacing(20)
        hdr_layout.addWidget(QLabel("Side:"))
        self.d_side = QLabel("")
        self.d_side.setFont(QFont("", 9, QFont.Weight.Bold))
        hdr_layout.addWidget(self.d_side)
        hdr_layout.addSpacing(20)
        hdr_layout.addWidget(QLabel("Conf:"))
        self.d_conf = QLabel("")
        hdr_layout.addWidget(self.d_conf)
        hdr_layout.addStretch()
        dl.addLayout(hdr_layout)

        # Entry / Exit
        entry_layout = QHBoxLayout()
        entry_layout.addWidget(QLabel("Entry:"))
        self.d_entry = QLabel("")
        self.d_entry.setWordWrap(True)
        entry_layout.addWidget(self.d_entry, 1)
        entry_layout.addSpacing(20)
        entry_layout.addWidget(QLabel("Target:"))
        self.d_target = QLabel("")
        entry_layout.addWidget(self.d_target)
        entry_layout.addSpacing(20)
        entry_layout.addWidget(QLabel("Stop:"))
        self.d_stop = QLabel("")
        entry_layout.addWidget(self.d_stop)
        entry_layout.addStretch()
        dl.addLayout(entry_layout)

        # Rationale
        dl.addWidget(QLabel("Rationale:"))
        self.d_rationale = QTextEdit()
        self.d_rationale.setReadOnly(True)
        self.d_rationale.setMaximumHeight(80)
        dl.addWidget(self.d_rationale)

        # Actuals box
        actuals_group = QGroupBox("Actuals")
        ag = QHBoxLayout(actuals_group)
        ag.addWidget(QLabel("Open:"))
        self.d_aopen = QLabel("—")
        ag.addWidget(self.d_aopen)
        ag.addSpacing(10)
        ag.addWidget(QLabel("Close:"))
        self.d_aclose = QLabel("—")
        ag.addWidget(self.d_aclose)
        ag.addSpacing(10)
        ag.addWidget(QLabel("High:"))
        self.d_ahigh = QLabel("—")
        ag.addWidget(self.d_ahigh)
        ag.addSpacing(10)
        ag.addWidget(QLabel("Low:"))
        self.d_alow = QLabel("—")
        ag.addWidget(self.d_alow)
        ag.addSpacing(20)
        ag.addWidget(QLabel("Dir✓:"))
        self.d_dir = QLabel("—")
        ag.addWidget(self.d_dir)
        ag.addSpacing(10)
        ag.addWidget(QLabel("Target✓:"))
        self.d_tgt = QLabel("—")
        ag.addWidget(self.d_tgt)
        ag.addSpacing(10)
        ag.addWidget(QLabel("Stop✓:"))
        self.d_stp = QLabel("—")
        ag.addWidget(self.d_stp)
        ag.addSpacing(10)
        ag.addWidget(QLabel("Exit✓:"))
        self.d_exit = QLabel("—")
        ag.addWidget(self.d_exit)
        ag.addSpacing(10)
        ag.addWidget(QLabel("PnL:"))
        self.d_pnl = QLabel("—")
        ag.addWidget(self.d_pnl)
        ag.addStretch()
        dl.addWidget(actuals_group)

        # Buttons for prompt/response
        btn_row = QHBoxLayout()
        self.btn_prompt = QPushButton("📋 Show Prompt")
        self.btn_prompt.clicked.connect(self._show_prompt)
        btn_row.addWidget(self.btn_prompt)
        self.btn_response = QPushButton("📋 Show API Response")
        self.btn_response.clicked.connect(self._show_response)
        btn_row.addWidget(self.btn_response)
        btn_row.addStretch()
        dl.addLayout(btn_row)

        splitter.addWidget(details_w)
        splitter.setSizes([420, 280])

    def load_logs(self):
        try:
            ticker = self.ticker_combo.currentData() or None
            method = self.method_combo.currentData() or None
            status = self.status_combo.currentData() or None
            date_from = self.date_from.text().strip() or None
            date_to = self.date_to.text().strip() or None
            self._logs = self.api.get_logs(
                ticker=ticker, method=method, status=status,
                date_from=date_from, date_to=date_to, limit=500,
            )
            self._refresh_model_filter()
            self._populate_table()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load logs:\n{e}")

    def _refresh_model_filter(self):
        current = self.model_combo.currentData()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItem("ALL", "")
        models = sorted({str(log.model or "") for log in self._logs if log.model})
        for m in models:
            self.model_combo.addItem(m, m)
        idx = self.model_combo.findData(current)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        self.model_combo.blockSignals(False)

    def refresh_ticker_filter(self, tickers: List[str]):
        current = self.ticker_combo.currentData()
        self.ticker_combo.blockSignals(True)
        self.ticker_combo.clear()
        self.ticker_combo.addItem("ALL", "")
        for t in sorted(set(tickers)):
            self.ticker_combo.addItem(t, t)
        idx = self.ticker_combo.findData(current)
        if idx >= 0:
            self.ticker_combo.setCurrentIndex(idx)
        self.ticker_combo.blockSignals(False)

    def _populate_table(self):
        model_filter = self.model_combo.currentData() or ""
        self._visible = [
            log for log in self._logs
            if not model_filter or str(log.model or "") == model_filter
        ]
        self.table.setSortingEnabled(False)  # disable while filling
        self.table.setRowCount(0)
        def _bool_cell(v):
            if v is None:
                return ""
            try:
                return "✅" if bool(v) else "❌"
            except Exception:
                return str(v)

        def _pnl_cell(v):
            if v is None:
                return ""
            try:
                return f"{float(v):+.2f}%"
            except Exception:
                return str(v)

        for row_idx, log in enumerate(self._visible):
            self.table.insertRow(row_idx)
            created = str(log.created_at or "")[:16]  # YYYY-MM-DD HH:MM
            fdate   = str(log.forecast_date or "")[:10]
            cells = [
                created,
                fdate,
                str(log.ticker or ""),
                str(log.method or ""),
                str(log.model or ""),
                str(log.side or ""),
                str(log.confidence or ""),
                str(log.status or ""),
                _bool_cell(log.direction_correct),
                _bool_cell(log.target_hit),
                _bool_cell(log.stop_hit),
                _pnl_cell(log.pnl_pct),
            ]
            for col, val in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row_idx)
                self.table.setItem(row_idx, col, item)

            # Color by side/status
            side = str(log.side or "").upper()
            status = str(log.status or "").upper()
            color = _SIDE_COLORS.get(side, QColor("#ffffff"))
            if status == "EVALUATED":
                color = color.darker(115)
            for col in range(self.table.columnCount()):
                it = self.table.item(row_idx, col)
                if it:
                    it.setBackground(QBrush(color))

        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()
        self.count_label.setText(f"Found: {len(self._visible)}")

    def _on_selection_changed(self):
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        log_idx = item.data(Qt.ItemDataRole.UserRole)
        if log_idx is None or log_idx >= len(self._visible):
            return
        log = self._visible[log_idx]
        self._current_log = log
        self._update_details(log)

    def _update_details(self, log: ForecastLog):
        self.d_id.setText(str(log.id or ""))
        self.d_ticker.setText(str(log.ticker or ""))
        self.d_date.setText(str(log.created_at or "")[:16])
        self.d_fdate.setText(str(log.forecast_date or "")[:10])
        self.d_method.setText(str(log.method or ""))
        self.d_model.setText(str(log.model or ""))
        side = str(log.side or "")
        self.d_side.setText(side)
        side_upper = side.upper()
        if side_upper == "LONG":
            self.d_side.setStyleSheet("color: #2e7d32; font-weight: bold;")
        elif side_upper == "SHORT":
            self.d_side.setStyleSheet("color: #c62828; font-weight: bold;")
        else:
            self.d_side.setStyleSheet("color: #555; font-weight: bold;")
        self.d_conf.setText(f"{log.confidence}%" if log.confidence is not None else "—")
        self.d_entry.setText(str(log.entry_conditions or "—"))
        self.d_target.setText(str(log.exit_target or "—"))
        self.d_stop.setText(str(log.exit_stop or "—"))
        self.d_rationale.setPlainText(str(log.rationale or ""))

        def _fmt(val):
            if val is None:
                return "—"
            try:
                return f"{float(val):.4f}"
            except Exception:
                return str(val)

        def _bool_icon(val):
            if val is None:
                return "—"
            try:
                return "✅" if bool(val) else "❌"
            except Exception:
                return str(val)

        self.d_aopen.setText(_fmt(log.actual_open))
        self.d_aclose.setText(_fmt(log.actual_close))
        self.d_ahigh.setText(_fmt(log.actual_high))
        self.d_alow.setText(_fmt(log.actual_low))
        self.d_dir.setText(_bool_icon(log.direction_correct))
        self.d_tgt.setText(_bool_icon(log.target_hit))
        self.d_stp.setText(_bool_icon(log.stop_hit))
        self.d_exit.setText(_bool_icon(log.exit_successful))
        pnl = log.pnl_pct
        if pnl is not None:
            try:
                pnl_f = float(pnl)
                pnl_txt = f"{pnl_f:+.2f}%"
                self.d_pnl.setStyleSheet("color: #2e7d32;" if pnl_f >= 0 else "color: #c62828;")
            except Exception:
                pnl_txt = str(pnl)
                self.d_pnl.setStyleSheet("")
            self.d_pnl.setText(pnl_txt)
        else:
            self.d_pnl.setText("—")
            self.d_pnl.setStyleSheet("")

    def _show_prompt(self):
        if self._current_log:
            dlg = TextDialog("Forecast Prompt", self._current_log.forecast_prompt, self)
            dlg.exec()

    def _show_response(self):
        if self._current_log:
            dlg = TextDialog("API Response", self._current_log.prompt_response, self)
            dlg.exec()


class TickersTab(QWidget):
    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._tickers: List[TickerSetting] = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        btn_row = QHBoxLayout()
        self.add_btn = QPushButton("+ Add Ticker")
        self.add_btn.clicked.connect(self._add_ticker)
        btn_row.addWidget(self.add_btn)
        self.save_btn = QPushButton("💾 Save")
        self.save_btn.clicked.connect(self._save)
        btn_row.addWidget(self.save_btn)
        self.refresh_btn = QPushButton("↺ Refresh")
        self.refresh_btn.clicked.connect(self.load)
        btn_row.addWidget(self.refresh_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Active", "Ticker", "Comment"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)

    def load(self):
        try:
            self._tickers = self.api.get_tickers()
            self._populate_table()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load tickers:\n{e}")

    def _populate_table(self):
        self.table.setRowCount(0)
        for row_idx, t in enumerate(self._tickers):
            self.table.insertRow(row_idx)

            cb_widget = QWidget()
            cb_layout = QHBoxLayout(cb_widget)
            cb_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cb_layout.setContentsMargins(0, 0, 0, 0)
            cb = QCheckBox()
            cb.setChecked(bool(t.active))
            cb_layout.addWidget(cb)
            self.table.setCellWidget(row_idx, 0, cb_widget)

            ticker_item = QTableWidgetItem(str(t.ticker or ""))
            ticker_item.setFlags(ticker_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row_idx, 1, ticker_item)

            comment_item = QTableWidgetItem(str(t.comment or ""))
            self.table.setItem(row_idx, 2, comment_item)

    def _get_checkbox(self, row: int) -> Optional[QCheckBox]:
        w = self.table.cellWidget(row, 0)
        if w:
            for child in w.children():
                if isinstance(child, QCheckBox):
                    return child
        return None

    def _add_ticker(self):
        dlg = AddTickerDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            if not data["ticker"]:
                QMessageBox.warning(self, "Error", "Ticker cannot be empty")
                return
            try:
                t = self.api.add_ticker(data["ticker"], data["active"], data["comment"])
                self._tickers.append(t)
                self._populate_table()
                QMessageBox.information(self, "Added", f"Ticker {t.ticker} added.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to add ticker:\n{e}")

    def _save(self):
        errors = []
        for row_idx, t in enumerate(self._tickers):
            try:
                cb = self._get_checkbox(row_idx)
                active = 1 if (cb and cb.isChecked()) else 0
                comment_item = self.table.item(row_idx, 2)
                comment = comment_item.text() if comment_item else ""
                self.api.update_ticker(t.ticker, active=active, comment=comment)
            except Exception as e:
                errors.append(f"{t.ticker}: {e}")
        if errors:
            QMessageBox.warning(self, "Partial Save", "Some tickers failed:\n" + "\n".join(errors))
        else:
            QMessageBox.information(self, "Saved", "All tickers saved successfully.")
        self.load()


_OPENROUTER_MODELS = [
    # Anthropic — актуальные slugs на OpenRouter
    "anthropic/claude-sonnet-4",
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3.5-haiku",
    "anthropic/claude-3-opus",
    # OpenAI
    "openai/gpt-4.1",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "openai/o3",
    "openai/o3-mini",
    "openai/o4-mini",
    # DeepSeek
    "deepseek/deepseek-chat-v3-0324",
    "deepseek/deepseek-chat",
    "deepseek/deepseek-r1",
    "deepseek/deepseek-r1-distill-llama-70b",
    # Google
    "google/gemini-2.5-pro-preview",
    "google/gemini-2.5-flash-preview",
    "google/gemini-2.0-flash-001",
    # Perplexity
    "perplexity/sonar-pro",
    "perplexity/sonar",
    "perplexity/sonar-reasoning-pro",
    # Meta
    "meta-llama/llama-4-maverick",
    "meta-llama/llama-3.3-70b-instruct",
    # Mistral
    "mistralai/mistral-large-2411",
    "mistralai/mistral-small-3.1-24b-instruct",
    # xAI
    "x-ai/grok-3-mini-beta",
    "x-ai/grok-2-1212",
    # Qwen
    "qwen/qwen-2.5-72b-instruct",
    "qwen/qwq-32b",
]


class ProvidersTab(QWidget):
    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._providers: List[ProviderSetting] = []
        self._or_key_visible = False
        self._catalog_ids: List[str] = list(_OPENROUTER_MODELS)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── OpenRouter API Key block ──────────────────────────────────────────
        key_group = QGroupBox("OpenRouter API Key")
        kg = QHBoxLayout(key_group)
        self.or_key_edit = QLineEdit()
        self.or_key_edit.setPlaceholderText("sk-or-v1-...")
        self.or_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        kg.addWidget(self.or_key_edit, 1)
        self.reveal_key_btn = QPushButton("�")
        self.reveal_key_btn.setFixedWidth(36)
        self.reveal_key_btn.setCheckable(True)
        self.reveal_key_btn.toggled.connect(self._toggle_key_visibility)
        kg.addWidget(self.reveal_key_btn)
        save_key_btn = QPushButton("💾 Save Key")
        save_key_btn.clicked.connect(self._save_or_key)
        kg.addWidget(save_key_btn)
        layout.addWidget(key_group)

        # ── AI Models table ───────────────────────────────────────────────────
        models_group = QGroupBox("AI Models (OpenRouter)")
        mg = QVBoxLayout(models_group)

        bar = QHBoxLayout()
        add_btn = QPushButton("➕ Add Model")
        add_btn.clicked.connect(self._add_row)
        bar.addWidget(add_btn)
        self.del_btn = QPushButton("🗑 Remove Selected")
        self.del_btn.clicked.connect(self._remove_selected)
        bar.addWidget(self.del_btn)
        bar.addStretch()
        self.catalog_btn = QPushButton("🌐 Update Catalog")
        self.catalog_btn.setToolTip("Fetch full model list from OpenRouter API")
        self.catalog_btn.clicked.connect(self._refresh_catalog)
        bar.addWidget(self.catalog_btn)
        self.save_btn = QPushButton("💾 Save All")
        self.save_btn.clicked.connect(self._save_models)
        bar.addWidget(self.save_btn)
        self.refresh_btn = QPushButton("🔄 Reload")
        self.refresh_btn.clicked.connect(self.load)
        bar.addWidget(self.refresh_btn)
        mg.addLayout(bar)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Active", "Name", "Model", "Rate/min", "Max Tokens"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        mg.addWidget(self.table)
        layout.addWidget(models_group, 1)

        # ── Data providers (read-only info) ──────────────────────────────────
        data_group = QGroupBox("Data Providers")
        dg = QVBoxLayout(data_group)
        self.data_table = QTableWidget(0, 4)
        self.data_table.setHorizontalHeaderLabels(["Active", "Name", "API Key", "Rate/min"])
        self.data_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.data_table.setMaximumHeight(140)
        dg.addWidget(self.data_table)
        save_data_btn = QPushButton("💾 Save Data Providers")
        save_data_btn.clicked.connect(self._save_data_providers)
        dg.addWidget(save_data_btn)
        layout.addWidget(data_group)

    # ── Load ─────────────────────────────────────────────────────────────────

    def load(self):
        try:
            # Load OpenRouter key from config
            cfg = self.api.get_config()
            or_key = next((c.value for c in cfg.items if c.key == "OPENROUTER_API_KEY"), "")
            self.or_key_edit.setText(or_key)

            # Load model catalog for combos
            try:
                cat = self.api.get_model_catalog()
                ids = [item["model_id"] for item in cat.get("items", [])]
                if ids:
                    self._catalog_ids = ids
            except Exception:
                pass

            # Load providers — split by type field or presence of model string
            self._providers = self.api.get_providers()
            _DATA_NAMES = {"alpha_vantage", "yfinance", "finnhub", "polygon"}
            ai_providers = [
                p for p in self._providers
                if getattr(p, 'model', '') and
                   (p.get_name().lower() not in _DATA_NAMES)
            ]
            data_providers = [
                p for p in self._providers
                if p.get_name().lower() in _DATA_NAMES or not getattr(p, 'model', '')
            ]
            self._populate_ai_table(ai_providers)
            self._populate_data_table(data_providers)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load providers:\n{e}")

    def _populate_ai_table(self, providers):
        self.table.setRowCount(0)
        for p in providers:
            self._insert_ai_row(
                active=bool(int(p.active or 0)),
                name=p.get_name(),
                model=p.model or "",
                rate=int(p.rate_limit or 60),
                tokens=int(p.max_tokens or 2000),
            )

    def _insert_ai_row(self, active=True, name="", model="", rate=60, tokens=2000):
        row = self.table.rowCount()
        self.table.insertRow(row)

        cb_w = QWidget(); cb_l = QHBoxLayout(cb_w)
        cb_l.setAlignment(Qt.AlignmentFlag.AlignCenter); cb_l.setContentsMargins(0,0,0,0)
        cb = QCheckBox(); cb.setChecked(active); cb_l.addWidget(cb)
        self.table.setCellWidget(row, 0, cb_w)

        self.table.setItem(row, 1, QTableWidgetItem(name))

        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(self._catalog_ids)
        if model and model not in self._catalog_ids:
            combo.insertItem(0, model)
        combo.setCurrentText(model or (self._catalog_ids[0] if self._catalog_ids else ""))
        self.table.setCellWidget(row, 2, combo)

        self.table.setItem(row, 3, QTableWidgetItem(str(rate)))
        self.table.setItem(row, 4, QTableWidgetItem(str(tokens)))

    def _populate_data_table(self, providers):
        self.data_table.setRowCount(0)
        for p in providers:
            row = self.data_table.rowCount()
            self.data_table.insertRow(row)
            cb_w = QWidget(); cb_l = QHBoxLayout(cb_w)
            cb_l.setAlignment(Qt.AlignmentFlag.AlignCenter); cb_l.setContentsMargins(0,0,0,0)
            cb = QCheckBox(); cb.setChecked(bool(int(p.active or 0))); cb_l.addWidget(cb)
            self.data_table.setCellWidget(row, 0, cb_w)
            self.data_table.setItem(row, 1, QTableWidgetItem(p.get_name()))
            self.data_table.setItem(row, 2, QTableWidgetItem(p.get_api_key()))
            self.data_table.setItem(row, 3, QTableWidgetItem(str(p.rate_limit or "")))

    # ── Actions ───────────────────────────────────────────────────────────────

    def _toggle_key_visibility(self, checked: bool):
        self.or_key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )

    def _save_or_key(self):
        from scripts.shared.models import ConfigParam
        key = self.or_key_edit.text().strip()
        try:
            self.api.update_config("OPENROUTER_API_KEY", ConfigParam(key="OPENROUTER_API_KEY", value=key))
            QMessageBox.information(self, "Saved", "OpenRouter API key saved.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save key:\n{e}")

    def _refresh_catalog(self):
        self.catalog_btn.setEnabled(False)
        self.catalog_btn.setText("⏳ Updating...")
        try:
            result = self.api.refresh_model_catalog()
            count = result.get("refreshed", 0)
            QMessageBox.information(
                self, "Catalog Updated",
                f"✅ Loaded {count} models from OpenRouter."
            )
            self.load()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to update catalog:\n{e}")
        finally:
            self.catalog_btn.setEnabled(True)
            self.catalog_btn.setText("🌐 Update Catalog")

    def _add_row(self):
        self._insert_ai_row(active=True, name="",
                            model=self._catalog_ids[0] if self._catalog_ids else "")

    def _remove_selected(self):
        rows = sorted(set(i.row() for i in self.table.selectedItems()), reverse=True)
        if not rows:
            QMessageBox.information(self, "Info", "Select a row to remove.")
            return
        for row in rows:
            name_item = self.table.item(row, 1)
            name = name_item.text().strip() if name_item else ""
            if name:
                try:
                    self.api.delete_provider(name)
                except Exception:
                    pass
            self.table.removeRow(row)

    def _get_cb(self, table, row) -> Optional[QCheckBox]:
        w = table.cellWidget(row, 0)
        if w:
            for c in w.children():
                if isinstance(c, QCheckBox):
                    return c
        return None

    def _save_models(self):
        errors = []
        for row in range(self.table.rowCount()):
            try:
                cb = self._get_cb(self.table, row)
                active = 1 if (cb and cb.isChecked()) else 0
                name_item = self.table.item(row, 1)
                name = (name_item.text().strip() if name_item else "").replace(" ", "_")
                if not name:
                    continue
                combo = self.table.cellWidget(row, 2)
                model = combo.currentText().strip() if combo else ""
                rate_item = self.table.item(row, 3)
                tokens_item = self.table.item(row, 4)
                try:
                    rate = int(rate_item.text()) if rate_item and rate_item.text() else 60
                except ValueError:
                    rate = 60
                try:
                    tokens = int(tokens_item.text()) if tokens_item and tokens_item.text() else 2000
                except ValueError:
                    tokens = 2000
                self.api.update_provider(name, model=model, rate_limit=rate,
                                         max_tokens=tokens, active=active)
            except Exception as e:
                errors.append(f"Row {row}: {e}")
        if errors:
            QMessageBox.warning(self, "Partial Save", "\n".join(errors))
        else:
            QMessageBox.information(self, "Saved", "AI models saved.")
        self.load()

    def _save_data_providers(self):
        errors = []
        for row in range(self.data_table.rowCount()):
            try:
                name_item = self.data_table.item(row, 1)
                name = name_item.text().strip() if name_item else ""
                if not name:
                    continue
                cb = self._get_cb(self.data_table, row)
                active = 1 if (cb and cb.isChecked()) else 0
                key_item = self.data_table.item(row, 2)
                api_key = key_item.text().strip() if key_item else ""
                rate_item = self.data_table.item(row, 3)
                try:
                    rate = int(rate_item.text()) if rate_item and rate_item.text() else 60
                except ValueError:
                    rate = 60
                self.api.update_provider(name, api_key=api_key or None,
                                         rate_limit=rate, active=active)
            except Exception as e:
                errors.append(f"{name}: {e}")
        if errors:
            QMessageBox.warning(self, "Partial Save", "\n".join(errors))
        else:
            QMessageBox.information(self, "Saved", "Data providers saved.")
        self.load()


class RunTab(QWidget):
    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._poller: Optional[StatusPoller] = None
        self._last_log_count = 0
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        actions_group = QGroupBox("Actions")
        ag = QVBoxLayout(actions_group)

        btn_row = QHBoxLayout()
        self.forecast_btn = QPushButton("🤖 Forecast")
        self.forecast_btn.setMinimumHeight(40)
        self.forecast_btn.clicked.connect(lambda: self._run("forecast"))
        btn_row.addWidget(self.forecast_btn)

        self.evaluate_btn = QPushButton("📊 Evaluate")
        self.evaluate_btn.setMinimumHeight(40)
        self.evaluate_btn.clicked.connect(lambda: self._run("evaluate"))
        btn_row.addWidget(self.evaluate_btn)

        self.full_btn = QPushButton("🔄 Full Cycle")
        self.full_btn.setMinimumHeight(40)
        self.full_btn.clicked.connect(lambda: self._run("full"))
        btn_row.addWidget(self.full_btn)

        ag.addLayout(btn_row)

        info_row = QHBoxLayout()
        info_row.addWidget(QLabel("Status:"))
        self.status_label = QLabel("● IDLE")
        self.status_label.setFont(QFont("", 10, QFont.Weight.Bold))
        self.status_label.setStyleSheet("color: #555;")
        info_row.addWidget(self.status_label)
        info_row.addSpacing(30)
        info_row.addWidget(QLabel("Started:"))
        self.started_label = QLabel("—")
        info_row.addWidget(self.started_label)
        info_row.addSpacing(20)
        info_row.addWidget(QLabel("Duration:"))
        self.duration_label = QLabel("—")
        info_row.addWidget(self.duration_label)
        info_row.addStretch()
        ag.addLayout(info_row)

        layout.addWidget(actions_group)

        log_group = QGroupBox("Log Output")
        lg = QVBoxLayout(log_group)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setFont(QFont("Consolas", 9))
        lg.addWidget(self.log_edit, 1)

        clear_row = QHBoxLayout()
        clear_row.addStretch()
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.log_edit.clear)
        clear_row.addWidget(self.clear_btn)
        lg.addLayout(clear_row)

        layout.addWidget(log_group, 1)

    def _run(self, mode: str):
        try:
            if mode == "forecast":
                resp = self.api.run_forecast()
            elif mode == "evaluate":
                resp = self.api.run_evaluate()
            else:
                resp = self.api.run_full()
            self._set_running(True)
            self._apply_status(resp)
            self._start_polling()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to start {mode}:\n{e}")

    def _set_running(self, running: bool):
        self.forecast_btn.setEnabled(not running)
        self.evaluate_btn.setEnabled(not running)
        self.full_btn.setEnabled(not running)

    def _start_polling(self):
        if self._poller and self._poller.isRunning():
            self._poller.stop()
        self._last_log_count = 0
        self._poller = StatusPoller(self.api)
        self._poller.status_updated.connect(self._apply_status)
        self._poller.finished.connect(lambda: self._set_running(False))
        self._poller.start()

    def _apply_status(self, resp):
        status = resp.status.upper()
        if status == "RUNNING":
            self.status_label.setText("● RUNNING")
            self.status_label.setStyleSheet("color: #f57f17; font-weight: bold;")
        elif status == "DONE":
            self.status_label.setText("● DONE")
            self.status_label.setStyleSheet("color: #2e7d32; font-weight: bold;")
            self._set_running(False)
        elif status == "ERROR":
            self.status_label.setText("● ERROR")
            self.status_label.setStyleSheet("color: #c62828; font-weight: bold;")
            self._set_running(False)
        else:
            self.status_label.setText("● IDLE")
            self.status_label.setStyleSheet("color: #555; font-weight: bold;")

        if resp.started_at:
            self.started_label.setText(str(resp.started_at)[:19])
        if resp.duration_sec is not None:
            self.duration_label.setText(f"{resp.duration_sec:.1f}s")

        if resp.log_lines:
            new_lines = resp.log_lines[self._last_log_count:]
            if new_lines:
                self.log_edit.append("\n".join(new_lines))
                self._last_log_count = len(resp.log_lines)


# ---------------------------------------------------------------------------
# Config Tab
# ---------------------------------------------------------------------------

class ConfigTab(QWidget):
    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._items = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        bar = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter key...")
        self.search_edit.textChanged.connect(self._filter)
        bar.addWidget(self.search_edit)
        self.reload_btn = QPushButton("🔄 Reload")
        self.reload_btn.clicked.connect(self.load)
        bar.addWidget(self.reload_btn)
        self.save_btn = QPushButton("💾 Save selected")
        self.save_btn.clicked.connect(self._save_selected)
        bar.addWidget(self.save_btn)
        layout.addLayout(bar)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Key", "Value", "Description"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table)

    def load(self):
        try:
            resp = self.api.get_config()
            self._items = resp.items if resp else []
            self._render(self._items)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load config:\n{e}")

    def _render(self, items):
        self.table.setRowCount(len(items))
        for r, item in enumerate(items):
            self.table.setItem(r, 0, QTableWidgetItem(item.key or ""))
            val_item = QTableWidgetItem(item.value or "")
            self.table.setItem(r, 1, val_item)
            desc_item = QTableWidgetItem(item.description or "")
            desc_item.setFlags(desc_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, 2, desc_item)

    def _filter(self, text):
        text = text.lower()
        filtered = [i for i in self._items if text in (i.key or "").lower()]
        self._render(filtered)

    def _save_selected(self):
        from scripts.shared.models import ConfigParam
        rows = set(i.row() for i in self.table.selectedItems())
        if not rows:
            QMessageBox.information(self, "Info", "Select a row to save.")
            return
        saved = 0
        for row in rows:
            key = self.table.item(row, 0).text()
            value = self.table.item(row, 1).text()
            try:
                self.api.update_config(key, ConfigParam(key=key, value=value))
                saved += 1
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save {key}:\n{e}")
        if saved:
            QMessageBox.information(self, "Saved", f"Saved {saved} parameter(s).")


# ---------------------------------------------------------------------------
# Prompts Tab
# ---------------------------------------------------------------------------

_VARIABLES_HELP = """\
Доступные переменные шаблона:

  {ticker}          — тикер (NASDAQ:NVDA)
  {forecast_date}   — дата прогноза
  {horizon}         — горизонт в днях
  {market_regime}   — рыночный режим
  {market_context}  — контекст SPY/VIX
  {history}         — история метода (win rate, PnL)
  {footer}          — инструкция формата JSON (вставлять в конец)

  {price}           — текущая цена
  {ma20}  {ma50}  {ma200}
  {ema9}  {ema21}
  {rsi}             — RSI(14)
  {adx}             — ADX(14)
  {macd}  {macd_hist}
  {stoch_rsi}
  {atr}             — ATR в $
  {atr_pct}         — ATR в %
  {bb_upper}  {bb_lower}  {bb_pos}  {bb_width}
  {obv_trend}       — «↑ бычий» / «↓ медвежий»
  {change_5d}  {change_10d}  {change_20d}  {change_50d}
  {volume_current}  {vol_ratio}  {ma20_dev}

Формат числовых переменных: {price:.2f}, {rsi:.1f}, {adx:.1f}
"""

_METHOD_LABELS = {
    "momentum_trend":    "📈 Momentum Trend",
    "price_action":      "🕯 Price Action",
    "relative_strength": "💪 Relative Strength",
    "volatility":        "⚡ Volatility Breakout",
    "mean_reversion":    "↩ Mean Reversion",
    "volume_breakout":   "📦 Volume Breakout",
}


class PromptsTab(QWidget):
    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._templates: dict = {}
        self._current_method: str = ""
        self._dirty = False
        self._build_ui()

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setSpacing(8)

        # ── Left: method list ─────────────────────────────────────────
        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Methods</b>"))
        self.method_list = QListWidget()
        self.method_list.setMaximumWidth(210)
        self.method_list.setMinimumWidth(170)
        for m in METHODS:
            item = QListWidgetItem(_METHOD_LABELS.get(m, m))
            item.setData(Qt.ItemDataRole.UserRole, m)
            self.method_list.addItem(item)
        self.method_list.currentItemChanged.connect(self._on_method_changed)
        left.addWidget(self.method_list)
        left.addStretch()
        root.addLayout(left)

        # ── Right: editor ─────────────────────────────────────────────
        right = QVBoxLayout()

        top_bar = QHBoxLayout()
        self.method_lbl = QLabel("Select a method")
        self.method_lbl.setStyleSheet("font-weight: bold; font-size: 13px;")
        top_bar.addWidget(self.method_lbl)
        top_bar.addStretch()
        vars_btn = QPushButton("{…} Variables")
        vars_btn.setToolTip("Show available template variables")
        vars_btn.clicked.connect(self._show_variables)
        top_bar.addWidget(vars_btn)
        right.addLayout(top_bar)

        self.editor = QTextEdit()
        self.editor.setFont(QFont("Consolas", 9))
        self.editor.setPlaceholderText("Select a method on the left to edit its prompt template...")
        self.editor.textChanged.connect(self._on_text_changed)
        right.addWidget(self.editor, 1)

        btn_bar = QHBoxLayout()
        self.save_btn = QPushButton("💾 Save")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save_current)
        btn_bar.addWidget(self.save_btn)
        self.reset_btn = QPushButton("↺ Reset to Default")
        self.reset_btn.setEnabled(False)
        self.reset_btn.clicked.connect(self._reset_current)
        btn_bar.addWidget(self.reset_btn)
        btn_bar.addStretch()
        reload_btn = QPushButton("🔄 Reload")
        reload_btn.clicked.connect(self.load)
        btn_bar.addWidget(reload_btn)
        right.addLayout(btn_bar)

        root.addLayout(right, 1)

    # ── Load ─────────────────────────────────────────────────────────

    def load(self):
        try:
            data = self.api.get_prompt_templates()
            self._templates = data.get("templates", {})
            if self._current_method:
                self._load_editor(self._current_method)
            elif self.method_list.count() > 0:
                self.method_list.setCurrentRow(0)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load templates:\n{e}")

    def _load_editor(self, method: str):
        self._current_method = method
        self.method_lbl.setText(_METHOD_LABELS.get(method, method))
        text = self._templates.get(method, "")
        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)
        self._dirty = False
        self.save_btn.setEnabled(False)
        self.reset_btn.setEnabled(bool(method))

    def _on_method_changed(self, current, previous):
        if previous and self._dirty:
            method = previous.data(Qt.ItemDataRole.UserRole)
            ans = QMessageBox.question(
                self, "Unsaved Changes",
                f"Save changes to {_METHOD_LABELS.get(method, method)}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if ans == QMessageBox.StandardButton.Yes:
                self._save_template(method, self.editor.toPlainText())
        if current:
            self._load_editor(current.data(Qt.ItemDataRole.UserRole))

    def _on_text_changed(self):
        if self._current_method:
            self._dirty = True
            self.save_btn.setEnabled(True)

    # ── Save / Reset ─────────────────────────────────────────────────

    def _save_template(self, method: str, text: str):
        try:
            self.api.save_prompt_template(method, text)
            self._templates[method] = text
            self._dirty = False
            self.save_btn.setEnabled(False)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save:\n{e}")

    def _save_current(self):
        if self._current_method:
            self._save_template(self._current_method, self.editor.toPlainText())
            QMessageBox.information(self, "Saved",
                f"{_METHOD_LABELS.get(self._current_method, self._current_method)} saved.")

    def _reset_current(self):
        if not self._current_method:
            return
        ans = QMessageBox.question(
            self, "Reset to Default",
            f"Reset {_METHOD_LABELS.get(self._current_method)} to built-in default?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            self.api.reset_prompt_template(self._current_method)
            self.load()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to reset:\n{e}")

    def _show_variables(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Template Variables")
        dlg.resize(480, 440)
        lay = QVBoxLayout(dlg)
        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setFont(QFont("Consolas", 9))
        txt.setPlainText(_VARIABLES_HELP)
        lay.addWidget(txt)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()


# ---------------------------------------------------------------------------
# System Log Tab
# ---------------------------------------------------------------------------

class SystemLogTab(QWidget):
    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._build_ui()
        self._timer = QTimer()
        self._timer.timeout.connect(self.load)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Level:"))
        self.level_combo = QComboBox()
        for lvl in ["ALL", "INFO", "WARNING", "ERROR", "DEBUG"]:
            self.level_combo.addItem(lvl)
        bar.addWidget(self.level_combo)
        bar.addWidget(QLabel("Lines:"))
        self.lines_spin = QComboBox()
        for n in ["100", "200", "500", "1000"]:
            self.lines_spin.addItem(n)
        self.lines_spin.setCurrentIndex(1)
        bar.addWidget(self.lines_spin)
        self.reload_btn = QPushButton("🔄 Reload")
        self.reload_btn.clicked.connect(self.load)
        bar.addWidget(self.reload_btn)
        self.auto_cb = QCheckBox("Auto (5s)")
        self.auto_cb.toggled.connect(self._toggle_auto)
        bar.addWidget(self.auto_cb)
        bar.addStretch()
        layout.addLayout(bar)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setFont(QFont("Consolas", 9))
        layout.addWidget(self.log_edit)

    def load(self):
        try:
            level = self.level_combo.currentText()
            lines = int(self.lines_spin.currentText())
            resp = self.api.get_system_log(lines=lines, level=level if level != "ALL" else None)
            if resp:
                self.log_edit.setPlainText("\n".join(resp.lines))
                sb = self.log_edit.verticalScrollBar()
                sb.setValue(sb.maximum())
        except Exception as e:
            logger.warning(f"System log load error: {e}")

    def _toggle_auto(self, checked: bool):
        if checked:
            self._timer.start(5000)
        else:
            self._timer.stop()


# ---------------------------------------------------------------------------
# Price Data Tab
# ---------------------------------------------------------------------------

class PriceDataTab(QWidget):
    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Ticker:"))
        self.ticker_edit = QLineEdit()
        self.ticker_edit.setPlaceholderText("NASDAQ:NVDA")
        self.ticker_edit.setMaximumWidth(180)
        bar.addWidget(self.ticker_edit)
        bar.addWidget(QLabel("From:"))
        self.from_edit = QLineEdit()
        self.from_edit.setPlaceholderText("2025-01-01")
        self.from_edit.setMaximumWidth(120)
        bar.addWidget(self.from_edit)
        bar.addWidget(QLabel("To:"))
        self.to_edit = QLineEdit()
        self.to_edit.setPlaceholderText("2025-12-31")
        self.to_edit.setMaximumWidth(120)
        bar.addWidget(self.to_edit)
        self.load_btn = QPushButton("🔄 Load")
        self.load_btn.clicked.connect(self.load)
        bar.addWidget(self.load_btn)
        bar.addStretch()
        layout.addLayout(bar)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Ticker", "Date", "Open", "High", "Low", "Close", "Volume"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)

    def load(self):
        try:
            ticker = self.ticker_edit.text().strip() or None
            date_from = self.from_edit.text().strip() or None
            date_to = self.to_edit.text().strip() or None
            resp = self.api.get_price_data(ticker=ticker, date_from=date_from, date_to=date_to)
            items = resp.items if resp else []
            self.table.setRowCount(len(items))
            for r, p in enumerate(items):
                for c, val in enumerate([p.ticker, p.date,
                                         f"{p.open:.2f}" if p.open else "",
                                         f"{p.high:.2f}" if p.high else "",
                                         f"{p.low:.2f}" if p.low else "",
                                         f"{p.close:.2f}" if p.close else "",
                                         f"{int(p.volume):,}" if p.volume else ""]):
                    self.table.setItem(r, c, QTableWidgetItem(str(val)))
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load price data:\n{e}")


# ---------------------------------------------------------------------------
# Indicators Tab
# ---------------------------------------------------------------------------

class IndicatorsTab(QWidget):
    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Ticker:"))
        self.ticker_edit = QLineEdit()
        self.ticker_edit.setPlaceholderText("NASDAQ:NVDA")
        self.ticker_edit.setMaximumWidth(180)
        bar.addWidget(self.ticker_edit)
        self.load_btn = QPushButton("🔄 Load")
        self.load_btn.clicked.connect(self.load)
        bar.addWidget(self.load_btn)
        bar.addStretch()
        layout.addLayout(bar)

        headers = ["Ticker", "Date", "Price", "MA20", "MA50", "MA200",
                   "EMA9", "EMA21", "RSI14", "StochRSI", "ATR14", "ADX14",
                   "MACD", "Signal", "Hist", "BB▲", "BB▼", "OBV",
                   "Chg5d%", "Chg20d%", "Vol/Avg", "Regime"]
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)

    def load(self):
        try:
            ticker = self.ticker_edit.text().strip() or None
            resp = self.api.get_indicators(ticker=ticker)
            items = resp.items if resp else []
            self.table.setRowCount(len(items))
            for r, ind in enumerate(items):
                vol_ratio = ""
                if ind.volume_current and ind.volume_avg_20 and float(ind.volume_avg_20) > 0:
                    vol_ratio = f"{float(ind.volume_current)/float(ind.volume_avg_20):.1f}x"
                vals = [
                    ind.ticker, ind.date,
                    f"{ind.price:.2f}" if ind.price else "",
                    f"{ind.ma20:.2f}" if ind.ma20 else "",
                    f"{ind.ma50:.2f}" if ind.ma50 else "",
                    f"{ind.ma200:.2f}" if ind.ma200 else "",
                    f"{ind.ema9:.2f}" if ind.ema9 else "",
                    f"{ind.ema21:.2f}" if ind.ema21 else "",
                    f"{ind.rsi14:.1f}" if ind.rsi14 else "",
                    f"{ind.stoch_rsi:.2f}" if ind.stoch_rsi else "",
                    f"{ind.atr14:.2f}" if ind.atr14 else "",
                    f"{ind.adx14:.1f}" if ind.adx14 else "",
                    f"{ind.macd:.2f}" if ind.macd else "",
                    f"{ind.macd_signal:.2f}" if ind.macd_signal else "",
                    f"{ind.macd_hist:+.2f}" if ind.macd_hist else "",
                    f"{ind.bb_upper:.2f}" if ind.bb_upper else "",
                    f"{ind.bb_lower:.2f}" if ind.bb_lower else "",
                    f"{ind.obv:.0f}" if ind.obv else "",
                    f"{ind.change_5d:+.1f}%" if ind.change_5d else "",
                    f"{ind.change_20d:+.1f}%" if ind.change_20d else "",
                    vol_ratio,
                    ind.market_regime or "",
                ]
                for c, v in enumerate(vals):
                    item = QTableWidgetItem(str(v))
                    if c == 21 and v:  # market_regime
                        colors = {
                            "STRONG_UPTREND":   "#c8e6c9",
                            "STRONG_DOWNTREND": "#ffcdd2",
                            "RANGING":          "#fff9c4",
                            "WEAK_TREND":       "#f5f5f5",
                        }
                        item.setBackground(QBrush(QColor(colors.get(v, "#f5f5f5"))))
                    self.table.setItem(r, c, item)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load indicators:\n{e}")


class _TabLoader:
    """Chains tab loads via QTimer.singleShot(0) so the event loop
    stays responsive between each step (all runs in the main thread)."""

    def __init__(self, win: "MainWindow"):
        self._win = win
        w = win
        self._steps = [
            ("Forecasts",   lambda: w.forecasts_tab.load_logs()),
            ("Tickers",     lambda: w.tickers_tab.load()),
            ("Providers",   lambda: w.providers_tab.load()),
            ("Config",      lambda: w.config_tab.load()),
            ("Prompts",     lambda: w.prompts_tab.load()),
            ("Price Data",  lambda: w.price_tab.load()),
            ("Indicators",  lambda: w.indicators_tab.load()),
            ("System Log",  lambda: w.syslog_tab.load()),
            ("Done",        lambda: self._finish()),
        ]
        self._idx = 0

    def start(self):
        self._schedule_next()

    def _schedule_next(self):
        QTimer.singleShot(0, self._run_step)

    def _run_step(self):
        if self._idx >= len(self._steps):
            return
        label, fn = self._steps[self._idx]
        self._idx += 1
        if label != "Done":
            self._win.info_label.setText(f"Loading {label}…")
        try:
            fn()
        except Exception as e:
            logger.warning(f"Tab load error ({label}): {e}")
        if self._idx < len(self._steps):
            self._schedule_next()

    def _finish(self):
        try:
            tickers = [t.ticker for t in self._win.tickers_tab._tickers]
            self._win.forecasts_tab.refresh_ticker_filter(tickers)
        except Exception:
            pass
        self._win.info_label.setText("Ready")


class MainWindow(QMainWindow):
    def __init__(self, config: ClientConfig):
        super().__init__()
        self.config = config
        self.api = ForecastApiClient(config.server_url, config.api_key)
        self.setWindowTitle("Forecast Trading Robot")
        self.resize(1400, 900)
        self._build_ui()
        self._check_connection()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 4)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        self.forecasts_tab = ForecastsTab(self.api)
        self.tabs.addTab(self.forecasts_tab, "📊 Forecasts")

        self.tickers_tab = TickersTab(self.api)
        self.tabs.addTab(self.tickers_tab, "🎯 Tickers")

        self.providers_tab = ProvidersTab(self.api)
        self.tabs.addTab(self.providers_tab, "🔑 Providers")

        self.run_tab = RunTab(self.api)
        self.tabs.addTab(self.run_tab, "▶ Run")

        self.config_tab = ConfigTab(self.api)
        self.tabs.addTab(self.config_tab, "🔧 Config")

        self.prompts_tab = PromptsTab(self.api)
        self.tabs.addTab(self.prompts_tab, "📝 Prompts")

        self.syslog_tab = SystemLogTab(self.api)
        self.tabs.addTab(self.syslog_tab, "📋 System Log")

        self.price_tab = PriceDataTab(self.api)
        self.tabs.addTab(self.price_tab, "💹 Price Data")

        self.indicators_tab = IndicatorsTab(self.api)
        self.tabs.addTab(self.indicators_tab, "📈 Indicators")

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.conn_label = QLabel("● Connecting...")
        self.conn_label.setStyleSheet("color: #f57f17;")
        self.status_bar.addPermanentWidget(self.conn_label)
        self.info_label = QLabel("")
        self.status_bar.addWidget(self.info_label)

    def _check_connection(self):
        try:
            h = self.api.health()
            self.conn_label.setText(f"● Connected  {self.config.server_url}")
            self.conn_label.setStyleSheet("color: #2e7d32; font-weight: bold;")
            self.info_label.setText(f"Server: {h.server}")
            self._load_all()
        except Exception as e:
            self.conn_label.setText(f"● Disconnected — {e}")
            self.conn_label.setStyleSheet("color: #c62828; font-weight: bold;")
            QMessageBox.critical(
                self, "Connection Error",
                f"Cannot connect to server at {self.config.server_url}\n\n{e}\n\n"
                f"Make sure run_server.bat is running and the API key in client_config.ini matches."
            )

    def _load_all(self):
        """Load all tabs via chained QTimer.singleShot — UI stays responsive."""
        self.info_label.setText("Loading data…")
        self._loader = _TabLoader(self)
        self._loader.start()
