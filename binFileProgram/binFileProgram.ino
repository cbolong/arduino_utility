
// Arduino core — provides Serial, pinMode/digitalWrite, plus the CMSIS
// transitive includes (CoreDebug used by tdbgEnableDwt() etc).
// Arduino IDE injects this implicitly for .ino sketches, but on some IDE
// versions / folder layouts the implicit injection doesn't happen, so
// keeping it explicit makes the build deterministic.
#include <Arduino.h>

// TC6_Handler (TC2 ch0 compare-match ISR) is intentionally NOT defined
// here. It lives in a sibling `tdbg_tc_isr.c` file inside the sketch
// folder. Arduino IDE compiles `.c` files with `gcc` (not `g++`), so
// the symbol naturally gets C linkage and bypasses the IDE's auto-
// prototype generator that mangles the vector-table override when
// the handler body sits in a .ino. See the file-header comment in
// `tdbg_tc_isr.c` for the full diagnosis.

// ----- Cortex-M3 DWT cycle counter (manual declaration) ----------------
// Empirically, the Atmel-bundled core_cm3.h shipped with Arduino SAM
// 1.6.x declares CoreDebug_Type but NOT DWT_Type — building against it
// gives "'DWT' was not declared in this scope" while CoreDebug works
// fine. The DWT block is architecturally fixed by ARMv7-M (DWT @
// 0xE0001000 on every Cortex-M3, including the SAM3X8E on the Due), so
// declaring just the registers we touch is safe and produces the same
// machine code as a CMSIS DWT would. Guard with #ifndef DWT_BASE so a
// future SAM core that DOES expose DWT silently wins. We deliberately do
// NOT redeclare CoreDebug_Type — CMSIS provides that and adding our own
// would conflict (see commits dbc9544 → 814938a).
#ifndef DWT_BASE
typedef struct {
  volatile uint32_t CTRL;     // 0x000  Control
  volatile uint32_t CYCCNT;   // 0x004  Cycle Count
} DWT_Type;
#define DWT_BASE                  (0xE0001000UL)
#define DWT                       ((DWT_Type*)DWT_BASE)
#define DWT_CTRL_CYCCNTENA_Msk    (1UL << 0)
#endif


// USER define
#define UART_BAUDRATE 115200
#define CHUNK_SIZE 4096
#define EXPECTED_CHUNKS 32                // 128 KB / CHUNK_SIZE

#define RECEIVED_DATA_TIMEOUT 10000       // 10 sec, kept only as fallback
                                          // (host now sends an explicit
                                          // ARDUINO_TRANSFER_DONE_SIGNAL)

// Note on UART baud: started at 500000 to cut transfer time but the
// ATmega16U2 firmware on some Arduino Due boards (clones in particular)
// does not generate a clean 500000 baud UART even though the SAM3X side
// can — output ends up garbled in Serial Monitor and the host's
// _wait_for_line times out waiting for ARDUINO_ERASE_READY. 115200 is the
// known-stable rate. If your specific board handles 500000 cleanly you
// can bump this back here AND in binFileTransfer_core.py:BAUD; the rest
// of the speed wins (CRC32 verify, Data# Polling, no 10 s timeout, no
// cosmetic LED delay) are independent of baud.



// MCU/PYTHON Handshake Commands
const char* strEraseReady = "ARDUINO_ERASE_READY";
const char* strEraseTrigger = "ARDUINO_ERASE_TRIGGER";
const char* strReadyStart = "ARDUINO_READY_TO_RECEIVED_DATA";
const char* strLineReceivedResponse = "ARDUINO_RECEIVED_LINE_DONE";
const char* strVerifyRequest = "ARDUINO_VERIFY_REQUEST";   // followed by " <crc32_hex>"
const char* strVerifyOK = "ARDUINO_VERIFY_OK";
const char* strTransferDone = "ARDUINO_TRANSFER_DONE_SIGNAL";
const char* strTransferCompleted = "ARDUINO_DATA_COMPLETED";
const char* strError = "ARDUINO_ERROR";



// Support Device list
const uint8_t vendorID = 0xBF;
const uint8_t deviceID_SST39SF010 = 0xB5;
const uint8_t deviceID_SST39LF010 = 0xD5;


// Pin define
const int addrPins[] = {44, 42, 40, 38, 36, 34, 32, 30, 33, 35, 41, 37, 28, 31, 29, 26, 24, 27, 22};
const int addrPinsCount = sizeof(addrPins) / sizeof(addrPins[0]);
const int dataPins[] = {46, 48, 50, 53, 51, 49, 47, 45};
const int dataPinsCount = sizeof(dataPins) / sizeof(dataPins[0]);

const int CE_PIN = 43;
const int OE_PIN = 39;
const int WE_PIN = 25;


// Global Variable
unsigned char buffer[CHUNK_SIZE];
// read_buffer[] removed: per-chunk readback replaced by single end-of-stream
// CRC32 verify (see verifyRomCrc32). Saves 4 KB MCU RAM.

uint16_t bytesRead = 0;
uint32_t chunkCount = 0;

// Recevied Data Timeout
unsigned long lastRecvTime = 0;      // last received data time
bool isTransferring = false;         // is transferring flag

// Chip-detect outcome from readSoftwareID(). When false, FLASH mode is
// blocked (ARDUINO_ERASE_TRIGGER replies ARDUINO_ERROR) but TDBG / GPIO /
// RECORD modes work — those don't touch the parallel-flash bus.
bool gChipDetected = true;

// Timing instrumentation (cumulative, ms). Reset on transfer start.
unsigned long t_recv_start_ms = 0;   // millis() when strReadyStart was sent
unsigned long t_program_total_ms = 0;
unsigned long t_read_total_ms = 0;
unsigned long t_compare_total_ms = 0;


void processChunk(uint32_t num) {
  digitalWrite(LED_BUILTIN, HIGH);
  Serial.print("Chunk ");
  Serial.print(num);
  Serial.print(" received    | First 8 bytes: ");
  for (int i = 0; i < 8; i++) {
    if (buffer[i] < 0x10) Serial.print("0");
    Serial.print(buffer[i], HEX);
    Serial.print(" ");
  }
  Serial.println();
  // Cosmetic 50 ms LED-blink delay removed: 32 × 50 ms = ~1.6 s of pure
  // wait-for-no-reason. The LED toggle stays for visual progress.
  digitalWrite(LED_BUILTIN, LOW);

  unsigned long t0 = millis();
  programChunkData(num);
  unsigned long t1 = millis();

  t_program_total_ms += (t1 - t0);
  // Per-chunk readback + compare retired in favour of one end-of-stream
  // CRC32 sweep (see verifyRomCrc32 below). t_read_total_ms / t_compare_total_ms
  // therefore stay 0 during the chunk loop and are reused by verifyRomCrc32
  // to capture the verify cost.
}

