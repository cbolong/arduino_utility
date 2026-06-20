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
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout, QWidget,
)

import gui_data as D
import gui_theme as T
from binFileTransfer_core import program_firmware
from gui_widgets import (
    Card, LogPane, PinGrid, WaveformView, open_waveform_preview,
)


# --------------------------------------------------------------------------
class Worker(QObject):
    """Runs submitted callables on a dedicated thread (one per page).

    `on_error(msg)` (called on the worker thread) surfaces any uncaught
    exception so it reaches the page log instead of a print() that's lost in
    a windowed EXE. `on_failure(detail, traceback_str)` (optional) lets a
    page register a state-reset hook — see `_register_failure_handler` on
    Page. Both must be thread-safe (pages pass signal-emitting callbacks)."""
    _run = Signal(object)

    def __init__(self, on_error=None, on_failure=None) -> None:
        super().__init__()
        self._on_error = on_error
        self._on_failure = on_failure
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
            import traceback
            detail = f"{type(e).__name__}: {e}"
            tb = traceback.format_exc(limit=3)
            # on_error / on_failure themselves can raise (page being torn
            # down mid-callback, log sink gone, etc). Catching that prevents
            # the exception from killing the worker thread and stranding
            # _busy True.
            try:
                if self._on_error is not None:
                    self._on_error(f"背景作業錯誤：{detail}\n{tb}")
                else:
                    print("worker error:", detail, "\n", tb)
            except Exception:
                pass
            try:
                if self._on_failure is not None:
                    self._on_failure(detail, tb)
            except Exception:
                pass
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
    """Common scaffold: a worker, a colour log pane, thread-safe logging.

    Subclasses can override `_on_worker_failure(detail, tb)` to reset their
    own state when a worker job raises — covers the family of bugs where a
    sync state-set followed by enqueue() leaves the page wedged if the
    worker raises before it can emit its done/fail signal."""
    logSig = Signal(str, str)
    workerFailSig = Signal(str, str)   # detail, traceback — queued to GUI

    def __init__(self, app) -> None:
        super().__init__()
        self.app = app
        self.worker = Worker(
            on_error=lambda msg: self.log(msg, "err"),
            on_failure=lambda d, tb: self.workerFailSig.emit(d, tb),
        )
        self.log_pane = LogPane()
        self.logSig.connect(self.log_pane.append)
        self.workerFailSig.connect(self._on_worker_failure)

    def _on_worker_failure(self, detail: str, tb: str) -> None:
        """Default: no-op. Subclasses override to reset transient state
        (busy flags, button enable/disable) when a worker raises."""
        pass

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
            # Always emit doneSig — Worker._exec would swallow any exception
            # from program_firmware, leaving _start permanently disabled and
            # the port locked. Guard with try/finally so _on_done always runs.
            ok = False
            try:
                ok = bool(program_firmware(path, self.log_cb, port=port))
            finally:
                self.doneSig.emit(ok)

        self.app.status("Programming…", "warn")
        # The shared link was just released (program_firmware owns the port
        # for the duration), so the sidebar would read 未連線 mid-flash —
        # technically true but alarming. Show what's actually happening.
        self.app._set_conn("燒錄中…", T.PALETTE["warning_dark"])
        self.app.lock_port(True)
        self.enqueue(work)

    def _on_worker_failure(self, detail: str, tb: str) -> None:
        # Safety net: if work() somehow doesn't reach its try/finally,
        # this still re-enables _start and unlocks the port so the page
        # never wedges. doneSig already does this in normal failures.
        if not self._start.isEnabled():
            self._start.setEnabled(True)
            self.app.lock_port(False)
            try:
                self.app._set_conn("未連線", T.PALETTE["danger_dark"])
            except Exception:
                pass

    def _on_done(self, success: bool) -> None:
        if success:
            self.log(f"{os.path.basename(self.firmware_path or '')} 燒錄成功。", "ok")
            # The erase ended the MCU's idle loop; GPIO/TDBG/RECORD won't
            # respond again until the Due is reset and the shared
            # connection is re-opened. Spell that out — pressing Connect
            # alone won't help, and the previous flow gave no cue.
            self.log("按 Due 板上的 reset 鈕 + 再按 Connect 才能繼續使用 "
                     "GPIO / TDBG / 波形錄製。", "info")
            self.app.status("Success — reset Due to use GPIO/TDBG/RECORD",
                            "ok")
        else:
            self.app.status("Error", "err")
        # Flash is done either way and the shared link really is closed now —
        # restore the true indicator (matches _on_disconnect_done).
        self.app._set_conn("未連線", T.PALETTE["danger_dark"])
        self._start.setEnabled(True)
        self.app.lock_port(False)


