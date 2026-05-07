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
    QSpinBox, QFormLayout,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt6.QtGui import QColor, QFont, QBrush

_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from scripts.shared.models import ForecastLog, TickerSetting, ProviderSetting, PositionRecord, AccountRecord, ConsensusRecord
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


class ConsensusTab(QWidget):
    """Tab for displaying aggregated consensus signals from the consensus table."""

    _TABLE_COLS = [
        "Date", "Eval Date", "Ticker", "Signal", "Conf%",
        "Target", "Stop", "Entry",
        "Actual Close", "Dir", "Tgt Hit", "Stp Hit", "First Hit", "PnL%", "R", "Status",
        "Disagree",
    ]

    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._records: List[ConsensusRecord] = []
        self._visible: List[ConsensusRecord] = []
        self._current_record: Optional[ConsensusRecord] = None
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

        fl.addWidget(QLabel("Signal:"))
        self.signal_combo = QComboBox()
        self.signal_combo.setMinimumWidth(110)
        self.signal_combo.addItem("ALL", "")
        for s in ["LONG", "SHORT", "NEUTRAL"]:
            self.signal_combo.addItem(s, s)
        fl.addWidget(self.signal_combo)

        fl.addWidget(QLabel("Eval:"))
        self.eval_combo = QComboBox()
        self.eval_combo.setMinimumWidth(110)
        self.eval_combo.addItem("ALL", "")
        for s in ["PENDING", "EVALUATED", "NO_DATA"]:
            self.eval_combo.addItem(s, s)
        fl.addWidget(self.eval_combo)

        self.search_btn = QPushButton("🔍 Search")
        self.search_btn.clicked.connect(self.load)
        fl.addWidget(self.search_btn)

        self.refresh_btn = QPushButton("↺ Refresh")
        self.refresh_btn.clicked.connect(self.load)
        fl.addWidget(self.refresh_btn)

        self.evaluate_btn = QPushButton("📊 Evaluate Now")
        self.evaluate_btn.setToolTip("Trigger evaluation of pending consensus records")
        self.evaluate_btn.clicked.connect(self._on_evaluate_now)
        fl.addWidget(self.evaluate_btn)

        self.recalc_btn = QPushButton("🔄 Recalculate")
        self.recalc_btn.setToolTip("Recalculate consensus from historical forecast logs")
        self.recalc_btn.clicked.connect(self._on_recalculate_consensus)
        fl.addWidget(self.recalc_btn)

        fl.addStretch()
        layout.addWidget(filter_frame)

        # Stats bar
        stats_frame = QFrame()
        stats_frame.setFrameShape(QFrame.Shape.StyledPanel)
        sl = QHBoxLayout(stats_frame)
        sl.setSpacing(16)
        sl.setContentsMargins(6, 2, 6, 2)
        self.stat_total = QLabel("Total: 0")
        self.stat_evaluated = QLabel("Evaluated: 0")
        self.stat_win_rate = QLabel("Win Rate: —")
        self.stat_avg_pnl = QLabel("Avg PnL: —")
        self.stat_pending = QLabel("Pending: 0")
        for lbl in [self.stat_total, self.stat_evaluated, self.stat_win_rate, self.stat_avg_pnl, self.stat_pending]:
            lbl.setStyleSheet("font-weight: bold;")
            sl.addWidget(lbl)
        sl.addStretch()
        layout.addWidget(stats_frame)

        # Splitter: table top, details bottom
        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter, 1)

        # Table
        table_w = QWidget()
        tl = QVBoxLayout(table_w)
        tl.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget()
        self.table.setColumnCount(len(self._TABLE_COLS))
        self.table.setHorizontalHeaderLabels(self._TABLE_COLS)
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
        dl.setContentsMargins(0, 0, 0, 0)

        # Details header row
        hdr_layout = QHBoxLayout()
        hdr_layout.addWidget(QLabel("ID:"))
        self.d_id = QLabel("")
        self.d_id.setStyleSheet("font-weight: bold;")
        hdr_layout.addWidget(self.d_id)
        hdr_layout.addSpacing(20)
        hdr_layout.addWidget(QLabel("Ticker:"))
        self.d_ticker = QLabel("")
        self.d_ticker.setStyleSheet("font-weight: bold;")
        hdr_layout.addWidget(self.d_ticker)
        hdr_layout.addSpacing(20)
        hdr_layout.addWidget(QLabel("Date:"))
        self.d_date = QLabel("")
        self.d_date.setStyleSheet("font-weight: bold;")
        hdr_layout.addWidget(self.d_date)
        hdr_layout.addSpacing(20)
        hdr_layout.addWidget(QLabel("Signal:"))
        self.d_signal = QLabel("")
        self.d_signal.setStyleSheet("font-weight: bold;")
        hdr_layout.addWidget(self.d_signal)
        hdr_layout.addStretch()
        dl.addLayout(hdr_layout)

        # Details grid — left (forecast) + right (methods) + eval (bottom)
        grid = QHBoxLayout()
        left_col = QVBoxLayout()
        right_col = QVBoxLayout()

        def _row(label, attr_name, parent_layout):
            row_l = QHBoxLayout()
            row_l.addWidget(QLabel(label))
            lbl = QLabel("")
            setattr(self, attr_name, lbl)
            row_l.addWidget(lbl)
            row_l.addStretch()
            parent_layout.addLayout(row_l)

        _row("Confidence:", "d_conf", left_col)
        _row("Target:", "d_target", left_col)
        _row("Stop Loss:", "d_stop", left_col)
        _row("Entry Limit:", "d_entry", left_col)
        _row("Horizon (h):", "d_horizon", left_col)
        _row("Eval Target Date:", "d_eval_date", left_col)

        for lbl_text, attr in [("Methods Long:", "d_methods_long"), ("Methods Short:", "d_methods_short"), ("Methods Neutral:", "d_methods_neutral")]:
            vl = QVBoxLayout()
            vl.addWidget(QLabel(lbl_text))
            te = QTextEdit()
            te.setReadOnly(True)
            te.setMaximumHeight(55)
            setattr(self, attr, te)
            vl.addWidget(te)
            right_col.addLayout(vl)

        grid.addLayout(left_col, 1)
        grid.addLayout(right_col, 2)
        dl.addLayout(grid)

        # Evaluation results row
        eval_frame = QFrame()
        eval_frame.setFrameShape(QFrame.Shape.StyledPanel)
        eval_l = QHBoxLayout(eval_frame)
        eval_l.setSpacing(12)

        for lbl_text, attr in [
            ("Eval Status:", "d_eval_status"),
            ("Actual Date:", "d_actual_date"),
            ("Actual Close:", "d_actual_close"),
            ("Entry Actual:", "d_entry_actual"),
            ("Direction:", "d_direction"),
            ("Target Hit:", "d_target_hit"),
            ("Stop Hit:", "d_stop_hit"),
            ("First Hit:", "d_first_hit"),
            ("PnL%:", "d_pnl_pct"),
            ("R-multiple:", "d_r_multiple"),
        ]:
            eval_l.addWidget(QLabel(lbl_text))
            lbl = QLabel("—")
            setattr(self, attr, lbl)
            eval_l.addWidget(lbl)

        eval_l.addStretch()
        dl.addWidget(eval_frame)

        # Rationale
        dl.addWidget(QLabel("Rationale:"))
        self.d_rationale = QTextEdit()
        self.d_rationale.setReadOnly(True)
        self.d_rationale.setMaximumHeight(55)
        dl.addWidget(self.d_rationale)

        splitter.addWidget(details_w)
        splitter.setSizes([350, 350])

    def load(self):
        try:
            ticker = self.ticker_combo.currentData() or None
            date_from = self.date_from.text().strip() or None
            date_to = self.date_to.text().strip() or None
            limit = 500
            resp = self.api.get_consensus(ticker=ticker, limit=limit, date_from=date_from, date_to=date_to)
            self._records = resp.items
            self._populate_table()
            self._update_stats()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load consensus:\n{e}")

    def _on_evaluate_now(self):
        try:
            self.evaluate_btn.setEnabled(False)
            self.evaluate_btn.setText("⏳ Evaluating...")
            result = self.api.evaluate_consensus()

            processed = result.get('processed', 0)
            ready_before = result.get('ready_before', 0)
            ready_after = result.get('ready_after', 0)
            not_ready = result.get('not_ready', 0)
            no_target = result.get('no_target', 0)

            if processed > 0:
                msg = f"✅ Evaluated {processed} consensus records\n\n"
                msg += f"Ready to evaluate: {ready_before} → {ready_after}\n"
                msg += f"Still pending (future): {not_ready}\n"
                msg += f"Pending (no target date): {no_target}\n"
                msg += f"Total evaluated in DB: {result.get('total_evaluated', 0)}"
                QMessageBox.information(self, "Evaluate Consensus", msg)
            else:
                msg = "ℹ️ No consensus records were evaluated\n\n"
                if ready_before == 0:
                    total_pending = ready_before + not_ready + no_target
                    msg += f"No records ready for evaluation.\n\n"
                    msg += f"Total PENDING status: {total_pending}\n"
                    msg += f"  - Target date passed (ready): {ready_before}\n"
                    msg += f"  - Target date in future: {not_ready}\n"
                    msg += f"  - No target date: {no_target}\n\n"
                    msg += "Records will be evaluated when eval_target_date <= current time."
                else:
                    msg += f"Found {ready_before} ready records, but none processed.\n"
                    msg += "Check system log for details."
                QMessageBox.information(self, "Evaluate Consensus", msg)

            # Auto-refresh table to show updated statuses
            self.load()

        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to evaluate consensus:\n{e}")
        finally:
            self.evaluate_btn.setEnabled(True)
            self.evaluate_btn.setText("📊 Evaluate Now")

    def _on_recalculate_consensus(self):
        # Ask for confirmation with force option
        reply = QMessageBox.question(
            self,
            "Recalculate Consensus",
            "This will recalculate ALL consensus records from historical forecast logs,\n"
            "including already EVALUATED records.\n\n"
            "Eval fields will be reset and re-evaluated from scratch.\n\n"
            "Proceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self.recalc_btn.setEnabled(False)
            self.recalc_btn.setText("⏳ Recalculating...")

            result = self.api.recalculate_consensus(force=True)

            created = result.get('created', 0)
            updated = result.get('updated', 0)
            skipped = result.get('skipped', 0)
            errors = result.get('errors', 0)
            total = result.get('total_groups', 0)

            evaluated = result.get('evaluated', 0)
            msg = f"✅ Consensus recalculation completed\n\n"
            msg += f"Total groups processed: {total}\n"
            msg += f"  Created: {created}\n"
            msg += f"  Updated: {updated}\n"
            msg += f"  Evaluated (past dates): {evaluated}\n"
            msg += f"  Skipped (no logs): {skipped}\n"
            if errors > 0:
                msg += f"  Errors: {errors}\n"

            QMessageBox.information(self, "Recalculate Consensus", msg)

            # Auto-refresh table
            self.load()

        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to recalculate consensus:\n{e}")
        finally:
            self.recalc_btn.setEnabled(True)
            self.recalc_btn.setText("🔄 Recalculate")

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

    def _update_stats(self):
        total = len(self._records)
        evaluated = [r for r in self._records if str(r.eval_status or "") == "EVALUATED"]
        pending   = [r for r in self._records if str(r.eval_status or "") == "PENDING"]
        wins = [r for r in evaluated if r.direction_correct and int(r.direction_correct) == 1]
        win_rate = f"{len(wins)/len(evaluated)*100:.0f}%" if evaluated else "—"
        pnl_vals = []
        for r in evaluated:
            try:
                pnl_vals.append(float(r.pnl_pct))
            except (TypeError, ValueError):
                pass
        avg_pnl = f"{sum(pnl_vals)/len(pnl_vals):.2f}%" if pnl_vals else "—"
        self.stat_total.setText(f"Total: {total}")
        self.stat_evaluated.setText(f"Evaluated: {len(evaluated)}")
        self.stat_win_rate.setText(f"Win Rate: {win_rate}")
        self.stat_avg_pnl.setText(f"Avg PnL: {avg_pnl}")
        self.stat_pending.setText(f"Pending: {len(pending)}")

    def _populate_table(self):
        signal_filter = self.signal_combo.currentData() or ""
        eval_filter   = self.eval_combo.currentData() or ""
        self._visible = [
            r for r in self._records
            if (not signal_filter or str(r.signal or "") == signal_filter)
            and (not eval_filter or str(r.eval_status or "") == eval_filter)
        ]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)

        def _fmtp(val):
            if val is None:
                return ""
            try:
                return f"{float(val):.2f}"
            except Exception:
                return str(val)

        def _bool_icon(val):
            if val is None:
                return ""
            try:
                return "✅" if int(val) == 1 else "❌"
            except Exception:
                return str(val)

        for row_idx, rec in enumerate(self._visible):
            self.table.insertRow(row_idx)
            date_str = str(rec.date or "")[:16]
            has_disagreement = rec.high_model_disagreement or (rec.rationale and "disagreement" in rec.rationale.lower())
            eval_status = str(rec.eval_status or "")
            eval_date_str = str(rec.eval_target_date or "")[:16]
            cells = [
                date_str,                    # Date
                eval_date_str,               # Eval Date (target)
                str(rec.ticker or ""),       # Ticker
                str(rec.signal or ""),       # Signal
                str(rec.confidence or ""),   # Conf%
                _fmtp(rec.target_price),     # Target
                _fmtp(rec.stop_loss),        # Stop
                _fmtp(rec.entry_limit_price), # Entry
                _fmtp(rec.actual_close),     # Actual Close
                _bool_icon(rec.direction_correct), # Dir
                _bool_icon(rec.target_hit),  # Tgt Hit
                _bool_icon(rec.stop_hit),    # Stp Hit
                str(rec.first_hit or ""),    # First Hit
                _fmtp(rec.pnl_pct),          # PnL%
                _fmtp(rec.r_multiple),       # R
                eval_status,                   # Status
                "⚠️" if has_disagreement else "", # Disagree
            ]
            for col, val in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row_idx)
                self.table.setItem(row_idx, col, item)

            # Color by signal first, then override eval result column
            signal = str(rec.signal or "").upper()
            base_color = _SIDE_COLORS.get(signal, QColor("#ffffff"))
            for col in range(self.table.columnCount()):
                it = self.table.item(row_idx, col)
                if it:
                    it.setBackground(QBrush(base_color))

            # Override eval-result column colors
            pnl_col = self._TABLE_COLS.index("PnL%")
            status_col = self._TABLE_COLS.index("Status")
            try:
                pnl_val = float(rec.pnl_pct) if rec.pnl_pct is not None else None
                if pnl_val is not None:
                    pnl_item = self.table.item(row_idx, pnl_col)
                    if pnl_item:
                        pnl_item.setForeground(QBrush(QColor("#1b5e20") if pnl_val >= 0 else QColor("#b71c1c")))
            except Exception:
                pass
            status_item = self.table.item(row_idx, status_col)
            if status_item:
                if eval_status == "EVALUATED":
                    status_item.setForeground(QBrush(QColor("#1b5e20")))
                elif eval_status == "NO_DATA":
                    status_item.setForeground(QBrush(QColor("#888888")))
                elif eval_status == "PENDING":
                    status_item.setForeground(QBrush(QColor("#e65100")))

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
        rec_idx = item.data(Qt.ItemDataRole.UserRole)
        if rec_idx is None or rec_idx >= len(self._visible):
            return
        rec = self._visible[rec_idx]
        self._current_record = rec
        self._update_details(rec)

    def _update_details(self, rec: ConsensusRecord):
        self.d_id.setText(str(rec.id or ""))
        self.d_ticker.setText(str(rec.ticker or ""))
        self.d_date.setText(str(rec.date or "")[:16])

        signal = str(rec.signal or "")
        self.d_signal.setText(signal)
        signal_upper = signal.upper()
        if signal_upper == "LONG":
            self.d_signal.setStyleSheet("color: #2e7d32; font-weight: bold;")
        elif signal_upper == "SHORT":
            self.d_signal.setStyleSheet("color: #c62828; font-weight: bold;")
        else:
            self.d_signal.setStyleSheet("color: #555; font-weight: bold;")

        conf = rec.confidence
        self.d_conf.setText(f"{conf}%" if conf is not None else "—")

        def _fmt(val, decimals=4):
            if val is None:
                return "—"
            try:
                return f"{float(val):.{decimals}f}"
            except Exception:
                return str(val)

        def _bool_txt(val):
            if val is None:
                return "—"
            try:
                return "Yes ✅" if int(val) == 1 else "No ❌"
            except Exception:
                return str(val)

        self.d_target.setText(_fmt(rec.target_price))
        self.d_stop.setText(_fmt(rec.stop_loss))
        self.d_entry.setText(_fmt(rec.entry_limit_price))
        self.d_horizon.setText(str(rec.horizon_hours) + "h" if rec.horizon_hours else "—")
        self.d_eval_date.setText(str(rec.eval_target_date or "")[:16] or "—")
        self.d_methods_long.setPlainText(str(rec.methods_long or "—"))
        self.d_methods_short.setPlainText(str(rec.methods_short or "—"))
        self.d_methods_neutral.setPlainText(str(rec.methods_neutral or "—"))
        self.d_rationale.setPlainText(str(rec.rationale or ""))

        # Evaluation fields
        eval_status = str(rec.eval_status or "—")
        self.d_eval_status.setText(eval_status)
        if eval_status == "EVALUATED":
            self.d_eval_status.setStyleSheet("color: #1b5e20; font-weight: bold;")
        elif eval_status == "NO_DATA":
            self.d_eval_status.setStyleSheet("color: #888;")
        elif eval_status == "PENDING":
            self.d_eval_status.setStyleSheet("color: #e65100; font-weight: bold;")
        else:
            self.d_eval_status.setStyleSheet("")

        self.d_actual_date.setText(str(rec.actual_date or "")[:10] or "—")
        self.d_actual_close.setText(_fmt(rec.actual_close, 2))
        self.d_entry_actual.setText(_fmt(rec.entry_price_actual, 2))
        self.d_direction.setText(_bool_txt(rec.direction_correct))
        self.d_target_hit.setText(_bool_txt(rec.target_hit))
        self.d_stop_hit.setText(_bool_txt(rec.stop_hit))
        first_hit = str(rec.first_hit or "")
        if first_hit == "target":
            self.d_first_hit.setText("target ✅")
            self.d_first_hit.setStyleSheet("color: #1b5e20; font-weight: bold;")
        elif first_hit == "stop":
            self.d_first_hit.setText("stop ❌")
            self.d_first_hit.setStyleSheet("color: #b71c1c; font-weight: bold;")
        else:
            self.d_first_hit.setText("—")
            self.d_first_hit.setStyleSheet("")

        pnl = rec.pnl_pct
        if pnl is not None:
            try:
                pnl_f = float(pnl)
                self.d_pnl_pct.setText(f"{pnl_f:+.2f}%")
                self.d_pnl_pct.setStyleSheet("color: #1b5e20; font-weight: bold;" if pnl_f >= 0 else "color: #b71c1c; font-weight: bold;")
            except Exception:
                self.d_pnl_pct.setText(str(pnl))
                self.d_pnl_pct.setStyleSheet("")
        else:
            self.d_pnl_pct.setText("—")
            self.d_pnl_pct.setStyleSheet("")

        self.d_r_multiple.setText(_fmt(rec.r_multiple, 2))


