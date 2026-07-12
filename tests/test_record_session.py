"""RecordSession protocol tests against a scripted fake MCU — exercises the
trickiest concurrency in the codebase: the live-thread → stop() handoff and
the binary blob read.

  RS1  full start → live ticks → stop returns parsed events, CRC verified
  RS2  MCU under-sends the blob → stop() fails with the tail_hex diagnostic
  RS3  CRC mismatch → stop() fails with a CRC log, no crash
  RS4  start with too many / invalid pins is rejected locally
"""
from _helpers import FakeSerial, collecting_log  # noqa: F401

import struct
import threading
import time

import binFileTransfer_core as core


def make_blob(events):
    return b"".join(struct.pack("<IB", d, m) for d, m in events)


class RecordMcu(FakeSerial):
    """Scripted MCU for the RECORD flow. Emits RECORD_STARTED, a couple of
    RECORD_LIVE heartbeats, then on RECORD_STOP the full stop exchange."""

    def __init__(self, events, *, short_by=0, bad_crc=False):
        super().__init__()
        self.events = events
        self.short_by = short_by
        self.bad_crc = bad_crc

    def write(self, data):
        self.written.append(bytes(data))
        text = data.decode(errors="ignore").strip()
        if text.startswith("RECORD_START"):
            self.buf += b"RECORD_STARTED\r\n"
            self.buf += b"RECORD_LIVE 01\r\n"
            self.buf += b"RECORD_LIVE 00\r\n"
        elif text == "RECORD_STOP":
            blob = make_blob(self.events)
            crc = core.tdbg_crc16(blob)
            if self.bad_crc:
                crc ^= 0xFFFF
            if self.short_by:
                blob = blob[: -self.short_by]
            self.buf += b"RECORD_STOPPED\r\n"
            self.buf += f"RECORD_DATA {len(self.events)}\r\n".encode()
            self.buf += blob
            self.buf += f"RECORD_DONE {crc:04X}\r\n".encode()


def test_round_trip():
    log, entries = collecting_log()
    events = [(0, 0b01), (1000, 0b00), (2500, 0b01)]
    mcu = RecordMcu(events)
    sess = core.RecordSession(log, ser=mcu, lock=threading.Lock())

    live_states = []
    ok = sess.start([22], on_live=lambda st: live_states.append(dict(st)))
    assert ok, [m for _, m in entries][-3:]
    # Give the live thread a beat to consume the RECORD_LIVE lines.
    deadline = time.time() + 2.0
    while time.time() < deadline and len(live_states) < 2:
        time.sleep(0.02)
    assert live_states[:2] == [{22: True}, {22: False}], live_states

    result = sess.stop()
    assert result is not None, [m for _, m in entries][-5:]
    pins, parsed = result
    assert pins == [22]
    assert parsed == [(0, {22: True}), (1000, {22: False}), (2500, {22: True})]
    print("RS1: start → live → stop round trip with CRC: OK")


def test_short_blob_diag():
    log, entries = collecting_log()
    mcu = RecordMcu([(0, 1), (500, 0), (900, 1)], short_by=3)
    sess = core.RecordSession(log, ser=mcu, lock=threading.Lock())
    assert sess.start([22]) is True
    result = sess.stop()
    assert result is None, "short blob must fail"
    diag = next((m for lvl, m in entries if "tail_hex" in m), None)
    assert diag is not None, f"missing diag; got {[m for _, m in entries][-4:]}"
    # ser.read ate the start of "RECORD_DONE" -> blob tail ends with 'REC'.
    assert "52 45 43" in diag, diag
    print("RS2: under-sent blob → tail_hex diag shows eaten 'REC': OK")


def test_crc_mismatch():
    log, entries = collecting_log()
    mcu = RecordMcu([(0, 1), (500, 0)], bad_crc=True)
    sess = core.RecordSession(log, ser=mcu, lock=threading.Lock())
    assert sess.start([22]) is True
    result = sess.stop()
    assert result is None, "CRC mismatch must fail"
    assert any("CRC mismatch" in m for _, m in entries), \
        [m for _, m in entries][-4:]
    print("RS3: CRC mismatch fails cleanly: OK")


def test_local_validation():
    log, entries = collecting_log()
    sess = core.RecordSession(log, ser=FakeSerial(), lock=None)
    assert sess.start([1, 2, 3, 4, 5]) is False, "5 pins must be rejected"
    assert sess.start([]) is False
    assert sess.start([99]) is False
    print("RS4: local pin validation: OK")


if __name__ == "__main__":
    test_round_trip()
    test_short_blob_diag()
    test_crc_mismatch()
    test_local_validation()
    print("PASS test_record_session")
