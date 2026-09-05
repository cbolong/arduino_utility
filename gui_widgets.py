"""Reusable Qt widgets for the GUI rewrite.

Sidebar nav, Card container, colour LogPane, GPIO PinGrid/PinRow, and a
QPainter-based WaveformView (replacing the old tk.Canvas waveform drawing).
"""
from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QTabWidget, QVBoxLayout, QWidget,
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

    def set_hint(self, text: str) -> None:
        """Set the placeholder shown while the pane is empty.

        Qt draws it only when there is no content and drops it the moment a
        line arrives, so it costs no layout and cannot cover real output.
        This is where a page explains WHY its controls are dead — previously
        nothing in the app did (a disabled page just sat there), and the
        pages' own '尚未連線' messages are unreachable because the gating
        disables the very controls that would emit them."""
        self.setPlaceholderText(text)

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
    """[●] label | OUT/IN toggle | [HIGH|LOW] segmented toggle | Read value.

    Emits setRequested(pin, mode, value) — value is "HIGH"/"LOW" for an
    OUTPUT drive, or None for a bare mode switch (mirrors the old contract).
    Emits readRequested(pin) when the leading read dot is clicked.
    """
    setRequested = Signal(int, str, object)
    readRequested = Signal(int)

    def __init__(self, label: str, pin: int, parent=None) -> None:
        super().__init__(parent)
        self.pin = pin
        self._mode = "INPUT"
        self._suppress = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 1, 2, 1)
        lay.setSpacing(6)

        # Leading read dot: click to GPIO_READ this one pin. The dot's fill
        # also doubles as the latest-known level indicator (high=green solid,
        # low=grey hollow, none=pale hollow when not yet read this session).
        self._read_btn = QPushButton()
        self._read_btn.setObjectName("readdot")
        self._read_btn.setFixedSize(16, 16)
        self._read_btn.setEnabled(False)
        self._read_btn.setProperty("level", "none")
        self._read_btn.clicked.connect(
            lambda: self.readRequested.emit(self.pin))
        lay.addWidget(self._read_btn)

        name = QLabel(label)
        name.setFixedWidth(40)
        lay.addWidget(name)

        self._mode_btn = QPushButton("--")
        self._mode_btn.setObjectName("chip")
        self._mode_btn.setFixedWidth(42)
        self._mode_btn.setEnabled(False)
        self._mode_btn.clicked.connect(self._on_mode_click)
        lay.addWidget(self._mode_btn)

        # Segmented [HIGH|LOW]: two checkable QPushButtons in an exclusive
        # QButtonGroup. Sub-layout with spacing=0 so they render as a single
        # connected control; QSS rounds the outer corners and removes the
        # left button's right border. The QButtonGroup contract (toggled
        # signal, isChecked / setChecked, setExclusive) is identical to the
        # previous QRadioButton pair, so _on_mode_click and _on_value_changed
        # work unchanged.
        seg = QHBoxLayout()
        seg.setContentsMargins(0, 0, 0, 0)
        seg.setSpacing(0)
        self._high = QPushButton("HIGH")
        self._low = QPushButton("LOW")
        self._grp = QButtonGroup(self)
        self._grp.setExclusive(True)
        self._grp.addButton(self._high)
        self._grp.addButton(self._low)
        for b, side in ((self._high, "left"), (self._low, "right")):
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setProperty("side", side)
            # Default mode is INPUT → readmode=True. Setting the property
            # BEFORE addWidget lets the first polish (during show) pick it
            # up naturally, so we skip 132 explicit unpolish/polish calls
            # across the 66-row grid.
            b.setProperty("readmode", True)
            b.setEnabled(False)
            b.toggled.connect(self._on_value_changed)
            seg.addWidget(b)
        lay.addLayout(seg)
        lay.addStretch(1)

    def _refresh_mode_btn(self, enabled: bool) -> None:
        if not enabled:
            self._mode_btn.setText("--")
            self._mode_btn.setEnabled(False)
            return
        self._mode_btn.setText("OUT" if self._mode == "OUTPUT" else "IN")
        self._mode_btn.setEnabled(True)

    def _apply_readmode(self) -> None:
        """Toggle the segment between drive styling (blue when checked, in
        OUTPUT mode) and read-indicator styling (green when checked, in INPUT
        mode) by flipping the `readmode` dynamic property the QSS keys off.
        Property selectors don't re-evaluate on their own — repolish."""
        read = self._mode == "INPUT"
        for b in (self._high, self._low):
            if b.property("readmode") != read:
                b.setProperty("readmode", read)
                b.style().unpolish(b)
                b.style().polish(b)

    def _set_checked(self, high: bool, low: bool) -> None:
        """Set the two segments' checked state programmatically without
        emitting a drive command (echoes, read indicators, clears)."""
        self._suppress = True
        try:
            self._grp.setExclusive(False)
            self._high.setChecked(high)
            self._low.setChecked(low)
            self._grp.setExclusive(True)
        finally:
            self._suppress = False

    def _set_dot_level(self, level: str) -> None:
        """Tri-state the leading read dot: 'high' / 'low' / 'none'. Mirrors
        the unpolish/polish pattern from _apply_readmode — QSS attribute
        selectors don't re-evaluate on their own."""
        if self._read_btn.property("level") != level:
            self._read_btn.setProperty("level", level)
            self._read_btn.style().unpolish(self._read_btn)
            self._read_btn.style().polish(self._read_btn)

    def set_enabled(self, enabled: bool) -> None:
        self._refresh_mode_btn(enabled)
        out = enabled and self._mode == "OUTPUT"
        self._high.setEnabled(out)
        self._low.setEnabled(out)
        self._read_btn.setEnabled(enabled)
        if not enabled:
            self._set_checked(False, False)   # clear indicator on disconnect
            self._set_dot_level("none")
        self._apply_readmode()

    def _on_mode_click(self) -> None:
        self._mode = "INPUT" if self._mode == "OUTPUT" else "OUTPUT"
        self._refresh_mode_btn(True)
        out = self._mode == "OUTPUT"
        self._high.setEnabled(out)
        self._low.setEnabled(out)
        # Start the new mode clean: drop any prior blue drive / green read so
        # OUTPUT waits for a click and INPUT waits for the read that follows.
        self._set_checked(False, False)
        self._set_dot_level("none")
        self._apply_readmode()
        self.setRequested.emit(self.pin, self._mode, None)

    def _on_value_changed(self, checked: bool) -> None:
        if self._suppress or not checked:
            return
        value = "HIGH" if self._high.isChecked() else "LOW"
        self.setRequested.emit(self.pin, "OUTPUT", value)

    def set_read_value(self, value: str) -> None:
        """Update the leading dot to reflect the last-read level (works in
        either mode). INPUT mode additionally lights the matching segment
        green; OUTPUT mode leaves the segment alone since it already shows
        the driven value in blue."""
        self._set_dot_level("high" if value == "HIGH" else "low")
        if self._mode != "INPUT":
            return
        self._set_checked(value == "HIGH", value == "LOW")


class PinGrid(QScrollArea):
    """Scrollable 2-column grid of 66 PinRows (column-major: D0-D32 / D33-D65).
    Emits setRequested(pin, mode, value) and readRequested(pin) bubbled up
    from the rows."""
    setRequested = Signal(int, str, object)
    readRequested = Signal(int)
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
            row.readRequested.connect(self.readRequested.emit)
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



# Waveform widgets live in gui_waveform now; re-export so existing
# `from gui_widgets import WaveformView, open_waveform_preview, …`
# importers keep working unchanged.
from gui_waveform import (   # noqa: E402,F401  (re-export)
    WaveformView,
    open_waveform_preview,
    _scroll_wave,
    _render_wave,
    _PREVIEW_VIEW_W,
    _ZOOM_STEP,
    _ZOOM_MAX,
)
