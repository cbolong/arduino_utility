# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A two-piece utility that programs SST39xF010/020/040-family parallel NOR flash chips using an **Arduino Due** as the bit-banged programmer. The PDF datasheet for the target part is in `spec/`.

- `binFileProgram.ino` — sketch that runs on the Due. Drives 19 address pins, 8 data pins, and CE/OE/WE to talk to the flash.
- `binFileTransfer.py` — host-side script that auto-detects the Due, opens its USB serial port, and streams `firmware.bin` over to the sketch in 4 KB chunks.

There is no build system, no test suite, and no package manifest. Workflow is: edit → upload sketch via Arduino IDE → run the Python script.

## Running

```bash
# Host side. Requires pyserial. Place firmware.bin next to the script.
python binFileTransfer.py
```

The sketch is uploaded with the Arduino IDE (board: Arduino Due, **Programming Port** — the script auto-detects VID `0x2341` / PID `0x003D`, which is the Due's programming port, not the native USB port).

## Architecture: the handshake protocol

The two files are tightly coupled by a string-based serial protocol at 115200 baud. **Both sides must change together.** The full sequence:

1. Sketch boots → reads SST software ID → emits `ARDUINO_ERASE_READY`.
2. Host sends `ARDUINO_ERASE_TRIGGER`.
3. Sketch performs full chip erase + verifies first 1 KB is `0xFF` → emits `ARDUINO_READY_TO_RECEIVED_DATA`.
4. Host pads `firmware.bin` to exactly 128 KB with `\x00`, then sends 32 × 4 KB chunks. After each chunk it waits for `ARDUINO_RECEIVED_LINE_DONE` before sending the next.
5. For each chunk the sketch runs program → read-back → byte compare; any mismatch emits `ARDUINO_ERROR` and the sketch halts in `while(1)`.
6. After the host stops sending, the sketch's 10 s `RECEIVED_DATA_TIMEOUT` fires and it emits `ARDUINO_DATA_COMPLETED`.

Constants that must stay in sync between the two files: the six handshake strings, `CHUNK_SIZE` (4096), and the implied total size (`FILE_SIZE_SUPPORT` = 128 KB on the host = 32 chunks for an SST39xF010).

## Hardware wiring assumptions baked into the sketch

`binFileProgram.ino` hard-codes the Due pin map:

- `addrPins[]` — 19 pins for A0–A18 (note A8/A9 etc. are **not** in numeric pin order; the array order *is* the address bit order, index 0 = A0).
- `dataPins[]` — 8 pins for D0–D7, again in bit order.
- `CE_PIN=43`, `OE_PIN=39`, `WE_PIN=25`.

Idle state is `CE=LOW, OE=HIGH, WE=HIGH`. CE is held low for the whole session — only OE and WE are toggled per byte. If you change wiring, only the arrays need to change; the rest of the code indexes through them.

The supported device IDs are also hard-coded (`vendorID 0xBF`, `deviceID_SST39SF010 0xB5`, `deviceID_SST39LF010 0xD5`). Any other ID halts the sketch at boot.

## Things that look like bugs but aren't (or are, and matter)

- **`Serial.readString()` in `setup()`** depends on the default 1 s serial timeout to terminate. Don't "fix" it to `readStringUntil('\n')` without also changing how the host sends `ARDUINO_ERASE_TRIGGER` (currently sent with `\n`, which works but isn't what gates the read).
- **End-of-transfer detection is timeout-based**, not protocol-based. The host finishes sending and the sketch waits 10 s of silence before declaring `ARDUINO_DATA_COMPLETED`. Shortening `RECEIVED_DATA_TIMEOUT` will break slow transfers; lengthening it just makes the user wait.
- The host always pads to 128 KB and sends exactly 32 chunks. The sketch does not know the original file size — it programs all 128 KB. Larger flash variants (020/040) would need both sides updated.
- `verifyReadData()` in the sketch is defined but unused (commented call in `loop()`); leave it unless the user asks to wire it up.