void readSoftwareID() {
  Serial.print("Software ID read : ");

  // Step 1: Software ID Entry
  writeByte(0x5555, 0xAA);
  writeByte(0x2AAA, 0x55);
  writeByte(0x5555, 0x90);
  delayMicroseconds(10); // 等待進入 ID 模式

  // Step 2: Read Vendor ID(Address : 0x0000) and Device ID(Address : 0x0001)
  uint8_t r_vendorID = readByte(0x0000);
  uint8_t r_deviceID = readByte(0x0001);

  Serial.print("Vendor ID: 0x"); Serial.print(r_vendorID, HEX);       // Expect 0xBF (SST)
  Serial.print(", Device ID: 0x"); Serial.println(r_deviceID, HEX);   // Expect 0xB5 (SST39SF010A)

  // Step 3: Software ID Exit
  writeByte(0x5555, 0xAA);
  writeByte(0x2AAA, 0x55);
  writeByte(0x5555, 0xF0);
  delayMicroseconds(10);
  
  if ((r_vendorID == vendorID) && (r_deviceID == deviceID_SST39SF010)) {
    Serial.println("SST39SF010 detected!!");
  }
  else if ((r_vendorID == vendorID) && (r_deviceID == deviceID_SST39LF010)) {
    Serial.println("SST39LF010/SST39VF010 detected!!");
  }
  else {
    Serial.println("EEPROM ID ERROR (TDBG/GPIO/RECORD will still work; "
                   "FLASH mode requires a recognised chip and will reply "
                   "ARDUINO_ERROR if attempted)");
    gChipDetected = false;
    // No more while(1) halt — we drop through and let the idle loop come
    // up. FLASH mode is gated below by gChipDetected.
  }
}

void doChipErase() {
  Serial.println("CHIP ERASE trigger.");
  // --- Step 1: Chip Erase Sequence ---
  writeByte(0x5555, 0xAA); // Cycle 1 (Unlock)
  writeByte(0x2AAA, 0x55); // Cycle 2 (Unlock)
  writeByte(0x5555, 0x80); // Cycle 3 (Setup Command)
  writeByte(0x5555, 0xAA); // Cycle 4 (Unlock)
  writeByte(0x2AAA, 0x55); // Cycle 5 (Unlock)
  writeByte(0x5555, 0x10); // Cycle 6 (Chip-Erase Command)

  // Wait ERASE Complete
  Serial.println("Erasing... Please wait.");
  delay(100); 

  // --- Step 2: Verify Erase (Check if all addresses are 0xFF) ---
  Serial.println("Checking Erase...");
  bool eraseSuccess = true;
  
  // Read 1024 Byte to check ERASE success
  for (uint32_t addr = 0; addr < 1024; addr++) {
    if (readByte(addr) != 0xFF) {
      Serial.print("Erase failed at 0x");
      Serial.println(addr, HEX);
      eraseSuccess = false;
      break;
    }
  }

  if (eraseSuccess) {
    Serial.println("CHIP ERASE SUCCESSFUL!");
  }
  else {
    Serial.println("CHIP ERASE FAILED!");

    while (!Serial);
    Serial.println(strError);
    while (1) {}
  }
}

// Hard ceiling for Data# Polling. Datasheet Tbp max is 20 µs; 100 µs is 5×
// margin. Anything longer means the chip genuinely failed to program — we
// emit ARDUINO_ERROR and halt so the host doesn't get false "OK".
#define PROGRAM_POLL_TIMEOUT_US 100

void programChunkData(uint32_t chunk) {
  uint32_t addr = (chunk - 1) * CHUNK_SIZE;

  Serial.print("Chunk ");
  Serial.print(chunk);
  Serial.print(" programming | Address: 0x");
  Serial.print(addr, HEX);
  Serial.println("...");

  for (int i = 0; i < CHUNK_SIZE; i++) {
    uint32_t targetAddr = (uint32_t)i + (uint32_t)addr;
    uint8_t targetData = buffer[i];

    // Byte-Program Sequence (4-cycle program command)
    writeByte(0x5555, 0xAA); // Cycle 1 (Unlock)
    writeByte(0x2AAA, 0x55); // Cycle 2 (Unlock)
    writeByte(0x5555, 0xA0); // Cycle 3 (Program Command)
    writeByte(targetAddr, targetData); // Cycle 4 (Address & Data)

    // Data# Polling (datasheet section 4 / Figure 7-15): while the chip is
    // still programming, reading targetAddr returns ~DQ7 (the inverse of
    // the value being written); once programming completes the read
    // matches targetData. Exit the wait the moment the chip is ready
    // instead of always sleeping the worst-case 30 µs.
    uint32_t pollStart = micros();
    while ((uint32_t)(micros() - pollStart) < PROGRAM_POLL_TIMEOUT_US) {
      if (readByte(targetAddr) == targetData) {
        goto programmed;
      }
    }

    // Hit the ceiling — programming actually failed.
    Serial.print("Byte program timeout at 0x");
    Serial.print(targetAddr, HEX);
    Serial.print(", expected 0x");
    Serial.println(targetData, HEX);
    while (!Serial);
    Serial.println(strError);
    while (1) {}

    programmed: ;
  }
}

// ----------------------------------------------------------------------------
// GPIO test commands — single-pin manual debug, used by the GUI's "GPIO 設定"
// tab. These are accepted while the MCU is sitting in setup()'s pre-erase
// wait loop. Once the host sends ARDUINO_ERASE_TRIGGER the MCU drops into
// the chip-erase + flash-program flow and stops accepting GPIO commands
// until the next reset.
//
// Wire format (newline-terminated):
//   GPIO_SET <pin> <OUTPUT|INPUT> [HIGH|LOW]   -> reply "GPIO_OK"  or "GPIO_ERROR <why>"
//   GPIO_READ <pin>                            -> reply "GPIO_VALUE <pin> <0|1>"
//                                                 or "GPIO_ERROR <why>"
//
// Caveats: setting CE_PIN / OE_PIN / WE_PIN / address pins / data pins via
// these commands disturbs the IDLE bus state assumed by the flash flow.
// Reset the Due before attempting a flash again.
// ----------------------------------------------------------------------------

