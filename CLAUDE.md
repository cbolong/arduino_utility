# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Workflow rules

- Commit changes directly on `main` and push. Do not create feature branches or PRs unless the user explicitly asks for one.

## What this repo is

A utility that programs SST39xF010-family parallel NOR flash chips using an **Arduino Due** as the bit-banged programmer. The PDF datasheet is in `spec/`.

- `binFileProgram.ino` — sketch on the Due. Drives 19 address pins, 8 data pins, CE/OE/WE. Also exposes a single-pin GPIO debug protocol for wiring verification, and a TDBG waveform-replay protocol that loads a captured pattern into RAM and plays it on a chosen pin via DWT cycle-counter timing.
- `binFileTransfer_core.py` — host-side library. All handshake logic, port detection, timeout handling, CRC32 verify, `GpioSession`, `TdbgSession`, and `parse_acute_txt` live here. The two front-ends are thin shells.
- `binFileTransfer.py` — CLI front-end (argparse around `core.program_firmware()`). No TDBG sub-command yet; `TdbgSession` is library-only.
- `binFileTransferGui.py` — Tkinter GUI front-end. Three tabs: `燒錄 ROM` (flash), `GPIO 設定` (manual pin poker, persistent connection), and `TDBG` (waveform replay).

`README.md` is the authoritative end-user doc (Chinese). When a question is about *user-facing behaviour* — wiring tables, CLI flags, troubleshooting — read it. When it's about *internal coupling* between the sketch and host, this file is faster.

## Commands

```bash
# Install host deps
pip install -r requirements.txt        # pyserial only; tkinter is stdlib

# Run CLI (auto-detects Due Programming Port via VID 0x2341 / PID 0x003D)
python binFileTransfer.py
python binFileTransfer.py --port COM19 --file ./builds/v1.bin --timeout 60

# Run GUI
python binFileTransferGui.py
```

The sketch is uploaded with the Arduino IDE: board **Arduino Due (Programming Port)** — auto-detection only matches the programming port, not the native USB port.

There is no test suite. Verification is hardware-in-the-loop: flash a known `firmware.bin`, watch the CRC32 match.

## Architecture: protocol coupling

`binFileProgram.ino` and `binFileTransfer_core.py` are tightly coupled by **three** string-based serial protocols at 115200 8N1 (Flash, GPIO, TDBG). Both sides must change together — the host has no version negotiation, mismatched constants just timeout.

### Flash flow (8 strings, primary path)

1. Sketch boots → reads SST software ID (vendor `0xBF`, device `0xB5` SF010 / `0xD5` LF010 — anything else halts in `while(1)`) → emits `ARDUINO_ERASE_READY`.
2. Host sends `ARDUINO_ERASE_TRIGGER`.
3. Sketch chip-erases, samples first 1 KB == `0xFF`, emits `ARDUINO_READY_TO_RECEIVED_DATA`.
4. Host pads `firmware.bin` to 128 KB, sends 32 × 4 KB chunks. After each chunk it waits for `ARDUINO_RECEIVED_LINE_DONE`.
5. Host sends `ARDUINO_VERIFY_REQUEST <crc32_hex>` → sketch sweeps the chip with bitwise CRC32 (~1.3 s for 128 KB) → `ARDUINO_VERIFY_OK` or `ARDUINO_ERROR`. **CRC32 is the primary integrity check; the per-chunk read-back compare in the sketch's program loop is a redundant inner check.**
6. Host sends `ARDUINO_TRANSFER_DONE_SIGNAL` → sketch prints timing report → `ARDUINO_DATA_COMPLETED`.

The 10 s `RECEIVED_DATA_TIMEOUT` in the sketch is a **legacy fallback** for old hosts that didn't send `ARDUINO_TRANSFER_DONE_SIGNAL`. New hosts always send the explicit signal and never hit it.

### GPIO flow (used by `GPIO 設定` tab and `core.GpioSession`)

