"""Shared helpers for the offscreen test suite.

Every test in tests/ runs WITHOUT hardware: Qt renders offscreen and the
serial layer is replaced by fakes. Run everything via `python tests/run_all.py`
from the repo root."""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Tests run from repo root or tests/ — make the repo root importable either way.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def drain(app, cond, timeout=3.0, interval=0.01):
    """Pump the Qt event loop until cond() is truthy or timeout expires.
    Returns cond()'s final value."""
    deadline = time.time() + timeout
    while time.time() < deadline and not cond():
        app.processEvents()
        time.sleep(interval)
    return cond()


class FakeSerial:
    """Scriptable serial stand-in for protocol round-trip tests.

    `script` maps a sent command prefix -> bytes to queue as the reply.
    Raw pre-queued bytes can be set via `preload`."""

    def __init__(self, script=None, preload=b""):
        self.script = script or {}
        self.buf = bytearray(preload)
        self.written = []
        self.timeout = 0.1
        self.closed = False

    # -- pyserial surface ---------------------------------------------------
    @property
    def in_waiting(self):
        return len(self.buf)

    def write(self, data):
        self.written.append(bytes(data))
        text = data.decode(errors="ignore").strip()
        for prefix, reply in self.script.items():
            if text.startswith(prefix):
                if callable(reply):
                    reply = reply(text)
                if reply:
                    self.buf += reply
                break

    def flush(self):
        pass

    def readline(self):
        idx = self.buf.find(b"\n")
        if idx < 0:
            data, self.buf = bytes(self.buf), bytearray()
            return data
        line = bytes(self.buf[: idx + 1])
        del self.buf[: idx + 1]
        return line

    def read(self, n):
        n = min(n, len(self.buf))
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data

    def reset_input_buffer(self):
        self.buf = bytearray()

    def close(self):
        self.closed = True


def collecting_log():
    """Return (log_fn, entries) where entries accumulates (level, msg)."""
    entries = []

    def log(msg, level="info"):
        entries.append((level, msg))

    return log, entries
