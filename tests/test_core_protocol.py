"""Protocol round-trip tests for binFileTransfer_core against a scripted
fake serial — no hardware, no Qt.

Covers:
  C1  GpioSession.set_pin OUTPUT HIGH / INPUT — GPIO_OK handling
  C2  GpioSession.set_pin rejects bad mode/value locally (no serial write)
  C3  GpioSession.read_pin parses GPIO_VALUE, returns HIGH/LOW, None on garbage
  C4  read_pin timeout returns None within GPIO_READ_TIMEOUT_S (not 5 s)
  C5  TdbgSession.send_preset ack / no-ack
  C6  tdbg_pack_events layout matches the documented '<IB' wire format
  C7  tdbg_crc16 self-test vector (also validated at import time)
  C8  parse_record_blob decodes mask bits per pin-list position
  C9  parse_record_blob rejects non-multiple-of-5 blobs
  C10 program_firmware full happy path against a scripted MCU
  C11 program_firmware chip-refusal path logs the actionable hint
  C12 program_firmware CRC mismatch fails cleanly
"""
from _helpers import FakeSerial, collecting_log  # noqa: F401

import os
import struct
import tempfile
import time

import binFileTransfer_core as core


def test_gpio_set():
    log, entries = collecting_log()
    ser = FakeSerial(script={"GPIO_SET": b"GPIO_OK\r\n"})
    sess = core.GpioSession(log, ser=ser, lock=None)
    assert sess.set_pin(7, "OUTPUT", "HIGH") is True
    assert ser.written[-1] == b"GPIO_SET 7 OUTPUT HIGH\n"
    assert sess.set_pin(7, "INPUT") is True
    assert ser.written[-1] == b"GPIO_SET 7 INPUT\n"
    print("C1: set_pin OUTPUT/INPUT round trip: OK")

    n_writes = len(ser.written)
    assert sess.set_pin(7, "WEIRD") is False
    assert sess.set_pin(7, "OUTPUT", "MAYBE") is False
    assert len(ser.written) == n_writes, "invalid args must not hit the wire"
    print("C2: local validation blocks bad mode/value: OK")


def test_gpio_read():
    log, entries = collecting_log()
    ser = FakeSerial(script={"GPIO_READ": b"GPIO_VALUE 7 1\r\n"})
    sess = core.GpioSession(log, ser=ser, lock=None)
    assert sess.read_pin(7) == "HIGH"
    ser.script = {"GPIO_READ": b"GPIO_VALUE 7 0\r\n"}
    assert sess.read_pin(7) == "LOW"
    ser.script = {"GPIO_READ": b"BANANA\r\n"}
    assert sess.read_pin(7) is None
    print("C3: read_pin parses GPIO_VALUE / rejects garbage: OK")

    ser.script = {}  # no reply at all
    t0 = time.time()
    assert sess.read_pin(7) is None
    dt = time.time() - t0
    assert dt < core.GPIO_READ_TIMEOUT_S + 0.5, f"timeout took {dt:.1f}s"
    assert dt >= core.GPIO_READ_TIMEOUT_S - 0.2, f"gave up too fast ({dt:.1f}s)"
    print(f"C4: read_pin no-reply times out in {dt:.2f}s (~{core.GPIO_READ_TIMEOUT_S}s): OK")


def test_tdbg_preset():
    log, entries = collecting_log()
    ser = FakeSerial(script={"TDBG_PRESET": b"TDBG_PRESET_OK 1\r\n"})
    sess = core.TdbgSession(log, ser=ser, lock=None)
    assert sess.send_preset(1, 30) is True
    assert ser.written[-1] == b"TDBG_PRESET 1 30\n"
    ser.script = {"TDBG_PRESET": b"TDBG_ERROR nope\r\n"}
    assert sess.send_preset(2, 30) is False
    print("C5: send_preset ack / nak: OK")


