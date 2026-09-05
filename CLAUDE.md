# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Workflow rules

- Commit changes directly on `main` and push. Do not create feature branches or PRs unless the user explicitly asks for one.

## What this repo is

A utility that programs SST39xF010-family parallel NOR flash chips using an **Arduino Due** as the bit-banged programmer. The PDF datasheet is in `spec/`.

- `binFileProgram/binFileProgram.ino` — sketch on the Due. Drives 19 address pins, 8 data pins, CE/OE/WE. Also exposes a single-pin GPIO debug protocol, a TDBG waveform-replay protocol (load captured pattern into RAM, play on a chosen pin via DWT timing), a RECORD live-capture protocol (interrupt-driven multi-pin recorder, 1–4 pins, 4096 events × 5 bytes RAM), and an SGPIO passive decoder (SFF-8485; taps SClock/SLoad/SDataOut as inputs, decodes frames, streams changed frames to the host).
- `binFileTransfer_core.py` — host-side library. All handshake logic, port detection, timeout handling, CRC32 verify, `GpioSession`, `TdbgSession`, `RecordSession`, `parse_acute_txt`, `parse_record_blob` live here. The two front-ends are thin shells.
- `binFileTransfer.py` — CLI front-end (argparse around `core.program_firmware()`). No TDBG/RECORD sub-commands yet; `TdbgSession` / `RecordSession` are library-only.
- `binFileTransferGui.py` — **PySide6/Qt** GUI front-end (rewritten from Tkinter). Dark left sidebar nav + light card-based content area, blue accent. Four pages: `燒錄 ROM` (flash), `GPIO 設定` (manual pin poker with per-row read dot + Read All + auto-read timer), `TDBG` (pin picker + three one-click preset waveforms TDBG1/2/3 via `TDBG_PRESET`), `波形錄製` (live multi-pin recorder with fit-to-width waveform preview: zoom toolbar, HIGH/LOW rails, drag-pan). Split across sibling modules: `gui_theme.py` (QSS stylesheet + the `PALETTE` colour dict), `gui_widgets.py` (`Sidebar` / `Card` / `LogPane` / `PinRow` / `PinGrid`; re-exports the waveform widgets), `gui_waveform.py` (QPainter `WaveformView` + the shared `open_waveform_preview` dialog), `gui_worker.py` (the threaded `Worker` + `Page` base with the `_on_worker_failure` reset hook), `gui_pages.py` (the four page classes), and `gui_data.py` (flash-pin constants, `TDBG_PIN_LABELS`, `format_us`). `binFileTransferGui.py` itself is the `QMainWindow` + the shared-serial connection manager. `binFileTransfer_core.py` is shared by CLI and GUI.

`README.md` is the authoritative end-user doc (Chinese). When a question is about *user-facing behaviour* — wiring tables, CLI flags, troubleshooting — read it. When it's about *internal coupling* between the sketch and host, this file is faster.

## Commands

```bash
# Install host deps
pip install -r requirements.txt        # pyserial + PySide6

# Run CLI (auto-detects Due Programming Port via VID 0x2341 / PID 0x003D)
python binFileTransfer.py
python binFileTransfer.py --port COM19 --file ./builds/v1.bin --timeout 60

# Run GUI
python binFileTransferGui.py
```

The sketch is uploaded with the Arduino IDE: board **Arduino Due (Programming Port)** — auto-detection only matches the programming port, not the native USB port.

```bash
# Run the offscreen regression suite (no hardware, no display needed)
python tests/run_all.py            # all 8 modules
python tests/run_all.py smoke      # filter by name
```

`tests/` covers everything mockable: wire-level protocol round trips for all four flows (incl. failure paths), the RECORD live-thread handoff, TDBG load/play, every known stuck-state regression, waveform rendering, and a 12-step GUI smoke. See `VERIFICATION.md` for the full scenario matrix — including which scenarios still need hardware-in-the-loop (flash a known `firmware.bin`, watch the CRC32 match; TDBG has the calibration-pattern self-test noted at the end of "TDBG flow"). Run the suite before every push — anything red means a regression.

## Host-side concurrency

