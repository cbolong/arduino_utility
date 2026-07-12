"""Waveform rendering tests — fit-to-width, zoom, rails, panning, thumbnail.

  W1  slow (seconds-scale) capture fits the 820px window at zoom 1.0
  W2  the wave visits BOTH rail Y levels (regression: 'always HIGH' bug)
  W3  zoom-in widens the canvas; 整體 restores the fit width
  W4  constant-LOW trace sits on the 0 rail only; constant-HIGH on 1 rail
  W5  rails: 2 dashed rails per channel + 1/0 markers + centred label
  W6  thumbnail has no rails and still emits clicked (no pan)
  W7  drag-pan scrolls the enclosing scroll area; release restores cursor
"""
from _helpers import drain  # noqa: F401


def main():
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt, QPointF
    import gui_theme as T
    from gui_waveform import (
        WaveformView, _render_wave, _scroll_wave, _PREVIEW_VIEW_W, _ZOOM_STEP,
    )

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(T.build_qss())

    def fmt(u):
        return f"{u/1000:.0f}ms"

    def make_view(traces, zoom=1.0):
        total = max((sum(d for d, _ in ev) for _, _, ev in traces),
                    default=0) or 1
        v = WaveformView()
        v._wf_traces, v._wf_total = traces, total
        v._wf_fit_ppu = max(_PREVIEW_VIEW_W - 90, 1) / total
        v._wf_fmt, v._wf_zoom = fmt, zoom
        _render_wave(v)
        return v

    y_top0 = WaveformView.TOP_PAD + 14
    y_low0 = WaveformView.TOP_PAD + WaveformView.ROW_H - 24

    slow = [(0, 1)] + [(1_000_000, i % 2) for i in range(6)]
    v = make_view([("D22", 1, slow)])
    assert v.width() <= 900, f"W1: fit canvas {v.width()}px exceeds window"
    print(f"W1: 6s capture fits in {v.width()}px: OK")

    ys = {round(v._wave.elementAt(i).y) for i in range(v._wave.elementCount())}
    assert y_top0 in ys and y_low0 in ys, f"W2: wave Ys {sorted(ys)}"
    print("W2: wave visits both levels (no 'always HIGH'): OK")

    w_fit = v.width()
    v._wf_zoom = _ZOOM_STEP ** 3
    _render_wave(v)
    assert v.width() > w_fit, "W3: zoom-in didn't widen"
    v._wf_zoom = 1.0
    _render_wave(v)
    assert v.width() == w_fit, "W3: fit not restored"
    print("W3: zoom in/out round trip: OK")

    v_lo = make_view([("D23", 0, [(0, 0), (2_000_000, 0)])])
    ys_lo = {round(v_lo._wave.elementAt(i).y)
             for i in range(v_lo._wave.elementCount())}
    assert ys_lo == {y_low0}, f"W4: constant LOW Ys {ys_lo}"
    v_hi = make_view([("D23", 1, [(0, 1), (2_000_000, 1)])])
    ys_hi = {round(v_hi._wave.elementAt(i).y)
             for i in range(v_hi._wave.elementCount())}
    assert ys_hi == {y_top0}, f"W4: constant HIGH Ys {ys_hi}"
    print("W4: constant traces sit on the correct rail: OK")

    two = make_view([("D22", 1, slow), ("D23", 0, [(0, 0), (6_000_000, 0)])])
    assert two._grid.elementCount() == 8, \
        f"W5: expected 8 rail elements, got {two._grid.elementCount()}"
    marks = [t[2] for t in two._texts if t[2] in ("0", "1")]
    labels = [t[2] for t in two._texts if t[2] in ("D22", "D23")]
    assert len(marks) == 4 and len(labels) == 2
    print("W5: rails + 1/0 markers + labels per channel: OK")

    vt = WaveformView()
    fired = []
    vt.clicked.connect(lambda: fired.append(1))
    vt.set_thumbnail(1, [(0, 1), (10, 0)])
    assert vt._grid.isEmpty(), "W6: thumbnail must not draw rails"

    class FakeEvt:
        def __init__(self, x, y, button=Qt.LeftButton):
            self._p, self._b = QPointF(x, y), button

        def globalPosition(self):
            return self._p

        def button(self):
            return self._b

    vt.mousePressEvent(FakeEvt(5, 5))
    assert fired == [1], "W6: thumbnail click lost"
    print("W6: thumbnail — no rails, click still opens preview: OK")

    vz = make_view([("D22", 1, slow)], zoom=4.0)
    sa = _scroll_wave(vz)
    sa.resize(820, 300)
    sa.show()
    app.processEvents()
    hbar = sa.horizontalScrollBar()
    assert hbar.maximum() > 0, "W7 precondition: canvas must overflow"
    h0 = hbar.value()
    vz.mousePressEvent(FakeEvt(500, 100))
    vz.mouseMoveEvent(FakeEvt(300, 100))
    assert hbar.value() == h0 + 200, f"W7: pan moved {hbar.value() - h0}"
    vz.mouseReleaseEvent(FakeEvt(300, 100))
    assert vz._pan_origin is None
    print("W7: drag-pan scrolls 200px on a 200px drag: OK")


if __name__ == "__main__":
    main()
    print("PASS test_waveform")
