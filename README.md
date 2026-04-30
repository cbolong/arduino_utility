# Arduino Utility — SST39 Flash EEPROM Programmer

用 **Arduino Due** 來燒錄並驗證 SST39 系列並列介面 Flash EEPROM 的工具組。
PC 端用 Python 透過 USB Serial 把 `firmware.bin` 傳給 Arduino，Arduino 直接驅動 EEPROM 的位址/資料/控制腳位完成 Erase → Program → Read-Back Verify。

---

## 1. 專案結構

```
arduino_utility/
├── binFileProgram.ino   # Arduino 端燒錄程式（燒入 Arduino Due）
├── binFileTransfer.py   # PC 端傳輸/握手腳本
├── firmware.bin         # 要燒錄的二進位檔（自行放置，與 .py 同目錄）
└── spec/
    └── SST39LF010-...-DS20005023.pdf  # Microchip SST39LF/VF010/020/040 Datasheet
```

---

## 2. 支援的 Flash IC

程式碼中以 `Software ID` 自動辨識，目前只接受以下兩種：

| Vendor ID | Device ID | 型號 | 容量 |
|-----------|-----------|------|------|
| `0xBF` (SST) | `0xB5` | SST39SF010 / SST39SF010A | 128 KB（1 Mbit）|
| `0xBF` (SST) | `0xD5` | SST39LF010 / SST39VF010 | 128 KB（1 Mbit）|

不是這兩顆會直接送 `ARDUINO_ERROR` 並停在 `while(1)`。
如果未來要支援 SST39xF020 / SST39xF040，需要：
- 在 `binFileProgram.ino` 增加新的 `deviceID_*` 常數並修改 `readSoftwareID()` 判斷
- 在 `binFileTransfer.py` 把 `FILE_SIZE_SUPPORT` 從 `128 * 1024` 改成 256K / 512K
- 注意 SST39xF020 多一條 A17、SST39xF040 多到 A18，硬體接線與 `addrPins[]` 也要擴充

詳細命令時序請參考 `spec/` 內的 datasheet（Section 3 / Section 4 / Figure 7-x）。

---

## 3. 硬體接線

控制板：**Arduino Due**（程式以 Due 為前提，Mega 也接得起來但 Python 端的自動偵測 VID/PID 寫死 Due Programming Port）。

接線定義在 `binFileProgram.ino` 開頭：

### Address bus（A0 ~ A18，共 19 條，1Mbit 用到 A0~A16）

| Flash 腳位 | A0 | A1 | A2 | A3 | A4 | A5 | A6 | A7 | A8 | A9 | A10 | A11 | A12 | A13 | A14 | A15 | A16 | A17 | A18 |
|------------|----|----|----|----|----|----|----|----|----|----|-----|-----|-----|-----|-----|-----|-----|-----|-----|
| Arduino    | 44 | 42 | 40 | 38 | 36 | 34 | 32 | 30 | 33 | 35 | 41  | 37  | 28  | 31  | 29  | 26  | 24  | 27  | 22  |

### Data bus（DQ0 ~ DQ7）

| Flash 腳位 | DQ0 | DQ1 | DQ2 | DQ3 | DQ4 | DQ5 | DQ6 | DQ7 |
|------------|-----|-----|-----|-----|-----|-----|-----|-----|
| Arduino    | 46  | 48  | 50  | 53  | 51  | 49  | 47  | 45  |

### Control

| Flash | Arduino | 備註 |
|-------|---------|------|
| `CE#` | 43 | 燒錄期間恆為 LOW（晶片永遠 enable）|
| `OE#` | 39 | Read 時拉 LOW |
| `WE#` | 25 | Write 時拉 LOW |
| `VDD` | 3.3V | **不可接 5V**，SST39LF/VF 都是 3.0–3.6 V；SST39SF010 雖然支援 4.5–5.5 V，建議統一 3.3 V 配合 Due |
| `VSS` | GND | |

