"""The four content pages for the Qt GUI.

Each connectable page owns a Worker (defined in gui_worker) that runs the
blocking serial calls on its own QThread; results/log lines marshal back to
the GUI thread via queued signals — never touching widgets off-thread.
Serial/protocol logic lives in binFileTransfer_core.
"""
from __future__ import annotations

import os
import threading

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout,
    QHBoxLayout, QLabel, QProgressBar, QPushButton, QSpinBox, QVBoxLayout,
    QWidget,
)

import gui_data as D
import gui_theme as T
from binFileTransfer_core import (
    SgpioFraming, parse_sgpio_frame, program_firmware,
)
from gui_widgets import (
    Card, LogPane, PinGrid, WaveformView, open_waveform_preview,
)
from gui_worker import Page, Worker  # re-export so existing importers work



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
    # Flash does NOT need 連線 — program_firmware opens the port itself and
    # in fact drops any shared link first. Override the inherited gate
    # wording so this page never tells the user to connect.
    GATE_HINT = "選擇 .bin 檔後按「開始燒錄」。此操作會抹除晶片並中斷連線。"
    READY_HINT = GATE_HINT
    AFTER_FLASH_HINT = GATE_HINT
    doneSig = Signal(bool)
    progSig = Signal(int, int)   # (done_chunks, total_chunks) → GUI thread

    def __init__(self, app) -> None:
        super().__init__(app)
        self.firmware_path: str | None = None
        self.doneSig.connect(self._on_done)
        self.progSig.connect(self._on_progress)

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

        # Chunk progress: a flash is ~70 s of scrolling log otherwise —
        # this is the at-a-glance answer to "how far along is it?".
        self._progress = QProgressBar()
        self._progress.setRange(0, 32)
        self._progress.setFormat("燒錄中 %v / %m chunks (%p%)")
        self._progress.setVisible(False)
        card.body.addWidget(self._progress)

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
                ok = bool(program_firmware(
                    path, self.log_cb, port=port,
                    on_progress=lambda d, t: self.progSig.emit(d, t)))
            finally:
                self.doneSig.emit(ok)

        self._progress.setValue(0)
        self._progress.setVisible(True)
        self.app.status("Programming…", "warn")
        # The shared link was just released (program_firmware owns the port
        # for the duration), so the sidebar would read 未連線 mid-flash —
        # technically true but alarming. Show what's actually happening.
        self.app._set_conn("燒錄中…", "warn")
        self.app.lock_port(True)
        self.enqueue(work)

    def _on_progress(self, done: int, total: int) -> None:
        if self._progress.maximum() != total:
            self._progress.setRange(0, total)
        self._progress.setValue(done)
        self.app.status(f"Programming… {done}/{total} ({100*done//total}%)",
                        "warn")

    def _on_worker_failure(self, detail: str, tb: str) -> None:
        # Safety net: if work() somehow doesn't reach its try/finally,
        # this still re-enables _start and unlocks the port so the page
        # never wedges. doneSig already does this in normal failures.
        if not self._start.isEnabled():
            self._start.setEnabled(True)
            self._progress.setVisible(False)
            self.app.lock_port(False)
            try:
                self.app._set_conn("未連線", "err")
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
            # Let the other pages surface the reset requirement in their own
            # empty-state hint; cleared on the next successful connect.
            self.app._needs_reset = True
            for pg in self.app.pages:
                if pg is not self:
                    pg.apply_gate_hint(False)
        else:
            self.app.status("Error", "err")
        # Flash is done either way and the shared link really is closed now —
        # restore the true indicator (matches _on_disconnect_done).
        self.app._set_conn("未連線", "err")
        self._progress.setVisible(False)
        self._start.setEnabled(True)
        self.app.lock_port(False)


# --------------------------------------------------------------------------
class GpioPage(Page):
    readSig = Signal(int, str)
    READY_HINT = "點各列圓點讀取單一腳位，或按 Read All 掃描全部。"

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
        # Explicit lambda: clicked emits a `checked` bool that would land in
        # _on_read_all's quiet parameter by accident otherwise.
        self._read_all.clicked.connect(lambda: self._on_read_all(quiet=False))
        self._auto = QCheckBox("自動讀取，每")
        self._auto.setEnabled(False)
        self._auto.toggled.connect(self._on_toggle_auto)
        self._interval = QDoubleSpinBox()
        self._interval.setRange(0.2, 60.0)
        self._interval.setSingleStep(0.5)
        self._interval.setValue(1.0)
        self._interval.setFixedWidth(70)
        # Match _read_all / _auto: set_connected() governs this afterwards,
        # but it is never called at construction — without this the interval
        # spinbox was the single live control on an otherwise dead page.
        self._interval.setEnabled(False)
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
        self.apply_gate_hint(connected)
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

    def _on_read_all(self, quiet: bool = False) -> None:
        """Sweep-read all pins. `quiet=True` (the auto-read timer) suppresses
        the per-pin send/recv logs — 66 pins × 2 lines at 1 Hz floods the
        5000-line pane in ~40 s and buries every real message; the read dots
        already carry the data. Errors still log either way."""
        if self._session is None:
            self.log("尚未連線，無法讀取", "warn")
            return

        def cmd():
            for pin in self._grid.pins():
                if self._abort.is_set():
                    break
                sess = self._session
                v = (sess.read_pin(pin, quiet=quiet)
                     if sess is not None else None)
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
        # will catch up on the next one (no pile-up). quiet=True: the sweep's
        # routine logs would drown the pane — dots carry the data.
        if not self.is_busy():
            self._on_read_all(quiet=True)