void handleGpioSet(const String& cmd) {
  // "GPIO_SET <pin> <mode> [value]"
  int p1 = cmd.indexOf(' ');
  int p2 = cmd.indexOf(' ', p1 + 1);
  if (p1 < 0 || p2 < 0) {
    Serial.println("GPIO_ERROR bad_format");
    return;
  }
  int p3 = cmd.indexOf(' ', p2 + 1);

  int pin = cmd.substring(p1 + 1, p2).toInt();
  String modeStr = (p3 > 0) ? cmd.substring(p2 + 1, p3) : cmd.substring(p2 + 1);
  String valueStr = (p3 > 0) ? cmd.substring(p3 + 1) : String("");

  int mode;
  if (modeStr == "OUTPUT") {
    mode = OUTPUT;
  } else if (modeStr == "INPUT") {
    mode = INPUT;
  } else {
    Serial.println("GPIO_ERROR bad_mode");
    return;
  }

  pinMode(pin, mode);
  if (mode == OUTPUT && valueStr.length() > 0) {
    int value;
    if (valueStr == "HIGH") {
      value = HIGH;
    } else if (valueStr == "LOW") {
      value = LOW;
    } else {
      Serial.println("GPIO_ERROR bad_value");
      return;
    }
    digitalWrite(pin, value);
  }
  Serial.println("GPIO_OK");
}

void handleGpioRead(const String& cmd) {
  // "GPIO_READ <pin>"
  int p1 = cmd.indexOf(' ');
  if (p1 < 0) {
    Serial.println("GPIO_ERROR bad_format");
    return;
  }
  int pin = cmd.substring(p1 + 1).toInt();
  pinMode(pin, INPUT);
  int value = digitalRead(pin);
  Serial.print("GPIO_VALUE ");
  Serial.print(pin);
  Serial.print(" ");
  Serial.println(value);  // 0 or 1
}


// ----------------------------------------------------------------------------
// TDBG (timing debug) waveform playback — replays a captured waveform on a
// chosen GPIO with cycle-accurate timing using the Cortex-M3 DWT cycle
// counter. Like GPIO_*, only available while the MCU sits in setup()'s
// pre-erase wait loop. Once ARDUINO_ERASE_TRIGGER fires, TDBG is gone until
// reset.
//
// Wire format (newline-terminated):
//   TDBG_LOAD <pin> <num_events> <initial_state>
//     -> "TDBG_READY"
//        (host then writes <num_events> * 5 raw bytes:
//         uint32_le delta_cycles + uint8 state, repeated)
//     -> "TDBG_LOADED <crc16_hex>"   (CRC-16/CCITT-FALSE over the blob)
//        or "TDBG_ERROR <reason>"
//   <initial_state> is 0 or 1, the level the pin holds before the first
//   transition. Setting it to the same value as the first event's state
//   means no edge fires for the first transition — that's intentional if
//   the captured trace started mid-level.
//   TDBG_PLAY                  -> "TDBG_PLAY_STARTED" ... "TDBG_PLAY_DONE"
//   TDBG_PLAY_LOOP <n>         -> same; n=0 means infinite loop until STOP.
//   TDBG_STOP                  -> drained inside long-gap windows during
//                                 playback only; replies "TDBG_STOPPED"
//                                 followed by "TDBG_PLAY_DONE".
//
// Timing budget: two engines, switched via TDBG_USE_TC_ENGINE.
//   * Spin (legacy):  DWT->CYCCNT @ 84 MHz, ~11.9 ns per tick, loop body
//     cost ~30 cycles → minimum reliable delta ~30-50 cycles (~360-600 ns).
//     Patterns at 32 cycles ride the floor and collapse to a uniform comb
//     at the loop's natural cadence. Useful as a fallback only.
//   * TC (default):   TC2 ch0 @ 42 MHz (TIMER_CLOCK1), CPCS interrupt fires
//     the pin. ISR round-trip ~50-60 cycles → minimum reliable delta
//     ~60 cycles (~715 ns). 32-cycle patterns still slip but the floor is
//     interrupt latency, not loop body — ports cleanly to a future DMA
//     engine that lifts the floor below 1 cycle (Sprint 3).
// Pin transitions use direct PIO_SODR/PIO_CODR — single-cycle store, no
// digitalWrite() latency.
// ----------------------------------------------------------------------------

#define TDBG_MAX_EVENTS         4096
#define TDBG_EVENT_BYTES        5            // uint32_le delta + uint8 state
#define TDBG_LONG_GAP_CYCLES    840000UL     // ≥10 ms — open service window
#define TDBG_LONG_GAP_BAILOUT   16800UL      // ~200 µs slack before deadline

// Switch between the two playback engines. The TC engine is interrupt-
// scheduled via SAM3X Timer Counter and produces deterministic 50-cycle
// floor jitter on dense bursts; the spin-loop fallback (TDBG_USE_TC_ENGINE
// 0) is the original DWT spin-wait, kept for instant rollback if the TC
// path misbehaves on a particular pattern.
#define TDBG_USE_TC_ENGINE      1

// TC playback runs the channel at MCK/2 = 42 MHz (TIMER_CLOCK1). Stored
// host deltas are in 84 MHz CPU cycles → divide by 2 in the ISR. The
// 16-bit native counter caps single-RC reaches at 65535 ticks ≈ 1.56 ms;
// for longer deltas (long gaps), the ISR splits the wait into chunks
// without writing the pin until the final chunk lands.
#define TDBG_TC_CHUNK_TC        32768U       // 32k ticks ≈ 780 µs per chunk
#define TDBG_TC_CHUNK_CPU       (TDBG_TC_CHUNK_TC * 2U)   // = 65536 CPU cycles

static uint8_t  tdbgBuf[TDBG_MAX_EVENTS * TDBG_EVENT_BYTES];
static uint16_t tdbgEventCount = 0;
static uint8_t  tdbgPin = 0;
static uint8_t  tdbgInitialState = 0;
static volatile bool tdbgStopRequested = false;
static bool tdbgDwtReady = false;
static bool tdbgTcReady = false;

// TC playback state shared with tdbg_tc_isr.c — see file-header comment
// in that .c file for the full rationale on why the ISR lives in a
// separate translation unit. These vars are intentionally NOT static
// (they need cross-TU visibility) and wrapped in an `extern "C"` block
// so the symbol names use C linkage; the ISR's `extern uint32_t
// tdbgMask;` etc. references resolve at link time without C++ name
// mangling getting in the way.
//
// `volatile` on the bits the ISR mutates that main reads (and vice
// versa). 32-bit aligned single-word reads/writes are atomic on
// Cortex-M3 so we don't need a critical section for these.
#ifdef __cplusplus
extern "C" {
#endif
Pio*                    tdbgPort = NULL;
uint32_t                tdbgMask = 0;
volatile const uint8_t* tdbgPlayPtr   = NULL;
volatile uint16_t       tdbgPlayLeft  = 0;
volatile uint16_t       tdbgTcDeadline = 0;
volatile uint32_t       tdbgRemainCpu = 0;
volatile bool           tdbgPlayDone  = false;
#ifdef __cplusplus
}
#endif