# --------------------------------------------------------------------------
class GpioPage(Page):
    readSig = Signal(int, str)

    def __init__(self, app) -> None:
        super().__init__(app)
        self._abort = threading.Event()
        self._timer = QTimer(self)   # repeating; interval set on toggle
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
        self._interval.valueChanged.connect(self._on_interval_changed)
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
        self._grid.readRequested.connect(self._on_pin_read)
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

    def _on_pin_read(self, pin: int) -> None:
        if self._session is None:
            self.log(f"尚未連線，無法讀取 D{pin}", "warn")
            return

        def cmd():
            sess = self._session
            v = sess.read_pin(pin) if sess is not None else None
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

    def _interval_ms(self) -> int:
        return max(200, int(self._interval.value() * 1000))

    def _on_toggle_auto(self, on: bool) -> None:
        if on and self._session is not None:
            self._timer.start(self._interval_ms())   # repeating
        else:
            self._timer.stop()

    def _on_interval_changed(self, _v) -> None:
        if self._timer.isActive():
            self._timer.setInterval(self._interval_ms())

    def _auto_tick(self) -> None:
        if self._session is None:
            self._timer.stop()
            return
        # Skip this tick if the worker is still busy; the repeating timer
        # will catch up on the next one (no pile-up).
        if not self.is_busy():
            self._on_read_all()


