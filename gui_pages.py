"""The four content pages for the Qt GUI + a threaded worker.

Each connectable page owns a Worker (QObject on its own QThread) that runs the
blocking serial calls; results/log lines marshal back to the GUI thread via
queued signals — never touching widgets off-thread. Serial/protocol logic is
unchanged; it all lives in binFileTransfer_core (imported lazily).
"""
from __future__ import annotations

import os
import threading

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QTabWidget, QVBoxLayout,
    QWidget,
)

import gui_data as D
import gui_theme as T
from gui_widgets import Card, LogPane, PinGrid, WaveformView


# --------------------------------------------------------------------------
class Worker(QObject):
    """Runs submitted callables on a dedicated thread (one per page)."""
    _run = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._thread = QThread()
        self.moveToThread(self._thread)
        self._run.connect(self._exec)
        self._busy = False
        self._thread.start()

    @Slot(object)
    def _exec(self, fn) -> None:
        self._busy = True
        try:
            fn()
        except Exception as e:  # never let a worker exception kill the thread
            print("worker error:", e)
        finally:
            self._busy = False

    def submit(self, fn) -> None:
        self._run.emit(fn)

    def is_busy(self) -> bool:
        return self._busy

    def shutdown(self) -> None:
        self._thread.quit()
        self._thread.wait(3000)


# --------------------------------------------------------------------------
class Page(QWidget):
    """Common scaffold: a worker, a colour log pane, thread-safe logging."""
    logSig = Signal(str, str)

    def __init__(self, app) -> None:
        super().__init__()
        self.app = app
        self.worker = Worker()
        self.log_pane = LogPane()
        self.logSig.connect(self.log_pane.append)

    # thread-safe — emit queues onto the GUI thread
    def log(self, message: str, level: str = "info") -> None:
        self.logSig.emit(message, level)

    def log_cb(self, message: str, level: str) -> None:
        self.logSig.emit(message, level)

    def enqueue(self, fn) -> None:
        self.worker.submit(fn)

    def is_busy(self) -> bool:
        return self.worker.is_busy()

    def set_connected(self, connected: bool) -> None:
        pass

    def shutdown(self) -> None:
        self.worker.shutdown()

    # small helper to lay out a card + log within the page
    def _frame(self) -> QVBoxLayout:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(14)
        return lay


def _pin_combo() -> QComboBox:
    c = QComboBox()
    c.addItem("選擇 pin…")
    for label in D.TDBG_PIN_LABELS:
        c.addItem(label)
    return c


def _combo_pin(combo: QComboBox):
    idx = combo.currentIndex()
    return (idx - 1) if idx > 0 else None


# --------------------------------------------------------------------------
class FlashPage(Page):
    doneSig = Signal(bool)

    def __init__(self, app) -> None:
        super().__init__(app)
        self.firmware_path: str | None = None
        self.doneSig.connect(self._on_done)

        lay = self._frame()
        card = Card("韌體燒錄 ROM")
        row1 = QHBoxLayout()
        self._path_lbl = QLabel("（尚未選擇檔案）")
        self._path_lbl.setObjectName("Muted")
        browse = QPushButton("選擇檔案…")
        browse.clicked.connect(self._on_browse)
        row1.addWidget(self._path_lbl, 1)
        row1.addWidget(browse)
        card.body.addLayout(row1)

        row2 = QHBoxLayout()
        self._start = QPushButton("開始燒錄")
        self._start.setObjectName("accent")
        self._start.setEnabled(False)
        self._start.clicked.connect(self._on_start)
        clear = QPushButton("清除紀錄")
        clear.clicked.connect(self.log_pane.clear)
        row2.addWidget(self._start)
        row2.addWidget(clear)
        row2.addStretch(1)
        card.body.addLayout(row2)
        lay.addWidget(card)
        lay.addWidget(self.log_pane, 1)

    def _on_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "選擇韌體", "", "Firmware (*.bin);;All files (*)")
        if path:
            self.firmware_path = path
            self._path_lbl.setText(os.path.basename(path))
            self._start.setEnabled(True)

    def _on_start(self) -> None:
        if not self.firmware_path or not os.path.isfile(self.firmware_path):
            self.log("請先選擇有效的韌體檔。", "err")
            return
        self._start.setEnabled(False)
        # Destructive: release the shared GPIO/TDBG/RECORD connection first.
        self.app.disconnect_then(self._do_start)

    def _do_start(self) -> None:
        port = self.app.get_port()
        path = self.firmware_path

        def work():
            from binFileTransfer_core import program_firmware
            ok = program_firmware(path, self.log_cb, port=port)
            self.doneSig.emit(bool(ok))

        self.app.set_status("Programming…", T.PALETTE["warning_dark"])
        self.app.lock_port(True)
        self.enqueue(work)

    def _on_done(self, success: bool) -> None:
        if success:
            self.log(f"{os.path.basename(self.firmware_path or '')} 燒錄成功。", "ok")
            self.app.set_status("Success", T.PALETTE["success_dark"])
        else:
            self.app.set_status("Error", T.PALETTE["danger_dark"])
        self._start.setEnabled(True)
        self.app.lock_port(False)


