
// USER define
#define UART_BAUDRATE 500000
#define CHUNK_SIZE 4096
#define EXPECTED_CHUNKS 32                // 128 KB / CHUNK_SIZE

#define RECEIVED_DATA_TIMEOUT 10000       // 10 sec, kept only as fallback
                                          // (host now sends an explicit
                                          // ARDUINO_TRANSFER_DONE_SIGNAL)



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
    Serial.println("EEPROM ID ERROR!!");
    
    while (!Serial);
    Serial.println(strError);
    while (1) {}
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

  // Wait for strEraseTrigger received
  while (true) {
    if (Serial.available() > 0) {
      String input = Serial.readString();
      input.trim();

      if (input == strEraseTrigger) {
        break; // 跳出迴圈，繼續往 loop 走
      }
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
