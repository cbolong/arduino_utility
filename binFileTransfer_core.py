from __future__ import annotations

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
    ser: serial.Serial, log: LogCallback, timeout_s: float
) -> str | None:
    """Read one line (stripped) within timeout. Returns None on timeout.
    Echoes any non-empty intermediate MCU lines via log so the user sees
    boot chatter (Vendor ID, etc.) just like the flash flow."""
    deadline = time.monotonic() + timeout_s
    while True:
        if ser.in_waiting > 0:
            line = ser.readline().decode(errors="ignore").strip()
            if line:
                return line
        if time.monotonic() > deadline:
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
    ) -> None:
        self._log = log
        self._port = port
        self._handshake_timeout_s = handshake_timeout_s
        self._ser: serial.Serial | None = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None

    def open(self) -> bool:
        if self.is_open:
            return True
        ser = _open_and_wait_idle(self._port, self._log, self._handshake_timeout_s)
        if ser is None:
            return False
        self._ser = ser
        return True

    def close(self) -> None:
        if self._ser is not None:
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