static void tdbgEnableDwt() {
  if (tdbgDwtReady) return;
  CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
  DWT->CYCCNT = 0;
  DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
  tdbgDwtReady = true;
}

// One-time init for the TC channel used by the new playback engine.
// TC2 channel 0 (peripheral ID 27 + 6 = 33, ID_TC6, vector TC6_Handler).
// Avoids Servo (TC4 = TC1.ch1), Tone (TC0 = TC0.ch0), and the lazy
// analogWrite() PWM mapping which never picks ch0 of TC2 by default.
// Idempotent — safe to call from every TDBG_LOAD.
static void tdbgEnableTc() {
  if (tdbgTcReady) return;
  pmc_enable_periph_clk(ID_TC6);                 // TC2 ch0 clock gate
  // CMR: waveform mode, count up (no auto-reset), TIMER_CLOCK1 = MCK/2
  TC_Configure(TC2, 0,
               TC_CMR_WAVE
               | TC_CMR_WAVSEL_UP
               | TC_CMR_TCCLKS_TIMER_CLOCK1);
  // Highest priority — only competitor is SysTick (1 kHz), which costs
  // ~50-80 cycles per displaced event. Bounded jitter across a 1.34 s
  // playback is acceptable.
  NVIC_SetPriority(TC6_IRQn, 0);
  tdbgTcReady = true;
}

// CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflect, no xor-out.
// Test vector: tdbgCrc16("123456789", 9) == 0x29B1.
static uint16_t tdbgCrc16(const uint8_t* data, uint32_t len) {
  uint16_t crc = 0xFFFF;
  for (uint32_t i = 0; i < len; i++) {
    crc ^= ((uint16_t)data[i] << 8);
    for (int b = 0; b < 8; b++) {
      if (crc & 0x8000) crc = (uint16_t)((crc << 1) ^ 0x1021);
      else              crc = (uint16_t)(crc << 1);
    }
  }
  return crc;
}

void handleTdbgLoad(const String& cmd) {
  // "TDBG_LOAD <pin> <count> <initial_state>"
  int p1 = cmd.indexOf(' ');
  int p2 = (p1 >= 0) ? cmd.indexOf(' ', p1 + 1) : -1;
  int p3 = (p2 >= 0) ? cmd.indexOf(' ', p2 + 1) : -1;
  if (p1 < 0 || p2 < 0 || p3 < 0) {
    Serial.println("TDBG_ERROR bad_format");
    return;
  }
  int pin = cmd.substring(p1 + 1, p2).toInt();
  long count = cmd.substring(p2 + 1, p3).toInt();
  int initialState = cmd.substring(p3 + 1).toInt();
  if (pin < 0 || pin > 65) {
    Serial.println("TDBG_ERROR bad_pin");
    return;
  }
  if (count < 1 || count > TDBG_MAX_EVENTS) {
    Serial.println("TDBG_ERROR bad_count");
    return;
  }
  if (initialState != 0 && initialState != 1) {
    Serial.println("TDBG_ERROR bad_initial");
    return;
  }

  uint32_t expectedBytes = (uint32_t)count * TDBG_EVENT_BYTES;

  // Cache PIO mapping early so we fail fast on bad pins.
  Pio* port = g_APinDescription[pin].pPort;
  uint32_t mask = g_APinDescription[pin].ulPin;
  if (port == NULL || mask == 0) {
    Serial.println("TDBG_ERROR no_pio");
    return;
  }

  // 20 KB at 115200 baud takes ~1.7 s — bump the timeout above the 1 s
  // default, restore afterwards so other handlers stay snappy.
  unsigned long savedTimeout = Serial.getTimeout();
  Serial.setTimeout(5000);

  Serial.println("TDBG_READY");

  size_t got = Serial.readBytes((char*)tdbgBuf, expectedBytes);
  Serial.setTimeout(savedTimeout);

  if (got != expectedBytes) {
    Serial.print("TDBG_ERROR short_read ");
    Serial.print((unsigned long)got);
    Serial.print("/");
    Serial.println((unsigned long)expectedBytes);
    return;
  }

  uint16_t crc = tdbgCrc16(tdbgBuf, expectedBytes);

  tdbgEventCount = (uint16_t)count;
  tdbgPin = (uint8_t)pin;
  tdbgPort = port;
  tdbgMask = mask;
  tdbgInitialState = (uint8_t)initialState;

  tdbgEnableDwt();
  tdbgEnableTc();

  char hexbuf[8];
  snprintf(hexbuf, sizeof(hexbuf), "%04X", crc);
  Serial.print("TDBG_LOADED ");
  Serial.println(hexbuf);
}

// Drain any pending TDBG_STOP\n from Serial without blocking. Any other
// inbound bytes during playback are discarded — the host shouldn't be
// sending non-STOP traffic while we're playing.
static bool tdbgPumpStop() {
  static char lineBuf[16];
  static uint8_t lineLen = 0;
  while (Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == '\r') continue;
    if (c == '\n') {
      lineBuf[lineLen] = 0;
      bool match = (strcmp(lineBuf, "TDBG_STOP") == 0);
      lineLen = 0;
      if (match) return true;
      continue;
    }
    if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = (char)c;
    } else {
      lineLen = 0;  // overflow, drop
    }
  }
  return false;
}

// ----- Spin-loop playback engine (legacy, kept for rollback) ------------
//
// Interrupt-masking strategy: noInterrupts() is hoisted to *cluster*
// scope, not per-event. Empirically the captured patterns we replay are
// dense bursts of short pulses (~380 ns / 32 cycles each) separated by
// long gaps (≥10 ms = TDBG_LONG_GAP_CYCLES). A per-event mask was the
// previous design — it left interrupts ON between consecutive short
// events, so SysTick (1 kHz) or USB CDC RX could fire in the gap and
// steal hundreds of cycles. With short deltas already at ~32 cycles,
// even one interrupt blows the deadline and the next several events
// fire as fast as the loop body lets them, collapsing what should be
// even 380 ns pulses into an irregular burst.
//
// Cluster-scope masking still leaves a hard floor at ~30 CPU cycles per
// event from the loop body itself (memcpy + compare + spin + fire), so
// patterns with deltas ≤ ~30 cycles can't be replayed faithfully on this
// engine. That's the motivation for the TC engine below.
static bool tdbgPlayOnceSpin() {
  // Pre-set initial level via direct PIO BEFORE flipping output enable, so
  // there's no float-LOW glitch between mode change and first write.
  if (tdbgInitialState) tdbgPort->PIO_SODR = tdbgMask;
  else                  tdbgPort->PIO_CODR = tdbgMask;
  tdbgPort->PIO_PER = tdbgMask;   // PIO control (not peripheral)
  tdbgPort->PIO_OER = tdbgMask;   // output enable

  uint32_t deadline = DWT->CYCCNT;
  const uint8_t* p = tdbgBuf;
  bool masked = false;

  for (uint16_t i = 0; i < tdbgEventCount; ++i) {
    uint32_t delta;
    memcpy(&delta, p, sizeof(delta));
    uint8_t state = p[4];
    p += TDBG_EVENT_BYTES;
    deadline += delta;

    if (delta >= TDBG_LONG_GAP_CYCLES) {
      if (masked) { interrupts(); masked = false; }
      while ((int32_t)(deadline - DWT->CYCCNT) > (int32_t)TDBG_LONG_GAP_BAILOUT) {
        if (tdbgPumpStop()) {
          tdbgStopRequested = true;
          return false;
        }
      }
    }

    if (!masked) { noInterrupts(); masked = true; }
    while ((int32_t)(deadline - DWT->CYCCNT) > 0) { /* spin */ }
    if (state) tdbgPort->PIO_SODR = tdbgMask;
    else       tdbgPort->PIO_CODR = tdbgMask;
  }
  if (masked) interrupts();
  return true;
}