# --------------------------------------------------------------------------
class GpioPage(Page):
    readSig = Signal(int, str)

    def __init__(self, app) -> None:
        super().__init__(app)
        self._abort = threading.Event()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._auto_tick)

        lay = self._frame()
        card = Card("GPIO 設定")
        ctl = QHBoxLayout()
        self._read_all = QPushButton("Read All")
        self._read_all.setEnabled(False)
        self._read_all.clicked.connect(self._on_read_all)
        self._auto = QCheckBox("自動讀取，每")
        self._auto.setEnabled(False)
        self._auto.toggled.connect(self._on_toggle_auto)
        self._interval = QDoubleSpinBox()
        self._interval.setRange(0.2, 60.0)
        self._interval.setSingleStep(0.5)
        self._interval.setValue(1.0)
        self._interval.setFixedWidth(70)
        clear = QPushButton("清除紀錄")
        clear.clicked.connect(self.log_pane.clear)
        ctl.addWidget(self._read_all)
        ctl.addSpacing(12)
        ctl.addWidget(self._auto)
        ctl.addWidget(self._interval)
        ctl.addWidget(QLabel("秒"))
        ctl.addStretch(1)
        ctl.addWidget(clear)
        card.body.addLayout(ctl)

        self._grid = PinGrid(D.GPIO_PINS)
        self._grid.setRequested.connect(self._on_pin_set)
        card.body.addWidget(self._grid, 1)
        lay.addWidget(card, 1)

        self.readSig.connect(self._grid.set_read_value)
        self.log_pane.setFixedHeight(130)
        lay.addWidget(self.log_pane)

    @property
    def _session(self):
        return self.app.gpio_session

    def set_connected(self, connected: bool) -> None:
        self._read_all.setEnabled(connected)
        self._auto.setEnabled(connected)
        self._interval.setEnabled(connected)
        self._grid.set_enabled(connected)
        if connected:
            self._abort.clear()
        else:
            self._abort.set()
            self._auto.setChecked(False)
            self._timer.stop()

    def _on_pin_set(self, pin: int, mode: str, value) -> None:
        if self._session is None:
            self.log(f"尚未連線，無法設定 D{pin}", "warn")
            return

        def cmd():
            sess = self._session
            if sess is None:
                return
            ok = sess.set_pin(pin, mode, value)
            if ok:
                if value is not None:
                    self.readSig.emit(pin, value)
                else:
                    v = sess.read_pin(pin)
                    if v is not None:
                        self.readSig.emit(pin, v)

        self.enqueue(cmd)

    def _on_read_all(self) -> None:
        if self._session is None:
            self.log("尚未連線，無法讀取", "warn")
            return

        def cmd():
            for pin in self._grid.pins():
                if self._abort.is_set():
                    break
                sess = self._session
                v = sess.read_pin(pin) if sess is not None else None
                if v is not None:
                    self.readSig.emit(pin, v)

        self.enqueue(cmd)

    def _on_toggle_auto(self, on: bool) -> None:
        if on and self._session is not None:
            self._timer.start(max(200, int(self._interval.value() * 1000)))
        else:
            self._timer.stop()

    def _auto_tick(self) -> None:
        if self._session is None:
            self._timer.stop()
            return
        # restart with the (possibly changed) interval; skip if worker busy
        self._timer.start(max(200, int(self._interval.value() * 1000)))
        if not self.is_busy():
            self._on_read_all()


# --------------------------------------------------------------------------
def _cycles_to_label(cycles: float) -> str:
    # 84 MHz Due → 1 cycle ≈ 11.9 ns
    return D._format_duration_ns(cycles / 0.084)


def _build_cluster_tabs(clusters, fmt_axis, fmt_pulse,
                        unit_per_cluster=None) -> QWidget:
    """Return a QTabWidget (or single scroll area) of WaveformViews."""
    if len(clusters) == 1:
        return _scroll_wave(clusters[0])
    tabs = QTabWidget()
    for i, view in enumerate(clusters):
        tabs.addTab(_scroll_wave(view), f"段 {i + 1}")
    return tabs


