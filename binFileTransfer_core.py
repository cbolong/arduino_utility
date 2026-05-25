from __future__ import annotations

import contextlib
import os
import time
import zlib
from typing import Callable

import serial
import serial.tools.list_ports


# 500000 turned out unstable on the user's specific 16U2 firmware (output
# arrived garbled, host kept timing out waiting for ARDUINO_ERASE_READY),
# so we're back to the safe 115200. Must match UART_BAUDRATE in
# binFileProgram.ino exactly. See the note in the .ino for context.
BAUD = 115200
CHUNK_SIZE = 4096
FILE_SIZE_SUPPORT = 128 * 1024

TARGET_VID = 0x2341
TARGET_PID = 0x003D

DEFAULT_HANDSHAKE_TIMEOUT_S = 30.0

MCU_ERASE_READY = "ARDUINO_ERASE_READY"
MCU_ERASE_TRIGGER = "ARDUINO_ERASE_TRIGGER"
MCU_READY_TO_START = "ARDUINO_READY_TO_RECEIVED_DATA"
MCU_RECEIVED_LINE_RESPONSE = "ARDUINO_RECEIVED_LINE_DONE"
MCU_VERIFY_REQUEST = "ARDUINO_VERIFY_REQUEST"   # host sends "<sentinel> <hex>"
MCU_VERIFY_OK = "ARDUINO_VERIFY_OK"
MCU_TRANSFER_DONE_SIGNAL = "ARDUINO_TRANSFER_DONE_SIGNAL"
MCU_TRANSFER_COMPLETED = "ARDUINO_DATA_COMPLETED"
MCU_ERROR = "ARDUINO_ERROR"


LogCallback = Callable[[str, str], None]


def _find_arduino_port() -> str | None:
    for port in serial.tools.list_ports.comports():
        if port.vid == TARGET_VID and port.pid == TARGET_PID:
            return port.device
    return None


def _wait_for_line(
    ser: serial.Serial, expected: str, log: LogCallback, timeout_s: float
) -> bool:
    log(f"handshake wait     : {expected}", "wait")
    deadline = time.monotonic() + timeout_s
    while True:
        if ser.in_waiting > 0:
            line = ser.readline().decode(errors="ignore").strip()
            if line == expected:
                log(f"handshake received : {expected}", "ok")
                return True
            if line == MCU_ERROR:
                log("MCU ERROR!!", "err")
                return False
            if line:
                log(f"MCU: {line}", "info")
        if time.monotonic() > deadline:
            log(
                f"Timeout after {timeout_s:.1f}s waiting for: {expected}",
                "err",
            )
            return False
        time.sleep(0.01)


def program_firmware(
    firmware_path: str,
    log: LogCallback,
    *,
    port: str | None = None,
    handshake_timeout_s: float = DEFAULT_HANDSHAKE_TIMEOUT_S,
) -> bool:
    """Program the given firmware.bin to a connected Arduino-Due-driven SST39 flash.

    log(message, level) where level is one of: info, ok, warn, err, wait.
    If `port` is given, skip USB VID/PID auto-detection and use it directly.
    `handshake_timeout_s` bounds each wait for an MCU response so a stuck MCU
    no longer hangs the caller forever.
    Returns True on success, False on any failure. Never calls sys.exit / input.
    """
    if port is None:
        port = _find_arduino_port()
        if port is None:
            log("Arduino Device not found.", "err")
            return False
    log(f"Arduino Found : {port}.", "ok")

    if not os.path.isfile(firmware_path):
        log(f"File not found: {firmware_path}", "err")
        return False

    file_size = os.path.getsize(firmware_path)
    if file_size == 0:
        log(f"File is empty: {firmware_path}", "err")
        return False
    if file_size > FILE_SIZE_SUPPORT:
        log(
            f"File size too large ({file_size // 1024} KB). "
            f"Must be under {FILE_SIZE_SUPPORT // 1024} KB.",
            "err",
        )
        return False

    try:
        with serial.Serial(port, BAUD, timeout=0.1) as ser:
            if not _wait_for_line(ser, MCU_ERASE_READY, log, handshake_timeout_s):
                return False

            log(f"handshake send     : {MCU_ERASE_TRIGGER}", "info")
            ser.write(f"{MCU_ERASE_TRIGGER}\n".encode("UTF-8"))

            if not _wait_for_line(ser, MCU_READY_TO_START, log, handshake_timeout_s):
                return False

            with open(firmware_path, "rb") as f:
                file_data = f.read()

            padding_size = FILE_SIZE_SUPPORT - len(file_data)
            log(
                f"File size {file_size} bytes. Padded with {padding_size} bytes "
                f"to reach {FILE_SIZE_SUPPORT // 1024} KB.",
                "info",
            )
            if len(file_data) < FILE_SIZE_SUPPORT:
                file_data += b"\x00" * padding_size

            log(
                f"Start to transfer firmware to MCU (Chunk Size : {CHUNK_SIZE} bytes)",
                "info",
            )
            chunk_count = 0
            for i in range(0, len(file_data), CHUNK_SIZE):
                chunk = file_data[i : i + CHUNK_SIZE]
                ser.write(chunk)
                chunk_count += 1
                log(
                    f"Chunk {chunk_count} sending     | Waiting for MCU response ...",
                    "info",
                )
                if not _wait_for_line(
                    ser, MCU_RECEIVED_LINE_RESPONSE, log, handshake_timeout_s
                ):
                    return False

            log(
                f"File transfer completed. Total {chunk_count:2d} chunks.",
                "ok",
            )

            # Ask the MCU to verify by computing CRC32 over the full ROM and
            # comparing against the host's CRC32 of the (padded) firmware
            # bytes. Replaces the previous per-chunk readback+compare path.
            crc = zlib.crc32(file_data) & 0xFFFFFFFF
            verify_msg = f"{MCU_VERIFY_REQUEST} {crc:08X}"
            log(f"handshake send     : {verify_msg}", "info")
            ser.write(f"{verify_msg}\n".encode("UTF-8"))

            # CRC32 sweep over 128 KB at ~10 µs/byte ≈ 1.3 s on the MCU,
            # so allow generous margin on top of handshake_timeout_s.
            if not _wait_for_line(
                ser, MCU_VERIFY_OK, log, max(handshake_timeout_s, 30.0)
            ):
                return False

            # Tell the MCU explicitly that the binary stream is done so it
            # doesn't have to wait the full RECEIVED_DATA_TIMEOUT (~10s) of
            # silence before declaring completion.
            log(f"handshake send     : {MCU_TRANSFER_DONE_SIGNAL}", "info")
            ser.write(f"{MCU_TRANSFER_DONE_SIGNAL}\n".encode("UTF-8"))

            if not _wait_for_line(
                ser, MCU_TRANSFER_COMPLETED, log, handshake_timeout_s
            ):
                return False

        return True

    except serial.SerialException as e:
        log(f"Serial error: {e}", "err")
        return False
    except Exception as e:
        log(f"Error: {e}", "err")
        return False


