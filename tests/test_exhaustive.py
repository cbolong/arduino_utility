"""Exhaustive / combinatorial verification.

Where a function's input space is genuinely bounded, enumerate ALL of it
rather than sampling representative values. Ordered boundary → equivalence
class → state transition → conditional branch, per the review's plan.

Most of these PIN DOWN ALREADY-CORRECT BEHAVIOUR (regression protection);
E6/E7/E8 additionally verify this round's fixes.

  E1  set_pin mode x value                     — 12 combinations, complete
  E2  parse_sgpio_frame bits_per_drive x order — 16 combinations, complete
  E3  SgpioFraming.validate_config boundaries  — every param at min-1/min/max/max+1
  E4  parse_record_blob cardinality x pin count
  E5  TdbgSession.play iterations branch boundary
  E6  pin-range guard across all five entry points  (verifies T3)
  E7  capture-lock state transitions                (verifies T1)
  E8  connection-indicator contrast ratios          (verifies T2)
"""
from _helpers import FakeSerial, collecting_log, drain  # noqa: F401

import itertools
import struct
import threading

import binFileTransfer_core as core
from binFileTransfer_core import SgpioFraming, parse_sgpio_frame


# ---------------------------------------------------------------- E1
def test_set_pin_matrix():
    """All 12 (mode, value) combinations. Valid modes are OUTPUT/INPUT;
    valid values are HIGH/LOW/None. The host forwards `value` whenever it is
    not None regardless of mode — the MCU ignores it for INPUT. That
    asymmetry is deliberate; pin it so nobody 'fixes' one side alone."""
    modes = ["OUTPUT", "INPUT", "WEIRD"]
    values = ["HIGH", "LOW", None, "MAYBE"]
    checked = 0
    for mode, value in itertools.product(modes, values):
        log, entries = collecting_log()
        ser = FakeSerial(script={"GPIO_SET": b"GPIO_OK\r\n"})
        sess = core.GpioSession(log, ser=ser, lock=None)
        ok = sess.set_pin(7, mode, value)
        valid = mode in ("OUTPUT", "INPUT") and value in ("HIGH", "LOW", None)
        assert ok is valid, f"mode={mode} value={value} -> {ok}, expected {valid}"
        if valid:
            expect = f"GPIO_SET 7 {mode}" + (f" {value}" if value else "") + "\n"
            assert ser.written[-1] == expect.encode(), \
                f"mode={mode} value={value} wire={ser.written[-1]!r}"
        else:
            assert ser.written == [], \
                f"invalid mode={mode} value={value} must not reach the wire"
        checked += 1
    assert checked == 12
    print(f"E1: set_pin — all {checked} mode x value combinations: OK")


# ---------------------------------------------------------------- E2
def test_parse_frame_matrix():
    """bits_per_drive 1..8 x msb_first {True, False} = 16 combinations.
    Verifies the value arithmetic in both bit orders and the rule that named
    Activity/Locate/Fault fields appear only at exactly 3 bits."""
    checked = 0
    for w, msb in itertools.product(range(1, 9), (True, False)):
        f = SgpioFraming(num_drives=1, bits_per_drive=w, msb_first=msb)
        bits = "1" + "0" * (w - 1)          # leading 1, rest 0
        d = parse_sgpio_frame(bits, f)[0]
        expected = (1 << (w - 1)) if msb else 1
        assert d["value"] == expected, \
            f"w={w} msb={msb}: value={d['value']}, expected {expected}"
        assert d["bits"] == bits
        has_named = "activity" in d
        assert has_named is (w == 3), \
            f"w={w}: named fields present={has_named}, expected {w == 3}"
        if w == 3:
            # ordered = MSB..LSB; "100" msb-first -> activity set.
            assert d["activity"] is msb
            assert d["fault"] is (not msb)
        checked += 1
    assert checked == 16
    print(f"E2: parse_sgpio_frame — all {checked} width x order combos: OK")


