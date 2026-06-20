"""WaveformView (QPainter-driven logic-analyzer-style trace renderer) and
the preview dialog factory shared by the TDBG and RECORD pages.

Extracted from gui_widgets so the waveform/painter concerns live in one
file, separate from the simple GPIO/chrome widgets."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QTabWidget, QVBoxLayout, QWidget,
)

import gui_theme as T


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
        # Click-drag panning (full view only): when the canvas is wider than
        # the scroll viewport, hold the left button and drag to scroll. _scroll
        # is the enclosing QScrollArea, wired by _scroll_wave().
        self._scroll = None
        self._pannable = False
        self._pan_origin = None
        self._pan_h0 = 0
        self._pan_v0 = 0
        self.setStyleSheet(
            f"background: {T.PALETTE['canvas_bg']}; border-radius: 6px;")

    # -- configuration (rebuilds the cached geometry) -----------------------
    def set_thumbnail(self, initial: int, events: list, n: int = 6) -> None:
        self.setFixedSize(26, 26)
        self._pannable = False
        self._build_thumb(initial, events[:n])
        self.update()

    def set_full(self, traces: list, px_per_unit: float, tick_step: float,
                 fmt_axis, fmt_pulse=None) -> None:
        total = max((sum(d for d, _ in ev) for _, _, ev in traces), default=0)
        width = int(60 + total * px_per_unit + 30)
        height = self.TOP_PAD + len(traces) * self.ROW_H + self.AXIS_H
        self.setMinimumSize(max(width, 760), height)
        self.resize(max(width, 760), height)
        self._pannable = True
        self.setCursor(Qt.OpenHandCursor)   # affordance: drag to pan
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

    # -- interaction --------------------------------------------------------
    def mousePressEvent(self, e) -> None:
        # Thumbnail: a click opens the preview. Full view: left-drag pans the
        # scroll area (global coords, since scrolling repositions this widget
        # under the cursor).
        if not self._pannable:
            self.clicked.emit()
            return
        if e.button() == Qt.LeftButton and self._scroll is not None:
            self._pan_origin = e.globalPosition().toPoint()
            self._pan_h0 = self._scroll.horizontalScrollBar().value()
            self._pan_v0 = self._scroll.verticalScrollBar().value()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e) -> None:
        if self._pan_origin is None:
            return
        delta = e.globalPosition().toPoint() - self._pan_origin
        self._scroll.horizontalScrollBar().setValue(self._pan_h0 - delta.x())
        self._scroll.verticalScrollBar().setValue(self._pan_v0 - delta.y())

    def mouseReleaseEvent(self, _e) -> None:
        if self._pan_origin is not None:
            self._pan_origin = None
            self.setCursor(Qt.OpenHandCursor)

    # -- painting (cheap: draw cached paths + texts) ------------------------

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
    view._scroll = sa   # let the view drive these scrollbars while panning
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