# ---------------------------------------------------------------------------
# GPIO debug commands — single-pin set/read for the GUI's "GPIO 設定" tab.
#
# Two layers:
#   - GpioSession: holds an open serial connection across many ops, used by
#     the GUI for instant click-to-drive interaction.
#   - gpio_set / gpio_read free functions: open + one op + close, kept for
#     CLI / scripting callers where one-shot semantics are simpler.
# ---------------------------------------------------------------------------

GPIO_OK = "GPIO_OK"
GPIO_VALUE_PREFIX = "GPIO_VALUE"
GPIO_ERROR_PREFIX = "GPIO_ERROR"


def _read_line(
    ser: serial.Serial, log: LogCallback, timeout_s: float, *, quiet: bool = False
) -> str | None:
    """Read one line (stripped) within timeout. Returns None on timeout.
    Echoes any non-empty intermediate MCU lines via log so the user sees
    boot chatter (Vendor ID, etc.) just like the flash flow.

    `quiet=True` suppresses the "Timeout after Xs..." error log on
    timeout — pass it from polling callers (e.g. _await_play_done's
    0.25s slices) where timeout is the expected steady state, not an
    error condition."""
    deadline = time.monotonic() + timeout_s
    while True:
        if ser.in_waiting > 0:
            line = ser.readline().decode(errors="ignore").strip()
            if line:
                return line
        if time.monotonic() > deadline:
            if not quiet:
                log(f"Timeout after {timeout_s:.1f}s waiting for MCU reply", "err")
            return None
        time.sleep(0.01)


def _open_and_wait_idle(
    port: str | None, log: LogCallback, handshake_timeout_s: float
) -> serial.Serial | None:
    """Open the Due, wait until it emits ARDUINO_ERASE_READY (i.e. the MCU
    is sitting in its pre-erase idle loop), and return the open serial.
    Returns None on failure (caller still owns nothing)."""
    if port is None:
        port = _find_arduino_port()
        if port is None:
            log("Arduino Device not found.", "err")
            return None
    log(f"Arduino Found : {port}.", "ok")
    try:
        ser = serial.Serial(port, BAUD, timeout=0.1)
    except serial.SerialException as e:
        log(f"Serial open error: {e}", "err")
        return None
    if not _wait_for_line(ser, MCU_ERASE_READY, log, handshake_timeout_s):
        ser.close()
        return None
    return ser


@contextlib.contextmanager
def _serial_guard(lock):
    """Acquire `lock` for the duration of a serial transaction so the GPIO
    and TDBG sessions sharing one port don't interleave their reads/writes.
    No-op when lock is None (standalone own-the-port lifecycle)."""
    if lock is None:
        yield
        return
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def open_due_link(
    port: str | None,
    log: LogCallback,
    handshake_timeout_s: float = DEFAULT_HANDSHAKE_TIMEOUT_S,
) -> "serial.Serial | None":
    """Public entry point for the GUI's single shared connection: open the
    Due and wait until it's sitting in the pre-erase idle loop. The three
    persistent-session tabs (GPIO / TDBG / RECORD) all share the returned
    serial; the App owns it and is responsible for closing it. Returns the
    open serial, or None on failure."""
    return _open_and_wait_idle(port, log, handshake_timeout_s)