# --------------------------------------------------------------------------
class TdbgPage(Page):
    # (success, preset_n) marshalled back to the GUI thread.
    doneSig = Signal(bool, int)

    def __init__(self, app) -> None:
        super().__init__(app)
        self.doneSig.connect(self._on_done)

        lay = self._frame()
        card = Card("TDBG 波形回放")
        row = QHBoxLayout()
        row.addWidget(QLabel("輸出腳位"))
        self._combo = _pin_combo()
        self._combo.setEnabled(False)
        self._combo.setFixedWidth(180)
        row.addWidget(self._combo)
        row.addStretch(1)
        clear = QPushButton("清除紀錄")
        clear.clicked.connect(self.log_pane.clear)
        row.addWidget(clear)
        card.body.addLayout(row)

        btn_row = QHBoxLayout()
        self._btns: list[QPushButton] = []
        for n in (1, 2, 3):
            b = QPushButton(f"TDBG{n}")
            b.setObjectName("accent")
            b.setEnabled(False)
            b.clicked.connect(lambda _checked=False, k=n: self._on_preset(k))
            self._btns.append(b)
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        card.body.addLayout(btn_row)

        hint = QLabel("選輸出腳位後，按 TDBG1/2/3 即一鍵送出對應波形。")
        hint.setObjectName("Muted")
        card.body.addWidget(hint)
        lay.addWidget(card)
        lay.addWidget(self.log_pane, 1)

    @property
    def _session(self):
        return self.app.tdbg_session

    def set_connected(self, connected: bool) -> None:
        self._combo.setEnabled(connected)
        for b in self._btns:
            b.setEnabled(connected)

    def _set_busy(self, busy: bool) -> None:
        self._combo.setEnabled(not busy)
        for b in self._btns:
            b.setEnabled(not busy)

    def _on_preset(self, n: int) -> None:
        if self._session is None:
            return
        pin = _combo_pin(self._combo)
        if pin is None:
            self.log("請先選擇輸出腳位。", "warn")
            return
        self._set_busy(True)
        self.app.status(f"TDBG{n} sending…", "warn")

        def cmd():
            sess = self._session
            ok = sess.send_preset(n, pin) if sess is not None else False
            self.doneSig.emit(bool(ok), n)

        self.enqueue(cmd)

    def _on_done(self, success: bool, n: int) -> None:
        # Clear busy unconditionally and then reapply the real connection
        # state. Previously this was gated on self._session is not None,
        # which permanently locked the buttons when a TDBG send raced a
        # Disconnect (session became None before _on_done arrived).
        self._set_busy(False)
        self.set_connected(self._session is not None)
        self.app.status(f"TDBG{n} sent" if success else f"TDBG{n} error",
                        "ok" if success else "err")

    def _on_worker_failure(self, detail: str, tb: str) -> None:
        # Safety net: clear busy and re-apply real connection state, even
        # if the worker job died before reaching doneSig.
        self._set_busy(False)
        self.set_connected(self._session is not None)


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
        # Lock-step: any exception in the synchronous state-set block must
        # roll back to "not recording" — otherwise _recording is stuck True
        # and the UI is frozen with no path to recover.
        try:
            self._recording = True
            self._start.setEnabled(False)
            self._stop.setEnabled(True)
            self._add_btn.setEnabled(False)
            self._thumb.setVisible(False)
            self.app.set_recording(True)
            for r in self._rows:
                r.set_combo_enabled(False)
                r.set_live_state(None)
            self.app.status("RECORD recording…", "warn")
        except Exception as e:
            self.log(f"RECORD start setup failed: {e}", "err")
            self.failSig.emit()
            return

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
            try:
                ok = sess.start(pins, on_live=on_live)
            except Exception as e:
                # A raised exception (e.g. WriteFile PermissionError when the
                # USB CDC handle goes stale) used to escape into Worker's
                # generic handler and leave the UI stuck in _recording=True
                # because failSig never fired. Surface it as a normal start
                # failure so the page resets.
                self.log(f"RECORD start exception: {e}", "err")
                self.failSig.emit()
                return
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
        self.app.status("RECORD start failed", "err")

    def _on_worker_failure(self, detail: str, tb: str) -> None:
        # Safety net: if the start/stop worker dies before its try/except,
        # roll back the recording UI so the page is usable again.
        if self._recording:
            self._on_fail()

    def _on_stop(self) -> None:
        if not self._recording:
            return
        self._stop.setEnabled(False)
        self.app.status("RECORD stopping…", "warn")

        def cmd():
            sess = self._session
            if sess is None:
                self.stopSig.emit(None)
                return
            try:
                result = sess.stop()
            except Exception as e:
                # Mirror _on_start: an exception during stop (e.g. mid-blob
                # USB hiccup) must still route through _on_stop_done so
                # _recording is cleared and the UI re-enables.
                self.log(f"RECORD stop exception: {e}", "err")
                result = None
            self.stopSig.emit(result)

        self.enqueue(cmd)

    def _on_stop_done(self, result) -> None:
        self._recording = False
        self.app.set_recording(False)
        for r in self._rows:
            r.set_combo_enabled(self._connected)
        self._sync_row_controls()
        self._refresh_start()
        if result is None:
            self.app.status("RECORD stop error", "err")
            return
        pins, events = result
        self._recorded = (pins, events)
        self._draw_thumb(pins, events)
        self._thumb.setVisible(True)
        total_us = sum(d for d, _ in events)
        # events[0] is the seed sample injected by the MCU at RECORD_START,
        # not a real transition — back it out of the user-facing edge count.
        edge_count = max(0, len(events) - 1)
        if edge_count == 0:
            self.app.status("RECORD done: no edges (level held constant)", "warn")
        else:
            self.app.status(
                f"RECORD done: {edge_count} edges, {total_us/1000:.3f} ms", "ok")

    def _draw_thumb(self, pins, events) -> None:
        p0 = pins[0]
        trace = [(d, 1 if st.get(p0) else 0) for d, st in events]
        initial = trace[0][1] if trace else 0
        self._thumb.set_thumbnail(initial, trace)

    def _open_preview(self) -> None:
        if not self._recorded:
            return
        pins, events = self._recorded
        # one trace per pin; x in microseconds
        traces = []
        for p in pins:
            ev = [(d, 1 if st.get(p) else 0) for d, st in events]
            traces.append((f"D{p}", ev[0][1] if ev else 0, ev))
        open_waveform_preview(self, "錄製波形", [traces], D.format_us, min_ppu=0.05)