After `ARDUINO_ERASE_READY` and *before* `ARDUINO_ERASE_TRIGGER`, the sketch loops on:

- `GPIO_SET <pin> <OUTPUT|INPUT> [HIGH|LOW]` → `GPIO_OK` or `GPIO_ERROR <why>`
- `GPIO_READ <pin>` → `GPIO_VALUE <pin> <0|1>` (forces INPUT first)

Once `ARDUINO_ERASE_TRIGGER` fires, GPIO mode is gone until the Due is reset. Poking flash pins (CE/OE/WE/Ax/DQx) via GPIO leaves the bus in an undefined state — the user has to reset before flashing.

The GUI keeps a persistent `GpioSession` open behind the GPIO tab; the CLI doesn't expose GPIO. When the user clicks Start Programming with a GPIO or TDBG session open, FlashTab calls `App.release_port_then(except_tab=self, on_done=...)` which sequentially calls `gpio_tab.disconnect_for_other()` and `tdbg_tab.disconnect_for_other()` before starting the flash flow.

### TDBG flow (used by `TDBG` tab and `core.TdbgSession`)

Same gating as GPIO — only available between `ARDUINO_ERASE_READY` and `ARDUINO_ERASE_TRIGGER`. Wire format:

- `TDBG_LOAD <pin> <num_events> <initial_state>` → `TDBG_READY` → host writes `num_events × 5` raw bytes (`<I` delta_cycles + `B` state per event) → MCU replies `TDBG_LOADED <crc16_hex>` (CRC-16/CCITT-FALSE) or `TDBG_ERROR <reason>`
- `TDBG_PLAY` or `TDBG_PLAY_LOOP <n>` (n=0 = infinite) → `TDBG_PLAY_STARTED` → playback → `TDBG_PLAY_DONE`
- `TDBG_STOP` (during playback) → drained inside long-gap windows only → `TDBG_STOPPED` followed by `TDBG_PLAY_DONE`

Playback engine sits in `tdbgPlayOnce()`. Three implementation details that look like they could be simplified but can't:

1. **`noInterrupts()` is per-event, not whole-playback.** Spin-and-write is masked, but between events Serial RX / SysTick / USB CDC keep running so STOP can be received in long-gap windows.
2. **Direct PIO `SODR/CODR`, not `digitalWrite()`.** Single-cycle store, deterministic to within ±1 CPU cycle (~12 ns). `digitalWrite()` adds ~100 ns variable latency.
3. **`deadline += delta` accumulates**, never `deadline = now + delta`. This absorbs ISR jitter without long-term drift; the `int32_t (deadline - DWT->CYCCNT) > 0` comparison handles 32-bit CYCCNT wrap-around (~51 s at 84 MHz).

Buffer is `TDBG_MAX_EVENTS × 5 = 20480 bytes` of static RAM. `Serial.setTimeout(5000)` is bumped during the load read because 20 KB at 115200 baud takes ~1.7 s — exceeds the default 1 s and would otherwise short-read.

### Constants that must stay in sync

| Constant | `.ino` | `binFileTransfer_core.py` |
|---|---|---|
| Baud | `UART_BAUDRATE` | `BAUD` |
| Chunk size | `CHUNK_SIZE` (4096) | `CHUNK_SIZE` |
| Total size | `EXPECTED_CHUNKS` × `CHUNK_SIZE` | `FILE_SIZE_SUPPORT` |
| Handshake strings | `strEraseReady` … `strError` (9 of them, incl. `strVerifyRequest` / `strTransferDone`) | `MCU_ERASE_READY` … `MCU_ERROR` |
| GPIO strings | inline string literals in `handleGpioSet/Read` | inline literals in `GpioSession` |
| TDBG strings | inline literals in `handleTdbgLoad/Play` | `MCU_TDBG_*` constants in `TdbgSession` |
| TDBG buffer cap | `TDBG_MAX_EVENTS` (4096) | `TDBG_MAX_EVENTS` |
| TDBG event format | `tdbgBuf` packs `uint32_le delta + uint8 state` | `tdbg_pack_events()` uses `struct.pack('<IB', ...)` |
| CRC poly for TDBG | `tdbgCrc16` (CCITT-FALSE) | `tdbg_crc16` (asserted on import) |