class GpioSession:
    """Persistent GPIO session over one open serial port.

    Lifecycle: construct with port + log, call open() to connect (~2 s Due
    reset wait), then call set_pin / read_pin as many times as needed (each
    ~5 ms over UART), finally close(). open()/close() are idempotent.

    Not thread-safe internally — callers must serialise set_pin / read_pin
    onto a single worker thread (the GUI does this with a per-tab cmd queue).
    """

    def __init__(
        self,
        log: LogCallback,
        *,
        port: str | None = None,
        handshake_timeout_s: float = DEFAULT_HANDSHAKE_TIMEOUT_S,
        ser: "serial.Serial | None" = None,
        lock: "threading.Lock | None" = None,
    ) -> None:
        self._log = log
        self._port = port
        self._handshake_timeout_s = handshake_timeout_s
        # When `ser` is supplied the session BORROWS the App's shared
        # serial — it neither opens nor closes it (the App owns the
        # lifecycle). `lock` serialises serial access across the GPIO /
        # TDBG sessions that share one port. Both default to None for the
        # standalone (own-the-port) lifecycle the CLI / tests still use.
        self._ser: serial.Serial | None = ser
        self._owns_ser = ser is None
        self._lock = lock

    @property
    def is_open(self) -> bool:
        return self._ser is not None

    def open(self) -> bool:
        if self.is_open:
            return True
        if not self._owns_ser:
            return self._ser is not None
        ser = _open_and_wait_idle(self._port, self._log, self._handshake_timeout_s)
        if ser is None:
            return False
        self._ser = ser
        return True

    def close(self) -> None:
        if self._ser is not None and self._owns_ser:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    def set_pin(self, pin: int, mode: str, value: str | None = None) -> bool:
        if not self.is_open:
            self._log("GPIO session not open.", "err")
            return False
        if mode not in ("OUTPUT", "INPUT"):
            self._log(f"Invalid mode: {mode}", "err")
            return False
        if value is not None and value not in ("HIGH", "LOW"):
            self._log(f"Invalid value: {value}", "err")
            return False

        cmd = f"GPIO_SET {pin} {mode}"
        if value is not None:
            cmd += f" {value}"
        with _serial_guard(self._lock):
            self._log(f"send: {cmd}", "info")
            self._ser.write(f"{cmd}\n".encode("UTF-8"))
            reply = _read_line(self._ser, self._log, 5.0)
        if reply == GPIO_OK:
            self._log(f"recv: {reply}", "ok")
            return True
        self._log(f"recv: {reply or '(no reply)'}", "err")
        return False

    def read_pin(self, pin: int) -> str | None:
        """Returns 'HIGH' or 'LOW' on success, None on any error."""
        if not self.is_open:
            self._log("GPIO session not open.", "err")
            return None

        cmd = f"GPIO_READ {pin}"
        with _serial_guard(self._lock):
            self._log(f"send: {cmd}", "info")
            self._ser.write(f"{cmd}\n".encode("UTF-8"))
            reply = _read_line(self._ser, self._log, 5.0)
        if reply and reply.startswith(GPIO_VALUE_PREFIX):
            # Format: "GPIO_VALUE <pin> <0|1>"
            parts = reply.split()
            if len(parts) == 3 and parts[2] in ("0", "1"):
                level = "HIGH" if parts[2] == "1" else "LOW"
                self._log(f"recv: {reply} -> {level}", "ok")
                return level
        self._log(f"recv: {reply or '(no reply)'}", "err")
        return None


def gpio_set(
    pin: int,
    mode: str,
    value: str | None,
    log: LogCallback,
    *,
    port: str | None = None,
    handshake_timeout_s: float = DEFAULT_HANDSHAKE_TIMEOUT_S,
) -> bool:
    """One-shot wrapper over GpioSession for CLI / scripting use.

    Each call opens a fresh connection (incurs ~2 s Due auto-reset), runs one
    GPIO_SET, and closes. For interactive use prefer GpioSession directly.
    """
    session = GpioSession(log, port=port, handshake_timeout_s=handshake_timeout_s)
    if not session.open():
        return False
    try:
        return session.set_pin(pin, mode, value)
    finally:
        session.close()


def gpio_read(
    pin: int,
    log: LogCallback,
    *,
    port: str | None = None,
    handshake_timeout_s: float = DEFAULT_HANDSHAKE_TIMEOUT_S,
) -> str | None:
    """One-shot wrapper over GpioSession for CLI / scripting use."""
    session = GpioSession(log, port=port, handshake_timeout_s=handshake_timeout_s)
    if not session.open():
        return None
    try:
        return session.read_pin(pin)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# TDBG (timing debug) — replays a captured logic-analyzer waveform on a Due
# GPIO at cycle-accurate timing. Pattern is uploaded once over UART, then
# the MCU plays it locally using DWT->CYCCNT.
# ---------------------------------------------------------------------------

DUE_CPU_HZ = 84_000_000
TDBG_MAX_EVENTS = 4096
TDBG_EVENT_BYTES = 5            # uint32_le delta + uint8 state
TDBG_MIN_DELTA_CYCLES = 32      # ~380 ns @ 84 MHz — below this the spin
                                # loop can't reliably hit the deadline.

MCU_TDBG_READY = "TDBG_READY"
MCU_TDBG_LOADED_PREFIX = "TDBG_LOADED"
MCU_TDBG_PLAY_STARTED = "TDBG_PLAY_STARTED"
MCU_TDBG_PLAY_DONE = "TDBG_PLAY_DONE"
MCU_TDBG_STOPPED = "TDBG_STOPPED"
MCU_TDBG_ERROR_PREFIX = "TDBG_ERROR"


def tdbg_crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflect, no xor-out).

    Test vector: tdbg_crc16(b"123456789") == 0x29B1.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


# Self-test: imported once on first module load. Keeps host/MCU CRC in lock-
# step — if someone "optimises" the polynomial, the import fails loudly.
assert tdbg_crc16(b"123456789") == 0x29B1, "tdbg_crc16 self-test failed"


