from __future__ import annotations

import argparse
import os
import sys

from binFileTransfer_core import (
    DEFAULT_HANDSHAKE_TIMEOUT_S,
    program_firmware,
)


FILE_NAME = "firmware.bin"
PYTHON_DONE = "Press Enter to Exit..."

GRAY = "\033[90m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

LEVEL_COLORS = {
    "info": "",
    "ok": GREEN,
    "warn": YELLOW,
    "err": RED,
    "wait": GRAY,
}


def cli_log(message: str, level: str) -> None:
    color = LEVEL_COLORS.get(level, "")
    prefix = ">>>  "
    if color:
        print(f"{color}{prefix}{message}{RESET}")
    else:
        print(f"{prefix}{message}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload firmware.bin to an Arduino-Due-driven SST39 flash programmer.",
    )
    parser.add_argument(
        "--port",
        default=None,
        help="Serial port (e.g. COM19, /dev/ttyACM0). If omitted, auto-detects "
        "by Arduino Due Programming Port USB VID/PID.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_HANDSHAKE_TIMEOUT_S,
        help=f"Per-handshake timeout in seconds (default: {DEFAULT_HANDSHAKE_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--file",
        default=None,
        help=f"Path to firmware binary (default: ./{FILE_NAME}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    os.system("")  # enable ANSI on legacy Windows consoles

    args = _parse_args(argv)

    if args.file:
        firmware_path = os.path.abspath(args.file)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        firmware_path = os.path.join(base_dir, FILE_NAME)

    success = program_firmware(
        firmware_path,
        cli_log,
        port=args.port,
        handshake_timeout_s=args.timeout,
    )

    if success:
        cli_log(f"{os.path.basename(firmware_path)} Program Successful.", "ok")
    input(PYTHON_DONE)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