If you change a string, grep both files. The CLI sets `FILE_NAME = "firmware.bin"` as the only host-side default the core itself doesn't know.

## Hardware wiring assumptions baked into the sketch

- `addrPins[]` — 19 entries; **array index = address bit**, not numeric pin order. Index 0 is A0, regardless of which Due pin number lives there.
- `dataPins[]` — 8 entries, same convention (index 0 = DQ0).
- `CE_PIN=43`, `OE_PIN=39`, `WE_PIN=25`.
- Idle bus: `CE=LOW, OE=HIGH, WE=HIGH`. CE stays low for the entire session; only OE/WE toggle per byte.
- Due IO is **3.3 V**. SST39LF/VF010 require 3.0–3.6 V Vdd. Don't swap to a 5 V Mega without level shifters.

If wiring changes, only the two arrays move. The bit-banging code indexes through them.

## Things that look like bugs but aren't (or are, and matter)

- **`Serial.readString()` in `setup()`** depends on the default 1 s serial timeout to terminate. Don't "fix" it to `readStringUntil('\n')` without also changing the host.
- **`RECEIVED_DATA_TIMEOUT` (10 s)** is a fallback, not the primary terminator. The current host sends `ARDUINO_TRANSFER_DONE_SIGNAL` to end transfer immediately. Touching the timeout only matters if you also remove the explicit signal.
- **Host always pads to 128 KB.** The sketch programs the full chip; original file size is unrecoverable. SST39xF020/040 needs both sides updated (more address pins, larger size, ID table).
- **`verifyReadData()` is dead code.** It's defined but never called — superseded by the CRC32 sweep. Leave it alone unless asked to delete.
- **GUI GPIO tab does not auto-Read on connect.** The Read column starts as `??` until the user clicks `Read All`. An in-flight Read All is interruptible: `_begin_disconnect` sets `_abort_event` and flushes `_cmd_queue`, so Disconnect bails after at most one pending pin's serial timeout instead of 66.
- **TDBG `noInterrupts()` masks per event, not the whole playback.** Looks aggressive but is necessary so STOP is reachable mid-loop. See "TDBG flow" above for the three reasons the inner loop is shaped this way.
- **TDBG_LOAD takes `initial_state` as a separate arg, not implicit from event 0.** The captured trace's first row is the pre-trigger sample (often equal to event 0's state, but not always — if the trace starts mid-level, the first event has the same state as initial, intentionally producing a no-op transition that establishes timing anchor without an edge).
- **Pin selection in the TDBG tab has no default.** User must pick each session — flash-bus pins are annotated `(WE#)` / `(A0)` / `(DQ3)` etc. but not blocked. Driving a flash-bus pin via TDBG corrupts the bus, same caveat as GPIO mode.

## CI / release

`.github/workflows/build-release.yml` builds `SST39FlashProgrammer.exe` (PyInstaller `--onefile`, Python 3.12, Windows runner) on every push to `main` that touches non-doc files, and on `workflow_dispatch`. Onefile was chosen for distribution simplicity (single .exe, no surrounding folder) at the cost of a 5-10 s `%TEMP%` extraction on every cold launch — the GUI's lazy imports / background port scan / lazy GPIO panel only help once Python is running, they don't speed up the bootloader. There was a brief onedir+zip experiment (commit 11a5e0a, reverted) that's only relevant for understanding old release assets. Each build creates an `Auto build build-<ts>-<sha>` release marked Latest; the prune step keeps EXE assets (and any leftover legacy `.zip`) only on the 4 newest auto-builds (older release notes/tags survive). Manually-tagged semver releases (`v0.1.0`-style) are untouched by the prune.
