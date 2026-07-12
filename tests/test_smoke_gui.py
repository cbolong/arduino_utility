"""GUI smoke test — the 12-step end-to-end regression net.

Exercises every user-facing path with mocks (no hardware):
  S1  modules import + MainWindow constructs
  S2  all four pages build lazily
  S3  mock connect → 3 sessions + sidebar 已連線
  S4  GPIO per-pin read dot lights on click
  S5  GPIO Read All covers all 66 pins
  S6  TDBG preset busy→done re-enables buttons
  S7  TDBG mid-flight disconnect recovers on reconnect
  S8  RECORD start→stop round trip stores events + shows thumbnail
  S9  flash sidebar 燒錄中… → 未連線, _start re-enabled
  S10 disconnect close() raising still chains the callback
  S11 waveform preview rails + both Y levels
  S12 clean closeEvent
"""
from _helpers import drain, FakeSerial  # noqa: F401 (side effect: offscreen + sys.path)

import os
import tempfile
import threading
import time


def main():
    from PySide6.QtWidgets import QApplication
    import binFileTransfer_core as core
    import binFileTransferGui as gui
    print("S1: modules import")

    app = QApplication.instance() or QApplication([])
    win = gui.MainWindow()
    print("S1: MainWindow constructs: OK")

    for i in range(4):
        assert win._ensure_page(i) is not None, f"page {i} didn't build"
    print("S2: all 4 pages build lazily: OK")

    fake_ser = FakeSerial()
    core.open_due_link = lambda port, log: fake_ser
    gui.open_due_link = lambda port, log: fake_ser

    win._on_connect()
    assert drain(app, lambda: win._connected), "connect never completed"
    assert win.gpio_session and win.tdbg_session and win.record_session
    assert "已連線" in win.sidebar._conn.text()
    print("S3: mock connect → sessions + sidebar: OK")

    gpio_page = win.gpio_page
    gpio_page.set_connected(True)
    row = gpio_page._grid.rows[18]
    win.gpio_session.read_pin = lambda pin: "HIGH"
    row._read_btn.click()
    assert drain(app, lambda: row._read_btn.property("level") == "high"), \
        "read dot didn't light"
    assert row._high.isChecked() and not row._low.isChecked()
    print("S4: per-pin read dot: OK")

    seen = []
    win.gpio_session.read_pin = lambda pin: (seen.append(pin), "LOW")[1]
    gpio_page._on_read_all()
    assert drain(app, lambda: len(seen) >= 66, timeout=5.0), \
        f"Read All covered only {len(seen)} pins"
    print(f"S5: Read All covers {len(seen)} pins: OK")

    tdbg_page = win.tdbg_page
    tdbg_page._combo.setCurrentIndex(1)
    win.tdbg_session.send_preset = lambda n, pin: True
    tdbg_page._on_preset(1)
    assert drain(app, lambda: tdbg_page._btns[0].isEnabled()), \
        "TDBG buttons didn't re-enable"
    print("S6: TDBG preset cycle: OK")

    hold = threading.Event()

    def hanging_preset(n, pin):
        hold.wait(timeout=2.0)
        return False

    win.tdbg_session.send_preset = hanging_preset
    tdbg_page._on_preset(2)
    time.sleep(0.05)
    app.processEvents()
    old_sess = win.tdbg_session
    win.tdbg_session = None
    hold.set()
    drain(app, lambda: not tdbg_page.is_busy(), timeout=3.0)
    win.tdbg_session = old_sess
    tdbg_page.set_connected(True)
    assert tdbg_page._btns[0].isEnabled(), \
        "TDBG buttons not recoverable after mid-flight disconnect"
    print("S7: TDBG disconnect race recovers: OK")

    record_page = win.record_page
    record_page._rows[0].combo.setCurrentIndex(1)
    record_page._refresh_start()
    assert record_page._start.isEnabled()
    started, stopped = threading.Event(), threading.Event()

    def fake_start(pins, on_live=None):
        started.set()
        if on_live:
            for v in (True, False):
                on_live({pins[0]: v})
        return True

    def fake_stop():
        stopped.set()
        return [22], [(0, {22: True}), (100_000, {22: False})]

    win.record_session.start = fake_start
    win.record_session.stop = fake_stop
    record_page._on_start()
    assert drain(app, lambda: started.is_set())
    record_page._on_stop()
    assert drain(app, lambda: stopped.is_set() and not record_page._recording)
    assert record_page._recorded is not None
    assert not record_page._thumb.isHidden()
    print("S8: RECORD round trip: OK")

    flash_page = win.flash_page
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(b"\x55" * 256)
        fw = f.name
    flash_page.firmware_path = fw
    flash_page._start.setEnabled(True)
    release = threading.Event()

    def fake_pf(path, log, port=None, **kw):
        release.wait(timeout=5.0)
        return True

    core.program_firmware = fake_pf
    import gui_pages as _gp
    _gp.program_firmware = fake_pf
    flash_page._on_start()
    assert drain(app, lambda: "燒錄中" in win.sidebar._conn.text()), \
        "sidebar didn't show 燒錄中"
    release.set()
    assert drain(app, lambda: "未連線" in win.sidebar._conn.text())
    assert flash_page._start.isEnabled()
    os.unlink(fw)
    print("S9: flash sidebar transitions: OK")

    win._on_connect()
    assert drain(app, lambda: win._connected), "reconnect failed"

    class Boom:
        def close(self):
            raise OSError("boom")

    win.gpio_session = Boom()
    called = []
    win.disconnect_then(lambda: called.append(1))
    assert drain(app, lambda: called), "disconnect callback never chained"
    print("S10: disconnect close() raising chains callback: OK")

    from gui_waveform import WaveformView, _PREVIEW_VIEW_W, _render_wave
    events = [(0, 1), (500_000, 0), (500_000, 1)]
    traces = [("D22", 1, events), ("D23", 0, [(0, 0), (1_000_000, 0)])]
    total = max(sum(d for d, _ in ev) for _, _, ev in traces)
    v = WaveformView()
    v._wf_traces, v._wf_total = traces, total
    v._wf_fit_ppu = max(_PREVIEW_VIEW_W - 90, 1) / total
    v._wf_fmt = lambda u: f"{u}"
    v._wf_zoom = 1.0
    _render_wave(v)
    assert not v._grid.isEmpty()
    ys = {round(v._wave.elementAt(i).y) for i in range(v._wave.elementCount())}
    assert WaveformView.TOP_PAD + 14 in ys
    assert WaveformView.TOP_PAD + WaveformView.ROW_H - 24 in ys
    print("S11: waveform rails + both levels: OK")

    win.close()
    app.processEvents()
    print("S12: clean closeEvent: OK")


if __name__ == "__main__":
    main()
    print("PASS test_smoke_gui")
