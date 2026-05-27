"""Reusable Qt widgets for the GUI rewrite.

Sidebar nav, Card container, colour LogPane, GPIO PinGrid/PinRow, and a
QPainter-based WaveformView (replacing the old tk.Canvas waveform drawing).
"""
from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QRadioButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

import gui_theme as T


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------
class Sidebar(QFrame):
    currentChanged = Signal(int)

    def __init__(self, title: str, subtitle: str, items: list[str],
                 parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(190)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        t = QLabel(title)
        t.setObjectName("SidebarTitle")
        lay.addWidget(t)
        sub = QLabel(subtitle)
        sub.setObjectName("SidebarSub")
        lay.addWidget(sub)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for i, name in enumerate(items):
            b = QPushButton(name)
            b.setObjectName("NavButton")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            self._group.addButton(b, i)
            lay.addWidget(b)
            if i == 0:
                b.setChecked(True)
        self._group.idClicked.connect(self.currentChanged.emit)

        lay.addStretch(1)
        self._conn = QLabel("● 未連線")
        self._conn.setObjectName("ConnDot")
        self._conn.setContentsMargins(18, 0, 18, 16)
        lay.addWidget(self._conn)

    def set_current(self, index: int) -> None:
        b = self._group.button(index)
        if b is not None:
            b.setChecked(True)

    def set_connection(self, text: str, color: str) -> None:
        self._conn.setText(f"● {text}")
        self._conn.setStyleSheet(f"color: {color};")


# --------------------------------------------------------------------------
# Card
# --------------------------------------------------------------------------
class Card(QFrame):
    def __init__(self, title: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(10)
        if title:
            lbl = QLabel(title)
            lbl.setObjectName("CardTitle")
            outer.addWidget(lbl)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        outer.addLayout(self.body)
        T.card_shadow(self)


# --------------------------------------------------------------------------
# Log pane
# --------------------------------------------------------------------------
class LogPane(QPlainTextEdit):
    """Read-only coloured log. append()/clear() must be called on the GUI
    thread (workers route through a queued signal)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Log")
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def append(self, message: str, level: str = "info") -> None:
        color = T.LOG_COLORS.get(level, T.LOG_COLORS["info"])
        safe = html.escape(message)
        self.appendHtml(f'<span style="color:{color};">{safe}</span>')
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())


# --------------------------------------------------------------------------
# GPIO pin grid
# --------------------------------------------------------------------------
class PinRow(QWidget):
    """label | OUT/IN toggle | HIGH/LOW radios | Read value.

    Emits setRequested(pin, mode, value) — value is "HIGH"/"LOW" for an
    OUTPUT drive, or None for a bare mode switch (mirrors the old contract).
    """
    setRequested = Signal(int, str, object)

    def __init__(self, label: str, pin: int, parent=None) -> None:
        super().__init__(parent)
        self.pin = pin
        self._mode = "INPUT"
        self._suppress = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 1, 2, 1)
        lay.setSpacing(6)

        name = QLabel(label)
        name.setFixedWidth(40)
        lay.addWidget(name)

        self._mode_btn = QPushButton("--")
        self._mode_btn.setObjectName("chip")
        self._mode_btn.setFixedWidth(42)
        self._mode_btn.setEnabled(False)
        self._mode_btn.clicked.connect(self._on_mode_click)
        lay.addWidget(self._mode_btn)

        self._high = QRadioButton("HIGH")
        self._low = QRadioButton("LOW")
        self._grp = QButtonGroup(self)
        self._grp.setExclusive(True)
        self._grp.addButton(self._high)
        self._grp.addButton(self._low)
        for r in (self._high, self._low):
            r.setEnabled(False)
            r.toggled.connect(self._on_value_changed)
        lay.addWidget(self._high)
        lay.addWidget(self._low)

        lay.addSpacing(8)
        lay.addWidget(QLabel("讀:"))
        self._read = QLabel("??")
        self._read.setFixedWidth(40)
        self._read.setStyleSheet(f"color: {T.PALETTE['text_secondary']};")
        lay.addWidget(self._read)
        lay.addStretch(1)

    def _refresh_mode_btn(self, enabled: bool) -> None:
        if not enabled:
            self._mode_btn.setText("--")
            self._mode_btn.setEnabled(False)
            return
        self._mode_btn.setText("OUT" if self._mode == "OUTPUT" else "IN")
        self._mode_btn.setEnabled(True)

    def set_enabled(self, enabled: bool) -> None:
        self._refresh_mode_btn(enabled)
        out = enabled and self._mode == "OUTPUT"
        self._high.setEnabled(out)
        self._low.setEnabled(out)

    def _on_mode_click(self) -> None:
        self._mode = "INPUT" if self._mode == "OUTPUT" else "OUTPUT"
        self._refresh_mode_btn(True)
        out = self._mode == "OUTPUT"
        self._high.setEnabled(out)
        self._low.setEnabled(out)
        if not out:
            self._suppress = True
            try:
                self._grp.setExclusive(False)
                self._high.setChecked(False)
                self._low.setChecked(False)
                self._grp.setExclusive(True)
            finally:
                self._suppress = False
        self.setRequested.emit(self.pin, self._mode, None)

    def _on_value_changed(self, checked: bool) -> None:
        if self._suppress or not checked:
            return
        value = "HIGH" if self._high.isChecked() else "LOW"
        self.setRequested.emit(self.pin, "OUTPUT", value)

    def set_read_value(self, value: str) -> None:
        self._read.setText(value)
        color = (T.PALETTE["success_dark"] if value == "HIGH"
                 else T.PALETTE["text_primary"])
        self._read.setStyleSheet(f"color: {color};")


class PinGrid(QScrollArea):
    """Scrollable 2-column grid of 66 PinRows (column-major: D0-D32 / D33-D65).
    Emits setRequested(pin, mode, value) bubbled up from rows."""
    setRequested = Signal(int, str, object)
    ROWS_PER_COL = 33

    def __init__(self, pins: list[tuple[str, int]], parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(1)
        self.rows: dict[int, PinRow] = {}
        for idx, (label, pin) in enumerate(pins):
            row = PinRow(label, pin)
            row.setRequested.connect(self.setRequested.emit)
            self.rows[pin] = row
            grid.addWidget(row, idx % self.ROWS_PER_COL,
                           idx // self.ROWS_PER_COL)
        grid.setColumnStretch(2, 1)
        self.setWidget(inner)

    def set_enabled(self, enabled: bool) -> None:
        for r in self.rows.values():
            r.set_enabled(enabled)

    def set_read_value(self, pin: int, value: str) -> None:
        r = self.rows.get(pin)
        if r is not None:
            r.set_read_value(value)

    def pins(self) -> list[int]:
        return list(self.rows.keys())


# --------------------------------------------------------------------------
# Waveform view (QPainter)
# --------------------------------------------------------------------------
class WaveformView(QWidget):
    """Logic-analyzer-style square-wave renderer on a dark canvas.

    Traces: list of (label, initial_state, events) where events are
    [(delta_units, new_state), ...]. `px_per_unit` maps time-units to pixels.
    Used both as a tiny thumbnail (no axis/labels) and as the full preview.
    """
    clicked = Signal()

    ROW_H = 84
    TOP_PAD = 18
    AXIS_H = 26

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._traces: list[tuple[str, int, list]] = []
        self._ppu = 0.1
        self._left = 60
        self._right = 30
        self._tick_step = 0.0
        self._fmt_axis = lambda u: f"{u:g}"
        self._fmt_pulse = None
        self._thumb = False
        self.setStyleSheet(f"background: {T.PALETTE['canvas_bg']}; border-radius: 6px;")

    # -- configuration ------------------------------------------------------
    def set_thumbnail(self, initial: int, events: list, n: int = 6) -> None:
        self._thumb = True
        self._left = 2
        self._right = 2
        self._traces = [("", initial, events[:n])]
        self.setFixedSize(26, 26)
        self.update()

    def set_full(self, traces: list, px_per_unit: float, tick_step: float,
                 fmt_axis, fmt_pulse=None) -> None:
        self._thumb = False
        self._traces = traces
        self._ppu = px_per_unit
        self._tick_step = tick_step
        self._fmt_axis = fmt_axis
        self._fmt_pulse = fmt_pulse
        self._left = 60
        total = max((sum(d for d, _ in ev) for _, _, ev in traces), default=0)
        width = int(self._left + total * px_per_unit + self._right)
        height = self.TOP_PAD + len(traces) * self.ROW_H + self.AXIS_H
        self.setMinimumSize(max(width, 760), height)
        self.resize(max(width, 760), height)
        self.update()

    # -- painting -----------------------------------------------------------
    def mousePressEvent(self, _e) -> None:
        self.clicked.emit()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.fillRect(self.rect(), QColor(T.PALETTE["canvas_bg"]))
        if not self._traces:
            return
        orange = QColor(T.PALETTE["wave_orange"])
        if self._thumb:
            self._paint_trace(p, self._traces[0], y_top=4,
                              y_low=self.height() - 5, ppu=None, pen=orange)
            return

        for i, tr in enumerate(self._traces):
            row_top = self.TOP_PAD + i * self.ROW_H
            y_top = row_top + 14
            y_low = row_top + self.ROW_H - 24
            if tr[0]:
                p.setPen(QPen(QColor(T.PALETTE["axis_text"])))
                p.drawText(4, y_top + 10, tr[0])
            self._paint_trace(p, tr, y_top, y_low, self._ppu, orange,
                              draw_pulse=(i == 0))
        self._paint_axis(p)

    def _paint_trace(self, p, trace, y_top, y_low, ppu, pen, draw_pulse=False):
        _, initial, events = trace
        p.setPen(QPen(pen, 2))
        if ppu is None:  # thumbnail: uniform spacing
            usable = self.width() - self._left - self._right
            n = max(1, len(events))
            step = usable / n
            x = self._left
            state = initial
            y = y_top if state else y_low
            for _, ns in events:
                nx = x + step
                p.drawLine(int(x), int(y), int(nx), int(y))
                ny = y_top if ns else y_low
                if ny != y:
                    p.drawLine(int(nx), int(y), int(nx), int(ny))
                x, y = nx, ny
            p.drawLine(int(x), int(y), int(self.width() - self._right), int(y))
            return

        x = self._left
        state = initial
        y = y_top if state else y_low
        label_pen = QPen(QColor(T.PALETTE["wave_label"]))
        for delta, ns in events:
            nx = x + delta * ppu
            p.setPen(QPen(pen, 2))
            p.drawLine(int(x), int(y), int(nx), int(y))
            if draw_pulse and self._fmt_pulse and delta > 0 and (nx - x) > 24:
                p.setPen(label_pen)
                p.drawText(int((x + nx) / 2 - 24), y_top - 4, self._fmt_pulse(delta))
            ny = y_top if ns else y_low
            if ny != y:
                p.setPen(QPen(pen, 2))
                p.drawLine(int(nx), int(y), int(nx), int(ny))
            x, y = nx, ny
        p.setPen(QPen(pen, 2))
        p.drawLine(int(x), int(y), int(x + 8), int(y))

    def _paint_axis(self, p):
        if self._tick_step <= 0:
            return
        axis_y = self.height() - self.AXIS_H + 6
        p.setPen(QPen(QColor(T.PALETTE["axis_text"])))
        p.drawLine(self._left, axis_y, self.width() - self._right, axis_y)
        total_px = self.width() - self._left - self._right
        u = 0.0
        while u * self._ppu <= total_px:
            tx = int(self._left + u * self._ppu)
            p.drawLine(tx, axis_y, tx, axis_y + 4)
            p.drawText(tx + 2, axis_y + 16, self._fmt_axis(u))
            u += self._tick_step