def parse_acute_txt(
    text: str, channel: int = 0, unit_ps: int = 1
) -> tuple[int, list[tuple[int, int]]]:
    """Parse an Acute logic-analyzer text export.

    Format expected (header + CSV rows):
        Timestamp,CH-00[,CH-01,...]
        -40000,1
        0,0
        76800,1
        ...

    `channel` selects which CH-XX column to take (0 = first). `unit_ps` is
    the raw timestamp unit in picoseconds — Acute's default export is in
    picoseconds so the default of 1 is correct.

    Returns (initial_state, [(delta_cycles, new_state), ...]) where:
      * initial_state is taken from the first row (the pre-trigger sample)
      * the first event has delta=0 (transitions fire immediately at
        playback start) — playback's t=0 is anchored to the first
        transition row, not the pre-trigger row.
      * subsequent deltas are inter-transition cycle counts.

    Raises ValueError on malformed input or sub-minimum gaps.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("empty input")

    header_cols = [c.strip() for c in lines[0].split(",")]
    chan_indices = [
        i for i, c in enumerate(header_cols)
        if c.upper().startswith("CH")
    ]
    if not chan_indices:
        raise ValueError("no CH-* columns in header")
    if channel < 0 or channel >= len(chan_indices):
        raise ValueError(
            f"channel {channel} out of range; file has {len(chan_indices)}"
        )
    target_idx = chan_indices[channel]

    rows: list[tuple[int, int]] = []
    for lineno, raw in enumerate(lines[1:], start=2):
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) <= target_idx:
            raise ValueError(f"line {lineno}: missing column {target_idx}")
        try:
            ts = int(parts[0])
        except ValueError:
            # Allow scientific / float timestamps too.
            ts = int(float(parts[0]))
        try:
            state = int(parts[target_idx])
        except ValueError:
            raise ValueError(f"line {lineno}: state not 0/1: {parts[target_idx]!r}")
        if state not in (0, 1):
            raise ValueError(f"line {lineno}: state {state} not 0/1")
        rows.append((ts, state))

    if len(rows) < 2:
        raise ValueError("need at least one transition row after the initial state")

    initial_state = rows[0][1]
    base_ts = rows[1][0]    # anchor playback t=0 at the first transition

    events: list[tuple[int, int]] = []
    prev_cycles = 0
    for i in range(1, len(rows)):
        ts_ps = (rows[i][0] - base_ts) * unit_ps
        cycles_abs = round(ts_ps * DUE_CPU_HZ / 1e12)
        delta = cycles_abs - prev_cycles
        if i > 1 and delta < TDBG_MIN_DELTA_CYCLES:
            raise ValueError(
                f"line {i + 1}: delta {delta} cycles < min {TDBG_MIN_DELTA_CYCLES} "
                f"(too fast for playback engine)"
            )
        if delta < 0:
            raise ValueError(
                f"line {i + 1}: timestamps not monotonic (delta={delta})"
            )
        if delta > 0xFFFFFFFF:
            raise ValueError(
                f"line {i + 1}: delta {delta} exceeds uint32 — split with extra "
                f"no-op transitions or shorten the gap"
            )
        events.append((delta, rows[i][1]))
        prev_cycles = cycles_abs

    if len(events) > TDBG_MAX_EVENTS:
        raise ValueError(
            f"{len(events)} events > {TDBG_MAX_EVENTS} max — capture too long"
        )

    return initial_state, events


def tdbg_pack_events(events: list[tuple[int, int]]) -> bytes:
    """Serialise (delta, state) pairs into the wire format the MCU expects."""
    import struct
    out = bytearray()
    for delta, state in events:
        out += struct.pack("<IB", delta & 0xFFFFFFFF, state & 0x01)
    return bytes(out)


def tdbg_calibration_pattern() -> tuple[int, list[tuple[int, int]]]:
    """Built-in calibration pattern — four square-wave bursts at increasing
    delta sizes, separated by 1 ms gaps. Use this to verify the TC playback
    engine's timing fidelity end-to-end with a logic analyzer:

      Burst 1: 16 transitions at 100 cycles  (~1.19 µs period)
      Burst 2: 16 transitions at 200 cycles  (~2.38 µs period)
      Burst 3: 16 transitions at 500 cycles  (~5.95 µs period)
      Burst 4: 16 transitions at 1000 cycles (~11.9 µs period)

    All deltas are well above the TC engine's ~60-cycle floor, so the
    captured edges should land within ±5 cycles of the requested period.
    If burst 1 looks like burst 4 (uniform comb regardless of delta),
    the engine is broken.

    Returns (initial_state, [(delta_cycles, new_state), ...]) — same shape
    as parse_acute_txt() so the caller can drop it straight into
    TdbgSession.load() without further packaging.
    """
    DUE_HZ = 84_000_000
    GAP_CYCLES = round(1e-3 * DUE_HZ)   # 1 ms separation
    BURSTS = (100, 200, 500, 1000)
    BURST_LEN = 16

    events: list[tuple[int, int]] = []
    state = 1                          # toggling state across the run
    first = True
    for half_period in BURSTS:
        for i in range(BURST_LEN):
            delta = half_period
            if first:
                # First event has delta=0 — playback's t=0 anchor.
                delta = 0
                first = False
            elif i == 0:
                # Long inter-burst gap, replaces the first burst-event
                # delta so the gap shows up in the trace.
                delta = GAP_CYCLES
            state ^= 1
            events.append((delta, state))
    initial_state = 0
    return initial_state, events


def tdbg_retime_for_engine(
    events: list[tuple[int, int]],
    target_min_half_period_cycles: int = 126,
    gap_threshold_cycles: int = 84_000,
) -> tuple[list[tuple[int, int]], float]:
    """Scale within-cluster deltas so the minimum half-period meets the
    TC engine's safe floor. Used by the GUI before sending captured
    patterns whose source min delta (e.g. 32 cycles ≈ 380 ns from a
    1.3 MHz capture) sits below the ISR's ~700-800 ns round-trip floor.

    Long gaps (delta > gap_threshold_cycles) are passed through
    unchanged, so total runtime stays close to the original capture
    duration — most of the time in a captured pattern lives in
    inter-cluster gaps, and stretching those would multiply user-
    visible playback latency for no protocol benefit.

    The delta=0 anchor (event 0 from parse_acute_txt) is also passed
    through unchanged.

    Args:
        events: list of (delta_cycles, state) from parse_acute_txt.
        target_min_half_period_cycles: lower floor for short deltas
            after scaling. Default 126 = 1.5 µs at 84 MHz, matching
            the engine's safely-replayable minimum.
        gap_threshold_cycles: deltas above this are treated as "long
            gaps" and not scaled. Default 84_000 = 1 ms at 84 MHz.

    Returns:
        (scaled_events, scale_factor). scale_factor is 1.0 if no
        scaling was needed (i.e. min short delta already met the
        floor) — the caller can skip the "scaled to X×" log line.
    """
    short_deltas = [d for d, _ in events
                    if 0 < d <= gap_threshold_cycles]
    if not short_deltas:
        return events, 1.0
    min_short = min(short_deltas)
    if min_short >= target_min_half_period_cycles:
        return events, 1.0
    scale = target_min_half_period_cycles / min_short
    out: list[tuple[int, int]] = []
    for d, s in events:
        if 0 < d <= gap_threshold_cycles:
            out.append((round(d * scale), s))
        else:
            out.append((d, s))
    return out, scale


class TdbgSession:
    """Persistent TDBG session — same lifecycle pattern as GpioSession.

    Workflow:
        s = TdbgSession(log, port=...)
        s.open()
        s.load(pin=13, initial_state=1, events=events)
        s.play(iterations=1)        # blocks until MCU replies TDBG_PLAY_DONE
        # ... or for loop mode:
        s.play(iterations=0)        # async fire-and-forget; sets _playing=True
        s.stop()                    # request stop; waits for TDBG_STOPPED
        s.close()

    Not thread-safe; serialise calls on a single worker thread (GUI does).
    """

    def __init__(
        self,
        log: LogCallback,
        *,
        port: str | None = None,
        handshake_timeout_s: float = DEFAULT_HANDSHAKE_TIMEOUT_S,
        ser: "serial.Serial | None" = None,
        lock: "threading.Lock | None" = None,
    ) -> None:
        self._log = log
        self._port = port
        self._handshake_timeout_s = handshake_timeout_s
        # See GpioSession.__init__ — `ser` borrows the App's shared serial,
        # `lock` serialises access against the other shared-port sessions.
        self._ser: serial.Serial | None = ser
        self._owns_ser = ser is None
        self._lock = lock

    @property
    def is_open(self) -> bool:
        return self._ser is not None

    def open(self) -> bool:
        if self.is_open:
            return True
        if not self._owns_ser:
            return self._ser is not None
        ser = _open_and_wait_idle(self._port, self._log, self._handshake_timeout_s)
        if ser is None:
            return False
        self._ser = ser
        return True

    def close(self) -> None:
        if self._ser is not None and self._owns_ser:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    def load(
        self,
        pin: int,
        initial_state: int,
        events: list[tuple[int, int]],
    ) -> bool:
        if not self.is_open:
            self._log("TDBG session not open.", "err")
            return False
        if not (0 <= pin <= 65):
            self._log(f"Invalid pin: {pin}", "err")
            return False
        if initial_state not in (0, 1):
            self._log(f"Invalid initial state: {initial_state}", "err")
            return False
        if not events or len(events) > TDBG_MAX_EVENTS:
            self._log(
                f"Event count out of range: {len(events)} (1..{TDBG_MAX_EVENTS})",
                "err",
            )
            return False

        blob = tdbg_pack_events(events)
        expected_crc = tdbg_crc16(blob)

        with _serial_guard(self._lock):
            # Drain any leftover lines from a previous fire-and-forget play()
            # (TDBG_PLAY_DONE / TDBG_STOPPED). If we don't, _read_line below
            # would consume one of those in place of TDBG_READY and the
            # handshake mismatches.
            self._drain_stale()

            # Abort any still-running playback before loading. Under
            # fire-and-forget play() the MCU may still be mid-playback
            # (~1.3 s) when the user sends a new pattern; while playing it
            # is blocked in its play loop and silently DISCARDS any
            # non-TDBG_STOP line — so a TDBG_LOAD sent now would be eaten
            # and we'd time out waiting for TDBG_READY. TDBG_STOP is the
            # one command that loop honours: it cleanly aborts and returns
            # the MCU to its idle command loop. If nothing is playing, the
            # idle loop just ignores TDBG_STOP (no reply), so this is a
            # harmless no-op. Drain the TDBG_STOPPED / TDBG_PLAY_DONE the
            # abort produces before starting the LOAD handshake.
            self._ser.write(b"TDBG_STOP\n")
            self._ser.flush()
            _stop_deadline = time.monotonic() + 0.5
            while time.monotonic() < _stop_deadline:
                line = _read_line(self._ser, self._log, 0.15, quiet=True)
                if line is None:
                    break
                self._log(f"drained: {line}", "info")

            cmd = f"TDBG_LOAD {pin} {len(events)} {initial_state}"
            self._log(f"send: {cmd}", "info")
            self._ser.write(f"{cmd}\n".encode("UTF-8"))

            ready = _read_line(self._ser, self._log, 5.0)
            if ready != MCU_TDBG_READY:
                self._log(f"recv: {ready or '(no reply)'} (expected TDBG_READY)", "err")
                return False
            self._log(f"recv: {ready}", "ok")

            # Generous timeout — 20 KB at 115200 takes ~1.7 s, MCU then replies.
            self._ser.write(blob)
            self._ser.flush()
            self._log(f"sent {len(blob)} bytes of waveform data", "info")

            reply = _read_line(self._ser, self._log, 10.0)

        if not reply:
            self._log("no reply after blob", "err")
            return False
        if reply.startswith(MCU_TDBG_ERROR_PREFIX):
            self._log(f"recv: {reply}", "err")
            return False
        if not reply.startswith(MCU_TDBG_LOADED_PREFIX):
            self._log(f"recv: {reply} (expected TDBG_LOADED)", "err")
            return False
        try:
            mcu_crc = int(reply.split()[1], 16)
        except (IndexError, ValueError):
            self._log(f"malformed TDBG_LOADED reply: {reply!r}", "err")
            return False
        if mcu_crc != expected_crc:
            self._log(
                f"CRC mismatch: host=0x{expected_crc:04X} mcu=0x{mcu_crc:04X}",
                "err",
            )
            return False
        self._log(f"recv: {reply} (CRC OK)", "ok")
        return True

    def play(
        self,
        iterations: int = 1,
        total_duration_s: float = 0.0,
        stop_event: "threading.Event | None" = None,
    ) -> bool:
        """Send TDBG_PLAY and return as soon as the MCU acks with
        TDBG_PLAY_STARTED. Fire-and-forget — we do NOT wait for
        TDBG_PLAY_DONE.

        Rationale (per user spec): TDBG is a clock-burst output, not a
        request/response transaction. Once the MCU starts driving the
        pin, the receiver hardware is what cares about the signal. The
        host has nothing useful to do during the playback, and a
        timeout-based "did it finish" check just produces spurious
        errors when the MCU is fine but slower than the host's guess
        (or when the user yanks the cable mid-play, etc.).

        Subsequent commands on this session drain any leftover
        TDBG_PLAY_DONE / TDBG_STOPPED that the MCU may have queued
        after we walked away, so the next handshake doesn't see stale
        replies (see `load`).

        iterations=0 (infinite) is rejected here — without a wait
        loop, "infinite" just means "fire once and pretend it's
        infinite", which isn't useful. Use iterations=N for a finite
        loop the MCU will run on its own.

        stop_event and total_duration_s are accepted for API
        compatibility but ignored — there's no longer a poll loop to
        notice them.
        """
        del stop_event, total_duration_s   # unused under fire-and-forget
        if not self.is_open:
            self._log("TDBG session not open.", "err")
            return False
        if iterations < 1:
            self._log(
                f"iterations must be >= 1 under fire-and-forget play, got {iterations}",
                "err",
            )
            return False

        if iterations == 1:
            cmd = "TDBG_PLAY"
        else:
            cmd = f"TDBG_PLAY_LOOP {iterations}"
        with _serial_guard(self._lock):
            self._log(f"send: {cmd}", "info")
            self._ser.write(f"{cmd}\n".encode("UTF-8"))
            return self._await_play_started()

    def _await_play_started(self) -> bool:
        reply = _read_line(self._ser, self._log, 5.0)
        if reply == MCU_TDBG_PLAY_STARTED:
            self._log(f"recv: {reply}", "ok")
            return True
        if reply and reply.startswith(MCU_TDBG_ERROR_PREFIX):
            self._log(f"recv: {reply}", "err")
        else:
            self._log(f"recv: {reply or '(no reply)'} (expected TDBG_PLAY_STARTED)", "err")
        return False

    def _drain_stale(self, max_lines: int = 16) -> None:
        """Drain any leftover lines the MCU sent after a previous
        fire-and-forget play (typically TDBG_PLAY_DONE or TDBG_STOPPED).
        Called at the start of load() so the next handshake doesn't
        consume them in place of TDBG_READY / TDBG_LOADED."""
        # in_waiting is a count; readline blocks up to ser.timeout. To
        # stay non-blocking, only readline while there's data buffered.
        try:
            for _ in range(max_lines):
                if self._ser.in_waiting <= 0:
                    return
                stale = self._ser.readline().decode(errors="ignore").strip()
                if stale:
                    self._log(f"drained stale: {stale}", "info")
        except Exception:
            return

    def _await_play_done(
        self,
        timeout_s: float,
        stop_event: "threading.Event | None" = None,
    ) -> bool:
        import math
        # Use monotonic + relative deadline so timeout=inf works cleanly.
        if math.isinf(timeout_s):
            deadline = float("inf")
        else:
            deadline = time.monotonic() + timeout_s

        sent_stop = False
        while True:
            now = time.monotonic()
            if now >= deadline:
                self._log(
                    f"timeout waiting for TDBG_PLAY_DONE after {timeout_s:.1f}s",
                    "err",
                )
                return False

            # If caller asked us to stop, send STOP once and keep draining.
            if stop_event is not None and stop_event.is_set() and not sent_stop:
                self._log("send: TDBG_STOP", "info")
                try:
                    self._ser.write(b"TDBG_STOP\n")
                except Exception as e:
                    self._log(f"failed to send TDBG_STOP: {e}", "err")
                sent_stop = True

            # Bound per-iteration wait so we revisit stop_event promptly.
            # quiet=True: each empty slice IS the steady state during MCU
            # bit-banging — surfacing "Timeout after 0.2s..." every iteration
            # would spam the log with red lines that aren't real errors.
            slice_s = min(0.25, deadline - now) if not math.isinf(deadline) else 0.25
            line = _read_line(self._ser, self._log, slice_s, quiet=True)
            if line is None:
                continue
            if line == MCU_TDBG_PLAY_DONE:
                self._log(f"recv: {line}", "ok")
                return True
            if line == MCU_TDBG_STOPPED:
                self._log(f"recv: {line}", "ok")
                continue
            if line.startswith(MCU_TDBG_ERROR_PREFIX):
                self._log(f"recv: {line}", "err")
                return False
            # Surface anything else as info (debug chatter from MCU).
            self._log(f"MCU: {line}", "info")


# ---------------------------------------------------------------------------
# RECORD (live waveform capture) — multi-pin recorder. Pin states are sampled
# on the MCU via per-pin CHANGE interrupts; events arrive as
# (delta_us, mask) pairs. The mask bit `i` is the i-th pin in the host's
# pin list, NOT the absolute Due pin number.
# ---------------------------------------------------------------------------

import threading

RECORD_MAX_EVENTS = 4096
RECORD_MAX_PINS = 4
RECORD_EVENT_BYTES = 5

MCU_RECORD_STARTED = "RECORD_STARTED"
MCU_RECORD_LIVE_PREFIX = "RECORD_LIVE"
MCU_RECORD_OVERFLOW = "RECORD_OVERFLOW"
MCU_RECORD_STOPPED = "RECORD_STOPPED"
MCU_RECORD_DATA_PREFIX = "RECORD_DATA"
MCU_RECORD_DONE_PREFIX = "RECORD_DONE"
MCU_RECORD_ERROR_PREFIX = "RECORD_ERROR"


def parse_record_blob(
    blob: bytes, pins: list[int]
) -> list[tuple[int, dict[int, bool]]]:
    """Decode raw RECORD_DATA bytes → [(delta_us, {pin: bool, ...}), ...].

    `pins` is the ordered list given to RECORD_START — bit i of each event's
    mask byte corresponds to pins[i]. Mask bit set = HIGH.
    """
    if len(blob) % RECORD_EVENT_BYTES != 0:
        raise ValueError(
            f"blob length {len(blob)} not a multiple of {RECORD_EVENT_BYTES}"
        )
    out: list[tuple[int, dict[int, bool]]] = []
    import struct as _s
    for i in range(0, len(blob), RECORD_EVENT_BYTES):
        delta_us, mask = _s.unpack_from("<IB", blob, i)
        states = {pin: bool((mask >> bit) & 1) for bit, pin in enumerate(pins)}
        out.append((delta_us, states))
    return out


class RecordSession:
    """Persistent recording session — same lifecycle as TdbgSession.

    Workflow:
        s = RecordSession(log, port=...)
        s.open()
        s.start([13, 7], on_live=lambda states: ...)   # callbacks fire on
                                                        # each RECORD_LIVE
        # ... wait however long ...
        pins, events = s.stop()                         # returns capture
        s.close()
    """

    def __init__(
        self,
        log: LogCallback,
        *,
        port: str | None = None,
        handshake_timeout_s: float = DEFAULT_HANDSHAKE_TIMEOUT_S,
        ser: "serial.Serial | None" = None,
        lock: "threading.Lock | None" = None,
    ) -> None:
        self._log = log
        self._port = port
        self._handshake_timeout_s = handshake_timeout_s
        # See GpioSession.__init__ — `ser` borrows the App's shared serial.
        # RECORD is exclusive while recording (its live_loop owns the port,
        # and the App disables the other tabs' actions during a recording),
        # so `lock` is accepted for API symmetry but RECORD's own start/stop
        # transactions don't contend with GPIO/TDBG in practice.
        self._ser: serial.Serial | None = ser
        self._owns_ser = ser is None
        self._lock = lock
        self._pins: list[int] = []
        self._on_live = None
        self._live_thread: threading.Thread | None = None
        self._live_stop = threading.Event()
        # When the live thread sees a non-LIVE/OVERFLOW line (typically the
        # RECORD_STOPPED / RECORD_DATA / blob that comes back after we send
        # RECORD_STOP), it parks the line here so stop() can pick up the
        # exchange without competing for serial bytes.
        self._stop_handoff: list[str] = []
        self._stop_handoff_lock = threading.Lock()
        self._handed_off = threading.Event()

    @property
    def is_open(self) -> bool:
        return self._ser is not None

    def open(self) -> bool:
        if self.is_open:
            return True
        if not self._owns_ser:
            return self._ser is not None
        ser = _open_and_wait_idle(self._port, self._log, self._handshake_timeout_s)
        if ser is None:
            return False
        self._ser = ser
        return True

    def close(self) -> None:
        # Make sure live thread is wound up first.
        self._live_stop.set()
        if self._live_thread is not None:
            self._live_thread.join(timeout=2.0)
            self._live_thread = None
        if self._ser is not None and self._owns_ser:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    def start(self, pins: list[int], on_live=None) -> bool:
        if not self.is_open:
            self._log("RECORD session not open.", "err")
            return False
        if not pins or len(pins) > RECORD_MAX_PINS:
            self._log(
                f"pin count {len(pins)} out of range (1..{RECORD_MAX_PINS})",
                "err",
            )
            return False
        for p in pins:
            if not (0 <= p <= 65):
                self._log(f"bad pin: {p}", "err")
                return False

        self._pins = list(pins)
        self._on_live = on_live
        self._live_stop.clear()
        self._handed_off.clear()
        with self._stop_handoff_lock:
            self._stop_handoff.clear()

        cmd = "RECORD_START " + " ".join(str(p) for p in pins)
        self._log(f"send: {cmd}", "info")
        self._ser.write(f"{cmd}\n".encode("UTF-8"))

        reply = _read_line(self._ser, self._log, 5.0)
        if reply != MCU_RECORD_STARTED:
            if reply and reply.startswith(MCU_RECORD_ERROR_PREFIX):
                self._log(f"recv: {reply}", "err")
            else:
                self._log(f"recv: {reply or '(no reply)'} (expected RECORD_STARTED)", "err")
            return False
        self._log(f"recv: {reply}", "ok")

        # Spin up the live reader.
        self._live_thread = threading.Thread(
            target=self._live_loop, name="RecordLive", daemon=True,
        )
        self._live_thread.start()
        return True

    def _live_loop(self) -> None:
        """Reads serial lines while recording. RECORD_LIVE updates the
        on_live callback; RECORD_OVERFLOW logs a warning; anything else
        gets parked for stop() to pick up."""
        while not self._live_stop.is_set():
            # Same polling pattern as _await_play_done — empty slices are
            # the normal idle state while waiting for the next RECORD_LIVE,
            # not real errors. Suppress the timeout log.
            line = _read_line(self._ser, self._log, 0.25, quiet=True)
            if line is None:
                continue
            if line.startswith(MCU_RECORD_LIVE_PREFIX):
                # RECORD_LIVE <hex>
                parts = line.split()
                if len(parts) >= 2 and self._on_live is not None:
                    try:
                        mask = int(parts[1], 16)
                    except ValueError:
                        continue
                    states = {
                        pin: bool((mask >> bit) & 1)
                        for bit, pin in enumerate(self._pins)
                    }
                    try:
                        self._on_live(states)
                    except Exception as e:
                        self._log(f"on_live callback error: {e}", "warn")
                continue
            if line == MCU_RECORD_OVERFLOW:
                self._log("recv: RECORD_OVERFLOW (event buffer full)", "warn")
                continue
            # Any other line — likely the start of the stop-exchange (typically
            # RECORD_STOPPED). Park it for stop() to drain, then EXIT so the
            # main thread has exclusive ownership of the serial port for the
            # binary blob read that follows. Earlier the loop kept polling
            # after parking; the next 0.25 s timeout window could swallow
            # bytes from RECORD_DATA / blob / RECORD_DONE — and if the blob
            # happened to contain a 0x0A byte, ser.readline() would split
            # mid-blob and feed that fragment back to stop() in place of
            # RECORD_DONE. The forced-shutdown path (Disconnect mid-recording)
            # uses _live_stop.set() + join(), which still works because the
            # while-check at the top of the loop honours it.
            with self._stop_handoff_lock:
                self._stop_handoff.append(line)
            self._handed_off.set()
            return

    def stop(self) -> tuple[list[int], list[tuple[int, dict[int, bool]]]] | None:
        """Stop recording, return (pins, events). None on failure."""
        if not self.is_open:
            self._log("RECORD session not open.", "err")
            return None
        if self._live_thread is None:
            self._log("RECORD not active.", "err")
            return None

        self._log("send: RECORD_STOP", "info")
        self._ser.write(b"RECORD_STOP\n")

        # Wait briefly for the live thread to capture the first non-LIVE line
        # (typically RECORD_STOPPED). Then drain the rest ourselves.
        if not self._handed_off.wait(timeout=5.0):
            self._log("timeout waiting for RECORD_STOPPED", "err")
            self._live_stop.set()
            self._live_thread.join(timeout=2.0)
            self._live_thread = None
            return None
        self._live_stop.set()
        self._live_thread.join(timeout=2.0)
        self._live_thread = None

        with self._stop_handoff_lock:
            queued = list(self._stop_handoff)
            self._stop_handoff.clear()

        # Build a small re-source that yields the queued lines first then
        # falls back to fresh _read_line calls.
        def next_line(timeout=10.0) -> str | None:
            if queued:
                return queued.pop(0)
            return _read_line(self._ser, self._log, timeout)

        line = next_line()
        if line != MCU_RECORD_STOPPED:
            self._log(f"recv: {line or '(none)'} (expected RECORD_STOPPED)", "err")
            return None
        self._log(f"recv: {line}", "ok")

        line = next_line()
        if line is None or not line.startswith(MCU_RECORD_DATA_PREFIX):
            self._log(f"recv: {line or '(none)'} (expected RECORD_DATA)", "err")
            return None
        try:
            count = int(line.split()[1])
        except (IndexError, ValueError):
            self._log(f"malformed RECORD_DATA: {line!r}", "err")
            return None
        self._log(f"recv: {line}", "ok")

        blob = b""
        if count > 0:
            expected = count * RECORD_EVENT_BYTES
            saved_timeout = self._ser.timeout
            self._ser.timeout = 5.0
            blob = self._ser.read(expected)
            self._ser.timeout = saved_timeout
            if len(blob) != expected:
                self._log(
                    f"short blob read: got {len(blob)}/{expected}", "err",
                )
                return None

        line = next_line()
        if line is None or not line.startswith(MCU_RECORD_DONE_PREFIX):
            self._log(f"recv: {line or '(none)'} (expected RECORD_DONE)", "err")
            return None
        try:
            mcu_crc = int(line.split()[1], 16)
        except (IndexError, ValueError):
            self._log(f"malformed RECORD_DONE: {line!r}", "err")
            return None
        # Re-use TDBG's CRC-16/CCITT-FALSE.
        host_crc = tdbg_crc16(blob) if blob else 0
        if mcu_crc != host_crc:
            self._log(
                f"CRC mismatch: host=0x{host_crc:04X} mcu=0x{mcu_crc:04X}",
                "err",
            )
            return None
        self._log(f"recv: {line} (CRC OK)", "ok")

        try:
            events = parse_record_blob(blob, self._pins)
        except ValueError as e:
            self._log(f"blob decode error: {e}", "err")
            return None
        return list(self._pins), events