def _scroll_wave(view: WaveformView) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(False)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setWidget(view)
    return sa


class _PreviewDialog(QDialog):
    def __init__(self, title: str, content: QWidget, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(880, 360)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.addWidget(content)


class TdbgPage(Page):
    doneSig = Signal(bool)

    def __init__(self, app) -> None:
        super().__init__(app)
        self.doneSig.connect(self._on_send_done)

        lay = self._frame()
        card = Card("TDBG 波形回放")
        row = QHBoxLayout()
        row.addWidget(QLabel("輸出腳位"))
        self._combo = _pin_combo()
        self._combo.setEnabled(False)
        self._combo.setFixedWidth(160)
        row.addWidget(self._combo)
        self._send = QPushButton("送出")
        self._send.setObjectName("accent")
        self._send.setEnabled(False)
        self._send.clicked.connect(self._on_send)
        self._calib = QPushButton("校準")
        self._calib.setEnabled(False)
        self._calib.clicked.connect(self._on_calib)
        self._thumb = WaveformView()
        self._thumb.setCursor(Qt.PointingHandCursor)
        self._thumb.clicked.connect(self._open_preview)
        row.addWidget(self._send)
        row.addWidget(self._calib)
        row.addWidget(self._thumb)
        row.addStretch(1)
        clear = QPushButton("清除紀錄")
        clear.clicked.connect(self.log_pane.clear)
        row.addWidget(clear)
        card.body.addLayout(row)
        hint = QLabel("點縮圖可放大檢視內建波形（多段以分頁顯示）。")
        hint.setObjectName("Muted")
        card.body.addWidget(hint)
        lay.addWidget(card)
        lay.addWidget(self.log_pane, 1)

        try:
            i, e = D._ensure_builtin_parsed()
            self._thumb.set_thumbnail(i, e)
        except Exception:
            pass

    @property
    def _session(self):
        return self.app.tdbg_session

    def set_connected(self, connected: bool) -> None:
        self._send.setEnabled(connected)
        self._calib.setEnabled(connected)
        self._combo.setEnabled(connected)

    def _lock(self, locked: bool) -> None:
        self._send.setEnabled(not locked)
        self._calib.setEnabled(not locked)
        self._combo.setEnabled(not locked)

    def _on_send(self) -> None:
        if self._session is None:
            return
        pin = _combo_pin(self._combo)
        if pin is None:
            self.log("請先選擇輸出腳位。", "warn")
            return
        try:
            initial, events = D._ensure_builtin_parsed()
        except ValueError as e:
            self.log(f"內建波形解析失敗：{e}", "err")
            return
        from binFileTransfer_core import DUE_CPU_HZ, tdbg_retime_for_engine
        orig = sum(d for d, _ in events) / DUE_CPU_HZ
        events, scale = tdbg_retime_for_engine(events)
        dur = sum(d for d, _ in events) / DUE_CPU_HZ
        if scale != 1.0:
            self.log(f"已重定時：{scale:.2f}×（{orig*1000:.0f}ms → {dur*1000:.0f}ms）", "info")
        self._play(pin, initial, events, dur, "TDBG sending…")

    def _on_calib(self) -> None:
        if self._session is None:
            return
        pin = _combo_pin(self._combo)
        if pin is None:
            self.log("請先選擇輸出腳位。", "warn")
            return
        from binFileTransfer_core import DUE_CPU_HZ, tdbg_calibration_pattern
        initial, events = tdbg_calibration_pattern()
        dur = sum(d for d, _ in events) / DUE_CPU_HZ
        self._play(pin, initial, events, dur, "TDBG sending calibration…")

    def _play(self, pin, initial, events, dur, status) -> None:
        self._lock(True)
        self.app.set_status(status, T.PALETTE["warning_dark"])

        def cmd():
            sess = self._session
            if sess is None:
                self.doneSig.emit(False)
                return
            ok = sess.load(pin=pin, initial_state=initial, events=events)
            if ok:
                ok = sess.play(iterations=1, total_duration_s=dur)
            self.doneSig.emit(bool(ok))

        self.enqueue(cmd)

    def _on_send_done(self, success: bool) -> None:
        if self._session is not None:
            self._lock(False)
        self.app.set_status(
            "TDBG sent" if success else "TDBG error",
            T.PALETTE["success_dark"] if success else T.PALETTE["danger_dark"])

    def _open_preview(self) -> None:
        try:
            initial, events = D._ensure_builtin_parsed()
        except Exception:
            return
        clusters = D._split_into_clusters(initial, events)
        views = []
        for _start, c_init, c_events in clusters:
            total = sum(d for d, _ in c_events) or 1
            ppu = max(0.01, 760 / total)
            v = WaveformView()
            v.set_full([("", c_init, c_events)], ppu, total / 8.0,
                       _cycles_to_label, _cycles_to_label)
            views.append(v)
        content = _build_cluster_tabs(views, _cycles_to_label, _cycles_to_label)
        _PreviewDialog("TDBG 波形", content, self).exec()


# --------------------------------------------------------------------------
class _RecordPinRow(QWidget):
    removed = Signal(object)

    def __init__(self, removable: bool) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.combo = _pin_combo()
        self.combo.setFixedWidth(160)
        self._dot = QLabel("●")
        self._dot.setStyleSheet(f"color: {T.PALETTE['text_disabled']};")
        self._state = QLabel("—")
        self._state.setObjectName("Muted")
        self._remove = QPushButton("－")
        self._remove.setObjectName("chip")
        self._remove.setFixedWidth(32)
        self._remove.clicked.connect(lambda: self.removed.emit(self))
        self._remove.setVisible(removable)
        lay.addWidget(self.combo)
        lay.addWidget(self._dot)
        lay.addWidget(self._state)
        lay.addStretch(1)
        lay.addWidget(self._remove)

    def selected_pin(self):
        return _combo_pin(self.combo)

    def set_combo_enabled(self, enabled: bool) -> None:
        self.combo.setEnabled(enabled)

    def set_remove_visible(self, visible: bool) -> None:
        self._remove.setVisible(visible)

    def set_live_state(self, state) -> None:
        if state is None:
            self._dot.setStyleSheet(f"color: {T.PALETTE['text_disabled']};")
            self._state.setText("—")
        elif state:
            self._dot.setStyleSheet(f"color: {T.PALETTE['success']};")
            self._state.setText("HIGH")
        else:
            self._dot.setStyleSheet(f"color: {T.PALETTE['text_secondary']};")
            self._state.setText("LOW")


class RecordPage(Page):
    MAX_PINS = 4   # RECORD_MAX_PINS in binFileTransfer_core / firmware
    liveSig = Signal(dict)
    stopSig = Signal(object)
    failSig = Signal()

    def __init__(self, app) -> None:
        super().__init__(app)
        self._recording = False
        self._recorded = None
        self.liveSig.connect(self._apply_live)
        self.stopSig.connect(self._on_stop_done)
        self.failSig.connect(self._on_fail)

        self._connected = False
        lay = self._frame()
        card = Card("波形錄製")
        self._rows_box = QVBoxLayout()
        self._rows: list[_RecordPinRow] = []
        card.body.addLayout(self._rows_box)

        act = QHBoxLayout()
        self._add_btn = QPushButton("＋ 加 pin")
        self._add_btn.setObjectName("chip")
        self._add_btn.clicked.connect(self._add_row)
        self._start = QPushButton("開始")
        self._start.setObjectName("accent")
        self._start.setEnabled(False)
        self._start.clicked.connect(self._on_start)
        self._stop = QPushButton("結束")
        self._stop.setEnabled(False)
        self._stop.clicked.connect(self._on_stop)
        self._thumb = WaveformView()
        self._thumb.setCursor(Qt.PointingHandCursor)
        self._thumb.clicked.connect(self._open_preview)
        self._thumb.setVisible(False)
        clear = QPushButton("清除紀錄")
        clear.clicked.connect(self.log_pane.clear)
        act.addWidget(self._add_btn)
        act.addWidget(self._start)
        act.addWidget(self._stop)
        act.addWidget(self._thumb)
        act.addStretch(1)
        act.addWidget(clear)
        card.body.addLayout(act)
        lay.addWidget(card)
        lay.addWidget(self.log_pane, 1)
        self._add_row()

    @property
    def _session(self):
        return self.app.record_session

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        if self._recording:
            return
        for r in self._rows:
            r.set_combo_enabled(connected)
        self._add_btn.setEnabled(connected and len(self._rows) < self.MAX_PINS)
        self._refresh_start()

    def _add_row(self) -> None:
        if len(self._rows) >= self.MAX_PINS:
            return
        row = _RecordPinRow(removable=len(self._rows) > 0)
        row.removed.connect(self._remove_row)
        row.combo.currentIndexChanged.connect(self._refresh_start)
        self._rows.append(row)
        self._rows_box.addWidget(row)
        self._sync_row_controls()

    def _remove_row(self, row) -> None:
        if row in self._rows and len(self._rows) > 1:
            self._rows.remove(row)
            row.setParent(None)
            row.deleteLater()
        self._sync_row_controls()
        self._refresh_start()

    def _sync_row_controls(self) -> None:
        for i, r in enumerate(self._rows):
            r.set_remove_visible(len(self._rows) > 1)
        self._add_btn.setEnabled(
            getattr(self, "_connected", False)
            and len(self._rows) < self.MAX_PINS)

    def _selected_pins(self) -> list[int]:
        pins, seen = [], set()
        for r in self._rows:
            p = r.selected_pin()
            if p is not None and p not in seen:
                pins.append(p)
                seen.add(p)
        return pins

    def _refresh_start(self) -> None:
        self._start.setEnabled(
            self._connected and not self._recording
            and len(self._selected_pins()) > 0)

    def _on_start(self) -> None:
        if self._session is None or self._recording:
            return
        pins = self._selected_pins()
        if not pins:
            self.log("請至少選一個 pin。", "warn")
            return
        self._recording = True
        self._start.setEnabled(False)
        self._stop.setEnabled(True)
        self._add_btn.setEnabled(False)
        self._thumb.setVisible(False)
        self.app.set_recording(True)
        for r in self._rows:
            r.set_combo_enabled(False)
            r.set_live_state(None)
        self.app.set_status("RECORD recording…", T.PALETTE["warning_dark"])

        pin_to_row = {r.selected_pin(): r for r in self._rows
                      if r.selected_pin() is not None}
        self._pin_to_row = pin_to_row

        def on_live(states: dict) -> None:
            self.liveSig.emit(states)

        def cmd():
            sess = self._session
            if sess is None:
                self.failSig.emit()
                return
            ok = sess.start(pins, on_live=on_live)
            if not ok:
                self.failSig.emit()

        self.enqueue(cmd)

    def _apply_live(self, states: dict) -> None:
        for pin, state in states.items():
            row = getattr(self, "_pin_to_row", {}).get(pin)
            if row is not None:
                row.set_live_state(state)

    def _on_fail(self) -> None:
        self._recording = False
        self._stop.setEnabled(False)
        self.app.set_recording(False)
        for r in self._rows:
            r.set_combo_enabled(self._connected)
        self._sync_row_controls()
        self._refresh_start()
        self.app.set_status("RECORD start failed", T.PALETTE["danger_dark"])

    def _on_stop(self) -> None:
        if not self._recording:
            return
        self._stop.setEnabled(False)
        self.app.set_status("RECORD stopping…", T.PALETTE["warning_dark"])

        def cmd():
            sess = self._session
            if sess is None:
                self.stopSig.emit(None)
                return
            self.stopSig.emit(sess.stop())

        self.enqueue(cmd)

    def _on_stop_done(self, result) -> None:
        self._recording = False
        self.app.set_recording(False)
        for r in self._rows:
            r.set_combo_enabled(self._connected)
        self._sync_row_controls()
        self._refresh_start()
        if result is None:
            self.app.set_status("RECORD stop error", T.PALETTE["danger_dark"])
            return
        pins, events = result
        self._recorded = (pins, events)
        if events:
            self._draw_thumb(pins, events)
            self._thumb.setVisible(True)
            total_us = sum(d for d, _ in events)
            self.app.set_status(
                f"RECORD done: {len(events)} edges, {total_us/1000:.3f} ms",
                T.PALETTE["success_dark"])
        else:
            self.app.set_status("RECORD done: no edges", T.PALETTE["warning_dark"])

    def _draw_thumb(self, pins, events) -> None:
        p0 = pins[0]
        trace = [(d, 1 if st.get(p0) else 0) for d, st in events]
        initial = trace[0][1] if trace else 0
        self._thumb.set_thumbnail(initial, trace)

    def _open_preview(self) -> None:
        if not self._recorded:
            return
        pins, events = self._recorded
        if not events:
            return
        # one trace per pin; x in microseconds
        total = sum(d for d, _ in events) or 1
        ppu = max(0.05, 760 / total)
        traces = []
        for p in pins:
            ev = [(d, 1 if st.get(p) else 0) for d, st in events]
            initial = ev[0][1] if ev else 0
            traces.append((f"D{p}", initial, ev))
        v = WaveformView()
        fmt = lambda us: D._format_duration_ns(us * 1000)
        v.set_full(traces, ppu, total / 8.0, fmt, fmt)
        _PreviewDialog("錄製波形", _scroll_wave(v), self).exec()
