"""TdbgSession protocol tests against a scripted fake MCU.

  T1  load(): STOP-drain → TDBG_LOAD → TDBG_READY → blob → TDBG_LOADED CRC
  T2  load() with stale playback chatter in the buffer still succeeds
      (the pre-LOAD drain absorbs TDBG_PLAY_DONE etc.)
  T3  load() CRC mismatch fails cleanly
  T4  play() is fire-and-forget: writes TDBG_PLAY / TDBG_PLAY_LOOP n and
      returns True without waiting; iterations=0 rejected
  T5  calibration pattern is loadable (all deltas above the engine floor)
  T6  local validation: bad pin / initial state / event count never hit wire
"""
from _helpers import FakeSerial, collecting_log  # noqa: F401

import threading

import binFileTransfer_core as core


class TdbgMcu(FakeSerial):
    def __init__(self, *, bad_crc=False, stale_chatter=b""):
        super().__init__(preload=stale_chatter)
        self.bad_crc = bad_crc
        self.expect_blob = 0
        self.blob = b""

    def write(self, data):
        self.written.append(bytes(data))
        if self.expect_blob:
            self.blob += data
            if len(self.blob) >= self.expect_blob:
                crc = core.tdbg_crc16(self.blob)
                if self.bad_crc:
                    crc ^= 0xFFFF
                self.buf += f"TDBG_LOADED {crc:04X}\r\n".encode()
                self.expect_blob = 0
            return
        text = data.decode(errors="ignore").strip()
        if text.startswith("TDBG_LOAD "):
            n = int(text.split()[2])
            self.expect_blob = n * 5
            self.blob = b""
            self.buf += b"TDBG_READY\r\n"
        # TDBG_STOP while idle: MCU silently ignores (no reply) — modelled
        # by doing nothing here.


def test_load_happy():
    log, entries = collecting_log()
    mcu = TdbgMcu()
    sess = core.TdbgSession(log, ser=mcu, lock=threading.Lock())
    events = [(100, 1), (200, 0), (500, 1)]
    assert sess.load(30, 0, events) is True, [m for _, m in entries][-4:]
    # The wire saw: TDBG_STOP (abort), TDBG_LOAD header, then the blob.
    sent = b"".join(mcu.written)
    assert b"TDBG_STOP\n" in sent
    assert b"TDBG_LOAD 30 3 0\n" in sent
    assert core.tdbg_pack_events(events) in sent
    print("T1: load happy path (drain → header → blob → CRC): OK")


def test_load_with_stale_chatter():
    log, entries = collecting_log()
    mcu = TdbgMcu(stale_chatter=b"TDBG_PLAY_STARTED\r\nTDBG_PLAY_DONE\r\n")
    sess = core.TdbgSession(log, ser=mcu, lock=threading.Lock())
    assert sess.load(30, 1, [(100, 0)]) is True, \
        "stale fire-and-forget chatter must not break the LOAD handshake"
    print("T2: load with stale playback chatter drained: OK")


def test_load_bad_crc():
    log, entries = collecting_log()
    mcu = TdbgMcu(bad_crc=True)
    sess = core.TdbgSession(log, ser=mcu, lock=threading.Lock())
    assert sess.load(30, 0, [(100, 1)]) is False
    assert any("CRC mismatch" in m for _, m in entries)
    print("T3: load CRC mismatch fails cleanly: OK")


def test_play_fire_and_forget():
    log, entries = collecting_log()
    mcu = TdbgMcu()
    sess = core.TdbgSession(log, ser=mcu, lock=threading.Lock())
    assert sess.play(1) is True
    assert mcu.written[-1] == b"TDBG_PLAY\n"
    assert sess.play(5) is True
    assert mcu.written[-1] == b"TDBG_PLAY_LOOP 5\n"
    assert sess.play(0) is False, "iterations=0 must be rejected"
    print("T4: play fire-and-forget wire format + infinite rejected: OK")


def test_calibration_pattern():
    initial, events = core.tdbg_calibration_pattern()
    assert initial in (0, 1)
    assert 0 < len(events) <= core.TDBG_MAX_EVENTS
    # events[0] is the delta-0 timing anchor (documented no-op transition);
    # every real delta must sit above the ~60-cycle TC engine floor.
    assert all(d >= 60 for d, _ in events[1:]), \
        "calibration deltas must sit above the ~60-cycle TC engine floor"
    log, entries = collecting_log()
    mcu = TdbgMcu()
    sess = core.TdbgSession(log, ser=mcu, lock=threading.Lock())
    assert sess.load(30, initial, events) is True
    print(f"T5: calibration pattern ({len(events)} events) loads: OK")


def test_local_validation():
    log, entries = collecting_log()
    mcu = TdbgMcu()
    sess = core.TdbgSession(log, ser=mcu, lock=None)
    assert sess.load(99, 0, [(100, 1)]) is False
    assert sess.load(30, 2, [(100, 1)]) is False
    assert sess.load(30, 0, []) is False
    assert sess.load(30, 0, [(1, 1)] * (core.TDBG_MAX_EVENTS + 1)) is False
    assert mcu.written == [], "invalid args must never hit the wire"
    print("T6: local validation blocks bad args before the wire: OK")


if __name__ == "__main__":
    test_load_happy()
    test_load_with_stale_chatter()
    test_load_bad_crc()
    test_play_fire_and_forget()
    test_calibration_pattern()
    test_local_validation()
    print("PASS test_tdbg_session")
