"""PySide6 GUI front-end for the SST39xF010 flash utility.

Dark vertical sidebar + light card-based content area, blue accent. This is a
pure UI layer: all serial / protocol logic lives in binFileTransfer_core
(imported lazily), and per-page Worker threads run the blocking calls while
signals marshal results back to the GUI thread.

Run:  python binFileTransferGui.py
"""
from __future__ import annotations

import os
import sys
import threading

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QPushButton, QStackedWidget, QStatusBar, QVBoxLayout, QWidget,
)

import gui_theme as T
from gui_pages import FlashPage, GpioPage, RecordPage, TdbgPage, Worker
from gui_widgets import Sidebar

APP_TITLE = "Arduino應用軟體"
AUTO_DETECT_LABEL = "Auto-detect (Arduino Due Programming Port)"
DUE_TARGET_VID = 0x2341
DUE_TARGET_PID = 0x003D
PAGE_NAMES = ["燒錄 ROM", "GPIO 設定", "TDBG", "波形錄製"]


def _resource_path(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


class MainWindow(QMainWindow):
    connDoneSig = Signal(object)
    disDoneSig = Signal(object)
    portsSig = Signal(list, str)
    statusSig = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1040, 720)
        ico = _resource_path(os.path.join("assets", "icon.ico"))
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))

        # shared serial state (one connection for GPIO/TDBG/RECORD)
        self._ser = None
        self._serial_lock = threading.Lock()
        self._connected = False
        self._conn_busy = False
        self.gpio_session = None
        self.tdbg_session = None
        self.record_session = None
        # Page instances are created lazily (see _ensure_page); None until built.
        self.flash_page = None
        self.gpio_page = None
        self.tdbg_page = None
        self.record_page = None
        self._conn_worker = Worker()
        self.connDoneSig.connect(self._on_connect_done)
        self.disDoneSig.connect(self._on_disconnect_done)
        self.portsSig.connect(self._apply_ports)
        self.statusSig.connect(self.status)

        self._build_ui()
        self._refresh_ports()

    # ---- UI assembly ------------------------------------------------------
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.sidebar = Sidebar(APP_TITLE, "Arduino 工具集", PAGE_NAMES)
        self.sidebar.currentChanged.connect(self._on_nav)
        outer.addWidget(self.sidebar)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(0)
        right_lay.addWidget(self._build_header())

        # Lazy page construction — only the page navigated to is built (the
        # GPIO 66-row grid is the heavy one), so startup is near-instant AND
        # Connect doesn't freeze the GUI thread building three pages. Sessions
        # use lazy-resolving log callbacks (_page_log_cb) and a late-built
        # page inherits the live connection via _ensure_page().
        self.stack = QStackedWidget()
        self._page_factories = [
            lambda: FlashPage(self),
            lambda: GpioPage(self),
            lambda: TdbgPage(self),
            lambda: RecordPage(self),
        ]
        self._pages: list = [None, None, None, None]
        right_lay.addWidget(self.stack, 1)
        outer.addWidget(right, 1)
        self.setCentralWidget(root)

        self._status = QStatusBar()
        self._status_lbl = QLabel("Idle")
        self._status.addWidget(self._status_lbl)
        self.setStatusBar(self._status)
        self._on_nav(0)   # build + show the first page

    _PAGE_ATTR = ("flash_page", "gpio_page", "tdbg_page", "record_page")

    @property
    def pages(self) -> list:
        return [p for p in self._pages if p is not None]

    def _ensure_page(self, i: int):
        pg = self._pages[i]
        if pg is None:
            pg = self._page_factories[i]()
            self._pages[i] = pg
            setattr(self, self._PAGE_ATTR[i], pg)
            self.stack.addWidget(pg)
            # A page built AFTER connect must inherit the live state.
            if i in (1, 2, 3) and self._connected:
                pg.set_connected(True)
        return pg

    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("Header")
        lay = QHBoxLayout(header)
        lay.setContentsMargins(18, 12, 18, 12)
        lay.setSpacing(8)

        self._title_lbl = QLabel(PAGE_NAMES[0])
        self._title_lbl.setObjectName("PageTitle")
        lay.addWidget(self._title_lbl)
        lay.addStretch(1)

        lay.addWidget(QLabel("Port:"))
        self.port_combo = QComboBox()
        self.port_combo.addItem(AUTO_DETECT_LABEL)
        self.port_combo.setMinimumWidth(300)
        lay.addWidget(self.port_combo)
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setObjectName("chip")
        self.refresh_btn.clicked.connect(self._refresh_ports)
        lay.addWidget(self.refresh_btn)

        self.connect_btn = QPushButton("連線")
        self.connect_btn.setObjectName("accent")
        self.connect_btn.clicked.connect(self._on_connect)
        self.disconnect_btn = QPushButton("斷線")
        self.disconnect_btn.setEnabled(False)
        self.disconnect_btn.clicked.connect(lambda: self.disconnect_then(None))
        lay.addWidget(self.connect_btn)
        lay.addWidget(self.disconnect_btn)
        return header

    # ---- navigation -------------------------------------------------------
    def _on_nav(self, index: int) -> None:
        pg = self._ensure_page(index)
        self.stack.setCurrentWidget(pg)
        self._title_lbl.setText(PAGE_NAMES[index])

    # ---- ports ------------------------------------------------------------
    def get_port(self):
        label = self.port_combo.currentText()
        if label == AUTO_DETECT_LABEL or not label:
            return None
        return label.split(" — ", 1)[0]

    def lock_port(self, locked: bool) -> None:
        self.port_combo.setEnabled(not locked)
        self.refresh_btn.setEnabled(not locked)
        self.connect_btn.setEnabled(not locked and not self._connected)

    def _refresh_ports(self) -> None:
        def work():
            try:
                import serial.tools.list_ports
                values = [AUTO_DETECT_LABEL]
                default = AUTO_DETECT_LABEL
                for p in serial.tools.list_ports.comports():
                    label = f"{p.device} — {p.description or 'unknown'}"
                    values.append(label)
                    if (default == AUTO_DETECT_LABEL
                            and p.vid == DUE_TARGET_VID and p.pid == DUE_TARGET_PID):
                        default = label
                self.portsSig.emit(values, default)
            except Exception:
                self.portsSig.emit([AUTO_DETECT_LABEL], AUTO_DETECT_LABEL)

        self._conn_worker.submit(work)

    def _apply_ports(self, values: list, default: str) -> None:
        # Preserve the user's current pick across a refresh if it's still
        # present; otherwise fall back to the auto-detected default.
        prev = self.port_combo.currentText()
        self.port_combo.clear()
        self.port_combo.addItems(values)
        target = prev if prev in values else default
        self.port_combo.setCurrentIndex(values.index(target) if target in values else 0)

    # ---- shared connection ------------------------------------------------
    def is_connected(self) -> bool:
        return self._connected

    def _any_busy(self) -> bool:
        return any(p.is_busy() for p in self.pages)

    def set_status(self, text: str, color: str) -> None:
        self._status_lbl.setText(text)
        self._status_lbl.setStyleSheet(f"color: {color};")

    _STATUS_COLORS = {
        "ok": "success_dark", "warn": "warning_dark",
        "err": "danger_dark", "info": "text_secondary",
    }

    def status(self, text: str, level: str = "info") -> None:
        """Semantic status update — single place mapping level → colour so
        every page / call site stays consistent. Pages call this, not the
        raw set_status(text, color)."""
        self.set_status(
            text, T.PALETTE[self._STATUS_COLORS.get(level, "text_secondary")])

    def _set_conn(self, text: str, color: str) -> None:
        self.sidebar.set_connection(text, color)

    def _on_connect(self) -> None:
        if self._connected or self._conn_busy:
            return
        if self._any_busy():
            self.status("忙碌中，請稍候", "warn")
            return
        # Pages 1-3 stay lazy: late-built pages inherit live state via
        # _ensure_page(), and connect-handshake log goes to the status bar
        # via _connect_log_cb. Sessions are wired to per-page log callbacks
        # that resolve lazily so a page that isn't built yet doesn't block
        # the connection or pin the worker to the GUI thread.
        port = self.get_port()
        self._conn_busy = True
        self._set_conn("連線中…", T.PALETTE["warning_dark"])
        self.connect_btn.setEnabled(False)
        self.lock_port(True)
        self.status("Connecting…", "warn")

        def work():
            from binFileTransfer_core import open_due_link
            ser = open_due_link(port, self._connect_log_cb)
            self.connDoneSig.emit(ser)

        self._conn_worker.submit(work)

    def _connect_log_cb(self, msg: str, level: str = "info") -> None:
        """Connect-handshake log sink. open_due_link runs on the connect
        worker thread, so this MUST marshal to the GUI thread via a queued
        signal rather than touch the status-bar widget directly — calling
        self.status() from the worker thread freezes/crashes Qt."""
        self.statusSig.emit(msg, level)

    def _page_log_cb(self, attr: str):
        """Build a log callback that resolves the target page lazily — pages
        are built on first navigation, so sessions created at connect time
        must not capture a None page reference at construction."""
        def cb(*args, **kwargs):
            pg = getattr(self, attr, None)
            if pg is not None:
                pg.log_cb(*args, **kwargs)
        return cb

    def _on_connect_done(self, ser) -> None:
        self._conn_busy = False
        if ser is None:
            self._set_conn("未連線", T.PALETTE["danger_dark"])
            self.connect_btn.setEnabled(True)
            self.lock_port(False)
            self.status("Connect failed", "err")
            return
        from binFileTransfer_core import GpioSession, RecordSession, TdbgSession
        self._ser = ser
        self.gpio_session = GpioSession(
            self._page_log_cb("gpio_page"), ser=ser, lock=self._serial_lock)
        self.tdbg_session = TdbgSession(
            self._page_log_cb("tdbg_page"), ser=ser, lock=self._serial_lock)
        self.record_session = RecordSession(
            self._page_log_cb("record_page"), ser=ser, lock=self._serial_lock)
        self._connected = True
        self._set_conn("已連線", T.PALETTE["success_dark"])
        self.disconnect_btn.setEnabled(True)
        for pg in (self.gpio_page, self.tdbg_page, self.record_page):
            if pg is not None:
                pg.set_connected(True)
        self.status("Connected", "ok")

    def set_recording(self, active: bool) -> None:
        if not self._connected:
            return
        if self.gpio_page is not None:
            self.gpio_page.set_connected(not active)
        if self.tdbg_page is not None:
            self.tdbg_page.set_connected(not active)

    def disconnect_then(self, on_done) -> None:
        if not self._connected:
            if on_done is not None:
                on_done()
            return
        self._set_conn("斷線中…", T.PALETTE["warning_dark"])
        self.disconnect_btn.setEnabled(False)
        for pg in (self.gpio_page, self.tdbg_page, self.record_page):
            if pg is not None:
                pg.set_connected(False)
        sessions = [s for s in (self.record_session, self.tdbg_session,
                                self.gpio_session) if s is not None]
        ser = self._ser

        def work():
            # Any exception inside this worker must NOT swallow disDoneSig —
            # otherwise the caller (e.g. FlashPage._do_start chained off
            # disconnect_then) never runs, _start stays disabled, port stays
            # locked. Guard with try/finally so the signal always fires.
            try:
                for s in sessions:
                    try:
                        s.close()
                    except Exception:
                        pass
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
            finally:
                self.disDoneSig.emit(on_done)

        self._conn_worker.submit(work)

    def _on_disconnect_done(self, on_done) -> None:
        self._ser = None
        self.gpio_session = None
        self.tdbg_session = None
        self.record_session = None
        self._connected = False
        self._set_conn("未連線", T.PALETTE["danger_dark"])
        self.connect_btn.setEnabled(True)
        self.lock_port(False)
        self.status("Disconnected", "info")
        if on_done is not None:
            on_done()

    # ---- lifecycle --------------------------------------------------------
    def closeEvent(self, event) -> None:
        # Close the sessions FIRST — RecordSession.close() joins its live
        # reader thread, so the daemon stops touching the port before we drop
        # it. Closing only self._ser (as before) could leave that thread
        # reading a closed handle mid-recording.
        for s in (self.record_session, self.tdbg_session, self.gpio_session):
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        try:
            if self._ser is not None:
                self._ser.close()
        except Exception:
            pass
        for pg in self.pages:
            pg.shutdown()
        self._conn_worker.shutdown()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setFont(QFont(T.UI_FONT_FAMILY, 10))
    app.setStyleSheet(T.build_qss())
    win = MainWindow()
    win._set_conn("未連線", T.PALETTE["danger_dark"])
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
