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


def test_gpio_read_quiet():
    """quiet=True suppresses the routine send/recv logs (auto-read sweep);
    errors still log. quiet=False keeps full logging (manual reads)."""
    log, entries = collecting_log()
    ser = FakeSerial(script={"GPIO_READ": b"GPIO_VALUE 7 1\r\n"})
    sess = core.GpioSession(log, ser=ser, lock=None)

    assert sess.read_pin(7, quiet=True) == "HIGH"
    assert entries == [], f"quiet read must not log: {entries}"

    assert sess.read_pin(7) == "HIGH"
    assert any("send:" in m for _, m in entries), "verbose read must log send"
    assert any("recv:" in m for _, m in entries), "verbose read must log recv"

    entries.clear()
    ser.script = {"GPIO_READ": b"BANANA\r\n"}
    assert sess.read_pin(7, quiet=True) is None
    assert any(lvl == "err" for lvl, _ in entries), \
        "errors must still log even when quiet"
    print("C17: read_pin quiet suppresses routine logs, keeps errors: OK")


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
    tests break specific steps. `chip_blocks` (list of 32 CRC32s) simulates
    the new ARDUINO_VERIFY_BLOCKS report on a CRC mismatch; None models an
    old sketch that never sends it."""

    def __init__(self, refuse_erase=False, bad_crc=False, chip_blocks=None):
        super().__init__(preload=b"ARDUINO_ERASE_READY\r\n")
        self.refuse_erase = refuse_erase
        self.bad_crc = bad_crc
        self.chip_blocks = chip_blocks
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
            if self.bad_crc:
                if self.chip_blocks is not None:
                    line = "ARDUINO_VERIFY_BLOCKS " + " ".join(
                        f"{c:08X}" for c in self.chip_blocks)
                    self.buf += line.encode() + b"\r\n"
                self.buf += b"ARDUINO_ERROR\r\n"
            else:
                self.buf += b"ARDUINO_VERIFY_OK\r\n"
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


DEFAULT_FILE = b"\xA5" * 1000
# A 108 KB image whose every 4 KB block differs (block index in every byte) —
# needed by the aliasing test: content must extend past 64 KB with distinct
# blocks, or the "aliased" chip degenerates to all-identical-FF and the
# blank-chip guard correctly fires instead.
VARIED_108K = b"".join(bytes([i]) * core.CHUNK_SIZE for i in range(27))


def _padded(content):
    return content + b"\xFF" * (core.FILE_SIZE_SUPPORT - len(content))


def _with_flash_mcu(mcu, log, content=DEFAULT_FILE):
    import serial as _serial
    orig = _serial.Serial
    _serial.Serial = lambda *a, **kw: mcu
    try:
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(content)
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


def test_flash_progress_callback():
    """on_progress fires once per ACKed chunk with (done, total), monotonic
    up to (32, 32); a raising callback must not abort the flash."""
    import serial as _serial
    import tempfile as _tf
    log, entries = collecting_log()
    calls = []

    def on_progress(done, total):
        calls.append((done, total))
        if done == 5:
            raise RuntimeError("progress display broke")   # must be swallowed

    mcu = FlashMcu()
    orig = _serial.Serial
    _serial.Serial = lambda *a, **kw: mcu
    try:
        with _tf.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(b"\xA5" * 1000)
            fw = f.name
        try:
            ok = core.program_firmware(fw, log, port="COM_T",
                                       handshake_timeout_s=3.0,
                                       on_progress=on_progress)
        finally:
            os.unlink(fw)
    finally:
        _serial.Serial = orig
    assert ok is True
    assert len(calls) == 32, f"expected 32 progress calls, got {len(calls)}"
    assert calls[0] == (1, 32) and calls[-1] == (32, 32)
    assert [c[0] for c in calls] == list(range(1, 33)), "must be monotonic"
    print("C18: on_progress fires 32x, raising callback swallowed: OK")


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
    # Old sketch (no blocks line) → the fallback hint must appear.
    assert any("舊版 .ino" in m for _, m in entries), \
        [m for _, m in entries][-3:]
    print("C12: flash CRC mismatch (old sketch) fails with hint: OK")


def _host_block_crcs(content=DEFAULT_FILE):
    """Per-4KB CRCs of the padded image _with_flash_mcu programs."""
    import zlib
    padded = _padded(content)
    return [
        zlib.crc32(padded[i * core.CHUNK_SIZE:(i + 1) * core.CHUNK_SIZE])
        & 0xFFFFFFFF
        for i in range(core.FILE_SIZE_SUPPORT // core.CHUNK_SIZE)
    ]


def test_flash_verify_blocks_localised():
    # One corrupted block (block 5) → the diff names it and the diagnosis
    # points at transfer corruption.
    log, entries = collecting_log()
    chip = _host_block_crcs()
    chip[5] ^= 0xDEADBEEF
    ok = _with_flash_mcu(FlashMcu(bad_crc=True, chip_blocks=chip), log)
    assert ok is False
    msgs = [m for _, m in entries]
    assert any("1/32" in m and "區塊5" in m.replace(" ", "") for m in msgs), \
        msgs[-4:]
    assert any("傳輸中損毀" in m for m in msgs), msgs[-4:]
    print("C13: single bad block localised + transit diagnosis: OK")


def test_flash_verify_blocks_aliasing():
    # A16 stuck: chip content repeats with a 16-block (64 KB) period —
    # both halves hold the SECOND half's data. Needs an image with varied
    # content past 64 KB (VARIED_108K) so the aliased chip is NOT all-
    # identical. Diagnosis must implicate A16 / Due D24.
    log, entries = collecting_log()
    host = _host_block_crcs(VARIED_108K)
    chip = host[16:] + host[16:]
    ok = _with_flash_mcu(FlashMcu(bad_crc=True, chip_blocks=chip), log,
                         content=VARIED_108K)
    assert ok is False
    msgs = [m for _, m in entries]
    assert any("A16" in m and "D24" in m for m in msgs), msgs[-4:]
    print("C14: 64KB-period aliasing pattern → A16/D24 diagnosis: OK")


def test_flash_verify_blocks_blank_chip():
    # A chip that never got programmed (WE fault → still all 0xFF) has 32
    # IDENTICAL block CRCs. That trivially satisfies every period stride, so
    # without the all-identical guard this would misdiagnose as "A16 stuck".
    # It must be diagnosed as a write-path problem instead.
    import zlib
    log, entries = collecting_log()
    blank = zlib.crc32(b"\xFF" * core.CHUNK_SIZE) & 0xFFFFFFFF
    chip = [blank] * 32
    ok = _with_flash_mcu(FlashMcu(bad_crc=True, chip_blocks=chip), log)
    assert ok is False
    msgs = [m for _, m in entries]
    assert any("從未寫入" in m and "WE" in m for m in msgs), msgs[-4:]
    assert not any("A16" in m for m in msgs), \
        f"blank chip must NOT be misdiagnosed as A16: {msgs[-4:]}"
    print("C16: blank chip → write-path diagnosis, not A16 misdiagnosis: OK")


def test_flash_padding_is_ff():
    # The padded tail must be 0xFF (leave-erased), not 0x00 — chunk 1
    # carries the 0xA5 payload + the first padding bytes.
    log, entries = collecting_log()
    mcu = FlashMcu()
    ok = _with_flash_mcu(mcu, log)
    assert ok is True
    first_chunk = next(w for w in mcu.written if len(w) == core.CHUNK_SIZE)
    assert first_chunk[:1000] == b"\xA5" * 1000
    assert first_chunk[1000:] == b"\xFF" * (core.CHUNK_SIZE - 1000), \
        "padding must be 0xFF"
    print("C15: padding bytes are 0xFF: OK")


if __name__ == "__main__":
    test_gpio_set()
    test_gpio_read()
    test_gpio_read_quiet()
    test_flash_progress_callback()
    test_tdbg_preset()
    test_tdbg_pack()
    test_record_parse()
    test_flash_happy()
    test_flash_refusal()
    test_flash_bad_crc()
    test_flash_verify_blocks_localised()
    test_flash_verify_blocks_aliasing()
    test_flash_verify_blocks_blank_chip()
    test_flash_padding_is_ff()
    print("PASS test_core_protocol")
