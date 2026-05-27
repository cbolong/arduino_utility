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

from PySide6.QtCore import Qt, Signal
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
        self._conn_worker = Worker()
        self.connDoneSig.connect(self._on_connect_done)
        self.disDoneSig.connect(self._on_disconnect_done)
        self.portsSig.connect(self._apply_ports)

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

        self.stack = QStackedWidget()
        self.flash_page = FlashPage(self)
        self.gpio_page = GpioPage(self)
        self.tdbg_page = TdbgPage(self)
        self.record_page = RecordPage(self)
        self.pages = [self.flash_page, self.gpio_page,
                      self.tdbg_page, self.record_page]
        for pg in self.pages:
            self.stack.addWidget(pg)
        right_lay.addWidget(self.stack, 1)
        outer.addWidget(right, 1)
        self.setCentralWidget(root)

        self._status = QStatusBar()
        self._status_lbl = QLabel("Idle")
        self._status.addWidget(self._status_lbl)
        self.setStatusBar(self._status)

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
        self.stack.setCurrentIndex(index)
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
        self.port_combo.clear()
        self.port_combo.addItems(values)
        idx = values.index(default) if default in values else 0
        self.port_combo.setCurrentIndex(idx)

    # ---- shared connection ------------------------------------------------
    def is_connected(self) -> bool:
        return self._connected

    def _any_busy(self) -> bool:
        return any(p.is_busy() for p in self.pages)

    def set_status(self, text: str, color: str) -> None:
        self._status_lbl.setText(text)
        self._status_lbl.setStyleSheet(f"color: {color};")

    def _set_conn(self, text: str, color: str) -> None:
        self.sidebar.set_connection(text, color)

    def _on_connect(self) -> None:
        if self._connected or self._conn_busy:
            return
        if self._any_busy():
            self.set_status("忙碌中，請稍候", T.PALETTE["warning_dark"])
            return
        port = self.get_port()
        self._conn_busy = True
        self._set_conn("連線中…", T.PALETTE["warning_dark"])
        self.connect_btn.setEnabled(False)
        self.lock_port(True)
        self.set_status("Connecting…", T.PALETTE["warning_dark"])

        def work():
            from binFileTransfer_core import open_due_link
            ser = open_due_link(port, self.tdbg_page.log_cb)
            self.connDoneSig.emit(ser)

        self._conn_worker.submit(work)

    def _on_connect_done(self, ser) -> None:
        self._conn_busy = False
        if ser is None:
            self._set_conn("未連線", T.PALETTE["danger_dark"])
            self.connect_btn.setEnabled(True)
            self.lock_port(False)
            self.set_status("Connect failed", T.PALETTE["danger_dark"])
            return
        from binFileTransfer_core import GpioSession, RecordSession, TdbgSession
        self._ser = ser
        self.gpio_session = GpioSession(
            self.gpio_page.log_cb, ser=ser, lock=self._serial_lock)
        self.tdbg_session = TdbgSession(
            self.tdbg_page.log_cb, ser=ser, lock=self._serial_lock)
        self.record_session = RecordSession(
            self.record_page.log_cb, ser=ser, lock=self._serial_lock)
        self._connected = True
        self._set_conn("已連線", T.PALETTE["success_dark"])
        self.disconnect_btn.setEnabled(True)
        for pg in (self.gpio_page, self.tdbg_page, self.record_page):
            pg.set_connected(True)
        self.set_status("Connected", T.PALETTE["success_dark"])

    def set_recording(self, active: bool) -> None:
        if not self._connected:
            return
        self.gpio_page.set_connected(not active)
        self.tdbg_page.set_connected(not active)

    def disconnect_then(self, on_done) -> None:
        if not self._connected:
            if on_done is not None:
                on_done()
            return
        self._set_conn("斷線中…", T.PALETTE["warning_dark"])
        self.disconnect_btn.setEnabled(False)
        for pg in (self.gpio_page, self.tdbg_page, self.record_page):
            pg.set_connected(False)
        sessions = [s for s in (self.record_session, self.tdbg_session,
                                self.gpio_session) if s is not None]
        ser = self._ser

        def work():
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
        self.set_status("Disconnected", T.PALETTE["text_secondary"])
        if on_done is not None:
            on_done()

    # ---- lifecycle --------------------------------------------------------
    def closeEvent(self, event) -> None:
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