class TickersTab(QWidget):
    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._tickers: List[TickerSetting] = []
        self._positions: List[PositionRecord] = []
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
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Active", "Ticker", "Portfolio", "Comment"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)

    def load(self):
        try:
            self._tickers = self.api.get_tickers()
            portfolio_resp = self.api.get_portfolio()
            self._positions = portfolio_resp.items
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

            # Portfolio indicator: Yes if position exists with non-zero quantity
            position_qty = sum(
                (p.quantity or 0) for p in self._positions
                if p.ticker == t.ticker
            )
            portfolio_text = "Yes" if position_qty != 0 else "No"
            portfolio_item = QTableWidgetItem(portfolio_text)
            portfolio_item.setFlags(portfolio_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            portfolio_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row_idx, 2, portfolio_item)

            comment_item = QTableWidgetItem(str(t.comment or ""))
            self.table.setItem(row_idx, 3, comment_item)

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
                comment_item = self.table.item(row_idx, 3)
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
        kg_outer = QVBoxLayout()
        kg_outer.setContentsMargins(0, 0, 0, 0)
        kg_outer.setSpacing(4)
        kg_outer.addLayout(kg)
        self.free_only_cb = QCheckBox("Use only free models (:free suffix)")
        self.free_only_cb.setToolTip(
            "When checked, ':free' is appended to every model ID before calling OpenRouter.\n"
            "Free models have usage limits but require no credits."
        )
        kg_outer.addWidget(self.free_only_cb)
        key_group.setLayout(kg_outer)
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

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Active", "Execute", "Name", "Model", "Rate/min", "Max Tokens"])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
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
            free_only = next((c.value for c in cfg.items if c.key == "OPENROUTER_FREE_ONLY"), "false")
            self.free_only_cb.setChecked(free_only.strip().lower() == "true")

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
                execute=getattr(p, 'execute', 'yes') == 'yes',
                name=p.get_name(),
                model=p.model or "",
                rate=int(p.rate_limit or 60),
                tokens=int(p.max_tokens or 2000),
            )

    def _insert_ai_row(self, active=True, execute=True, name="", model="", rate=60, tokens=2000):
        row = self.table.rowCount()
        self.table.insertRow(row)

        # Active checkbox (col 0)
        cb_w = QWidget(); cb_l = QHBoxLayout(cb_w)
        cb_l.setAlignment(Qt.AlignmentFlag.AlignCenter); cb_l.setContentsMargins(0,0,0,0)
        cb = QCheckBox(); cb.setChecked(active); cb_l.addWidget(cb)
        self.table.setCellWidget(row, 0, cb_w)

        # Execute checkbox (col 1)
        exec_w = QWidget(); exec_l = QHBoxLayout(exec_w)
        exec_l.setAlignment(Qt.AlignmentFlag.AlignCenter); exec_l.setContentsMargins(0,0,0,0)
        exec_cb = QCheckBox(); exec_cb.setChecked(execute); exec_l.addWidget(exec_cb)
        self.table.setCellWidget(row, 1, exec_w)

        self.table.setItem(row, 2, QTableWidgetItem(name))

        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(self._catalog_ids)
        if model and model not in self._catalog_ids:
            combo.insertItem(0, model)
        combo.setCurrentText(model or (self._catalog_ids[0] if self._catalog_ids else ""))
        self.table.setCellWidget(row, 3, combo)

        self.table.setItem(row, 4, QTableWidgetItem(str(rate)))
        self.table.setItem(row, 5, QTableWidgetItem(str(tokens)))

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
        free_only = "true" if self.free_only_cb.isChecked() else "false"
        try:
            self.api.update_config("OPENROUTER_API_KEY", ConfigParam(key="OPENROUTER_API_KEY", value=key))
            self.api.update_config("OPENROUTER_FREE_ONLY", ConfigParam(key="OPENROUTER_FREE_ONLY", value=free_only))
            QMessageBox.information(self, "Saved", "OpenRouter settings saved.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save settings:\n{e}")

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

    def _get_execute_cb(self, table, row) -> Optional[QCheckBox]:
        """Get execute checkbox from column 1."""
        w = table.cellWidget(row, 1)
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
                exec_cb = self._get_execute_cb(self.table, row)
                execute = "yes" if (exec_cb and exec_cb.isChecked()) else "no"
                name_item = self.table.item(row, 2)
                name = (name_item.text().strip() if name_item else "").replace(" ", "_")
                if not name:
                    continue
                combo = self.table.cellWidget(row, 3)
                model = combo.currentText().strip() if combo else ""
                rate_item = self.table.item(row, 4)
                tokens_item = self.table.item(row, 5)
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
                # Save execute flag separately via dedicated endpoint
                self.api.update_provider_execute(name, execute)
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