Each connectable page (GPIO / TDBG / RECORD) owns a `Worker` QObject living on its own `QThread` (`gui_pages.py`). The page submits the blocking session call as a callable via a queued signal (`Worker.submit` → `_run` signal → `_exec` on the worker thread). Results / log lines / live states marshal back to the GUI thread via Qt signals (`readSig`, `logSig`, `liveSig`, `doneSig`, `stopSig`, …). The shared rule: **never touch a Qt widget from a worker thread — always go through a queued signal.** The App-level shared-serial connect/disconnect runs on a dedicated `Worker` too, with `connDoneSig` / `disDoneSig` marshalling completion back.

Two failure-recovery layers exist (added in the stability pass): each page can override `Page._on_worker_failure(detail, tb)` (`gui_worker.py`) to reset its transient UI state when a worker job raises, and the per-call `try/except` blocks in the page `cmd()` closures remain the primary defence. Connect handshake logs route through `statusSig` (never touch widgets from the worker thread).

`RecordSession` adds a second daemon thread `_live_loop` that reads serial during recording. After the user clicks 結束:
1. `_live_loop` reads the first non-`RECORD_LIVE` line (typically `RECORD_STOPPED`), parks it in `_stop_handoff`, and **exits** — this is critical, because the binary blob that follows must not be split between two threads' `ser.readline()` calls. (The blob can contain `0x0A` bytes; if `_live_loop` were still polling it would consume them as fake newlines, and the host would see truncated `ORD_DONE` instead of `RECORD_DONE`.)
2. Main thread's `RecordSession.stop()` reads the parked line, then `RECORD_DATA <count>`, then `ser.read(count*5)` raw bytes, then `RECORD_DONE <crc16>`.

Forced shutdown (Disconnect mid-recording) goes through `_live_stop.set()` + `join()`; the `while not _live_stop.is_set()` check at the top of `_live_loop` honours that path.

## Architecture: protocol coupling

`binFileProgram/binFileProgram.ino` and `binFileTransfer_core.py` are tightly coupled by **five** string-based serial protocols at 115200 8N1 (Flash, GPIO, TDBG, RECORD, SGPIO). Both sides must change together — the host has no version negotiation, mismatched constants just timeout.

### Flash flow (8 strings, primary path)