// ----- TC compare-interrupt playback engine -----------------------------
//
// Architecture: SAM3X TC2 channel 0 runs in waveform mode at 42 MHz
// (MCK/2). Each event's deadline is loaded into TC_RC; the channel's
// CPCS interrupt fires at compare match; the ISR writes the pin state
// and arms the next deadline. The 16-bit native counter caps a single
// RC reach at 65535 ticks (~1.56 ms) — for longer deltas, the ISR
// chunks the wait into TDBG_TC_CHUNK_TC-sized pieces, advancing the
// deadline accumulator on each chunk but only writing the pin on the
// final (possibly small) chunk that lands on the actual event time.
//
// Cycle budget per event: ~12 cycles entry + ~6 prologue + ~18 body +
// ~6 epilogue + ~12 exit = ~54 CPU cycles ≈ 27 TC ticks. Deltas under
// 30 TC ticks (~60 CPU cycles, ~715 ns) will slip — the engine simply
// can't service them faster than the ISR round-trip. For the 380 ns
// (~16 tick) target the user's existing pattern uses, that's still not
// enough — but the floor is now interrupt latency, not loop-body cost,
// so a future DMA-driven engine can push beneath it. Sprint 3.

// State machine: tdbgRemainCpu counts CPU cycles still owed BEFORE the
// next pin transition. Each ISR consumes up to TDBG_TC_CHUNK_CPU from
// remain and advances the deadline accordingly. When remain hits 0,
// THAT ISR is the firing one — write pin, load next event's delta into
// remain, schedule the first chunk. Setup mirrors the same logic so the
// first ISR gets to remain==0 only when the first event's full delta
// has elapsed (or immediately, for the conventional delta=0 anchor).
// TC6_Handler (TC2 ch0 compare-match ISR) lives in tdbg_tc_isr.c.
// See that file's header for why a separate .c TU is required (Arduino
// IDE auto-prototype + C++ name mangling were leaving the vector slot
// bound to Dummy_Handler, freezing the MCU on the first CPCS match).

static bool tdbgPlayOnceTc() {
  // Same float-LOW-safe pin priming as the spin engine.
  if (tdbgInitialState) tdbgPort->PIO_SODR = tdbgMask;
  else                  tdbgPort->PIO_CODR = tdbgMask;
  tdbgPort->PIO_PER = tdbgMask;
  tdbgPort->PIO_OER = tdbgMask;

  // Sanity-probe LED setup — D13 (LED_BUILTIN = PB27) starts dark; the
  // ISR will SODR it on at first entry. Disarm paths CODR it off so
  // each play attempt has its own visible "ISR ran" signal.
  PIOB->PIO_PER  = (1u << 27);
  PIOB->PIO_OER  = (1u << 27);
  PIOB->PIO_CODR = (1u << 27);

  // Drain leading delta=0 events synchronously, BEFORE arming TC. The
  // parser at parse_acute_txt anchors playback's t=0 at the first
  // transition by emitting events[0] with delta=0; arming TC with RC=0
  // against a freshly-reset CV=0 is a hardware no-op on SAM3X — the TC
  // compare event is edge-triggered (CV transitioning into equality
  // with RC), and CV: 0→0 produces no edge, so the CPCS interrupt
  // never fires and playback hangs. Treating the delta=0 prefix as
  // setup-time pin writes (analogous to the priming PIO_OER above)
  // lets us arm TC with the first non-zero-delta event, which yields
  // RC ≥ 1 and a clean CV: 0→1→...→RC edge into equality.
  tdbgPlayPtr  = tdbgBuf;
  tdbgPlayLeft = tdbgEventCount;
  tdbgPlayDone = false;

  while (tdbgPlayLeft > 0) {
    uint32_t prefix_delta;
    memcpy(&prefix_delta, (const void*)tdbgPlayPtr, sizeof(prefix_delta));
    if (prefix_delta != 0) break;
    uint8_t pstate = tdbgPlayPtr[4];
    if (pstate) tdbgPort->PIO_SODR = tdbgMask;
    else        tdbgPort->PIO_CODR = tdbgMask;
    tdbgPlayPtr += TDBG_EVENT_BYTES;
    tdbgPlayLeft--;
  }
  if (tdbgPlayLeft == 0) {
    // Degenerate input: every event had delta=0. Pin already reflects
    // the final state from the prefix loop. Nothing to schedule.
    tdbgPlayDone = true;
    return true;
  }

  // First non-zero-delta event ready to arm TC against.
  uint32_t delta_cpu;
  memcpy(&delta_cpu, (const void*)tdbgPlayPtr, sizeof(delta_cpu));
  tdbgTcDeadline = 0;
  tdbgRemainCpu  = delta_cpu;
  uint32_t step = (tdbgRemainCpu > TDBG_TC_CHUNK_CPU)
                  ? TDBG_TC_CHUNK_CPU : tdbgRemainCpu;
  tdbgRemainCpu -= step;
  uint16_t deadline_inc = (uint16_t)(step >> 1);
  if (deadline_inc == 0) deadline_inc = 1;   // RC ≥ 1 floor — see ISR
  tdbgTcDeadline = deadline_inc;
  TC2->TC_CHANNEL[0].TC_RC = tdbgTcDeadline;

  // Arm: enable CPCS interrupt, enable clock, software-trigger reset.
  TC2->TC_CHANNEL[0].TC_IER = TC_IER_CPCS;
  NVIC_ClearPendingIRQ(TC6_IRQn);
  NVIC_EnableIRQ(TC6_IRQn);
  TC2->TC_CHANNEL[0].TC_CCR = TC_CCR_CLKEN | TC_CCR_SWTRG;

  // Main loop spins polling for STOP — interrupts run normally so USB
  // CDC, SysTick, and inbound serial all work. Zero noInterrupts()
  // discipline needed: the ISR is short and self-contained.
  while (!tdbgPlayDone) {
    if (tdbgPumpStop()) {
      // Same disarm sequence as the natural-end disarm path in the
      // ISR — mask + stop + drain SR + clear NVIC pending — so a
      // subsequent re-arm doesn't immediately fire on a stale flag.
      TC2->TC_CHANNEL[0].TC_IDR = TC_IDR_CPCS;
      TC2->TC_CHANNEL[0].TC_CCR = TC_CCR_CLKDIS;
      (void)TC2->TC_CHANNEL[0].TC_SR;
      NVIC_DisableIRQ(TC6_IRQn);
      NVIC_ClearPendingIRQ(TC6_IRQn);
      PIOB->PIO_CODR = (1u << 27); // LED probe off — STOP path
      tdbgStopRequested = true;
      return false;
    }
  }
  return true;
}