# ---------------------------------------------------------------------------
# Settings Tab with sub-tabs
# ---------------------------------------------------------------------------

class _KeysSubTab(QWidget):
    """Sub-tab for configuration keys (moved from old ConfigTab)."""

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


class _IBSettingsSubTab(QWidget):
    """Sub-tab for Interactive Brokers settings."""

    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # IB Gateway Connection Settings
        conn_group = QGroupBox("IB Gateway Connection")
        conn_layout = QVBoxLayout(conn_group)

        host_row = QHBoxLayout()
        host_row.addWidget(QLabel("Host:"))
        self.host_edit = QLineEdit("127.0.0.1")
        self.host_edit.setMaximumWidth(200)
        host_row.addWidget(self.host_edit)
        host_row.addStretch()
        conn_layout.addLayout(host_row)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Port:"))
        self.port_edit = QLineEdit("7497")
        self.port_edit.setMaximumWidth(200)
        port_row.addWidget(self.port_edit)
        port_row.addWidget(QLabel("(7497 for TWS, 4001 for IB Gateway)"))
        port_row.addStretch()
        conn_layout.addLayout(port_row)

        client_id_row = QHBoxLayout()
        client_id_row.addWidget(QLabel("Client ID:"))
        self.client_id_edit = QLineEdit("1")
        self.client_id_edit.setMaximumWidth(200)
        client_id_row.addWidget(self.client_id_edit)
        client_id_row.addStretch()
        conn_layout.addLayout(client_id_row)

        layout.addWidget(conn_group)

        # Trading Settings
        trading_group = QGroupBox("Trading Settings")
        trading_layout = QVBoxLayout(trading_group)

        order_type_row = QHBoxLayout()
        order_type_row.addWidget(QLabel("Default Order Type:"))
        self.order_type_combo = QComboBox()
        self.order_type_combo.addItems(["MKT", "LMT", "STP", "STP LMT"])
        self.order_type_combo.setMaximumWidth(200)
        order_type_row.addWidget(self.order_type_combo)
        order_type_row.addStretch()
        trading_layout.addLayout(order_type_row)

        tif_row = QHBoxLayout()
        tif_row.addWidget(QLabel("Time in Force:"))
        self.tif_combo = QComboBox()
        self.tif_combo.addItems(["DAY", "GTC", "IOC", "OPG"])
        self.tif_combo.setMaximumWidth(200)
        tif_row.addWidget(self.tif_combo)
        tif_row.addStretch()
        trading_layout.addLayout(tif_row)

        layout.addWidget(trading_group)

        # IB Order Types Table
        orders_group = QGroupBox("IB Order Types Reference")
        orders_layout = QVBoxLayout(orders_group)

        self.orders_table = QTableWidget(0, 3)
        self.orders_table.setHorizontalHeaderLabels(["Order Type", "Code", "Description"])
        self.orders_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.orders_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.orders_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.orders_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.orders_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

        # Populate order types
        order_types = [
            ("Market", "MKT", "Execute immediately at market price"),
            ("Limit", "LMT", "Execute at specified price or better"),
            ("Stop", "STP", "Market order triggered when stop price is reached"),
            ("Stop Limit", "STP LMT", "Limit order triggered when stop price is reached"),
            ("Market on Close", "MOC", "Execute at closing price"),
            ("Limit on Close", "LOC", "Limit order executed at market close"),
            ("Trailing Stop", "TRAIL", "Stop price follows market by trailing amount"),
            ("Trailing Stop Limit", "TRAIL LMT", "Trailing stop with limit price"),
            ("Market on Open", "MOO", "Execute at market open price"),
            ("Limit on Open", "LOO", "Limit order executed at market open"),
            ("Pegged to Market", "PEGMKT", "Price pegged to market bid/ask"),
            ("Relative", "REL", "Price relative to bid/ask midpoint"),
            ("VWAP", "VWAP", "Volume-weighted average price order"),
            ("TWAP", "TWAP", "Time-weighted average price order"),
            ("Iceberg", "ICE", "Large order split into visible chunks"),
            ("Bracket", "BRACKET", "Entry with attached stop and target"),
            ("OCO", "OCO", "One-cancels-other contingent order"),
            ("MIT", "MIT", "Market if touched (market order when price touched)"),
            ("LIT", "LIT", "Limit if touched (limit order when price touched)"),
        ]

        self.orders_table.setRowCount(len(order_types))
        for row, (name, code, desc) in enumerate(order_types):
            self.orders_table.setItem(row, 0, QTableWidgetItem(name))
            self.orders_table.setItem(row, 1, QTableWidgetItem(code))
            self.orders_table.setItem(row, 2, QTableWidgetItem(desc))

        self.orders_table.resizeColumnsToContents()
        orders_layout.addWidget(self.orders_table)
        layout.addWidget(orders_group, 1)

        # Save button
        btn_row = QHBoxLayout()
        self.save_btn = QPushButton("💾 Save IB Settings")
        self.save_btn.clicked.connect(self._save_settings)
        btn_row.addWidget(self.save_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        layout.addStretch()

    def _save_settings(self):
        from scripts.shared.models import ConfigParam
        keys = {
            "IB_HOST": self.host_edit.text().strip() or "127.0.0.1",
            "IB_PORT": self.port_edit.text().strip() or "7497",
            "IB_CLIENT_ID": self.client_id_edit.text().strip() or "1",
        }
        errors = []
        for key, value in keys.items():
            try:
                self.api.update_config(key, ConfigParam(key=key, value=value))
            except Exception as e:
                errors.append(f"{key}: {e}")
        if errors:
            QMessageBox.warning(self, "Partial Save", "Some settings failed to save:\n" + "\n".join(errors))
        else:
            QMessageBox.information(self, "Saved", "IB Settings saved.")

    def load(self):
        try:
            cfg = self.api.get_config()
            cfg_map = {c.key: c.value for c in cfg.items}
            self.host_edit.setText(cfg_map.get("IB_HOST", "127.0.0.1"))
            self.port_edit.setText(cfg_map.get("IB_PORT", "7497"))
            self.client_id_edit.setText(cfg_map.get("IB_CLIENT_ID", "1"))
        except Exception:
            pass




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


class NewMethodDialog(QDialog):
    """Dialog to create a new forecast method."""

    def __init__(self, existing_methods: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Method")
        self.setMinimumWidth(360)
        self._existing = [m.lower() for m in existing_methods]

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. my_custom_method")
        form.addRow("Method name:", self.name_edit)

        self.timeframe_spin = QSpinBox()
        self.timeframe_spin.setRange(1, 8760)
        self.timeframe_spin.setValue(24)
        self.timeframe_spin.setSuffix(" h")
        form.addRow("Timeframe:", self.timeframe_spin)

        self.trigger_combo = QComboBox()
        self.trigger_combo.addItems(["both", "time", "price_level"])
        form.addRow("Trigger:", self.trigger_combo)

        self.execute_cb = QCheckBox("Execute Orders")
        self.execute_cb.setChecked(True)
        form.addRow("", self.execute_cb)

        layout.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._validate_and_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _validate_and_accept(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation", "Method name cannot be empty.")
            return
        import re
        if not re.match(r'^[a-z][a-z0-9_]*$', name):
            QMessageBox.warning(self, "Validation",
                                "Use snake_case (lowercase letters, digits, underscores).")
            return
        if name.lower() in self._existing:
            QMessageBox.warning(self, "Validation", f"Method '{name}' already exists.")
            return
        self.accept()

    def result_data(self) -> dict:
        return {
            "method": self.name_edit.text().strip(),
            "timeframe_hours": self.timeframe_spin.value(),
            "trigger": self.trigger_combo.currentText(),
            "execute": "yes" if self.execute_cb.isChecked() else "no",
        }


class PromptsTab(QWidget):
    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._templates: dict = {}
        self._method_configs: dict = {}  # method -> {execute: bool, ...}
        self._current_method: str = ""
        self._dirty = False
        self._build_ui()

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setSpacing(8)

        # ── Left: method list ─────────────────────────────────────────
        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Methods</b>"))

        # Execute checkbox for selected method
        self.execute_cb = QCheckBox("Execute Orders")
        self.execute_cb.setToolTip("Allow this method to create trading orders")
        self.execute_cb.stateChanged.connect(self._on_execute_changed)
        left.addWidget(self.execute_cb)

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
            # Load prompt templates
            data = self.api.get_prompt_templates()
            self._templates = data.get("templates", {})

            # Load method configs with execute flags
            configs = self.api.get_method_configs()
            self._method_configs = {cfg["method"]: cfg for cfg in configs}

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
        self._update_execute_checkbox()

    def _update_execute_checkbox(self):
        """Update execute checkbox based on current method config."""
        if not self._current_method:
            self.execute_cb.setEnabled(False)
            self.execute_cb.setChecked(False)
            return

        cfg = self._method_configs.get(self._current_method, {})
        execute = cfg.get("execute", "yes")
        self.execute_cb.blockSignals(True)
        self.execute_cb.setEnabled(True)
        self.execute_cb.setChecked(execute == "yes")
        self.execute_cb.blockSignals(False)

    def _on_execute_changed(self, state):
        """Handle execute checkbox change and save to API."""
        if not self._current_method:
            return

        execute = "yes" if state == Qt.CheckState.Checked.value else "no"
        try:
            self.api.update_method_execute(self._current_method, execute)
            # Update local cache
            if self._current_method in self._method_configs:
                self._method_configs[self._current_method]["execute"] = execute
            else:
                self._method_configs[self._current_method] = {"execute": execute}
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to update execute flag:\n{e}")
            # Revert checkbox
            self._update_execute_checkbox()

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
# Helpers
# ---------------------------------------------------------------------------

class NumericTableWidgetItem(QTableWidgetItem):
    """QTableWidgetItem that sorts by numeric value rather than display text."""

    def __init__(self, display: str, value: float):
        super().__init__(display)
        self._value = value

    def __lt__(self, other: "QTableWidgetItem") -> bool:
        if isinstance(other, NumericTableWidgetItem):
            return self._value < other._value
        try:
            return self._value < float(other.text().replace(",", ""))
        except (ValueError, AttributeError):
            return super().__lt__(other)


# ---------------------------------------------------------------------------
# Price Data Tab
# ---------------------------------------------------------------------------

class PriceDataTab(QWidget):
    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._items: list = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # --- Filter bar ---
        filter_frame = QFrame()
        filter_frame.setFrameShape(QFrame.Shape.StyledPanel)
        bar = QHBoxLayout(filter_frame)
        bar.setSpacing(6)

        bar.addWidget(QLabel("Ticker:"))
        self.ticker_combo = QComboBox()
        self.ticker_combo.setMinimumWidth(140)
        self.ticker_combo.addItem("ALL", "")
        bar.addWidget(self.ticker_combo)

        bar.addWidget(QLabel("From:"))
        self.from_edit = QLineEdit()
        self.from_edit.setPlaceholderText("2025-01-01")
        self.from_edit.setMaximumWidth(110)
        bar.addWidget(self.from_edit)

        bar.addWidget(QLabel("To:"))
        self.to_edit = QLineEdit()
        self.to_edit.setPlaceholderText("2025-12-31")
        self.to_edit.setMaximumWidth(110)
        bar.addWidget(self.to_edit)

        bar.addWidget(QLabel("Price ≥:"))
        self.price_min_edit = QLineEdit()
        self.price_min_edit.setPlaceholderText("0")
        self.price_min_edit.setMaximumWidth(80)
        bar.addWidget(self.price_min_edit)

        bar.addWidget(QLabel("Price ≤:"))
        self.price_max_edit = QLineEdit()
        self.price_max_edit.setPlaceholderText("∞")
        self.price_max_edit.setMaximumWidth(80)
        bar.addWidget(self.price_max_edit)

        bar.addWidget(QLabel("Vol ≥:"))
        self.vol_min_edit = QLineEdit()
        self.vol_min_edit.setPlaceholderText("0")
        self.vol_min_edit.setMaximumWidth(90)
        bar.addWidget(self.vol_min_edit)

        self.load_btn = QPushButton("🔄 Load")
        self.load_btn.clicked.connect(self.load)
        bar.addWidget(self.load_btn)

        self.filter_btn = QPushButton("🔍 Filter")
        self.filter_btn.clicked.connect(lambda: self._populate_table(self._items))
        bar.addWidget(self.filter_btn)

        self.total_label = QLabel("")
        bar.addWidget(self.total_label)
        bar.addStretch()
        layout.addWidget(filter_frame)

        # --- Table ---
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Ticker", "Date", "Open", "High", "Low", "Close", "Volume"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table)

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

    def load(self):
        try:
            ticker = self.ticker_combo.currentData() or None
            date_from = self.from_edit.text().strip() or None
            date_to = self.to_edit.text().strip() or None
            resp = self.api.get_price_data(ticker=ticker, date_from=date_from, date_to=date_to, limit=2000)
            self._items = resp.items if resp else []
            self._populate_table(self._items)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load price data:\n{e}")

    def _populate_table(self, items: list):
        try:
            price_min = float(self.price_min_edit.text().strip()) if self.price_min_edit.text().strip() else None
            price_max = float(self.price_max_edit.text().strip()) if self.price_max_edit.text().strip() else None
            vol_min   = float(self.vol_min_edit.text().strip())   if self.vol_min_edit.text().strip()   else None
        except ValueError:
            price_min = price_max = vol_min = None

        filtered = []
        for p in items:
            close = float(p.close) if p.close is not None else None
            vol   = float(p.volume) if p.volume is not None else None
            if price_min is not None and (close is None or close < price_min):
                continue
            if price_max is not None and (close is None or close > price_max):
                continue
            if vol_min is not None and (vol is None or vol < vol_min):
                continue
            filtered.append(p)

        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for r, p in enumerate(filtered):
            self.table.insertRow(r)
            # Ticker (text)
            self.table.setItem(r, 0, QTableWidgetItem(str(p.ticker or "")))
            # Date (text — sorts correctly as ISO)
            self.table.setItem(r, 1, QTableWidgetItem(str(p.date or "")))
            # Numeric columns: Open, High, Low, Close
            for col, raw in [(2, p.open), (3, p.high), (4, p.low), (5, p.close)]:
                try:
                    fval = float(raw)
                    item = NumericTableWidgetItem(f"{fval:.2f}", fval)
                except (TypeError, ValueError):
                    item = NumericTableWidgetItem("", -1.0)
                self.table.setItem(r, col, item)
            # Volume (numeric)
            try:
                vval = float(p.volume)
                vitem = NumericTableWidgetItem(f"{int(vval):,}", vval)
            except (TypeError, ValueError):
                vitem = NumericTableWidgetItem("", -1.0)
            self.table.setItem(r, 6, vitem)
        self.table.setSortingEnabled(True)
        self.total_label.setText(f"Rows: {len(filtered)}")


# ---------------------------------------------------------------------------
# Indicators Tab
# ---------------------------------------------------------------------------

class IndicatorsTab(QWidget):
    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._items: list = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # --- Filter bar ---
        filter_frame = QFrame()
        filter_frame.setFrameShape(QFrame.Shape.StyledPanel)
        bar = QHBoxLayout(filter_frame)
        bar.setSpacing(6)

        bar.addWidget(QLabel("Ticker:"))
        self.ticker_combo = QComboBox()
        self.ticker_combo.setMinimumWidth(140)
        self.ticker_combo.addItem("ALL", "")
        bar.addWidget(self.ticker_combo)

        bar.addWidget(QLabel("From:"))
        self.from_edit = QLineEdit()
        self.from_edit.setPlaceholderText("2025-01-01")
        self.from_edit.setMaximumWidth(110)
        bar.addWidget(self.from_edit)

        bar.addWidget(QLabel("To:"))
        self.to_edit = QLineEdit()
        self.to_edit.setPlaceholderText("2025-12-31")
        self.to_edit.setMaximumWidth(110)
        bar.addWidget(self.to_edit)

        self.load_btn = QPushButton("🔄 Load")
        self.load_btn.clicked.connect(self.load)
        bar.addWidget(self.load_btn)

        self.filter_btn = QPushButton("🔍 Filter")
        self.filter_btn.clicked.connect(lambda: self._populate_table(self._items))
        bar.addWidget(self.filter_btn)

        self.total_label = QLabel("")
        bar.addWidget(self.total_label)
        bar.addStretch()
        layout.addWidget(filter_frame)

        # --- Table ---
        headers = ["Ticker", "Date", "Price", "MA20", "MA50", "MA200",
                   "EMA9", "EMA21", "RSI14", "StochRSI", "ATR14", "ADX14",
                   "MACD", "Signal", "Hist", "BB▲", "BB▼", "OBV",
                   "Chg5d%", "Chg20d%", "Vol/Avg", "Regime"]
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table)

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

    def load(self):
        try:
            ticker = self.ticker_combo.currentData() or None
            date_from = self.from_edit.text().strip() or None
            date_to = self.to_edit.text().strip() or None
            resp = self.api.get_indicators(ticker=ticker, date_from=date_from, date_to=date_to, limit=2000)
            self._items = resp.items if resp else []
            self._populate_table(self._items)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load indicators:\n{e}")

    def _populate_table(self, items: list):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
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
            self.table.insertRow(r)
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
        self.table.setSortingEnabled(True)
        self.total_label.setText(f"Rows: {len(items)}")


class AccountsTab(QWidget):
    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._accounts: List[AccountRecord] = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        toolbar = QHBoxLayout()
        self.sync_btn = QPushButton("Sync with IB Gateway")
        self.sync_btn.setToolTip("Fetch account balances from IB Gateway")
        self.sync_btn.clicked.connect(self._on_sync)
        toolbar.addWidget(self.sync_btn)

        self.host_edit = QLineEdit("127.0.0.1")
        self.host_edit.setMaximumWidth(120)
        toolbar.addWidget(QLabel("Host:"))
        toolbar.addWidget(self.host_edit)

        self.port_edit = QLineEdit("7497")
        self.port_edit.setMaximumWidth(60)
        toolbar.addWidget(QLabel("Port:"))
        toolbar.addWidget(self.port_edit)

        toolbar.addStretch()
        layout.addLayout(toolbar)

        self.table = QTableWidget()
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels([
            "Broker", "Account ID", "Name", "Type", "Base CCY",
            "Net Liq", "Buying Power", "Available Funds", "Cash"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table, 1)

        self.summary_label = QLabel("Accounts: 0 | Total Net Liq: $0.00 | Total Buying Power: $0.00")
        layout.addWidget(self.summary_label)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.load)
        toolbar.addWidget(self.refresh_btn)

    def _on_sync(self):
        host = self.host_edit.text().strip()
        try:
            port = int(self.port_edit.text().strip())
        except ValueError:
            port = 7497
        self.sync_btn.setEnabled(False)
        self.sync_btn.setText("Syncing…")
        try:
            self.api.sync_accounts(host=host, port=port)
            self.load()
            QMessageBox.information(self, "Sync Complete", "Accounts synced from IB Gateway.")
        except Exception as e:
            QMessageBox.critical(self, "Sync Failed", str(e))
        finally:
            self.sync_btn.setEnabled(True)
            self.sync_btn.setText("Sync with IB Gateway")

    def load(self):
        try:
            resp = self.api.get_accounts()
            self._accounts = resp.items
            self._populate_table()
            self._update_summary()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load accounts:\n{e}")

    def _populate_table(self):
        self.table.setRowCount(len(self._accounts))
        for r, acc in enumerate(self._accounts):
            vals = [
                acc.broker or "",
                acc.account_id or "",
                acc.name or "",
                acc.account_type or "",
                acc.base_currency or "USD",
                f"${acc.net_liquidation:,.2f}" if acc.net_liquidation else "$0.00",
                f"${acc.buying_power:,.2f}" if acc.buying_power else "$0.00",
                f"${acc.available_funds:,.2f}" if acc.available_funds else "$0.00",
                f"${acc.cash:,.2f}" if acc.cash else "$0.00",
            ]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()

    def _update_summary(self):
        total_nliq = sum(a.net_liquidation or 0 for a in self._accounts)
        total_bp = sum(a.buying_power or 0 for a in self._accounts)
        self.summary_label.setText(
            f"Accounts: {len(self._accounts)} | "
            f"Total Net Liq: ${total_nliq:,.2f} | "
            f"Total Buying Power: ${total_bp:,.2f}"
        )


class PortfolioTab(QWidget):
    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._positions: List[PositionRecord] = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # Toolbar
        toolbar = QHBoxLayout()
        self.sync_btn = QPushButton("Sync with IB Gateway")
        self.sync_btn.setToolTip("Connect to IB Gateway and fetch current positions")
        self.sync_btn.clicked.connect(self._on_sync)
        toolbar.addWidget(self.sync_btn)

        self.host_edit = QLineEdit("127.0.0.1")
        self.host_edit.setMaximumWidth(120)
        toolbar.addWidget(QLabel("Host:"))
        toolbar.addWidget(self.host_edit)

        self.port_edit = QLineEdit("7497")
        self.port_edit.setMaximumWidth(60)
        toolbar.addWidget(QLabel("Port:"))
        toolbar.addWidget(self.port_edit)

        toolbar.addStretch()
        layout.addLayout(toolbar)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(10)
        self.table.setHorizontalHeaderLabels([
            "Ticker", "Account", "Qty", "Avg Cost", "Mkt Price",
            "Mkt Value", "Unreal PnL", "Real PnL", "Currency", "Updated"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table, 1)

        # Summary
        self.summary_label = QLabel("Positions: 0 | Total Value: $0.00 | Unrealized PnL: $0.00")
        layout.addWidget(self.summary_label)

        # Refresh btn
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.load)
        toolbar.addWidget(self.refresh_btn)

    def _on_sync(self):
        host = self.host_edit.text().strip()
        try:
            port = int(self.port_edit.text().strip())
        except ValueError:
            port = 7497
        self.sync_btn.setEnabled(False)
        self.sync_btn.setText("Syncing…")
        try:
            self.api.sync_portfolio(host=host, port=port)
            self.load()
            QMessageBox.information(self, "Sync Complete", "Portfolio synced from IB Gateway.")
        except Exception as e:
            QMessageBox.critical(self, "Sync Failed", str(e))
        finally:
            self.sync_btn.setEnabled(True)
            self.sync_btn.setText("Sync with IB Gateway")

    def load(self):
        try:
            resp = self.api.get_portfolio()
            self._positions = resp.items
            self._populate_table()
            self._update_summary()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load portfolio:\n{e}")

    def _populate_table(self):
        self.table.setRowCount(len(self._positions))
        for r, pos in enumerate(self._positions):
            vals = [
                pos.ticker or "",
                pos.account or "",
                f"{pos.quantity:,.0f}" if pos.quantity else "0",
                f"${pos.avg_cost:,.2f}" if pos.avg_cost else "$0.00",
                f"${pos.market_price:,.2f}" if pos.market_price else "$0.00",
                f"${pos.market_value:,.2f}" if pos.market_value else "$0.00",
                f"${pos.unrealized_pnl:,.2f}" if pos.unrealized_pnl else "$0.00",
                f"${pos.realized_pnl:,.2f}" if pos.realized_pnl else "$0.00",
                pos.currency or "USD",
                pos.last_update or "",
            ]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c == 6 and pos.unrealized_pnl and pos.unrealized_pnl > 0:
                    item.setForeground(QBrush(QColor("#2e7d32")))
                elif c == 6 and pos.unrealized_pnl and pos.unrealized_pnl < 0:
                    item.setForeground(QBrush(QColor("#c62828")))
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()

    def _update_summary(self):
        total_val = sum(p.market_value or 0 for p in self._positions)
        total_unreal = sum(p.unrealized_pnl or 0 for p in self._positions)
        self.summary_label.setText(
            f"Positions: {len(self._positions)} | "
            f"Total Value: ${total_val:,.2f} | "
            f"Unrealized PnL: ${total_unreal:,.2f}"
        )


# ---------------------------------------------------------------------------
# Scheduler Tab (main tab replacing Run)
# ---------------------------------------------------------------------------

_TASK_STATUS_COLORS = {
    "ok":    QColor("#c8e6c9"),
    "error": QColor("#ffcdd2"),
    "":      QColor("#f5f5f5"),
}

_HB_OK  = "✅"
_HB_ERR = "❌"


class SchedulerTab(QWidget):
    """Main Scheduler tab: manual run buttons + task list + heartbeat log."""

    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._tasks: list = []
        self._poller: Optional[StatusPoller] = None
        self._last_log_count = 0
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.load)
        self._timer.start(30_000)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # --- Manual Run section ---
        run_group = QGroupBox("Manual Run")
        run_layout = QVBoxLayout(run_group)

        btn_row = QHBoxLayout()
        self.forecast_btn = QPushButton("🤖 Forecast")
        self.forecast_btn.setMinimumHeight(36)
        self.forecast_btn.clicked.connect(lambda: self._run("forecast"))
        btn_row.addWidget(self.forecast_btn)

        self.price_data_btn = QPushButton("📈 Price Data")
        self.price_data_btn.setMinimumHeight(36)
        self.price_data_btn.clicked.connect(lambda: self._run("price_data"))
        btn_row.addWidget(self.price_data_btn)

        self.evaluate_btn = QPushButton("📊 Evaluate")
        self.evaluate_btn.setMinimumHeight(36)
        self.evaluate_btn.clicked.connect(lambda: self._run("evaluate"))
        btn_row.addWidget(self.evaluate_btn)

        self.full_btn = QPushButton("🔄 RECALCULATE ALL")
        self.full_btn.setMinimumHeight(42)
        self.full_btn.setStyleSheet("font-weight: bold; font-size: 12px; background-color: #1976d2; color: white;")
        self.full_btn.clicked.connect(lambda: self._run("full"))
        btn_row.addWidget(self.full_btn)

        btn_row.addStretch()
        run_layout.addLayout(btn_row)

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
        run_layout.addLayout(info_row)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setFont(QFont("Consolas", 9))
        self.log_edit.setMaximumHeight(120)
        run_layout.addWidget(self.log_edit)

        clear_row = QHBoxLayout()
        clear_row.addStretch()
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.log_edit.clear)
        clear_row.addWidget(self.clear_btn)
        run_layout.addLayout(clear_row)

        layout.addWidget(run_group)

        # --- Tasks table ---
        tasks_group = QGroupBox("Scheduled Tasks")
        tasks_layout = QVBoxLayout(tasks_group)

        task_btn_row = QHBoxLayout()
        self.refresh_btn = QPushButton("↺ Refresh")
        self.refresh_btn.clicked.connect(self.load)
        task_btn_row.addWidget(self.refresh_btn)
        self.save_btn = QPushButton("💾 Save")
        self.save_btn.clicked.connect(self._save)
        task_btn_row.addWidget(self.save_btn)
        task_btn_row.addStretch()
        tasks_layout.addLayout(task_btn_row)

        self.tasks_table = QTableWidget()
        self.tasks_table.setColumnCount(9)
        self.tasks_table.setHorizontalHeaderLabels([
            "Active", "Name", "Interval (s)", "Last Run", "Status",
            "Runs", "Errors", "Last Error", "Run Now",
        ])
        hdr = self.tasks_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(8, QHeaderView.ResizeMode.ResizeToContents)
        self.tasks_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tasks_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tasks_layout.addWidget(self.tasks_table)
        layout.addWidget(tasks_group, 1)

        # --- Heartbeat log ---
        hb_group = QGroupBox("Heartbeat Log (last 15)")
        hb_layout = QVBoxLayout(hb_group)

        self.hb_table = QTableWidget()
        self.hb_table.setColumnCount(5)
        self.hb_table.setHorizontalHeaderLabels(["Time", "IB", "OpenRouter", "SQLite", "Notes"])
        hb_hdr = self.hb_table.horizontalHeader()
        hb_hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hb_hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hb_hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hb_hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hb_hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.hb_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.hb_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hb_layout.addWidget(self.hb_table)
        layout.addWidget(hb_group)

    # --- Manual run helpers ---

    def _run(self, mode: str):
        try:
            if mode == "forecast":
                resp = self.api.run_forecast()
            elif mode == "evaluate":
                resp = self.api.run_evaluate()
            elif mode == "price_data":
                resp = self.api.run_price_data()
            else:
                resp = self.api.run_full()
            self._set_run_buttons(False)
            self._apply_run_status(resp)
            self._start_polling()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to start {mode}:\n{e}")

    def _set_run_buttons(self, enabled: bool):
        self.forecast_btn.setEnabled(enabled)
        self.price_data_btn.setEnabled(enabled)
        self.evaluate_btn.setEnabled(enabled)
        self.full_btn.setEnabled(enabled)

    def _start_polling(self):
        if self._poller and self._poller.isRunning():
            self._poller.stop()
        self._last_log_count = 0
        self._poller = StatusPoller(self.api)
        self._poller.status_updated.connect(self._apply_run_status)
        self._poller.finished.connect(lambda: self._set_run_buttons(True))
        self._poller.start()

    def _apply_run_status(self, resp):
        status = resp.status.upper()
        if status == "RUNNING":
            self.status_label.setText("● RUNNING")
            self.status_label.setStyleSheet("color: #f57f17; font-weight: bold;")
        elif status == "DONE":
            self.status_label.setText("● DONE")
            self.status_label.setStyleSheet("color: #2e7d32; font-weight: bold;")
            self._set_run_buttons(True)
        elif status == "ERROR":
            self.status_label.setText("● ERROR")
            self.status_label.setStyleSheet("color: #c62828; font-weight: bold;")
            self._set_run_buttons(True)
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

    # --- Scheduler task helpers ---

    def load(self):
        self._load_tasks()
        self._load_heartbeat()

    def _load_tasks(self):
        try:
            self._tasks = self.api.get_scheduler_tasks()
            self._populate_tasks()
        except Exception as e:
            logger.warning(f"Scheduler tasks load error: {e}")

    def _populate_tasks(self):
        self.tasks_table.setRowCount(0)
        for row_idx, t in enumerate(self._tasks):
            self.tasks_table.insertRow(row_idx)

            # Active checkbox
            cb_widget = QWidget()
            cb_layout = QHBoxLayout(cb_widget)
            cb_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cb_layout.setContentsMargins(0, 0, 0, 0)
            cb = QCheckBox()
            cb.setChecked(bool(t.get("is_active", 1)))
            cb_layout.addWidget(cb)
            self.tasks_table.setCellWidget(row_idx, 0, cb_widget)

            name        = str(t.get("name", ""))
            interval    = str(t.get("schedule_value", ""))
            last_run    = str(t.get("last_run_at", "") or "—")
            status_val  = str(t.get("last_run_status", "") or "")
            run_count   = str(t.get("run_count", 0))
            error_count = int(t.get("error_count", 0) or 0)
            last_error  = str(t.get("last_error", "") or "")

            cols = [name, interval, last_run, status_val, run_count, str(error_count), last_error]
            for col_idx, val in enumerate(cols, start=1):
                item = QTableWidgetItem(val)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col_idx == 4:
                    color = _TASK_STATUS_COLORS.get(status_val.lower(), _TASK_STATUS_COLORS[""])
                    item.setBackground(QBrush(color))
                if col_idx == 6 and error_count > 0:
                    item.setForeground(QBrush(QColor("#c62828")))
                    item.setFont(QFont("", -1, QFont.Weight.Bold))
                self.tasks_table.setItem(row_idx, col_idx, item)

            # Run Now button (col 8)
            run_btn = QPushButton("▶ Run")
            run_btn.setFixedHeight(24)
            run_btn.clicked.connect(lambda checked, task_name=name: self._trigger_task(task_name))
            self.tasks_table.setCellWidget(row_idx, 8, run_btn)

    def _trigger_task(self, task_name: str):
        _TASK_TO_MODE = {
            "forecast":           "forecast",
            "scheduled_forecast": "forecast",
            "evaluate":           "evaluate",
            "scheduled_evaluate": "evaluate",
            "consensus_evaluate": "evaluate",
            "full":               "full",
            "update_price_data":  "price_data",
        }
        mode = _TASK_TO_MODE.get(task_name)
        if mode:
            self._run(mode)
            return
        QMessageBox.information(
            self, "Run Task",
            f"Task '{task_name}' is managed by the server scheduler.\n"
            f"It runs automatically on its configured interval."
        )

    def _get_checkbox(self, row: int) -> Optional[QCheckBox]:
        w = self.tasks_table.cellWidget(row, 0)
        if w:
            for child in w.children():
                if isinstance(child, QCheckBox):
                    return child
        return None

    def _save(self):
        errors = []
        saved = 0
        for row_idx, t in enumerate(self._tasks):
            cb = self._get_checkbox(row_idx)
            if cb is None:
                continue
            new_active = 1 if cb.isChecked() else 0
            old_active = int(t.get("is_active", 1))
            if new_active != old_active:
                try:
                    self.api.set_task_active(t["name"], new_active)
                    saved += 1
                except Exception as e:
                    errors.append(f"{t['name']}: {e}")
        if errors:
            QMessageBox.warning(self, "Save Error", "\n".join(errors))
        elif saved:
            QMessageBox.information(self, "Saved", f"Updated {saved} task(s).")
        self.load()

    def _load_heartbeat(self):
        try:
            items = self.api.get_heartbeat_history(limit=15)
            self._populate_heartbeat(items)
        except Exception as e:
            logger.warning(f"Heartbeat history load error: {e}")

    def _populate_heartbeat(self, items: list):
        self.hb_table.setRowCount(0)
        for row_idx, h in enumerate(items):
            self.hb_table.insertRow(row_idx)
            checked_at = str(h.get("checked_at", "") or "")
            ib_ok  = _HB_OK if h.get("ib_ok")         else _HB_ERR
            or_ok  = _HB_OK if h.get("openrouter_ok") else _HB_ERR
            sq_ok  = _HB_OK if h.get("sqlite_ok")     else _HB_ERR
            notes  = str(h.get("notes", "") or "")

            for col_idx, val in enumerate([checked_at, ib_ok, or_ok, sq_ok, notes]):
                item = QTableWidgetItem(val)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col_idx in (1, 2, 3) and val == _HB_ERR:
                    item.setForeground(QBrush(QColor("#c62828")))
                self.hb_table.setItem(row_idx, col_idx, item)


