import os
import sys

from binFileTransfer_core import program_firmware


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


def main() -> int:
    os.system("")  # enable ANSI on legacy Windows consoles

    base_dir = os.path.dirname(os.path.abspath(__file__))
    firmware_path = os.path.join(base_dir, FILE_NAME)

    success = program_firmware(firmware_path, cli_log)

    if success:
        cli_log(f"{FILE_NAME} Program Successful.", "ok")
    input(PYTHON_DONE)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