# ---------------------------------------------------------------- E3
def test_validate_config_boundaries():
    """Every bounded parameter at min-1 / min / max / max+1, plus the
    frame_len ceiling that mirrors the firmware buffer (added this round)."""
    for nd, ok in ((0, False), (1, True), (64, True), (65, False)):
        got = SgpioFraming(num_drives=nd, bits_per_drive=1).validate_config()
        assert (got is None) is ok, f"num_drives={nd}: {got}"
    for bw, ok in ((0, False), (1, True), (8, True), (9, False)):
        got = SgpioFraming(num_drives=1, bits_per_drive=bw).validate_config()
        assert (got is None) is ok, f"bits_per_drive={bw}: {got}"
    assert SgpioFraming(header_bits=-1).validate_config() is not None
    assert SgpioFraming(header_bits=0).validate_config() is None

    # frame_len ceiling: exactly at the firmware limit passes, one over fails.
    cap = core.SGPIO_MAX_FRAME_BITS
    at = SgpioFraming(num_drives=cap // 4, bits_per_drive=4, header_bits=0)
    assert at.frame_len == cap and at.validate_config() is None
    over = SgpioFraming(num_drives=cap // 4, bits_per_drive=4, header_bits=1)
    assert over.frame_len == cap + 1
    assert "exceeds firmware limit" in (over.validate_config() or "")
    # The combination that motivated the fix: accepted before, rejected now.
    assert SgpioFraming(num_drives=64, bits_per_drive=8).validate_config()
    print(f"E3: validate_config — all boundaries incl. frame_len<={cap}: OK")


# ---------------------------------------------------------------- E4
def test_record_blob_cardinality():
    """Event count 0 / 1 / many x pin count 1..4, checking that mask bit i
    maps to pins[i] (list position, NOT pin number) at every width."""
    assert core.parse_record_blob(b"", [22]) == []
    one = struct.pack("<IB", 5, 0b1)
    assert core.parse_record_blob(one, [22]) == [(5, {22: True})]

    for n in range(1, 5):
        pins = [10 + i for i in range(n)]
        for bit in range(n):                     # each position, one at a time
            blob = struct.pack("<IB", 7, 1 << bit)
            (delta, states), = core.parse_record_blob(blob, pins)
            assert delta == 7
            for i, p in enumerate(pins):
                assert states[p] is (i == bit), \
                    f"n={n} bit={bit}: pin {p} -> {states[p]}"
    # Ragged input is rejected, not silently truncated.
    for bad in (b"\x00", b"\x00" * 4, b"\x00" * 6):
        try:
            core.parse_record_blob(bad, [22])
            raise AssertionError(f"len {len(bad)} should raise")
        except ValueError:
            pass
    print("E4: parse_record_blob — cardinality x pin-count mapping: OK")


# ---------------------------------------------------------------- E5
def test_play_iteration_boundary():
    """iterations branch: <1 rejected, ==1 sends TDBG_PLAY, >=2 sends
    TDBG_PLAY_LOOP n. 1 and 2 straddle the branch."""
    log, _ = collecting_log()
    ser = FakeSerial()
    sess = core.TdbgSession(log, ser=ser, lock=threading.Lock())
    for n, expect in ((-1, None), (0, None), (1, b"TDBG_PLAY\n"),
                      (2, b"TDBG_PLAY_LOOP 2\n"), (5, b"TDBG_PLAY_LOOP 5\n")):
        before = len(ser.written)
        ok = sess.play(n)
        assert ok is (expect is not None), f"play({n}) -> {ok}"
        if expect is None:
            assert len(ser.written) == before, f"play({n}) must not write"
        else:
            assert ser.written[-1] == expect, f"play({n}) wrote {ser.written[-1]!r}"
    print("E5: play — iterations branch boundary (1 vs 2): OK")


# ---------------------------------------------------------------- E6
def test_pin_range_all_entry_points():
    """Every session entry point that names a pin x {-1, 0, 65, 66}.
    0 and 65 are the inclusive bounds; -1 and 66 are just outside. Before
    this round GpioSession had no check at all, so an out-of-range pin
    reached the MCU's pinMode()."""
    cases = [(-1, False), (0, True), (65, True), (66, False)]
    checked = 0
    for pin, in_range in cases:
        log, _ = collecting_log()
        lock = threading.Lock()

        gs_ser = FakeSerial(script={"GPIO_SET": b"GPIO_OK\r\n"})
        g2 = core.GpioSession(log, ser=gs_ser, lock=lock)
        assert g2.set_pin(pin, "INPUT") is in_range
        assert (gs_ser.written != []) is in_range, \
            f"set_pin({pin}) reached the wire: {in_range=}"

        gr_ser = FakeSerial(script={"GPIO_READ": b"GPIO_VALUE 0 1\r\n"})
        g3 = core.GpioSession(log, ser=gr_ser, lock=lock)
        g3.read_pin(pin)
        assert (gr_ser.written != []) is in_range, f"read_pin({pin}) wire"

        # load(): an out-of-range pin must be rejected before any wire
        # traffic. (In range it still returns False here — the fake never
        # answers TDBG_READY — so only the wire tells us the guard ran.)
        t_ser = FakeSerial()
        t = core.TdbgSession(log, ser=t_ser, lock=lock)
        t.load(pin, 0, [(100, 1)])
        assert (t_ser.written != []) is in_range, f"load({pin}) wire"

        p_ser = FakeSerial(script={"TDBG_PRESET": b"TDBG_PRESET_OK 1\r\n"})
        t2 = core.TdbgSession(log, ser=p_ser, lock=lock)
        assert t2.send_preset(1, pin) is in_range
        assert (p_ser.written != []) is in_range, f"send_preset({pin}) wire"

        r_ser = FakeSerial(script={"RECORD_START": b"RECORD_STARTED\r\n"})
        r = core.RecordSession(log, ser=r_ser, lock=lock)
        started = r.start([pin])
        assert started is in_range, f"RECORD start({pin}) -> {started}"
        if started:
            r._live_stop.set()
            if r._live_thread:
                r._live_thread.join(timeout=1.0)

        s_ser = FakeSerial(script={"SGPIO_START": b"SGPIO_STARTED\r\n"})
        s = core.SgpioSession(log, ser=s_ser, lock=lock)
        # Use three distinct in-range pins except the one under test.
        others = [p for p in (2, 3, 4) if p != pin][:2]
        started = s.start(pin, others[0], others[1], SgpioFraming())
        assert started is in_range, f"SGPIO start({pin}) -> {started}"
        if started:
            s._live_stop.set()
            if s._live_thread:
                s._live_thread.join(timeout=1.0)
        checked += 1
    assert checked == 4
    print(f"E6: pin range — 5 entry points x {checked} boundary values: OK")


# ---------------------------------------------------------------- E7
def test_capture_lock_state_transitions():
    """A page built DURING a capture must inherit the lock; after the capture
    ends a newly built page must be live again. Covers both capture origins
    (RECORD and SGPIO) x the pages that can be built late."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import binFileTransferGui as gui
    import time

    app = QApplication.instance() or QApplication([])
    fake = FakeSerial()
    core.open_due_link = lambda port, log: fake
    gui.open_due_link = lambda port, log: fake

    for origin_idx, origin_name in ((3, "RECORD"), (4, "SGPIO")):
        win = gui.MainWindow()
        win._on_connect()
        deadline = time.time() + 3.0
        while time.time() < deadline and not win._connected:
            app.processEvents(); time.sleep(0.005)
        assert win._connected

        origin = win._ensure_page(origin_idx)
        win.set_recording(True, origin)
        assert win._capture_page is origin

        # GPIO (1) and TDBG (2) are still unbuilt — build them mid-capture.
        gp = win._ensure_page(1)
        assert not gp._read_all.isEnabled(), \
            f"{origin_name}: late GPIO page live during capture"
        assert not gp._grid.rows[0]._mode_btn.isEnabled()
        tp = win._ensure_page(2)
        assert not tp._btns[0].isEnabled(), \
            f"{origin_name}: late TDBG page live during capture"

        # End the capture: a page built now must be live.
        win.set_recording(False, origin)
        assert win._capture_page is None
        other = win._ensure_page(4 if origin_idx == 3 else 3)
        live = (other._c_sclk.isEnabled() if origin_idx == 3
                else other._rows[0].combo.isEnabled())
        assert live, f"{origin_name}: page built after capture stayed locked"

        # Disconnect clears the lock even if a capture was left marked.
        win.set_recording(True, origin)
        win._on_disconnect_done(None)
        assert win._capture_page is None, "disconnect must clear capture lock"
        win.close(); app.processEvents()
    print("E7: capture-lock inherited by late-built pages, both origins: OK")


# ---------------------------------------------------------------- E8
def _contrast(fg: str, bg: str) -> float:
    def lum(h):
        h = h.lstrip("#")
        c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
             for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def test_indicator_contrast():
    """Every connection-indicator state must clear WCAG AA (4.5:1) against
    the dark sidebar, and the disabled accent button against its own fill.
    Computed, not eyeballed — all three states failed before this round."""
    import gui_theme as T
    import binFileTransferGui as gui

    bg = T.PALETTE["sidebar_bg"]
    for level, key in gui.MainWindow._CONN_COLORS.items():
        ratio = _contrast(T.PALETTE[key], bg)
        assert ratio >= 4.5, f"conn '{level}' ({key}) = {ratio:.2f}:1 on {bg}"
    # The *_dark group must NOT be used here — prove it would have failed,
    # so a future edit that reverts to it trips this test.
    for key in ("success_dark", "danger_dark", "warning_dark"):
        assert _contrast(T.PALETTE[key], bg) < 4.5, \
            f"{key} unexpectedly passes; the guard below is now meaningless"

    qss = T.build_qss()
    assert "QPushButton#accent:disabled" in qss
    disabled_fill = T.PALETTE["text_secondary"]
    assert f"background: {disabled_fill}" in qss, \
        "disabled accent must use the higher-contrast fill"
    assert _contrast("#ffffff", disabled_fill) >= 4.5

    # Check the ACTUAL widget, not just the mapping table: the indicator was
    # invisible on a fresh launch because nothing called set_connection() at
    # startup and the QSS carried no colour — a table-only assertion passed
    # while the screen stayed broken.
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(qss)
    win = gui.MainWindow()
    sheet = win.sidebar._conn.styleSheet()
    assert "color:" in sheet, f"indicator unstyled at startup: {sheet!r}"
    startup_colour = sheet.split("color:")[1].strip().rstrip(";").strip()
    assert _contrast(startup_colour, bg) >= 4.5, \
        f"startup indicator {startup_colour} = " \
        f"{_contrast(startup_colour, bg):.2f}:1 on the sidebar"
    # And it must still be readable in every state the app can reach.
    for text, level in (("已連線", "ok"), ("連線中…", "warn"),
                        ("燒錄中…", "warn"), ("未連線", "err")):
        win._set_conn(text, level)
        s = win.sidebar._conn.styleSheet()
        col = s.split("color:")[1].strip().rstrip(";").strip()
        assert _contrast(col, bg) >= 4.5, f"'{text}' -> {col} too low"
    win.close(); app.processEvents()
    print("E8: indicator contrast >= 4.5:1 at startup and in every state: OK")


if __name__ == "__main__":
    test_set_pin_matrix()
    test_parse_frame_matrix()
    test_validate_config_boundaries()
    test_record_blob_cardinality()
    test_play_iteration_boundary()
    test_pin_range_all_entry_points()
    test_capture_lock_state_transitions()
    test_indicator_contrast()
    print("PASS test_exhaustive")