class OrdersTab(QWidget):
    """Tab showing all orders from the orders table with cancel support."""

    _COLUMNS = ["ID", "Ticker", "Side", "Qty", "Price", "Status", "Type", "Account", "IB Order ID", "Created At"]
    _STATUS_COLORS = {
        "FILLED": QColor("#c8e6c9"),
        "CANCELLED": QColor("#ffcdd2"),
        "REJECTED": QColor("#ffcdd2"),
        "SUBMITTED": QColor("#fff9c4"),
        "PENDING": QColor("#fff9c4"),
    }

    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._orders: list = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # Filter row
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Ticker:"))
        self.ticker_filter = QLineEdit()
        self.ticker_filter.setPlaceholderText("All")
        self.ticker_filter.setMaximumWidth(120)
        filter_row.addWidget(self.ticker_filter)

        filter_row.addWidget(QLabel("Status:"))
        self.status_filter = QComboBox()
        self.status_filter.addItems(["All", "PENDING", "SUBMITTED", "FILLED", "CANCELLED", "REJECTED"])
        self.status_filter.setMaximumWidth(120)
        filter_row.addWidget(self.status_filter)

        self.refresh_btn = QPushButton("🔄 Refresh")
        self.refresh_btn.clicked.connect(self.load)
        filter_row.addWidget(self.refresh_btn)

        self.cancel_btn = QPushButton("❌ Cancel Selected")
        self.cancel_btn.clicked.connect(self._cancel_selected)
        filter_row.addWidget(self.cancel_btn)

        filter_row.addStretch()
        self.total_label = QLabel("Orders: 0")
        filter_row.addWidget(self.total_label)
        layout.addLayout(filter_row)

        # Table
        self.table = QTableWidget(0, len(self._COLUMNS))
        self.table.setHorizontalHeaderLabels(self._COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(9, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table, 1)

    def load(self):
        ticker = self.ticker_filter.text().strip() or None
        status = self.status_filter.currentText()
        status = None if status == "All" else status
        try:
            self._orders = self.api.get_orders(ticker=ticker, status=status, limit=500)
        except Exception as e:
            logger.warning(f"OrdersTab.load error: {e}")
            self._orders = []
        self._populate()

    def _populate(self):
        orders = self._orders
        self.table.setRowCount(len(orders))
        for row, o in enumerate(orders):
            vals = [
                str(o.get("id", "")),
                str(o.get("ticker", "")),
                str(o.get("side", "")),
                str(o.get("quantity", "")),
                str(o.get("limit_price", "") or ""),
                str(o.get("status", "")),
                str(o.get("order_type", "") or ""),
                str(o.get("account_type", "") or ""),
                str(o.get("ib_order_id", "") or ""),
                str(o.get("created_at", "") or ""),
            ]
            bg = self._STATUS_COLORS.get(o.get("status", ""))
            for col, val in enumerate(vals):
                item = QTableWidgetItem(val)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if bg:
                    item.setBackground(QBrush(bg))
                self.table.setItem(row, col, item)
        self.total_label.setText(f"Orders: {len(orders)}")

    def _cancel_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "Info", "Select a row to cancel.")
            return
        row = rows[0].row()
        order_id = int(self.table.item(row, 0).text())
        status = self.table.item(row, 5).text() if self.table.item(row, 5) else ""
        if QMessageBox.question(
            self, "Cancel Order",
            f"Cancel order #{order_id}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self.api.cancel_order(order_id)
            ok = result.get("cancelled", False)
            msg = f"Order #{order_id}: {'cancelled' if ok else 'cancel failed'}."
            QMessageBox.information(self, "Result", msg)
            self.load()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))


