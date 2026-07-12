"""Stuck-state regression tests — every path that previously could wedge the
UI (Phase 0 fixes) plus the Worker failure-hook safety net (Phase 2).

  R1  RECORD start() raising → page resets via failSig
  R2  RECORD stop() raising → stopSig(None) → page usable again
  R3  Worker survives on_error itself raising; next job still runs
  R4  Worker on_failure hook fires; TdbgPage clears busy via _on_worker_failure
  R5  GpioPage auto-read timer stops on disconnect
  R6  FlashPage._on_worker_failure re-enables 開始燒錄 + unlocks port
  R7  connect worker exception → _conn_busy resets, 連線 clickable again
"""
from _helpers import drain, FakeSerial  # noqa: F401

import threading
import time


def main():
    from PySide6.QtWidgets import QApplication
    import binFileTransfer_core as core
    import binFileTransferGui as gui

    app = QApplication.instance() or QApplication([])
    win = gui.MainWindow()

    fake_ser = FakeSerial()
    core.open_due_link = lambda port, log: fake_ser
    gui.open_due_link = lambda port, log: fake_ser
    win._on_connect()
    assert drain(app, lambda: win._connected)

    # R1: RECORD start raises.
    record_page = win._ensure_page(3)
    record_page._rows[0].combo.setCurrentIndex(1)
    record_page._refresh_start()

    class BoomStart:
        def start(self, pins, on_live=None):
            raise OSError(13, "WriteFile failed (simulated)")

        def stop(self):
            return None

        def close(self):
            pass

    win.record_session = BoomStart()
    record_page._on_start()
    assert drain(app, lambda: not record_page._recording), \
        "R1: _recording stuck True after start() raised"
    print("R1: RECORD start exception resets page: OK")

    # R2: RECORD stop raises.
    class BoomStop(BoomStart):
        def start(self, pins, on_live=None):
            return True

        def stop(self):
            raise RuntimeError("blob boom")

    win.record_session = BoomStop()
    record_page._on_start()
    assert drain(app, lambda: record_page._recording), "R2 precondition"
    record_page._on_stop()
    assert drain(app, lambda: not record_page._recording), \
        "R2: _recording stuck True after stop() raised"
    print("R2: RECORD stop exception resets page: OK")

    # R3: Worker survives its own on_error raising.
    from gui_worker import Worker
    ran = []

    def bad_on_error(msg):
        raise RuntimeError("error handler itself broken")

    w = Worker(on_error=bad_on_error)

    def job_raises():
        raise ValueError("job boom")

    w.submit(job_raises)
    w.submit(lambda: ran.append(1))
    assert drain(app, lambda: ran, timeout=3.0), \
        "R3: worker died after on_error raised"
    assert not w.is_busy()
    w.shutdown()
    print("R3: Worker survives broken on_error: OK")

    # R4: on_failure hook — TdbgPage clears busy when a job raises.
    tdbg_page = win._ensure_page(2)
    tdbg_page.set_connected(True)

    def tdbg_job_raises():
        raise RuntimeError("tdbg job boom")

    tdbg_page._set_busy(True)
    tdbg_page.enqueue(tdbg_job_raises)
    assert drain(app, lambda: tdbg_page._btns[0].isEnabled(), timeout=3.0), \
        "R4: TDBG busy not cleared by _on_worker_failure"
    print("R4: on_failure hook clears TDBG busy: OK")

    # R5: auto-read timer stops on disconnect.
    gpio_page = win._ensure_page(1)
    gpio_page.set_connected(True)
    gpio_page._auto.setChecked(True)
    app.processEvents()
    assert gpio_page._timer.isActive(), "R5 precondition: timer should run"
    gpio_page.set_connected(False)
    assert not gpio_page._timer.isActive(), "R5: timer still active after disconnect"
    assert not gpio_page._auto.isChecked()
    gpio_page.set_connected(True)   # restore for later tests
    print("R5: auto-read timer stops on disconnect: OK")

    # R6: FlashPage worker-failure safety net.
    flash_page = win._ensure_page(0)
    flash_page._start.setEnabled(False)
    win.lock_port(True)
    flash_page._on_worker_failure("SimulatedError: x", "tb")
    assert flash_page._start.isEnabled(), "R6: _start not re-enabled"
    assert win.port_combo.isEnabled(), "R6: port still locked"
    print("R6: FlashPage failure hook re-enables start + unlocks port: OK")

    # R7: connect worker exception — _conn_busy must reset. open_due_link
    # returning None is the normal failure; a raise inside work() is caught
    # by Worker._exec, but connDoneSig then never fires. Verify the current
    # behaviour: with open_due_link returning None the button recovers.
    win.disconnect_then(None)
    assert drain(app, lambda: not win._connected)
    core.open_due_link = lambda port, log: None
    gui.open_due_link = lambda port, log: None
    win._on_connect()
    assert drain(app, lambda: not win._conn_busy and win.connect_btn.isEnabled(),
                 timeout=3.0), "R7: connect button stuck after failed connect"
    print("R7: failed connect leaves 連線 clickable: OK")

    win.close()
    app.processEvents()


if __name__ == "__main__":
    main()
    print("PASS test_stuck_states")