// Engine dispatcher — selected at compile time. Keep both functions
// linked even when one is unused; the dead one is ~150 bytes of flash,
// nothing on the SAM3X's 512 KB.
static bool tdbgPlayOnce() {
#if TDBG_USE_TC_ENGINE
  return tdbgPlayOnceTc();
#else
  return tdbgPlayOnceSpin();
#endif
}

void handleTdbgPlay(const String& cmd) {
  if (tdbgEventCount == 0) {
    Serial.println("TDBG_ERROR not_loaded");
    return;
  }

  // Default 1 iteration; TDBG_PLAY_LOOP <n>, n=0 means infinite.
  long iterations = 1;
  bool infinite = false;
  if (cmd.startsWith("TDBG_PLAY_LOOP")) {
    int sp = cmd.indexOf(' ');
    if (sp < 0) {
      Serial.println("TDBG_ERROR bad_format");
      return;
    }
    iterations = cmd.substring(sp + 1).toInt();
    if (iterations < 0) {
      Serial.println("TDBG_ERROR bad_count");
      return;
    }
    infinite = (iterations == 0);
  }

  tdbgStopRequested = false;
  Serial.println("TDBG_PLAY_STARTED");

  for (long n = 0; infinite || n < iterations; ++n) {
    if (!tdbgPlayOnce()) break;        // STOP observed mid-playback
    if (tdbgStopRequested) break;
  }

  if (tdbgStopRequested) {
    Serial.println("TDBG_STOPPED");
  }
  Serial.println("TDBG_PLAY_DONE");
}


// ----------------------------------------------------------------------------
// RECORD (live waveform capture) — interrupt-driven multi-pin recorder. Only
// active during the pre-erase idle window (same gating as GPIO / TDBG).
//
// Wire format:
//   RECORD_START <pin1> [<pin2> ... up to 4]
//     -> "RECORD_STARTED"
//        ... while recording, every ~100 ms:
//        "RECORD_LIVE <hex_mask>"   (current state of selected pins)
//   RECORD_STOP
//     -> "RECORD_STOPPED"
//     -> "RECORD_DATA <count>"
//     -> (raw binary: count * 5 bytes — uint32_le delta_us + uint8 mask)
//     -> "RECORD_DONE <crc16_hex>"  (CRC-16/CCITT-FALSE, same as TDBG)
//   "RECORD_OVERFLOW" emitted once if the buffer fills (auto-drops further
//   events but keeps the live heartbeat alive).
//   "RECORD_ERROR <reason>" on bad-input failure.
//
// Mask bit `i` corresponds to position `i` in the host's RECORD_START pin
// list — not the absolute Due pin number.
// ----------------------------------------------------------------------------

#define RECORD_MAX_EVENTS         4096
#define RECORD_EVENT_BYTES        5      // uint32_le delta_us + uint8 mask
#define RECORD_MAX_PINS           4
#define RECORD_LIVE_INTERVAL_MS   100

static uint8_t  recordBuf[RECORD_MAX_EVENTS * RECORD_EVENT_BYTES];
static volatile uint16_t recordEventCount = 0;
static uint8_t  recordPinList[RECORD_MAX_PINS];
static uint8_t  recordPinCount = 0;
static volatile uint8_t  recordCurrentMask = 0;
static volatile bool     recordActive = false;
static volatile bool     recordOverflow = false;
static volatile uint32_t recordPrevCycles = 0;
static unsigned long     recordLastLiveMs = 0;

// Read all selected pins, return the mask. Called from ISR and from
// handleRecordStart()'s priming step.
static inline uint8_t recordSampleMask() {
  uint8_t m = 0;
  for (uint8_t i = 0; i < recordPinCount; i++) {
    if (digitalRead(recordPinList[i])) m |= (uint8_t)(1u << i);
  }
  return m;
}

// Common ISR body — called by the per-pin trampolines. attachInterrupt() can't
// pass userdata, so we need 4 thin wrappers below.
static void recordIsrHandler() {
  if (!recordActive) return;
  uint32_t now = DWT->CYCCNT;
  uint8_t mask = recordSampleMask();
  if (mask == recordCurrentMask) return;       // glitch / re-entry — no edge
  uint32_t delta_cycles = now - recordPrevCycles;
  // 84 MHz → divide by 84 for microseconds. Fast enough inside an ISR.
  uint32_t delta_us = delta_cycles / 84UL;
  recordPrevCycles = now;
  recordCurrentMask = mask;
  if (recordEventCount < RECORD_MAX_EVENTS) {
    uint8_t* p = recordBuf + (uint32_t)recordEventCount * RECORD_EVENT_BYTES;
    memcpy(p, &delta_us, sizeof(delta_us));
    p[4] = mask;
    recordEventCount++;
  } else {
    recordOverflow = true;
  }
}

static void recordIsr0() { recordIsrHandler(); }
static void recordIsr1() { recordIsrHandler(); }
static void recordIsr2() { recordIsrHandler(); }
static void recordIsr3() { recordIsrHandler(); }
static void (* const recordIsrs[RECORD_MAX_PINS])() = {
  recordIsr0, recordIsr1, recordIsr2, recordIsr3,
};