1. Sketch boots → reads SST software ID (vendor `0xBF`, device `0xB5` SF010 / `0xD5` LF010). On match: emits `ARDUINO_ERASE_READY`. On mismatch: sets `gChipDetected=false` but still emits `ARDUINO_ERASE_READY` so GPIO/TDBG/RECORD remain testable without a chip wired; only `ARDUINO_ERASE_TRIGGER` checks the flag and rejects with `ARDUINO_ERROR` (see "Things that look like bugs but aren't" → `readSoftwareID()`).
2. Host sends `ARDUINO_ERASE_TRIGGER`.
3. Sketch chip-erases, samples first 1 KB == `0xFF`, emits `ARDUINO_READY_TO_RECEIVED_DATA`.
4. Host pads `firmware.bin` to 128 KB (with `0xFF`), sends 32 × 4 KB chunks. After each chunk it waits for `ARDUINO_RECEIVED_LINE_DONE`. The sketch skips programming `0xFF` bytes (chip is freshly erased → already `0xFF`; the CRC sweep still reads those addresses so a bad erase is caught, not masked). `program_firmware(on_progress=...)` reports each ACKed chunk — the GUI's progress bar; CLI passes nothing.
5. Host sends `ARDUINO_VERIFY_REQUEST <crc32_hex>` → sketch sweeps the chip with bitwise CRC32 (~1.3 s for 128 KB) → `ARDUINO_VERIFY_OK`, or on mismatch `ARDUINO_VERIFY_BLOCKS <32×8-hex>` (per-4KB-block CRCs, parsed by the host's `_diagnose_verify_failure` into a located diagnosis) followed by `ARDUINO_ERROR`. **CRC32 is the primary end-to-end integrity check; the inner check is per-byte Data# polling in `programChunkData` (the old per-chunk read-back compare was retired). Note the inner check verifies what *arrived*, not what the host *sent* — the chunk stream has no wire-level checksum, so transit corruption is only caught by the final CRC.**
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
- `TDBG_PRESET <n> <pin>` (n=1..3) → MCU bit-bangs one of three hard-coded 64-bit "password" waveforms on `pin` (fully-unrolled `sendTdbgPreset1/2/3`, nop-calibrated ~333 ns/bit, interrupts off ~50-100 µs) → `TDBG_PRESET_OK <n>` or `TDBG_ERROR <why>`. **This is what the GUI's TDBG page now exposes** (three one-click buttons + a pin picker); the `TDBG_LOAD`/`TDBG_PLAY` capture-replay path below stays as library API (`TdbgSession.load`/`play`) but is no longer surfaced in the GUI. The three patterns differ only in their last byte (`…B7AD` / `…B7AB` / `…B7D5`); they're hard-coded on the MCU, so the host only sends the index.

Playback has TWO engines, selected by `#define TDBG_USE_TC_ENGINE` near the top of the TDBG block:

- **TC engine (default, 1)** — `tdbgPlayOnceTc()`. SAM3X TC2 channel 0 in waveform mode at MCK/2 = 42 MHz. Compare-match (CPCS) interrupt fires `TC6_Handler`, which writes the pin via direct `PIO_SODR/CODR` and re-arms `TC_RC` for the next event. Long deltas (>65535 TC ticks ≈ 1.56 ms) are split into ~780 µs chunks by the ISR — chunked iterations don't write the pin, only the final residue chunk does. Main loop spins on `tdbgPlayDone` polling for `TDBG_STOP`. Interrupts ON the entire time, so USB CDC / Serial RX / SysTick keep working. Floor: ~50-60 CPU cycles per event = ISR round-trip.

- **Spin engine (legacy, 0)** — `tdbgPlayOnceSpin()`. `noInterrupts()` hoisted to *cluster* scope (held across runs of short pulses, released for long gaps so STOP is reachable). DWT->CYCCNT spin-wait. Floor: ~30 cycles per event = loop body cost, but jitter from any unmasked interrupt landing in a cluster collapses the burst into a uniform comb. Kept as instant-rollback path; not the production engine.

Channel choice for TC engine: TC2 channel 0 (peripheral ID `ID_TC6`, vector `TC6_Handler`). Avoids Servo (TC4 = TC1 ch1), Tone (TC0 ch0), and the lazy `analogWrite()` PWM mapping. `tdbgEnableTc()` is idempotent — called from `handleTdbgLoad`, sets up PMC clock and NVIC priority 0.

Three constants/decisions that look arbitrary but aren't:

1. **`deadline += delta` accumulates**, never `deadline = now + delta`. The TC engine's ISR uses `tdbgTcDeadline += chunk_or_delta` to maintain absolute scheduling; the spin engine does the same against DWT->CYCCNT. Both absorb ISR jitter without long-term drift. The `int32_t (deadline - now) > 0` comparison in the spin path handles 32-bit wrap-around (~51 s at 84 MHz).
2. **Direct PIO `SODR/CODR`, not `digitalWrite()`.** Single-cycle store, deterministic to within ±1 CPU cycle (~12 ns). `digitalWrite()` adds ~100 ns variable latency.
3. **`TDBG_TC_CHUNK_TC = 32768`.** Half of the 16-bit counter range, leaves margin for the next chunk's RC arm to be honoured before the counter wraps. Matches `TDBG_TC_CHUNK_CPU = 65536` (= chunk_tc × 2).

Buffer is `TDBG_MAX_EVENTS × 5 = 20480 bytes` of static RAM. `Serial.setTimeout(5000)` is bumped during the load read because 20 KB at 115200 baud takes ~1.7 s — exceeds the default 1 s and would otherwise short-read.

`tdbg_calibration_pattern()` in `core` returns a 4-burst square wave (deltas 100 / 200 / 500 / 1000 cycles, 1 ms separators). The GUI's 「校準」 button next to 「送出」 sends it. All deltas are well above the TC engine's ~60-cycle floor — if a logic-analyzer trace shows four distinct period groups, the engine is honouring deltas. If it looks like a uniform comb regardless of group, the engine is broken. This is the only end-to-end self-test for the playback path (no automated harness).

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

### SGPIO flow (used by `SGPIO` tab and `core.SgpioSession`)

Passive SFF-8485 decoder — the Due taps three signals as INPUTS and never drives the bus (RX-only; SDataIn is untouched). Same pre-erase gating as GPIO/TDBG/RECORD. Wire format:

- `SGPIO_START <sclk> <sload> <sdata> <rising 0|1> <sloadActiveHigh 0|1> <frameLen>` → MCU `attachInterrupt` on SClock (RISING/FALLING per arg) → `SGPIO_STARTED` (or `SGPIO_ERROR <why>`)
- Decoding: `sgpioIsr()` samples one SDataOut bit per SClock edge; when SLoad is sampled asserted, the accumulated bits form one frame (ping-pong buffered) and that edge's bit starts the next frame. `sgpioPump()` (idle loop) emits `SGPIO_FRAME <bitstring>` (ASCII `0`/`1`, first char = first bit sampled) **only when the frame content changes**, plus a ~200 ms heartbeat. Buffer contention (pump too slow) → `SGPIO_OVERRUN`.
- `SGPIO_STOP` → detach interrupt → `SGPIO_STOPPED`.

Frame **semantics live entirely on the host** — the MCU captures raw bits; `parse_sgpio_frame(bitstring, SgpioFraming)` splits them into per-drive Activity/Locate/Fault. `sgpio_frame_valid()` is the structural "is the data right?" check (length must equal `header_bits + num_drives × bits_per_drive`). All framing is GUI-editable, so a vendor variant or an off-by-one SLoad convention is a settings change, not a reflash — a wrong length is *flagged*, never silently mis-decoded.

RECORD and SGPIO are mutually exclusive (both own `attachInterrupt`): each `handle*Start` refuses while the other is active (`sgpioActive` is non-static + forward-declared before `handleRecordStart` so the RECORD-side guard compiles despite the .ino ordering), and the GUI greys out the sibling capture tabs via `App.set_recording(active, origin=<page>)`.

### Constants that must stay in sync

| Constant | `.ino` | `binFileTransfer_core.py` |
|---|---|---|
| Baud | `UART_BAUDRATE` | `BAUD` |
| Chunk size | `CHUNK_SIZE` (4096) | `CHUNK_SIZE` |
| Total size | `EXPECTED_CHUNKS` × `CHUNK_SIZE` | `FILE_SIZE_SUPPORT` |
| Handshake strings | `strEraseReady` … `strError` (9 of them, incl. `strVerifyRequest` / `strTransferDone`) | `MCU_ERASE_READY` … `MCU_ERROR` |
| Fast-connect ping | `"ARDUINO_PING"` in the idle dispatcher (replies `strEraseReady` when not recording) | `HOST_PING_CMD` in `_open_and_wait_idle` |
| Verify block report | `"ARDUINO_VERIFY_BLOCKS"` + 32×8-hex in `verifyRomCrc32` (mismatch only, before `strError`) | `MCU_VERIFY_BLOCKS_PREFIX`, parsed by `_diagnose_verify_failure` |
| GPIO strings | inline string literals in `handleGpioSet/Read` | inline literals in `GpioSession` |
| TDBG strings | inline literals in `handleTdbgLoad/Play` | `MCU_TDBG_*` constants in `TdbgSession` |
| TDBG preset | `TDBG_PRESET <n> <pin>` / `TDBG_PRESET_OK <n>` in `handleTdbgPreset` | `TdbgSession.send_preset` / `MCU_TDBG_PRESET_OK_PREFIX` |
| TDBG buffer cap | `TDBG_MAX_EVENTS` (4096) | `TDBG_MAX_EVENTS` |
| TDBG event format | `tdbgBuf` packs `uint32_le delta + uint8 state` | `tdbg_pack_events()` uses `struct.pack('<IB', ...)` |
| CRC poly for TDBG/RECORD | `tdbgCrc16` (CCITT-FALSE) — reused by RECORD blob | `tdbg_crc16` (asserted on import) — reused by RECORD |
| RECORD strings | inline literals in `handleRecordStart/Stop` / `recordPump` | `MCU_RECORD_*` constants in `RecordSession` |
| RECORD buffer cap | `RECORD_MAX_EVENTS` (4096) | `RECORD_MAX_EVENTS` |
| RECORD event format | `recordBuf` packs `uint32_le delta_us + uint8 mask` | `parse_record_blob()` uses `struct.unpack_from('<IB', ...)` |
| RECORD pin cap | `RECORD_MAX_PINS` (4) | `RECORD_MAX_PINS` |
| SGPIO strings | inline literals in `handleSgpioStart/Stop` / `sgpioPump` | `MCU_SGPIO_*` constants in `SgpioSession` |
| SGPIO start args | `SGPIO_START <sclk> <sload> <sdata> <rising> <activehigh> <frameLen>` order | same order built in `SgpioSession.start` |
| SGPIO frame cap | `SGPIO_MAX_FRAME_BITS` (256) | `SGPIO_MAX_FRAME_BITS` (mirrored; `validate_config` rejects a longer frame locally) |
| Max pin number | `gpioGuard()` / RECORD / SGPIO handlers bound to 65 | `MAX_DUE_PIN` via `_Session._check_pin()` |
| GPIO capture refusal | `"GPIO_ERROR capture_active"` in `gpioGuard` | surfaces as the reply mismatch in `GpioSession` |

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
- **FF-skip trades an exact erase-failure address for a 4 KB block guess — accepted.** `programChunkData` skips `0xFF` bytes, so an unerased cell at an address whose image byte is `0xFF` is no longer caught immediately by Data# polling (which used to report `Byte program timeout at 0x<addr>` and halt). It now surfaces at the end of the run as a CRC mismatch, localised by `_diagnose_verify_failure` to a 4 KB block. `doChipErase` only samples the first 1 KB, so on an image with a large `0xFF` tail that tail is exactly where erase faults are least precisely reported. This is the accepted cost of ~18 s saved per flash; the fault is still *caught*, just less precisely located.
- **The GPIO handlers refuse during a capture, and the guard has to be forward-declared.** `handleGpioSet` / `handleGpioRead` call `gpioGuard()`, which checks `recordActive || sgpioActive` (a GPIO command mid-capture would race the reply parsing and, for SGPIO, drive the very pins being passively tapped) and bounds the pin to 0..65 (`pinMode()` indexes `g_APinDescription[]` unchecked). Both flags are **non-static with an `extern` forward declaration above the GPIO handlers** — the .ino defines them hundreds of lines later, and C++ has no tentative definitions, so `static` + a second declaration would not compile.
- **The connection indicator takes a semantic level, not a colour.** `_set_conn(text, level)` maps through `MainWindow._CONN_COLORS` to the palette's *bright* group. The `*_dark` variants are for text on light backgrounds and measure 2.8-3.4:1 on the dark sidebar — below WCAG AA. Nine call sites previously passed raw `*_dark` colours and every one was wrong; the level API makes that unrepresentable. `QLabel#ConnDot` also carries a default colour in QSS because nothing calls `set_connection()` before the first connect.
- **`_ensure_page` inherits the capture lock, not just the connection.** `MainWindow._capture_page` records which page owns a RECORD/SGPIO capture. `set_recording()` can only reach pages that already exist, so a page first opened *during* a capture would otherwise come up fully live and race the capture's live reader for the shared port.
- **GUI GPIO page does not auto-Read on connect.** Rows start with an unlit read dot until the user clicks the dot (single-pin `GPIO_READ`), `Read All`, or enables the auto-read timer. An in-flight Read All is interruptible: disconnect sets the page's `_abort` event so the loop bails after at most one pending pin's `GPIO_READ_TIMEOUT_S` (1 s) instead of 66.
- **TDBG default engine is `tdbgPlayOnceTc` (compare-interrupt), not the DWT spin loop.** The spin loop (`tdbgPlayOnceSpin`) is kept behind `#define TDBG_USE_TC_ENGINE 0` for instant rollback but its loop body itself eats ~30 cycles, so it can't honour deltas that small no matter how interrupts are masked. The TC engine's floor is ISR latency (~50-60 cycles), and crucially the floor is *deterministic* — interrupt jitter doesn't compound across events because each event is scheduled absolutely from `tdbgTcDeadline`, not relative to "where we got to in the loop". See "TDBG flow" above.
- **Don't try to fold the long-gap chunking out of the TC ISR.** SAM3X TC channels are 16-bit native — a single RC compare can't reach more than 65535 ticks (~1.56 ms at 42 MHz). The chunking is what lets us schedule a 670 ms gap without falling off the end of the counter; collapsing it into one giant RC arm would silently miss compare matches.
- **TDBG_LOAD takes `initial_state` as a separate arg, not implicit from event 0.** The captured trace's first row is the pre-trigger sample (often equal to event 0's state, but not always — if the trace starts mid-level, the first event has the same state as initial, intentionally producing a no-op transition that establishes timing anchor without an edge).
- **Pin selection in the TDBG tab has no default.** User must pick each session — flash-bus pins are annotated `(WE#)` / `(A0)` / `(DQ3)` etc. but not blocked. Driving a flash-bus pin via TDBG corrupts the bus, same caveat as GPIO mode.
- **RECORD has 4 ISR trampolines, not one.** Don't try to consolidate `recordIsr0..3` into a single `recordIsrCommon(slot)` — `attachInterrupt()` takes a `void(*)(void)`, no userdata, so each pin needs its own thunk. The thunks are a one-line forward and the compiler inlines `recordIsrHandler` in practice.
- **Mask bit ordering follows the host's `RECORD_START` pin list, not pin numbers.** `pin1` = bit 0, regardless of whether pin1 is D7 or D44. `parse_record_blob(blob, pins)` does the reverse mapping.
- **`波形錄製` Start button enables on combo selection only.** Adding a pin row via `[+]` doesn't enable Start by itself — the user has to actually pick a pin from the combo (`currentIndexChanged` → `_refresh_start`).
- **`readSoftwareID()` no longer halts on missing/unrecognised chip.** Previously the sketch halted in `while(1)` so TDBG/GPIO/RECORD couldn't be tested without a flash chip wired. Now it sets `gChipDetected = false` and lets the idle loop come up; `ARDUINO_ERASE_TRIGGER` **re-probes once** before deciding (so wiring the chip after boot — or after a no-reset fast-connect — just needs another click, no physical reset) and replies `ARDUINO_ERROR` only if the re-probe still finds nothing. The boot banner `FW: arduino_utility build <date> <time>` (compiler stamp) is the canonical way to verify the running .ino matches the source.
- **Pages build lazily, one per first navigation** (`MainWindow._ensure_page`). The 66-row `PinGrid` is the heavy one; lazy build keeps both startup and Connect instant. A page built after connect inherits the live state via `_ensure_page`'s `set_connected(True)` call — don't force-build pages on Connect (that reintroduces a ~200 ms GUI freeze).
- **Fast-connect first, reset fallback second.** `_open_and_wait_idle` opens the port with DTR/RTS held low and probes with `ARDUINO_PING` (no board reset, sub-second when the sketch is already idle). On miss it closes, reopens with default DTR to force the bootloader reset, and waits the full handshake timeout. Consequence: a no-reset connect carries over MCU state (GPIO pin modes, `gChipDetected`) from the previous session — the erase-trigger re-probe exists precisely for that.
- **DWT must be declared manually in the .ino.** Atmel-bundled `core_cm3.h` (Arduino SAM 1.6.x) declares `CoreDebug_Type` but not `DWT_Type`. The `.ino` declares only DWT (CoreDebug stays from CMSIS) inside `#ifndef DWT_BASE` so a future SAM core that exposes DWT silently wins. `<Arduino.h>` must also be included explicitly — the IDE's implicit injection sometimes fails on non-default folder layouts.

## Repo layout convention

The `.ino` MUST sit in a same-named subfolder (`binFileProgram/binFileProgram.ino`) — Arduino IDE 2.x requires it. The host scripts and `.github/workflows/build-release.yml` only build the Python EXE; the `.ino` is uploaded manually via Arduino IDE.

## CI / release

`.github/workflows/build-release.yml` builds `Arduino_Utility.exe` (PyInstaller `--onefile`, Python 3.12, Windows runner) on every push to `main` that touches non-doc files, and on `workflow_dispatch`. Onefile was chosen for distribution simplicity (single .exe, no surrounding folder) at the cost of a 5-10 s `%TEMP%` extraction on every cold launch — the GUI's lazy imports / background port scan / lazy GPIO panel only help once Python is running, they don't speed up the bootloader. There was a brief onedir+zip experiment (commit 11a5e0a, reverted) that's only relevant for understanding old release assets. Each build creates an `Auto build build-<ts>-<sha>` release marked Latest; the prune step keeps EXE assets (and any leftover legacy `.zip`) only on the 4 newest auto-builds (older release notes/tags survive). Manually-tagged semver releases (`v0.1.0`-style) are untouched by the prune.