# --------------------------------------------------------------------------
class TdbgPage(Page):
    # (success, preset_n) marshalled back to the GUI thread.
    doneSig = Signal(bool, int)
    READY_HINT = "選輸出腳位後，按 TDBG1/2/3 送出對應波形。"

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
        self.apply_gate_hint(connected)
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
    READY_HINT = "選 1-4 支腳位後按「開始」錄製；按「結束」取回波形。"
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
        # Persistent capture summary — the status bar version is overwritten
        # by whatever happens next; this stays until the next recording.
        self._summary_lbl = QLabel("")
        self._summary_lbl.setObjectName("Muted")
        act.addWidget(self._summary_lbl)
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
        self.apply_gate_hint(connected)
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
            self.app.set_recording(True, self)
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
        self.app.set_recording(False, self)
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
        self.app.set_recording(False, self)
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
            self._summary_lbl.setText("0 邊緣(電位未變)")
            self.app.status("RECORD done: no edges (level held constant)", "warn")
        else:
            self._summary_lbl.setText(
                f"{edge_count} 邊緣 · {total_us/1000:.3f} ms")
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


# --------------------------------------------------------------------------
class SgpioPage(Page):
    """SGPIO (SFF-8485) passive decoder tab.

    Pick the three input pins (SClock / SLoad / SDataOut), set the framing
    (how the captured bits split into per-drive Activity/Locate/Fault), press
    Start. The MCU streams a decoded bit frame every time it CHANGES; this
    page decodes each frame with the same host-side parser the tests pin down
    (parse_sgpio_frame) and lights the per-drive dots. A frame whose length
    disagrees with the framing is flagged red — that is the "is the data
    right?" check surfaced in the UI."""
    READY_HINT = "選 SClock / SLoad / SDataOut 三支腳並設定 framing 後按「開始」。"
    frameSig = Signal(str)          # raw bitstring from the live thread
    doneSig = Signal(bool, str)     # (ok, action) for start/stop handshakes
    failSig = Signal()

    def __init__(self, app) -> None:
        super().__init__(app)
        self._active = False
        self._connected = False
        self._drive_cells: list = []    # per-drive (act, loc, flt, val) labels
        self._frame_count = 0           # liveness: every SGPIO_FRAME arrival
        self._last_bits: str | None = None   # skip re-render on heartbeats
        self.frameSig.connect(self._apply_frame)
        self.doneSig.connect(self._on_done)
        self.failSig.connect(self._on_fail)

        lay = self._frame()
        card = Card("SGPIO 被動解碼")

        # --- pin pickers ---------------------------------------------------
        pins = QHBoxLayout()
        self._c_sclk = _pin_combo()
        self._c_sload = _pin_combo()
        self._c_sdata = _pin_combo()
        for lbl, combo in (("SClock", self._c_sclk), ("SLoad", self._c_sload),
                           ("SDataOut", self._c_sdata)):
            pins.addWidget(QLabel(lbl))
            combo.setFixedWidth(150)
            combo.currentIndexChanged.connect(self._refresh_start)
            pins.addWidget(combo)
        pins.addStretch(1)
        card.body.addLayout(pins)

        # --- framing config ------------------------------------------------
        fr = QHBoxLayout()
        self._sp_drives = QSpinBox(); self._sp_drives.setRange(1, 64)
        self._sp_drives.setValue(4)
        self._sp_bits = QSpinBox(); self._sp_bits.setRange(1, 8)
        self._sp_bits.setValue(3)
        self._sp_header = QSpinBox(); self._sp_header.setRange(0, 64)
        self._sp_header.setValue(0)
        self._cb_msb = QCheckBox("MSB first"); self._cb_msb.setChecked(True)
        self._cb_rising = QCheckBox("SClock 上升沿取樣")
        self._cb_rising.setChecked(True)
        self._cb_sload_hi = QCheckBox("SLoad active-high")
        self._cb_sload_hi.setChecked(True)
        for lbl, w in (("drives", self._sp_drives), ("bits/drive", self._sp_bits),
                       ("header bits", self._sp_header)):
            fr.addWidget(QLabel(lbl))
            fr.addWidget(w)
        fr.addWidget(self._cb_msb)
        fr.addStretch(1)
        card.body.addLayout(fr)
        fr2 = QHBoxLayout()
        fr2.addWidget(self._cb_rising)
        fr2.addWidget(self._cb_sload_hi)
        self._frame_lbl = QLabel("每幀 12 bits")
        self._frame_lbl.setObjectName("Muted")
        self._sp_drives.valueChanged.connect(self._refresh_frame_len)
        self._sp_bits.valueChanged.connect(self._refresh_frame_len)
        self._sp_header.valueChanged.connect(self._refresh_frame_len)
        fr2.addWidget(self._frame_lbl)
        fr2.addStretch(1)
        card.body.addLayout(fr2)

        # --- actions -------------------------------------------------------
        act = QHBoxLayout()
        self._start = QPushButton("開始")
        self._start.setObjectName("accent")
        self._start.setEnabled(False)
        self._start.clicked.connect(self._on_start)
        self._stop = QPushButton("停止")
        self._stop.setEnabled(False)
        self._stop.clicked.connect(self._on_stop)
        clear = QPushButton("清除紀錄")
        clear.clicked.connect(self.log_pane.clear)
        act.addWidget(self._start)
        act.addWidget(self._stop)
        act.addStretch(1)
        act.addWidget(clear)
        card.body.addLayout(act)

        # --- live drive table ---------------------------------------------
        self._table = QGridLayout()
        self._table.setHorizontalSpacing(14)
        self._table.setVerticalSpacing(3)
        card.body.addLayout(self._table)
        raw_row = QHBoxLayout()
        self._raw_lbl = QLabel("—")
        self._raw_lbl.setObjectName("Muted")
        self._count_lbl = QLabel("")
        self._count_lbl.setObjectName("Muted")
        raw_row.addWidget(self._raw_lbl, 1)
        raw_row.addWidget(self._count_lbl)
        card.body.addLayout(raw_row)

        lay.addWidget(card)
        lay.addWidget(self.log_pane, 1)
        self._rebuild_table(4)

    # -- helpers ------------------------------------------------------------
    @property
    def _session(self):
        return self.app.sgpio_session

    def _framing(self) -> SgpioFraming:
        return SgpioFraming(
            num_drives=self._sp_drives.value(),
            bits_per_drive=self._sp_bits.value(),
            header_bits=self._sp_header.value(),
            msb_first=self._cb_msb.isChecked(),
        )

    def _refresh_frame_len(self) -> None:
        self._frame_lbl.setText(f"每幀 {self._framing().frame_len} bits")

    def _selected_pins(self):
        return (_combo_pin(self._c_sclk), _combo_pin(self._c_sload),
                _combo_pin(self._c_sdata))

    def _rebuild_table(self, num_drives: int) -> None:
        while self._table.count():
            item = self._table.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self._drive_cells = []
        _TIPS = {"A": "Activity", "L": "Locate", "F": "Fault"}
        for col, head in enumerate(("Drive", "A", "L", "F", "value")):
            h = QLabel(head)
            h.setObjectName("Muted")
            if head in _TIPS:
                h.setToolTip(_TIPS[head])
            self._table.addWidget(h, 0, col)
        for d in range(num_drives):
            self._table.addWidget(QLabel(f"D#{d}"), d + 1, 0)
            dots = []
            for col in range(1, 4):
                dot = QLabel("●")
                dot.setStyleSheet(f"color: {T.PALETTE['text_disabled']};")
                self._table.addWidget(dot, d + 1, col)
                dots.append(dot)
            val = QLabel("—")
            self._table.addWidget(val, d + 1, 4)
            self._drive_cells.append((dots[0], dots[1], dots[2], val))

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        self.apply_gate_hint(connected)
        if self._active and not connected:
            # Dropped mid-decode — reset UI (session already torn down by App).
            self._active = False
            self._stop.setEnabled(False)
        for w in (self._c_sclk, self._c_sload, self._c_sdata, self._sp_drives,
                  self._sp_bits, self._sp_header, self._cb_msb,
                  self._cb_rising, self._cb_sload_hi):
            w.setEnabled(connected and not self._active)
        self._refresh_start()

    def _refresh_start(self) -> None:
        sc, sl, sd = self._selected_pins()
        ready = (self._connected and not self._active
                 and None not in (sc, sl, sd) and len({sc, sl, sd}) == 3)
        self._start.setEnabled(ready)

    # -- start / frame / stop ----------------------------------------------
    def _on_start(self) -> None:
        if self._session is None or self._active:
            return
        sc, sl, sd = self._selected_pins()
        if None in (sc, sl, sd) or len({sc, sl, sd}) != 3:
            self.log("請選三支不同的腳位。", "warn")
            return
        framing = self._framing()
        rising = self._cb_rising.isChecked()
        sload_hi = self._cb_sload_hi.isChecked()
        try:
            self._active = True
            self._start.setEnabled(False)
            self._stop.setEnabled(True)
            self.set_connected(True)   # disables the config widgets
            self.app.set_recording(True, self)   # grey out the other tabs
            self._rebuild_table(framing.num_drives)
            self._frame_count = 0
            self._last_bits = None
            self._count_lbl.setText("已收 0 幀")
            # Also clear the raw-frame line. Without this a run that ended on
            # a flagged frame leaves that red text next to "已收 0 幀" of the
            # NEW run — showing the old framing's error beside the new
            # config, which is exactly the misreading this panel exists to
            # prevent.
            self._raw_lbl.setText("—")
            self._raw_lbl.setStyleSheet("")
            self.app.status("SGPIO decoding…", "warn")
        except Exception as e:
            self.log(f"SGPIO start setup failed: {e}", "err")
            self.failSig.emit()
            return

        self._live_framing = framing

        def cmd():
            sess = self._session
            if sess is None:
                self.failSig.emit()
                return
            try:
                ok = sess.start(
                    sc, sl, sd, framing,
                    sample_rising=rising, sload_active_high=sload_hi,
                    on_frame=lambda b: self.frameSig.emit(b))
            except Exception as e:
                self.log(f"SGPIO start exception: {e}", "err")
                self.failSig.emit()
                return
            self.doneSig.emit(bool(ok), "start")

        self.enqueue(cmd)

    @staticmethod
    def _group_bits(bits: str, framing: SgpioFraming) -> str:
        """Display-format a frame per drive: '100010001111' → '100 010 001
        111', with any header bits split off by '|'. Pure formatting — the
        decode itself stays in parse_sgpio_frame."""
        head = bits[:framing.header_bits]
        body = bits[framing.header_bits:]
        w = framing.bits_per_drive
        groups = [body[i:i + w] for i in range(0, len(body), w)]
        return (f"{head} | " if head else "") + " ".join(groups)

    def _apply_frame(self, bits: str) -> None:
        f = getattr(self, "_live_framing", None) or self._framing()
        # Liveness: count every arrival (including the ~200 ms heartbeat
        # re-send of an unchanged frame) so a quiet bus still visibly ticks.
        self._frame_count += 1
        self._count_lbl.setText(f"已收 {self._frame_count} 幀")
        # Heartbeats repeat the same frame 5x/s — skip the full re-parse +
        # re-style when nothing changed. (This is host-side polish; the MCU's
        # delta-report already keeps the serial link quiet.)
        if bits == self._last_bits:
            return
        self._last_bits = bits
        self._raw_lbl.setText(
            f"frame ({len(bits)} bits): {self._group_bits(bits, f)}")
        try:
            drives = parse_sgpio_frame(bits, f)
        except ValueError:
            # Structural mismatch — flag it instead of decoding garbage.
            self._raw_lbl.setStyleSheet(f"color: {T.PALETTE['danger']};")
            self.log(f"幀長 {len(bits)} != 預期 {f.frame_len} — 檢查 framing / "
                     "SLoad 設定", "err")
            return
        self._raw_lbl.setStyleSheet("")
        for cell, drv in zip(self._drive_cells, drives):
            act, loc, flt, val = cell
            for dot, key in ((act, "activity"), (loc, "locate"), (flt, "fault")):
                on = drv.get(key, False)
                color = T.PALETTE["success"] if on else T.PALETTE["text_disabled"]
                dot.setStyleSheet(f"color: {color};")
            val.setText(f"{drv['bits']} ({drv['value']})")

    def _on_stop(self) -> None:
        if not self._active:
            return
        self._stop.setEnabled(False)
        self.app.status("SGPIO stopping…", "warn")

        def cmd():
            sess = self._session
            ok = sess.stop() if sess is not None else False
            self.doneSig.emit(bool(ok), "stop")

        self.enqueue(cmd)

    def _on_done(self, ok: bool, action: str) -> None:
        if action == "start" and not ok:
            self._on_fail()
            return
        if action == "stop":
            self._active = False
            self.app.set_recording(False, self)   # re-enable the other tabs
            self.set_connected(self._connected)
            self.app.status("SGPIO stopped" if ok else "SGPIO stop error",
                            "ok" if ok else "err")

    def _on_fail(self) -> None:
        self._active = False
        self._stop.setEnabled(False)
        self.app.set_recording(False, self)       # re-enable the other tabs
        self.set_connected(self._connected)
        self.app.status("SGPIO start failed", "err")

    def _on_worker_failure(self, detail: str, tb: str) -> None:
        if self._active:
            self._on_fail()