void handleRecordStart(const String& cmd) {
  if (recordActive) {
    Serial.println("RECORD_ERROR already_active");
    return;
  }
  // Parse pin tokens after the command word.
  uint8_t pins[RECORD_MAX_PINS];
  uint8_t n = 0;
  int p = cmd.indexOf(' ');
  while (p >= 0 && n < RECORD_MAX_PINS) {
    int q = cmd.indexOf(' ', p + 1);
    String tok = (q >= 0) ? cmd.substring(p + 1, q) : cmd.substring(p + 1);
    tok.trim();
    if (tok.length() == 0) break;
    int pin = tok.toInt();
    if (pin < 0 || pin > 65) {
      Serial.print("RECORD_ERROR bad_pin ");
      Serial.println(pin);
      return;
    }
    pins[n++] = (uint8_t)pin;
    if (q < 0) break;
    p = q;
  }
  if (n == 0) {
    Serial.println("RECORD_ERROR no_pins");
    return;
  }

  for (uint8_t i = 0; i < n; i++) {
    pinMode(pins[i], INPUT);
    recordPinList[i] = pins[i];
  }
  recordPinCount = n;
  recordEventCount = 0;
  recordOverflow = false;
  tdbgEnableDwt();              // ensure DWT->CYCCNT runs
  recordCurrentMask = recordSampleMask();
  recordPrevCycles = DWT->CYCCNT;
  recordActive = true;
  for (uint8_t i = 0; i < n; i++) {
    attachInterrupt(digitalPinToInterrupt(pins[i]), recordIsrs[i], CHANGE);
  }
  recordLastLiveMs = millis();
  Serial.println("RECORD_STARTED");
}

// Called from the pre-erase idle loop while recordActive. Emits the periodic
// live-state heartbeat and watches Serial for RECORD_STOP.
void recordPump() {
  if (!recordActive) return;
  unsigned long now = millis();
  if ((now - recordLastLiveMs) >= RECORD_LIVE_INTERVAL_MS) {
    recordLastLiveMs = now;
    char hexbuf[6];
    snprintf(hexbuf, sizeof(hexbuf), "%02X", (unsigned)recordCurrentMask);
    Serial.print("RECORD_LIVE ");
    Serial.println(hexbuf);
    if (recordOverflow) {
      Serial.println("RECORD_OVERFLOW");
      recordOverflow = false;     // emit once per fill
    }
  }
}

void handleRecordStop() {
  if (!recordActive) {
    Serial.println("RECORD_ERROR not_active");
    return;
  }
  for (uint8_t i = 0; i < recordPinCount; i++) {
    detachInterrupt(digitalPinToInterrupt(recordPinList[i]));
  }
  recordActive = false;
  Serial.println("RECORD_STOPPED");

  uint16_t count = recordEventCount;
  Serial.print("RECORD_DATA ");
  Serial.println((unsigned)count);
  if (count > 0) {
    uint32_t bytes = (uint32_t)count * RECORD_EVENT_BYTES;
    Serial.write(recordBuf, bytes);
    uint16_t crc = tdbgCrc16(recordBuf, bytes);
    char hexbuf[8];
    snprintf(hexbuf, sizeof(hexbuf), "%04X", (unsigned)crc);
    Serial.print("RECORD_DONE ");
    Serial.println(hexbuf);
  } else {
    Serial.println("RECORD_DONE 0000");
  }
}


// IEEE 802.3 CRC32 (poly 0xEDB88320, refin/refout, init/xorout 0xFFFFFFFF).
// Bitwise form — small code, plenty fast for our 128 KB sweep
// (~150 ms on Cortex-M3 @ 84 MHz).
uint32_t crc32_update(uint32_t crc, uint8_t byte) {
  crc ^= byte;
  for (int i = 0; i < 8; i++) {
    uint32_t mask = -(crc & 1);
    crc = (crc >> 1) ^ (0xEDB88320 & mask);
  }
  return crc;
}

// Sweep 0..FILE_SIZE_SUPPORT-1, return CRC32 of the ROM contents.
// Uses readByte; FILE_SIZE_SUPPORT must equal the host's
// FILE_SIZE_SUPPORT for the comparison to be meaningful.
uint32_t computeRomCrc32() {
  uint32_t crc = 0xFFFFFFFFUL;
  const uint32_t romSize = 128UL * 1024UL;
  for (uint32_t addr = 0; addr < romSize; addr++) {
    crc = crc32_update(crc, readByte(addr));
  }
  return crc ^ 0xFFFFFFFFUL;
}

// Parse the hex argument from "ARDUINO_VERIFY_REQUEST <hex>" then verify.
// Replies strVerifyOK on match; on mismatch falls through to strError + halt.
void verifyRomCrc32(uint32_t expectedCrc) {
  Serial.print("Verifying ROM CRC32 (expected=0x");
  Serial.print(expectedCrc, HEX);
  Serial.println(") ...");
  unsigned long t0 = millis();
  uint32_t actualCrc = computeRomCrc32();
  unsigned long t1 = millis();
  // Reuse the read+compare timing slots to record verify cost in summary.
  t_read_total_ms += (t1 - t0);

  Serial.print("CRC32 actual=0x");
  Serial.print(actualCrc, HEX);
  Serial.print(", expected=0x");
  Serial.println(expectedCrc, HEX);

  if (actualCrc == expectedCrc) {
    Serial.println(strVerifyOK);
  } else {
    while (!Serial);
    Serial.println(strError);
    while (1) {}
  }
}

void setup() {
  delay(2000);
  Serial.begin(UART_BAUDRATE);

  // Boot banner — surfaces the build timestamp so the host log can
  // confirm the .ino on the Due actually matches the host expectations.
  // __DATE__ / __TIME__ are stamped by the compiler at every rebuild.
  Serial.println("FW: arduino_utility build " __DATE__ " " __TIME__);

  Serial.println("Pins initial.");
  // Initial All Pins
  for (int i = 0; i < addrPinsCount; i++) pinMode(addrPins[i], OUTPUT);
  for (int i = 0; i < dataPinsCount; i++) pinMode(dataPins[i], INPUT);
  pinMode(CE_PIN, OUTPUT);
  pinMode(OE_PIN, OUTPUT);
  pinMode(WE_PIN, OUTPUT);

  // Initial IDLE mode
  digitalWrite(CE_PIN, LOW);
  digitalWrite(OE_PIN, HIGH);
  digitalWrite(WE_PIN, HIGH);

  readSoftwareID();

  while (!Serial);
  Serial.println(strEraseReady);

  // Wait for either strEraseTrigger (start the flash flow) or a GPIO_*
  // command (single-pin set/read). GPIO commands keep the wait open;
  // strEraseTrigger breaks out and proceeds to chip erase + program.
  while (true) {
    // Record mode keeps a heartbeat going in the background while we
    // sit here waiting for the next command line.
    recordPump();

    if (Serial.available() > 0) {
      String input = Serial.readStringUntil('\n');
      input.trim();

      if (input == strEraseTrigger) {
        if (!gChipDetected) {
          // FLASH mode requires a recognised SST chip on the bus.
          // Refuse the trigger and stay in the idle loop so the user
          // can still use TDBG / GPIO / RECORD.
          Serial.println(strError);
          continue;
        }
        break;  // 跳出迴圈，繼續往 loop 走
      } else if (input.startsWith("GPIO_SET ")) {
        handleGpioSet(input);
      } else if (input.startsWith("GPIO_READ ")) {
        handleGpioRead(input);
      } else if (input.startsWith("TDBG_LOAD ")) {
        handleTdbgLoad(input);
      } else if (input == "TDBG_PLAY" || input.startsWith("TDBG_PLAY_LOOP ")) {
        handleTdbgPlay(input);
      } else if (input.startsWith("RECORD_START")) {
        handleRecordStart(input);
      } else if (input == "RECORD_STOP") {
        handleRecordStop();
      }
      // Unknown lines silently ignored.
    }
  }


  doChipErase();


  pinMode(LED_BUILTIN, OUTPUT);

  // Reset timing counters before the transfer phase begins.
  t_program_total_ms = 0;
  t_read_total_ms = 0;
  t_compare_total_ms = 0;
  t_recv_start_ms = millis();

  // Send strReadyStart to start receive data
  Serial.println(strReadyStart);
}

