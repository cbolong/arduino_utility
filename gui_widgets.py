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


# --------------------------------------------------------------------------
# Waveform view (QPainter)
# --------------------------------------------------------------------------
class WaveformView(QWidget):
    """Logic-analyzer-style square-wave renderer on a dark canvas.

    Traces: list of (label, initial_state, events) where events are
    [(delta_units, new_state), ...]. Geometry is computed ONCE per data load
    (set_full / set_thumbnail) into cached QPainterPaths + a label list, so
    repaints (scroll / resize / expose) stay cheap even for 4096-edge
    captures — the previous version recomputed every segment on every paint.
    """
    clicked = Signal()

    ROW_H = 84
    TOP_PAD = 18
    AXIS_H = 26

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._wave = QPainterPath()
        self._grid = QPainterPath()   # HIGH/LOW reference rails (full view only)
        self._axis = QPainterPath()
        self._texts: list[tuple[int, int, str, str]] = []  # x, y, text, colorkey
        self.setStyleSheet(
            f"background: {T.PALETTE['canvas_bg']}; border-radius: 6px;")

    # -- configuration (rebuilds the cached geometry) -----------------------
    def set_thumbnail(self, initial: int, events: list, n: int = 6) -> None:
        self.setFixedSize(26, 26)
        self._build_thumb(initial, events[:n])
        self.update()

    def set_full(self, traces: list, px_per_unit: float, tick_step: float,
                 fmt_axis, fmt_pulse=None) -> None:
        total = max((sum(d for d, _ in ev) for _, _, ev in traces), default=0)
        width = int(60 + total * px_per_unit + 30)
        height = self.TOP_PAD + len(traces) * self.ROW_H + self.AXIS_H
        self.setMinimumSize(max(width, 760), height)
        self.resize(max(width, 760), height)
        self._build_full(traces, px_per_unit, tick_step, fmt_axis, fmt_pulse)
        self.update()

    # -- one-time geometry build --------------------------------------------
    def _build_thumb(self, initial: int, events: list) -> None:
        self._wave = QPainterPath()
        self._grid = QPainterPath()   # no rails in the tiny thumbnail
        self._axis = QPainterPath()
        self._texts = []
        left = right = 2
        y_top, y_low = 4, self.height() - 5
        step = (self.width() - left - right) / max(1, len(events))
        x = left
        y = y_top if initial else y_low
        self._wave.moveTo(x, y)
        for _, ns in events:
            nx = x + step
            self._wave.lineTo(nx, y)
            ny = y_top if ns else y_low
            if ny != y:
                self._wave.lineTo(nx, ny)
            x, y = nx, ny
        self._wave.lineTo(self.width() - right, y)

    def _build_full(self, traces, ppu, tick_step, fmt_axis, fmt_pulse) -> None:
        self._wave = QPainterPath()
        self._grid = QPainterPath()
        self._axis = QPainterPath()
        self._texts = []
        left = 60
        rail_right = self.width() - 30
        for i, (label, initial, events) in enumerate(traces):
            row_top = self.TOP_PAD + i * self.ROW_H
            y_top = row_top + 14
            y_low = row_top + self.ROW_H - 24
            # HIGH / LOW reference rails (dashed, dim) so a flat trace is
            # unambiguous — the orange line resting on a rail tells the level.
            self._grid.moveTo(left, y_top)
            self._grid.lineTo(rail_right, y_top)
            self._grid.moveTo(left, y_low)
            self._grid.lineTo(rail_right, y_low)
            self._texts.append((46, y_top + 4, "1", "rail"))
            self._texts.append((46, y_low + 4, "0", "rail"))
            if label:
                self._texts.append(
                    (4, (y_top + y_low) // 2 + 4, label, "axis"))
            x = left
            y = y_top if initial else y_low
            self._wave.moveTo(x, y)
            for delta, ns in events:
                nx = x + delta * ppu
                self._wave.lineTo(nx, y)
                if i == 0 and fmt_pulse and delta > 0 and (nx - x) > 24:
                    self._texts.append(
                        (int((x + nx) / 2 - 24), y_top - 4, fmt_pulse(delta), "label"))
                ny = y_top if ns else y_low
                if ny != y:
                    self._wave.lineTo(nx, ny)
                x, y = nx, ny
            self._wave.lineTo(x + 8, y)
        if tick_step and tick_step > 0:
            axis_y = self.height() - self.AXIS_H + 6
            total_px = self.width() - left - 30
            self._axis.moveTo(left, axis_y)
            self._axis.lineTo(self.width() - 30, axis_y)
            u = 0.0
            while u * ppu <= total_px:
                tx = int(left + u * ppu)
                self._axis.moveTo(tx, axis_y)
                self._axis.lineTo(tx, axis_y + 4)
                self._texts.append((tx + 2, axis_y + 16, fmt_axis(u), "axis"))
                u += tick_step

    # -- painting (cheap: draw cached paths + texts) ------------------------
    def mousePressEvent(self, _e) -> None:
        self.clicked.emit()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(T.PALETTE["canvas_bg"]))
        # Rails first so the signal line sits on top of them.
        if not self._grid.isEmpty():
            p.setPen(QPen(QColor(T.PALETTE["wave_grid"]), 1, Qt.DashLine))
            p.drawPath(self._grid)
        p.setPen(QPen(QColor(T.PALETTE["wave_orange"]), 2))
        p.drawPath(self._wave)
        if not self._axis.isEmpty():
            p.setPen(QPen(QColor(T.PALETTE["axis_text"])))
            p.drawPath(self._axis)
        for x, y, text, key in self._texts:
            color = (T.PALETTE["wave_label"] if key == "label"
                     else T.PALETTE["axis_text"])
            p.setPen(QPen(QColor(color)))
            p.drawText(x, y, text)


def _scroll_wave(view: WaveformView) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(False)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setWidget(view)
    return sa


# Visible canvas width the preview fits a capture into at zoom 1.0 (dialog
# width minus margins/scrollbar). The whole recording lands inside the window
# by default — slow, human-speed captures used to blow up to hundreds of
# thousands of px at the old `max(min_ppu, ...)` floor and only the first
# ~16 ms (the initial level) was ever visible.
_PREVIEW_VIEW_W = 820
_ZOOM_STEP = 1.6
_ZOOM_MAX = 256.0


def open_waveform_preview(parent, title: str, clusters: list, fmt,
                          min_ppu: float = 0.01) -> None:
    """Modal waveform preview shared by the TDBG and RECORD pages.

    `clusters` is a list of clusters; each cluster is a list of traces
    (label, initial_state, events). One scrollable WaveformView per cluster
    (multiple clusters → one tab each). `fmt` maps a time-unit value to a
    label string (e.g. gui_data.format_us).

    Each view fits its whole capture into the window at zoom 1.0; the
    ＋ / － / 整體 toolbar rescales the active view horizontally so dense
    bursts (or sub-pixel edges inside a long idle) can be inspected.
    `min_ppu` is retained for call-site compatibility but no longer floors
    the fit — fitting the whole capture is the default.
    """
    views = []           # WaveformView, parallel to tab order
    for traces in clusters:
        total = max((sum(d for d, _ in ev) for _, _, ev in traces),
                    default=0) or 1
        v = WaveformView()
        # Stash render state on the view so the toolbar can rebuild it.
        v._wf_traces = traces
        v._wf_total = total
        # set_full reserves 60 px (label gutter) + 30 px (right pad); subtract
        # them so the trace itself fits the window at zoom 1.0.
        v._wf_fit_ppu = max(_PREVIEW_VIEW_W - 90, 1) / total
        v._wf_fmt = fmt
        v._wf_zoom = 1.0
        _render_wave(v)
        views.append(v)

    if len(views) == 1:
        content = _scroll_wave(views[0])

        def active_view():
            return views[0]
    else:
        content = QTabWidget()
        for i, v in enumerate(views):
            content.addTab(_scroll_wave(v), f"段 {i + 1}")

        def active_view():
            return views[content.currentIndex()]

    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.resize(880, 400)
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(10, 10, 10, 10)
    lay.setSpacing(8)

    bar = QHBoxLayout()
    zoom_out = QPushButton("－ 縮小"); zoom_out.setObjectName("chip")
    zoom_in = QPushButton("＋ 放大"); zoom_in.setObjectName("chip")
    zoom_fit = QPushButton("⤢ 整體"); zoom_fit.setObjectName("chip")
    zoom_lbl = QLabel("1.0×")
    zoom_lbl.setObjectName("Muted")

    def refresh_label():
        zoom_lbl.setText(f"{active_view()._wf_zoom:.2g}×")

    def apply_zoom(factor=None, fit=False):
        v = active_view()
        if fit:
            v._wf_zoom = 1.0
        else:
            v._wf_zoom = min(_ZOOM_MAX, max(1.0, v._wf_zoom * factor))
        _render_wave(v)
        refresh_label()

    zoom_out.clicked.connect(lambda: apply_zoom(1.0 / _ZOOM_STEP))
    zoom_in.clicked.connect(lambda: apply_zoom(_ZOOM_STEP))
    zoom_fit.clicked.connect(lambda: apply_zoom(fit=True))
    for w in (zoom_out, zoom_in, zoom_fit, zoom_lbl):
        bar.addWidget(w)
    bar.addStretch(1)
    lay.addLayout(bar)
    lay.addWidget(content, 1)
    if isinstance(content, QTabWidget):
        content.currentChanged.connect(lambda _i: refresh_label())
    dlg.exec()


def _render_wave(v: "WaveformView") -> None:
    """(Re)build a preview view's geometry at its current zoom. Ticks scale
    with zoom so ~8 land across the visible window at any magnification."""
    ppu = v._wf_fit_ppu * v._wf_zoom
    tick_step = (v._wf_total / 8.0) / v._wf_zoom
    v.set_full(v._wf_traces, ppu, tick_step, v._wf_fmt, v._wf_fmt)
