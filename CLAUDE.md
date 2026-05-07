# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Workflow rules

- Commit changes directly on `main` and push. Do not create feature branches or PRs unless the user explicitly asks for one.

## What this repo is

A utility that programs SST39xF010-family parallel NOR flash chips using an **Arduino Due** as the bit-banged programmer. The PDF datasheet is in `spec/`.

- `binFileProgram/binFileProgram.ino` — sketch on the Due. Drives 19 address pins, 8 data pins, CE/OE/WE. Also exposes a single-pin GPIO debug protocol, a TDBG waveform-replay protocol (load captured pattern into RAM, play on a chosen pin via DWT timing), and a RECORD live-capture protocol (interrupt-driven multi-pin recorder, 1–4 pins, 4096 events × 5 bytes RAM).
- `binFileTransfer_core.py` — host-side library. All handshake logic, port detection, timeout handling, CRC32 verify, `GpioSession`, `TdbgSession`, `RecordSession`, `parse_acute_txt`, `parse_record_blob` live here. The two front-ends are thin shells.
- `binFileTransfer.py` — CLI front-end (argparse around `core.program_firmware()`). No TDBG/RECORD sub-commands yet; `TdbgSession` / `RecordSession` are library-only.
- `binFileTransferGui.py` — Tkinter GUI front-end. Four tabs: `燒錄 ROM` (flash), `GPIO 設定` (manual pin poker, persistent connection), `TDBG` (waveform replay), and `波形錄製` (live multi-pin recorder). Custom `ttk.Style` on TNotebook (theme = clam, bold + blue selected tab) so the active tab is visible at a glance.

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

`binFileProgram/binFileProgram.ino` and `binFileTransfer_core.py` are tightly coupled by **four** string-based serial protocols at 115200 8N1 (Flash, GPIO, TDBG, RECORD). Both sides must change together — the host has no version negotiation, mismatched constants just timeout.

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

1. **`noInterrupts()` is hoisted to *cluster* scope.** Held across runs of short pulses (delta < `TDBG_LONG_GAP_CYCLES`), released for long gaps so SysTick / Serial RX / USB CDC stay live and STOP is reachable. Earlier versions masked per-event; that left interrupts ON between consecutive short events and SysTick (1 kHz) firing in those gaps stole hundreds of cycles, blowing the deadline for ~32-cycle (380 ns) inter-pulse intervals and collapsing dense clusters into irregular bursts. Cluster-scope masking is bounded by the captured pattern (typical 50-100 µs), well under the USB CDC stall threshold.
2. **Direct PIO `SODR/CODR`, not `digitalWrite()`.** Single-cycle store, deterministic to within ±1 CPU cycle (~12 ns). `digitalWrite()` adds ~100 ns variable latency.
3. **`deadline += delta` accumulates**, never `deadline = now + delta`. This absorbs ISR jitter without long-term drift; the `int32_t (deadline - DWT->CYCCNT) > 0` comparison handles 32-bit CYCCNT wrap-around (~51 s at 84 MHz).

Buffer is `TDBG_MAX_EVENTS × 5 = 20480 bytes` of static RAM. `Serial.setTimeout(5000)` is bumped during the load read because 20 KB at 115200 baud takes ~1.7 s — exceeds the default 1 s and would otherwise short-read.

### RECORD flow (used by `波形錄製` tab and `core.RecordSession`)

Same gating as GPIO/TDBG — only between `ARDUINO_ERASE_READY` and `ARDUINO_ERASE_TRIGGER`. Wire format:

- `RECORD_START <pin1> [<pin2> ...]` (1..4 pins) → MCU `attachInterrupt(CHANGE)` on each pin → `RECORD_STARTED`
- During recording: every ~100 ms MCU emits `RECORD_LIVE <hex_mask>` from the idle-loop's `recordPump()`. ISRs append `(delta_µs, mask)` to `recordBuf` (`RECORD_MAX_EVENTS × 5 = 20 KB`). Buffer-full → `RECORD_OVERFLOW` (one shot, ISR keeps live going but stops accumulating).
- `RECORD_STOP` → MCU detaches all interrupts, emits `RECORD_STOPPED`, then `RECORD_DATA <count>`, then writes the raw blob, then `RECORD_DONE <crc16_hex>` (CRC-16/CCITT-FALSE, reuses `tdbgCrc16`).

Mask bit `i` = host's pin-list position `i`, **not** the absolute Due pin number. Host's `parse_record_blob(blob, pins)` reverses the mapping into `[(delta_us, {pin: bool}), ...]`.

`RecordSession.start()` spawns a daemon `_live_loop` thread that reads serial lines while recording — RECORD_LIVE messages dispatch into the host's `on_live` callback, anything else (i.e. the start of the stop-exchange) gets parked into `_stop_handoff` for `stop()` to drain.

