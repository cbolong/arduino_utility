"""SGPIO passive-decode tests — the "is the data right?" core, offline.

  G1  parse a standard 4-drive/3-bit frame → correct Activity/Locate/Fault
  G2  frame length mismatch → sgpio_frame_valid False + parse raises
  G3  non-binary chars rejected
  G4  header_bits skipped; drive fields land correctly after the header
  G5  msb_first vs lsb_first flips field significance
  G6  bits_per_drive != 3 → generic value, no named fields
  G7  framing.validate_config catches nonsensical configs
  G8  SgpioSession start → frame stream → stop full round trip (fake serial)
  G9  SgpioSession rejects duplicate / out-of-range pins and bad framing
"""
from _helpers import FakeSerial, collecting_log, drain  # noqa: F401

import threading
import time

import binFileTransfer_core as core
from binFileTransfer_core import SgpioFraming, parse_sgpio_frame, sgpio_frame_valid


def test_parse_standard():
    f = SgpioFraming(num_drives=4, bits_per_drive=3)
    # drive0=100 (Activity), d1=010 (Locate), d2=001 (Fault), d3=111 (all)
    bits = "100" "010" "001" "111"
    assert sgpio_frame_valid(bits, f)
    drives = parse_sgpio_frame(bits, f)
    assert len(drives) == 4
    assert drives[0] == {"drive": 0, "bits": "100", "value": 4,
                         "activity": True, "locate": False, "fault": False}
    assert drives[1]["locate"] and not drives[1]["activity"]
    assert drives[2]["fault"] and drives[2]["value"] == 1
    assert all(drives[3][k] for k in ("activity", "locate", "fault"))
    print("G1: standard 4-drive/3-bit decode: OK")


def test_length_mismatch():
    f = SgpioFraming(num_drives=4, bits_per_drive=3)   # expects 12 bits
    assert not sgpio_frame_valid("10001000", f)         # 8 bits
    try:
        parse_sgpio_frame("10001000", f)
        raise AssertionError("should raise")
    except ValueError:
        pass
    print("G2: length mismatch → invalid + parse raises: OK")


def test_non_binary():
    f = SgpioFraming(num_drives=1, bits_per_drive=3)
    assert not sgpio_frame_valid("1X0", f)
    print("G3: non-binary chars rejected: OK")


def test_header_bits():
    f = SgpioFraming(num_drives=2, bits_per_drive=3, header_bits=4)
    # 4 header bits then two drives; header must not bleed into drive 0.
    bits = "1010" "111" "000"
    d = parse_sgpio_frame(bits, f)
    assert d[0]["bits"] == "111" and d[1]["bits"] == "000"
    print("G4: header_bits skipped correctly: OK")


def test_bit_order():
    f_msb = SgpioFraming(num_drives=1, bits_per_drive=3, msb_first=True)
    f_lsb = SgpioFraming(num_drives=1, bits_per_drive=3, msb_first=False)
    # "100": msb_first → value 4, activity(MSB)=1; lsb_first → reversed "001"
    # → value 1, activity(now MSB after reverse)=0, fault=1.
    assert parse_sgpio_frame("100", f_msb)[0]["value"] == 4
    assert parse_sgpio_frame("100", f_msb)[0]["activity"] is True
    lsb = parse_sgpio_frame("100", f_lsb)[0]
    assert lsb["value"] == 1 and lsb["activity"] is False and lsb["fault"] is True
    print("G5: msb_first vs lsb_first flips significance: OK")


def test_non_three_bits():
    f = SgpioFraming(num_drives=2, bits_per_drive=2)
    d = parse_sgpio_frame("10" "11", f)
    assert d[0]["value"] == 2 and "activity" not in d[0]
    assert d[1]["value"] == 3
    print("G6: bits_per_drive != 3 → value only, no named fields: OK")


def test_validate_config():
    assert SgpioFraming(num_drives=4).validate_config() is None
    assert SgpioFraming(num_drives=0).validate_config() is not None
    assert SgpioFraming(num_drives=999).validate_config() is not None
    assert SgpioFraming(bits_per_drive=0).validate_config() is not None
    assert SgpioFraming(bits_per_drive=9).validate_config() is not None
    print("G7: framing.validate_config catches bad configs: OK")


class SgpioMcu(FakeSerial):
    """Emits SGPIO_STARTED on start; a scripted list of frames on demand;
    SGPIO_STOPPED on stop."""

    def __init__(self, frames):
        super().__init__()
        self.frames = list(frames)

    def write(self, data):
        self.written.append(bytes(data))
        text = data.decode(errors="ignore").strip()
        if text.startswith("SGPIO_START"):
            self.buf += b"SGPIO_STARTED\r\n"
            for fr in self.frames:
                self.buf += f"SGPIO_FRAME {fr}\r\n".encode()
        elif text == "SGPIO_STOP":
            self.buf += b"SGPIO_STOPPED\r\n"


def test_session_round_trip():
    log, entries = collecting_log()
    f = SgpioFraming(num_drives=2, bits_per_drive=3)
    mcu = SgpioMcu(["100010", "010001"])
    sess = core.SgpioSession(log, ser=mcu, lock=threading.Lock())
    got = []
    ok = sess.start(2, 3, 4, f, on_frame=lambda b: got.append(b))
    assert ok, [m for _, m in entries][-3:]
    # Let the live thread consume the queued frames.
    deadline = time.time() + 2.0
    while time.time() < deadline and len(got) < 2:
        time.sleep(0.02)
    assert got == ["100010", "010001"], got
    # Decode the first frame end-to-end.
    drives = parse_sgpio_frame(got[0], f)
    assert drives[0]["activity"] and drives[1]["locate"]
    assert sess.stop() is True
    # Command wire format sanity.
    assert any(w.startswith(b"SGPIO_START 2 3 4 ") for w in mcu.written)
    print("G8: SgpioSession start → frames → stop round trip: OK")