class ForecastRunsTab(QWidget):
    """Tab for viewing forecast runs with full weight snapshots."""

    _RUN_COLS = ["ID", "Started", "Trigger", "Tickers", "Consensus", "Forecasts", "Included", "Status"]
    _LINK_COLS = ["Log ID", "Ticker", "Method", "Model", "Signal", "Raw Conf", "Win Rate", "EMA Acc", "Final Wt", "Cal Conf", "Norm R", "In Consensus", "Target", "Stop"]

    _RUN_STATUS_COLORS = {
        "completed": QColor("#c8e6c9"),
        "failed":    QColor("#ffcdd2"),
        "running":   QColor("#fff9c4"),
    }

    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._runs: list = []
        self._current_run_id: Optional[int] = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # Toolbar
        bar = QHBoxLayout()
        self.refresh_btn = QPushButton("↺ Refresh")
        self.refresh_btn.clicked.connect(self.load)
        bar.addWidget(self.refresh_btn)
        bar.addWidget(QLabel("Limit:"))
        self.limit_combo = QComboBox()
        for n in ["25", "50", "100", "200"]:
            self.limit_combo.addItem(n)
        self.limit_combo.setCurrentIndex(1)
        bar.addWidget(self.limit_combo)
        bar.addStretch()
        self.total_label = QLabel("Runs: 0")
        bar.addWidget(self.total_label)
        layout.addLayout(bar)

        # Splitter: runs table (top) + links table (bottom)
        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter, 1)

        # Runs table
        runs_w = QWidget()
        rl = QVBoxLayout(runs_w)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("<b>Forecast Runs</b>"))
        self.runs_table = QTableWidget(0, len(self._RUN_COLS))
        self.runs_table.setHorizontalHeaderLabels(self._RUN_COLS)
        self.runs_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.runs_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.runs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.runs_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.runs_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.runs_table.setSortingEnabled(True)
        self.runs_table.itemSelectionChanged.connect(self._on_run_selected)
        rl.addWidget(self.runs_table)
        splitter.addWidget(runs_w)

        # Links table
        links_w = QWidget()
        ll = QVBoxLayout(links_w)
        ll.setContentsMargins(0, 0, 0, 0)
        self.links_header = QLabel("<b>Forecast Weights</b> — select a run above")
        ll.addWidget(self.links_header)
        self.links_table = QTableWidget(0, len(self._LINK_COLS))
        self.links_table.setHorizontalHeaderLabels(self._LINK_COLS)
        self.links_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.links_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.links_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.links_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.links_table.setSortingEnabled(True)
        self.links_table.setAlternatingRowColors(True)
        ll.addWidget(self.links_table)
        splitter.addWidget(links_w)
        splitter.setSizes([300, 400])

    def load(self):
        try:
            limit = int(self.limit_combo.currentText())
            data = self.api.get_forecast_runs(limit=limit)
            self._runs = data.get("items", [])
            self._populate_runs()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load forecast runs:\n{e}")

    def _populate_runs(self):
        self.runs_table.setSortingEnabled(False)
        self.runs_table.setRowCount(0)
        for row_idx, run in enumerate(self._runs):
            self.runs_table.insertRow(row_idx)
            started = str(run.get("started_at", "") or "")[:16]
            cells = [
                str(run.get("id", "")),
                started,
                str(run.get("trigger_type", "") or ""),
                str(run.get("tickers_processed", "") or "0"),
                str(run.get("consensus_count", "") or "0"),
                str(run.get("total_forecasts", "") or ""),
                str(run.get("included_forecasts", "") or ""),
                str(run.get("status", "") or ""),
            ]
            status_val = str(run.get("status", "")).lower()
            bg = self._RUN_STATUS_COLORS.get(status_val)
            for col, val in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row_idx)
                if bg:
                    item.setBackground(QBrush(bg))
                self.runs_table.setItem(row_idx, col, item)
        self.runs_table.setSortingEnabled(True)
        self.runs_table.resizeColumnsToContents()
        self.total_label.setText(f"Runs: {len(self._runs)}")
        self.links_table.setRowCount(0)
        self.links_header.setText("<b>Forecast Weights</b> — select a run above")

    def _on_run_selected(self):
        row = self.runs_table.currentRow()
        if row < 0:
            return
        item = self.runs_table.item(row, 0)
        if item is None:
            return
        run_idx = item.data(Qt.ItemDataRole.UserRole)
        if run_idx is None or run_idx >= len(self._runs):
            return
        run = self._runs[run_idx]
        run_id = run.get("id")
        if run_id is None:
            return
        self._current_run_id = run_id
        self._load_links(run_id)

    def _load_links(self, run_id: int):
        try:
            data = self.api.get_forecast_run(run_id)
            links = data.get("links", [])
            run = data.get("run", {})
            ticker_count = run.get("tickers_with_forecasts") or len(set(l.get("ticker") for l in links))
            self.links_header.setText(
                f"<b>Forecast Weights — Run #{run_id}</b>  "
                f"({len(links)} forecasts, {ticker_count} tickers)"
            )
            self._populate_links(links)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load run details:\n{e}")

    def _populate_links(self, links: list):
        self.links_table.setSortingEnabled(False)
        self.links_table.setRowCount(0)

        def _fmt(val, decimals=3):
            if val is None:
                return ""
            try:
                return f"{float(val):.{decimals}f}"
            except Exception:
                return str(val)

        for row_idx, lnk in enumerate(links):
            self.links_table.insertRow(row_idx)
            included = lnk.get("included_in_consensus", 1)
            cells = [
                str(lnk.get("log_id", "") or ""),
                str(lnk.get("ticker", "") or ""),
                str(lnk.get("method", "") or ""),
                str(lnk.get("model", "") or ""),
                str(lnk.get("signal", "") or ""),
                _fmt(lnk.get("raw_confidence"), 1),
                _fmt(lnk.get("win_rate"), 3),
                _fmt(lnk.get("ema_accuracy"), 3),
                _fmt(lnk.get("final_weight"), 4),
                _fmt(lnk.get("calibrated_confidence"), 1),
                _fmt(lnk.get("normalized_r"), 3),
                "✅" if included else "❌",
                _fmt(lnk.get("target_price"), 2),
                _fmt(lnk.get("stop_loss"), 2),
            ]
            signal = str(lnk.get("signal", "")).upper()
            base_color = _SIDE_COLORS.get(signal, QColor("#ffffff"))
            if not included:
                base_color = QColor("#eeeeee")

            for col, val in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setBackground(QBrush(base_color))
                if col == 11 and not included:
                    item.setForeground(QBrush(QColor("#888888")))
                self.links_table.setItem(row_idx, col, item)

        self.links_table.setSortingEnabled(True)
        self.links_table.resizeColumnsToContents()


