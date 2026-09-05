"""Worker thread + Page base — the threading infrastructure all GUI pages
share. Extracted from gui_pages so the threading model is easy to find
without scrolling past four page implementations.

Worker runs blocking serial calls off the GUI thread. Page wires its
Worker's on_error/on_failure callbacks through queued Qt signals so they
land on the GUI thread (never touching widgets from the worker)."""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QVBoxLayout, QWidget

from gui_widgets import LogPane


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
        self.log_pane.set_hint(self.GATE_HINT)   # nothing calls
        # set_connected() at construction, so seed the gated wording here.
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

    # Shown in the empty log pane while the page's controls are gated, so a
    # dead-looking page always says why. Subclasses override READY_HINT with
    # what to do once connected.
    GATE_HINT = "請先按右上角「連線」，本頁功能才會啟用。"
    READY_HINT = ""
    # Shown instead of GATE_HINT right after a flash: erasing takes the MCU
    # out of its idle loop, so 連線 alone will NOT bring these pages back.
    # That instruction was previously logged only to FlashPage — the one page
    # the user is leaving when they need it.
    AFTER_FLASH_HINT = ("剛完成燒錄：請先按 Due 板上的 reset 鈕，"
                        "再按右上角「連線」，本頁功能才會恢復。")

    def apply_gate_hint(self, connected: bool) -> None:
        """Refresh the empty-state hint. Called by the pages from their own
        set_connected() so the wording is chosen in one place."""
        if connected:
            self.log_pane.set_hint(self.READY_HINT)
        elif getattr(self.app, "_needs_reset", False):
            self.log_pane.set_hint(self.AFTER_FLASH_HINT)
        else:
            self.log_pane.set_hint(self.GATE_HINT)

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