> ⚠️ Arduino Due 的 IO 是 **3.3 V**。如果改用 Arduino Mega（5V），請務必在資料/位址匯流排上加 level shifter，否則會打壞 LF/VF 系列。

---

## 4. PC 端環境需求

- Python 3.8+
- 套件：`pyserial`
  ```bash
  pip install pyserial
  ```
- Arduino IDE（用來燒 `binFileProgram.ino` 進 Arduino Due）

---

## 5. 使用流程（接手後第一次跑）

### Step 1：燒錄 Arduino sketch
1. 打開 Arduino IDE → 安裝 **Arduino SAM Boards (Cortex-M3)**（給 Due 用的）
2. 開啟 `binFileProgram.ino`
3. Board 選 `Arduino Due (Programming Port)`，Port 選對應的 COM
4. Upload

### Step 2：準備 firmware.bin
- 把要燒的檔案命名為 `firmware.bin`，放在 `binFileTransfer.py` 同一個資料夾
- 檔案 ≤ 128 KB，超過會被腳本拒絕
- 不足 128 KB 會自動用 `0x00` padding 到 128 KB 整片寫入

### Step 3：執行 Python 上傳
```bash
python binFileTransfer.py
```

預設 `AUTO_DETECT = 1`，會自動找 VID = `0x2341`、PID = `0x003D` 的 Arduino Due Programming Port。
若自動偵測失敗，把 `binFileTransfer.py` 內的 `AUTO_DETECT` 改成 `0`，並把 `PORT` 設成正確的 COM port（例如 `"COM19"` 或 `"/dev/ttyACM0"`）。

### Step 4：觀察輸出
正常流程 console 會看到：
```
>>>  Arduino Found : COMxx
>>>  handshake received : ARDUINO_ERASE_READY
>>>  handshake send     : ARDUINO_ERASE_TRIGGER
MCU: CHIP ERASE SUCCESSFUL!
>>>  handshake received : ARDUINO_READY_TO_RECEIVED_DATA
>>>  Chunk 1 sending     | Waiting for MCU response ...
...
>>>  firmware.bin Program Successful.
```

中途任何錯誤 MCU 都會送 `ARDUINO_ERROR` 然後死循環，需要 reset Arduino 重來。

---

## 6. 通訊協定（PC ⇄ MCU 握手）

UART：**115200 8N1**，chunk size = **4096 bytes**。

| 階段 | 方向 | 訊息字串 | 意義 |
|------|------|----------|------|
| 1 | MCU → PC | `ARDUINO_ERASE_READY` | Software ID 通過，等待 PC 下令 Erase |
| 2 | PC → MCU | `ARDUINO_ERASE_TRIGGER` | 觸發 Chip Erase |
| 3 | MCU → PC | `ARDUINO_READY_TO_RECEIVED_DATA` | Erase 驗證通過，可以開始送資料 |
| 4 | PC → MCU | （4096 bytes raw binary）| 一個 chunk 的資料 |
| 5 | MCU → PC | `ARDUINO_RECEIVED_LINE_DONE` | 該 chunk 已 Program + Read-Back + Verify 完成 |
| — | 重複 4–5 直到 32 個 chunk（128 KB）送完 | | |
| 6 | MCU → PC | `ARDUINO_DATA_COMPLETED` | 10 秒 idle 後 MCU 認定傳輸結束 |
| ✗ | MCU → PC | `ARDUINO_ERROR` | 任何 fatal error（ID 不符 / Erase 失敗 / Verify 失敗）|

對應字串常數：
- `.ino`：`strEraseReady` / `strEraseTrigger` / `strReadyStart` / `strLineReceivedResponse` / `strTransferCompleted` / `strError`
- `.py`：`MCU_ERASE_READY` 等同名變數

> 改字串時 **兩邊一定要一起改**，否則會卡在 handshake `while(true)`。

---

## 7. SST39 命令序列（datasheet 整理）

下列命令都由 `binFileProgram.ino` 中的 `writeByte(addr, data)` 完成（一個週期 = WE# 拉低再拉高）。