class SettingsTab(QWidget):
    """Main Settings tab containing Providers, Prompts, Accounts, Keys and IB Settings sub-tabs."""

    def __init__(self, api: ForecastApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.sub_tabs = QTabWidget()

        self.providers_tab = ProvidersTab(self.api)
        self.sub_tabs.addTab(self.providers_tab, "🔑 Providers")

        self.prompts_tab = PromptsTab(self.api)
        self.sub_tabs.addTab(self.prompts_tab, "📝 Prompts")

        self.accounts_tab = AccountsTab(self.api)
        self.sub_tabs.addTab(self.accounts_tab, "🏦 Accounts")

        self.keys_tab = _KeysSubTab(self.api)
        self.sub_tabs.addTab(self.keys_tab, "🔐 Keys")

        self.ib_settings_tab = _IBSettingsSubTab(self.api)
        self.sub_tabs.addTab(self.ib_settings_tab, "📡 IB settings")


        layout.addWidget(self.sub_tabs)

    def load(self):
        self.providers_tab.load()
        self.prompts_tab.load()
        self.accounts_tab.load()
        self.keys_tab.load()
        self.ib_settings_tab.load()


# Legacy alias for backward compatibility during transition
ConfigTab = SettingsTab


class _TabLoader:
    """Chains tab loads via QTimer.singleShot(0) so the event loop
    stays responsive between each step (all runs in the main thread)."""

    def __init__(self, win: "MainWindow"):
        self._win = win
        w = win
        self._steps = [
            ("Forecasts",   lambda: w.forecasts_tab.load_logs()),
            ("Consensus",   lambda: w.consensus_tab.load()),
            ("Tickers",     lambda: w.tickers_tab.load()),
            ("Price Data",  lambda: w.price_tab.load()),
            ("Indicators",  lambda: w.indicators_tab.load()),
            ("Portfolio",   lambda: w.portfolio_tab.load()),
            ("Orders",      lambda: w.orders_tab.load()),
            ("Runs",        lambda: w.forecast_runs_tab.load()),
            ("Scheduler",   lambda: w.scheduler_tab.load()),
            ("Settings",    lambda: w.settings_tab.load()),
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
            self._win.consensus_tab.refresh_ticker_filter(tickers)
            self._win.price_tab.refresh_ticker_filter(tickers)
            self._win.indicators_tab.refresh_ticker_filter(tickers)
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

        self.consensus_tab = ConsensusTab(self.api)
        self.tabs.addTab(self.consensus_tab, "🎯 Consensus")

        self.tickers_tab = TickersTab(self.api)
        self.tabs.addTab(self.tickers_tab, "📈 Tickers")

        self.scheduler_tab = SchedulerTab(self.api)
        self.tabs.addTab(self.scheduler_tab, "⏱ Scheduler")

        self.price_tab = PriceDataTab(self.api)
        self.tabs.addTab(self.price_tab, "💹 Price Data")

        self.indicators_tab = IndicatorsTab(self.api)
        self.tabs.addTab(self.indicators_tab, "📈 Indicators")

        self.portfolio_tab = PortfolioTab(self.api)
        self.tabs.addTab(self.portfolio_tab, "💼 Portfolio")

        self.orders_tab = OrdersTab(self.api)
        self.tabs.addTab(self.orders_tab, "📋 Orders")

        self.forecast_runs_tab = ForecastRunsTab(self.api)
        self.tabs.addTab(self.forecast_runs_tab, "🔬 Runs")

        self.settings_tab = SettingsTab(self.api)
        self.tabs.addTab(self.settings_tab, "⚙️ Setting")

        self.syslog_tab = SystemLogTab(self.api)
        self.tabs.addTab(self.syslog_tab, "📋 System Log")

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