def test_tdbg_pack():
    events = [(100, 1), (65536, 0), (4294967295, 1)]
    blob = core.tdbg_pack_events(events)
    assert len(blob) == len(events) * 5
    for i, (delta, state) in enumerate(events):
        d, s = struct.unpack_from("<IB", blob, i * 5)
        assert (d, s) == (delta, state)
    print("C6: tdbg_pack_events wire format '<IB': OK")

    # CRC self-test vector: the module asserts this at import, re-check here
    # so a broken table fails with a named test, not an ImportError.
    assert core.tdbg_crc16(b"123456789") == 0x29B1
    print("C7: tdbg_crc16 CCITT-FALSE check vector 0x29B1: OK")


def test_record_parse():
    # Two pins: D22 -> bit 0, D44 -> bit 1 (list position, not pin number).
    blob = struct.pack("<IB", 0, 0b01) + struct.pack("<IB", 500, 0b10)
    events = core.parse_record_blob(blob, [22, 44])
    assert events[0] == (0, {22: True, 44: False})
    assert events[1] == (500, {22: False, 44: True})
    print("C8: parse_record_blob mask-bit → pin-position mapping: OK")

    try:
        core.parse_record_blob(b"\x00\x01\x02", [22])
        raise AssertionError("should have raised")
    except ValueError:
        pass
    print("C9: parse_record_blob rejects ragged blob: OK")


class FlashMcu(FakeSerial):
    """Scripted MCU for the full flash flow. Behaviour flags let individual
    tests break specific steps."""

    def __init__(self, refuse_erase=False, bad_crc=False):
        super().__init__(preload=b"ARDUINO_ERASE_READY\r\n")
        self.refuse_erase = refuse_erase
        self.bad_crc = bad_crc
        self.chunks = 0

    def write(self, data):
        self.written.append(bytes(data))
        text = data[:64].decode(errors="ignore").strip()
        if text.startswith("ARDUINO_ERASE_TRIGGER"):
            if self.refuse_erase:
                self.buf += b"ARDUINO_ERROR\r\n"
            else:
                self.buf += b"ARDUINO_READY_TO_RECEIVED_DATA\r\n"
        elif text.startswith("ARDUINO_VERIFY_REQUEST"):
            self.buf += (b"ARDUINO_ERROR\r\n" if self.bad_crc
                         else b"ARDUINO_VERIFY_OK\r\n")
        elif text.startswith("ARDUINO_TRANSFER_DONE_SIGNAL"):
            self.buf += b"ARDUINO_DATA_COMPLETED\r\n"
        elif len(data) == core.CHUNK_SIZE:
            self.chunks += 1
            self.buf += b"ARDUINO_RECEIVED_LINE_DONE\r\n"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def _with_flash_mcu(mcu, log):
    import serial as _serial
    orig = _serial.Serial
    _serial.Serial = lambda *a, **kw: mcu
    try:
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(b"\xA5" * 1000)
            fw = f.name
        try:
            return core.program_firmware(fw, log, port="COM_T",
                                         handshake_timeout_s=3.0)
        finally:
            os.unlink(fw)
    finally:
        _serial.Serial = orig


def test_flash_happy():
    log, entries = collecting_log()
    mcu = FlashMcu()
    ok = _with_flash_mcu(mcu, log)
    assert ok is True, [m for _, m in entries][-5:]
    assert mcu.chunks == core.FILE_SIZE_SUPPORT // core.CHUNK_SIZE, \
        f"expected 32 chunks, MCU saw {mcu.chunks}"
    print(f"C10: flash happy path, {mcu.chunks} chunks + CRC OK: OK")


def test_flash_refusal():
    log, entries = collecting_log()
    ok = _with_flash_mcu(FlashMcu(refuse_erase=True), log)
    assert ok is False
    assert any("未偵測到 Flash 晶片" in m for _, m in entries), \
        "actionable hint missing on refusal"
    print("C11: flash refusal logs actionable hint: OK")


def test_flash_bad_crc():
    log, entries = collecting_log()
    ok = _with_flash_mcu(FlashMcu(bad_crc=True), log)
    assert ok is False
    print("C12: flash CRC mismatch fails cleanly: OK")


if __name__ == "__main__":
    test_gpio_set()
    test_gpio_read()
    test_tdbg_preset()
    test_tdbg_pack()
    test_record_parse()
    test_flash_happy()
    test_flash_refusal()
    test_flash_bad_crc()
    print("PASS test_core_protocol")