def test_session_validation():
    log, entries = collecting_log()
    f = SgpioFraming()
    sess = core.SgpioSession(log, ser=SgpioMcu([]), lock=None)
    assert sess.start(5, 5, 6, f) is False, "duplicate pins must be rejected"
    assert sess.start(2, 3, 99, f) is False, "out-of-range pin rejected"
    assert sess.start(2, 3, 4, SgpioFraming(num_drives=0)) is False, \
        "bad framing rejected"
    print("G9: SgpioSession rejects bad pins/framing: OK")


def test_group_bits_display():
    """Display grouping: bits split per drive, header split off with '|'."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from gui_pages import SgpioPage

    f = SgpioFraming(num_drives=4, bits_per_drive=3)
    assert SgpioPage._group_bits("100010001111", f) == "100 010 001 111"
    fh = SgpioFraming(num_drives=2, bits_per_drive=3, header_bits=4)
    assert SgpioPage._group_bits("1010111000", fh) == "1010 | 111 000"
    print("G11: frame display grouped per drive (+header |): OK")


def test_gui_tab():
    """The SGPIO sidebar tab exists (5th), builds, connects, decodes a live
    frame into the drive table, flags a bad frame, and stops cleanly."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import binFileTransfer_core as core2
    import binFileTransferGui as gui

    app = QApplication.instance() or QApplication([])
    win = gui.MainWindow()
    assert gui.PAGE_NAMES[4] == "SGPIO", gui.PAGE_NAMES
    page = win._ensure_page(4)
    assert page is not None and page.__class__.__name__ == "SgpioPage"

    fake_ser = FakeSerial()
    core.open_due_link = lambda port, log: fake_ser
    gui.open_due_link = lambda port, log: fake_ser
    win._on_connect()
    assert drain(app, lambda: win._connected), "connect failed"
    assert win.sgpio_session is not None

    # Pick 3 distinct pins + framing (2 drives, 3 bits).
    page._c_sclk.setCurrentIndex(3)   # D2
    page._c_sload.setCurrentIndex(4)  # D3
    page._c_sdata.setCurrentIndex(5)  # D4
    page._sp_drives.setValue(2)
    page._sp_bits.setValue(3)
    app.processEvents()
    assert page._start.isEnabled(), "start should enable with 3 distinct pins"

    # Patch the session start to just accept + hand back on_frame; drive a
    # frame through frameSig and check the drive dots update.
    captured = {}

    def fake_start(sc, sl, sd, framing, on_frame=None, **kw):
        captured["framing"] = framing
        captured["on_frame"] = on_frame
        return True

    win.sgpio_session.start = fake_start
    win.sgpio_session.stop = lambda: True
    page._on_start()
    assert page._active, "page didn't enter active (sync state)"
    # fake_start runs on the worker thread — wait for it to capture on_frame.
    assert drain(app, lambda: "on_frame" in captured), "start cmd never ran"
    assert captured["framing"].num_drives == 2

    # A valid frame: drive0 activity, drive1 fault.
    captured["on_frame"]("100" "001")
    drain(app, lambda: page._drive_cells and
          "success" in page._drive_cells[0][0].styleSheet() or True, timeout=0.5)
    app.processEvents()
    # drive0 activity dot green, drive1 fault dot green.
    assert T.PALETTE["success"] in page._drive_cells[0][0].styleSheet()
    assert T.PALETTE["success"] in page._drive_cells[1][2].styleSheet()
    assert page._frame_count == 1 and "1" in page._count_lbl.text()
    # Raw display is grouped per drive.
    assert "100 001" in page._raw_lbl.text()

    # Heartbeat re-send of the SAME frame: counter ticks (liveness) but the
    # table is NOT re-parsed/re-styled.
    import gui_pages as _gp
    parse_calls = []
    orig_parse = _gp.parse_sgpio_frame
    _gp.parse_sgpio_frame = (
        lambda b, fr: (parse_calls.append(1), orig_parse(b, fr))[1])
    try:
        page._apply_frame("100" "001")     # identical → skip
        assert page._frame_count == 2
        assert parse_calls == [], "identical heartbeat must skip re-parse"
        page._apply_frame("110" "001")     # changed → full decode
        assert parse_calls == [1]
    finally:
        _gp.parse_sgpio_frame = orig_parse

    # A bad-length frame flags red, does not crash.
    page._apply_frame("10101")   # 5 bits != expected 6
    assert T.PALETTE["danger"] in page._raw_lbl.styleSheet()

    page._on_stop()
    assert drain(app, lambda: not page._active), "page didn't leave active"
    win.close()
    app.processEvents()
    print("G10: SGPIO GUI tab — 5th tab, live decode, bad-frame flag, stop: OK")


import gui_theme as T  # noqa: E402  (used by test_gui_tab)


if __name__ == "__main__":
    test_parse_standard()
    test_length_mismatch()
    test_non_binary()
    test_header_bits()
    test_bit_order()
    test_non_three_bits()
    test_validate_config()
    test_session_round_trip()
    test_session_validation()
    test_group_bits_display()
    test_gui_tab()
    print("PASS test_sgpio")