### Software ID Entry / Exit
```
Entry : (5555,AA) (2AAA,55) (5555,90)   → 進入 ID 模式後讀 0x0000 = Vendor, 0x0001 = Device
Exit  : (5555,AA) (2AAA,55) (5555,F0)
```

### Chip Erase（整片回 0xFF）
```
(5555,AA) (2AAA,55) (5555,80) (5555,AA) (2AAA,55) (5555,10)
```
Datasheet 標稱 Chip Erase typ. 70 ms。`.ino` 目前用 `delay(100)` 後做 1024 byte 抽樣驗證；**之後若改成 SST39xF040 容量更大，建議延長 delay 並改用 Toggle Bit / Data# Polling 偵測完成**（datasheet Figure 7-15）。

### Byte Program
```
(5555,AA) (2AAA,55) (5555,A0) (Addr,Data)
```
每個 byte 後 `delayMicroseconds(30)` 等 Tbp（datasheet max 20 µs）。

### Sector Erase（4 KB，一個 sector）
*目前 `.ino` 沒實作*。如果未來要做局部更新而不整片擦：
```
(5555,AA) (2AAA,55) (5555,80) (5555,AA) (2AAA,55) (SA, 30)
```
參考 datasheet 3.3 / Table 4-2。

---

## 8. 常見問題排查

| 症狀 | 可能原因 | 解法 |
|------|----------|------|
| `Error: Arduino Device not found.` | USB 沒接 / 接到 Native Port 而非 Programming Port | 換到 Due 靠近 DC 插座那個 Port；或設 `AUTO_DETECT = 0` 手動指定 |
| `EEPROM ID ERROR!!` | 接線錯、IC 不在支援清單、VCC 沒供電 | 用三用電表先量 VDD/VSS；對著上面接線表逐條 check；不是 SST39SF010 / LF010 / VF010 就要改程式 |
| `CHIP ERASE FAILED!` | WE# 沒接好、VCC 不穩、IC 已經寫壞 | 先試另一顆新 IC；量 WE# 訊號 |
| `Data compare failed at 0x...` | Program 時資料線雜訊、`delayMicroseconds(30)` 不夠 | 縮短跳線、加 0.1 µF decoupling cap；必要時加大 program delay |
| handshake 卡住 | `.ino` 與 `.py` 的字串常數不同步 | 對照 §6 表格逐字檢查 |
| 10 秒就跳 `ARDUINO_DATA_COMPLETED` | UART 中斷或 PC 端跑太慢 | 確認 `RECEIVED_DATA_TIMEOUT` 是否合適；穩定環境下 32 個 chunk 應該幾秒就跑完 |

---

## 9. 重要常數一覽（修改前先看這裡）

`binFileProgram.ino`：
```c
#define UART_BAUDRATE         115200
#define CHUNK_SIZE            4096
#define RECEIVED_DATA_TIMEOUT 10000   // ms，最後一段過 10s 就視為結束
```

`binFileTransfer.py`：
```python
AUTO_DETECT       = 1
PORT              = "COM19"
BAUD              = 115200
CHUNK_SIZE        = 4096
FILE_SIZE_SUPPORT = 128 * 1024
FILE_NAME         = "firmware.bin"
TARGET_VID        = 0x2341    # Arduino
TARGET_PIDS       = 0x003D    # Due Programming Port
```

兩邊的 `BAUD` 與 `CHUNK_SIZE` **必須一致**。

---

## 10. 待辦 / 可改進

- [ ] 加入 Sector Erase 支援，做局部更新
- [ ] 用 Toggle Bit / Data# Polling 取代固定 delay，提升相容性
- [ ] 支援 SST39xF020 / SST39xF040（接更多位址線、調整 ID 表與容量）
- [ ] 加 CRC / SHA256 末端校驗，目前是逐 byte read-back 比對（已能抓到大多數錯誤但較慢）
- [ ] Python 端目前用 `Press Enter to Exit...` 阻塞，可考慮加 `--no-pause` flag 給 CI 用