void loop() {
  // Phase A: while we still expect chunk data, accumulate bytes.
  // Phase B: once all EXPECTED_CHUNKS chunks have been ACKed, switch to
  // line-based reading to look for the explicit ARDUINO_TRANSFER_DONE_SIGNAL
  // sentinel from the host. The 10 s timeout below is kept only as a fallback
  // for legacy hosts that don't send the sentinel.

  if (chunkCount < EXPECTED_CHUNKS) {
    if (Serial.available() > 0) {
      isTransferring = true;     // 開始接收，設為傳輸中
      lastRecvTime = millis();   // 更新最後接收時間點

      byte incomingByte = Serial.read();

      if (bytesRead < CHUNK_SIZE) {
        buffer[bytesRead++] = incomingByte;
      }

      // 當收滿 4KB 時
      if (bytesRead >= CHUNK_SIZE) {
        chunkCount++;
        processChunk(chunkCount);

        // Program complete, send strLineReceivedResponse
        Serial.println(strLineReceivedResponse);
        bytesRead = 0; // 重置計數器
      }
    }
  } else {
    // Post-data phase: accept either
    //   "ARDUINO_VERIFY_REQUEST <crc32_hex>"  — run CRC32 sweep, reply OK/ERROR
    //   "ARDUINO_TRANSFER_DONE_SIGNAL"         — finalise immediately
    if (Serial.available() > 0) {
      lastRecvTime = millis();
      String input = Serial.readStringUntil('\n');
      input.trim();

      if (input.startsWith(strVerifyRequest)) {
        // Expect "<sentinel> <hex>"; split on the space.
        int sp = input.indexOf(' ');
        uint32_t expectedCrc = 0;
        if (sp > 0 && (uint32_t)sp < (uint32_t)input.length() - 1) {
          expectedCrc = (uint32_t) strtoul(input.c_str() + sp + 1, NULL, 16);
        }
        verifyRomCrc32(expectedCrc);
        return;
      }

      if (input == strTransferDone) {
        finishTransfer();
        return;
      }
      // Unknown line — ignore and keep waiting (timeout fallback below).
    }
  }

  // 2. 檢查是否超時（10秒沒新資料）— fallback for hosts that never send the sentinel.
  if (isTransferring && (millis() - lastRecvTime > (unsigned long)RECEIVED_DATA_TIMEOUT)) {
    finishTransfer();
  }
}

void finishTransfer() {
  // 如果最後一段不足 4KB 但有殘餘資料，紀錄一下
  if (bytesRead > 0) {
    Serial.print("Final fragment received: ");
    Serial.print(bytesRead);
    Serial.println(" bytes.");
  }

  // Timing summary — captured by host log so we can compare across runs.
  unsigned long total_ms = millis() - t_recv_start_ms;
  unsigned long uart_overhead_ms = total_ms
    - t_program_total_ms - t_read_total_ms - t_compare_total_ms;
  Serial.print("Timing (ms): total=");
  Serial.print(total_ms);
  Serial.print(", program=");
  Serial.print(t_program_total_ms);
  Serial.print(", readback=");
  Serial.print(t_read_total_ms);
  Serial.print(", compare=");
  Serial.print(t_compare_total_ms);
  Serial.print(", uart+idle=");
  Serial.println(uart_overhead_ms);

  Serial.println(strTransferCompleted);

  // 重置狀態，等待下一次可能的傳輸
  isTransferring = false;
  bytesRead = 0;
  chunkCount = 0;
}









// Set Address Pins to Output mode
void setAddress(uint32_t addr) {
  for (int i = 0; i < addrPinsCount; i++) {
    digitalWrite(addrPins[i], (addr >> i) & 0x01);
  }
}

// Set Data Pins to Input or Output mode.
// Cache the current mode so back-to-back calls (common during Data# Polling)
// don't repeatedly re-execute pinMode for every data pin.
int currentDataMode = -1;
void setDataMode(int mode) {
  if (mode == currentDataMode) return;
  for (int i = 0; i < dataPinsCount; i++) {
    pinMode(dataPins[i], mode);
  }
  currentDataMode = mode;
}

// Program 1 Byte Data to ROM
void writeByte(uint32_t addr, uint8_t data) {
  setDataMode(OUTPUT);
  setAddress(addr);
  for (int i = 0; i < dataPinsCount; i++) {
    digitalWrite(dataPins[i], (data >> i) & 0x01);
  }
  digitalWrite(WE_PIN, LOW);
  delayMicroseconds(1);
  digitalWrite(WE_PIN, HIGH);
}

// Read 1 Byte Data from ROM
uint8_t readByte(uint32_t addr) {
  uint8_t data = 0;
  setDataMode(INPUT);
  setAddress(addr);
  digitalWrite(OE_PIN, LOW);
  delayMicroseconds(1);
  for (int i = 0; i < 8; i++) {
    if (digitalRead(dataPins[i])) data |= (1 << i);
  }
  digitalWrite(OE_PIN, HIGH);
  return data;
}




void verifyReadData() {
  uint32_t romSize = 128 * 1024;

  uint8_t readData;
  uint8_t expData;

  for (int i = 0; i < romSize; i++) {
  // for (int i = 0; i < progSize; i++) {
    // expData = i;
    uint8_t readData = readByte((uint32_t)i);
    Serial.print("Addr 0x");
    Serial.print(i, HEX);
    Serial.print(", Data 0x");
    Serial.println(readData, HEX);

  }
}