Implementation details that look like they could be simplified but can't:
1. **Four ISR trampolines** `recordIsr0..3` each calling a common `recordIsrHandler()`. `attachInterrupt()` doesn't accept userdata, so we can't share a single handler across pins; consolidating with a `digitalPinToInterrupt` lookup in the body would add function-pointer-table latency in the ISR.
2. **`recordCurrentMask` and `recordPrevCycles` are `volatile`** — they cross the ISR/loop boundary. Reads from `recordPump` are racy against ISR writes but the values are single bytes / 32-bit aligned words so torn reads aren't possible on Cortex-M3.
3. **Same `tdbgCrc16` reused for the blob.** No need for a separate CRC implementation — the polynomial choice is documented once and validated by the host's import-time self-test.

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
| CRC poly for TDBG/RECORD | `tdbgCrc16` (CCITT-FALSE) — reused by RECORD blob | `tdbg_crc16` (asserted on import) — reused by RECORD |
| RECORD strings | inline literals in `handleRecordStart/Stop` / `recordPump` | `MCU_RECORD_*` constants in `RecordSession` |
| RECORD buffer cap | `RECORD_MAX_EVENTS` (4096) | `RECORD_MAX_EVENTS` |
| RECORD event format | `recordBuf` packs `uint32_le delta_us + uint8 mask` | `parse_record_blob()` uses `struct.unpack_from('<IB', ...)` |
| RECORD pin cap | `RECORD_MAX_PINS` (4) | `RECORD_MAX_PINS` |

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
- **TDBG `noInterrupts()` is held across whole short-pulse clusters, not per-event and not whole-playback.** Per-event masking caused dense 380 ns clusters to collapse when SysTick fired in the unmasked gap between events. Whole-playback masking would block STOP. Cluster-scope (mask while consecutive deltas are < `TDBG_LONG_GAP_CYCLES`, release on the first long gap) is the middle ground: short bursts stay deterministic, long gaps stay interruptible. See "TDBG flow" above for the rest of the inner-loop rationale.
- **TDBG_LOAD takes `initial_state` as a separate arg, not implicit from event 0.** The captured trace's first row is the pre-trigger sample (often equal to event 0's state, but not always — if the trace starts mid-level, the first event has the same state as initial, intentionally producing a no-op transition that establishes timing anchor without an edge).
- **Pin selection in the TDBG tab has no default.** User must pick each session — flash-bus pins are annotated `(WE#)` / `(A0)` / `(DQ3)` etc. but not blocked. Driving a flash-bus pin via TDBG corrupts the bus, same caveat as GPIO mode.
- **RECORD has 4 ISR trampolines, not one.** Don't try to consolidate `recordIsr0..3` into a single `recordIsrCommon(slot)` — `attachInterrupt()` takes a `void(*)(void)`, no userdata, so each pin needs its own thunk. The thunks are a one-line forward and the compiler inlines `recordIsrHandler` in practice.
- **Mask bit ordering follows the host's `RECORD_START` pin list, not pin numbers.** `pin1` = bit 0, regardless of whether pin1 is D7 or D44. `parse_record_blob(blob, pins)` does the reverse mapping.
- **`波形錄製` Start button enables on combo selection only.** Adding a pin row via `[+]` doesn't enable Start by itself — the user has to actually pick a pin from the combo. The `<<ComboboxSelected>>` binding in `_add_pin_row` triggers the refresh.
- **`readSoftwareID()` no longer halts on missing/unrecognised chip.** Previously the sketch halted in `while(1)` so TDBG/GPIO/RECORD couldn't be tested without a flash chip wired. Now it sets `gChipDetected = false` and lets the idle loop come up; `ARDUINO_ERASE_TRIGGER` checks the flag and replies `ARDUINO_ERROR` if FLASH is attempted without a chip. The boot banner `FW: arduino_utility build <date> <time>` (compiler stamp) is the canonical way to verify the running .ino matches the source.

## CI / release

`.github/workflows/build-release.yml` builds `SST39FlashProgrammer.exe` (PyInstaller `--onefile`, Python 3.12, Windows runner) on every push to `main` that touches non-doc files, and on `workflow_dispatch`. Onefile was chosen for distribution simplicity (single .exe, no surrounding folder) at the cost of a 5-10 s `%TEMP%` extraction on every cold launch — the GUI's lazy imports / background port scan / lazy GPIO panel only help once Python is running, they don't speed up the bootloader. There was a brief onedir+zip experiment (commit 11a5e0a, reverted) that's only relevant for understanding old release assets. Each build creates an `Auto build build-<ts>-<sha>` release marked Latest; the prune step keeps EXE assets (and any leftover legacy `.zip`) only on the 4 newest auto-builds (older release notes/tags survive). Manually-tagged semver releases (`v0.1.0`-style) are untouched by the prune.
