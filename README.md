# Arduino應用軟體 — SST39 Flash EEPROM Programmer

用 **Arduino Due** 來燒錄並驗證 SST39 系列並列介面 Flash EEPROM 的工具組。
PC 端用 Python 透過 USB Serial 把 `firmware.bin` 傳給 Arduino，Arduino 直接驅動 EEPROM 的位址/資料/控制腳位完成 Erase → Program → Read-Back Verify。

---

## 1. 專案結構

```
arduino_utility/
├── binFileProgram/
│   └── binFileProgram.ino   # Arduino 端燒錄程式（燒入 Arduino Due；Arduino IDE 慣例：sketch 與資料夾同名）
├── binFileTransfer_core.py  # 共用核心：握手協定 + 傳輸主流程
├── binFileTransfer.py       # CLI 入口（argparse 包 core）
├── binFileTransferGui.py    # PySide6 GUI 入口（QMainWindow + 共用連線管理）
├── requirements.txt         # PC 端 Python 相依（pyserial + PySide6）
├── firmware.bin             # 要燒錄的二進位檔（自行放置，與 .py 同目錄；已被 .gitignore 排除）
├── .github/workflows/build-release.yml  # Release 觸發的 Windows EXE build
└── spec/
    └── SST39LF010-...-DS20005023.pdf    # Microchip SST39LF/VF010/020/040 Datasheet
```

> 三個 `binFileTransfer*.py` 的關係：CLI 與 GUI 都只是薄殼，所有握手邏輯、port 偵測、timeout 控制都在 `binFileTransfer_core.program_firmware()` 裡。改協定時只動 core，兩個前端會同步生效。

---

## 2. 支援的 Flash IC

程式碼中以 `Software ID` 自動辨識，目前只接受以下兩種：

| Vendor ID | Device ID | 型號 | 容量 |
|-----------|-----------|------|------|
| `0xBF` (SST) | `0xB5` | SST39SF010 / SST39SF010A | 128 KB（1 Mbit）|
| `0xBF` (SST) | `0xD5` | SST39LF010 / SST39VF010 | 128 KB（1 Mbit）|

不是這兩顆會印 `EEPROM ID ERROR` 但**不再停機** —— 仍進 idle loop，GPIO/TDBG/RECORD/SGPIO 照常可用；只有 `ARDUINO_ERASE_TRIGGER` 會被擋下（且會先重新偵測一次，接好晶片後直接再按燒錄即可，不必 reset）。
如果未來要支援 SST39xF020 / SST39xF040，需要：
- 在 `binFileProgram/binFileProgram.ino` 增加新的 `deviceID_*` 常數並修改 `readSoftwareID()` 判斷
- 在 `binFileTransfer_core.py` 把 `FILE_SIZE_SUPPORT` 從 `128 * 1024` 改成 256K / 512K
- 注意 SST39xF020 多一條 A17、SST39xF040 多到 A18，硬體接線與 `addrPins[]` 也要擴充

詳細命令時序請參考 `spec/` 內的 datasheet（Section 3 / Section 4 / Figure 7-x）。

---

## 3. 硬體接線

控制板：**Arduino Due**（程式以 Due 為前提，Mega 也接得起來但 Python 端的自動偵測 VID/PID 寫死 Due Programming Port）。

接線定義在 `binFileProgram/binFileProgram.ino` 開頭：

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

- Python 3.8+（程式內已加 `from __future__ import annotations`，舊版 Python 可用）
- 套件：用 `requirements.txt` 安裝
  ```bash
  pip install -r requirements.txt
  ```
  `pyserial>=3.5` 與 `PySide6>=6.6`。GUI 是 PySide6/Qt（不是 tkinter），`pip install -r requirements.txt` 會一併裝上。
- Arduino IDE（用來燒 `binFileProgram/binFileProgram.ino` 進 Arduino Due）
- *（可選）*想自己打 Windows EXE：`pip install pyinstaller==6.11.1`

---

## 5. 使用流程（接手後第一次跑）

### Step 1：燒錄 Arduino sketch
1. 打開 Arduino IDE → 安裝 **Arduino SAM Boards (Cortex-M3)**（給 Due 用的）
2. 開啟 `binFileProgram/binFileProgram.ino`
3. Board 選 `Arduino Due (Programming Port)`，Port 選對應的 COM
4. Upload

### Step 2：準備 firmware
- GUI 版：按 **Browse...** 可以挑任何副檔名的檔案，不限定 `.bin`（dialog 預設過濾就是 All files）
- CLI 版：預設找 `binFileTransfer.py` 同目錄下的 `firmware.bin`；要改路徑 / 改副檔名用 `--file` 指定
- 檔案 ≤ 128 KB，超過會被腳本拒絕
- 0 byte 空檔會被拒絕（避免使用者誤選空檔導致整片寫 0）
- 不足 128 KB 會自動用 `0xFF` padding 到 128 KB 整片寫入（刻意用 `0xFF` 而非 `0x00`:erase 後的 cell 本來就是 `0xFF`，燒 `0xFF` 是 no-op，因此補頁區的 erase 不完全仍會被最終 CRC 抓到而不會被遮蔽）
- 內容只看 bytes，不檢查格式：選錯檔（例如挑到 .txt 或 .docx）會把錯誤資料燒進去，CRC32 還是會 OK，**但對目標系統就是垃圾 firmware**。重燒一次即可救回

### Step 3：執行上傳

#### 3a. CLI（指令列）
```bash
# 自動偵測 Arduino Due Programming Port (VID 0x2341, PID 0x003D)
python binFileTransfer.py

# 自動偵測失敗時手動指定 port（Windows / Linux / macOS 範例）
python binFileTransfer.py --port COM19
python binFileTransfer.py --port /dev/ttyACM0
python binFileTransfer.py --port /dev/cu.usbmodem1411

# 也能換個別位置的 firmware 與調整 handshake 逾時
python binFileTransfer.py --file ./builds/v1.2.bin --timeout 60
```

完整參數：
| 參數 | 預設 | 說明 |
|------|------|------|
| `--port` | （自動偵測）| 手動指定 serial port，跳過 VID/PID 比對 |
| `--timeout` | `30.0` | 每段握手的等待秒數，超過會 log 並跳出（避免無限 hang）|
| `--file` | `./firmware.bin` | 換掉預設要燒的檔案路徑 |
| `--help` | — | 顯示說明 |

#### 3b. GUI（雙擊版本）
```bash
python binFileTransferGui.py
```
或從 GitHub Releases 下載 `Arduino_Utility.exe`（Windows 單檔執行）。

> 啟動速度：onefile bootloader 每次都會把 Python runtime 解壓到 `%TEMP%`，冷啟動約 5–10 秒，這是用「單檔可攜」換來的代價。Python runtime 一進去之後仍然有做 lazy import / 背景掃 port / GPIO 面板 lazy build 等優化，所以視窗本身會很快出現；只是 bootloader 解壓那一段沒辦法省。

GUI 操作：
1. 按 **Browse...** 選 firmware
2. 上方 **Port** 下拉選單啟動時會自動掃描所有 serial port；認到 Arduino Due Programming Port (VID `0x2341` / PID `0x003D`) 就預設幫你選那個 COM；認不到就停在 `Auto-detect`（等同舊版的「留空」行為）。Due 啟動後才插上的話，按右邊的 **↻ Refresh** 重掃。
3. 按 **Start Programming**，下方 log 區會顯示握手 / chunk / verify 訊息
4. 完成後 status 會顯示 Success（綠）或 Error（紅）

> 燒錄進行中按視窗右上 X 會跳「Busy」對話框拒絕關閉，避免燒到一半被切斷；要強制離開請從 Task Manager。

### Step 4：觀察輸出
CLI 正常流程 console 會看到：
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
若 PC 端 30 秒內沒收到預期回應（MCU 死掉、未燒錄、字串對不上），會 log
`Timeout after 30.0s waiting for: ...` 然後 exit 非 0（CLI）或顯示 Error（GUI）。

---

## 6. 通訊協定（PC ⇄ MCU 握手）

UART：**115200 8N1**，chunk size = **4096 bytes**。

> 註：原本想升到 500000 加速 UART 傳輸，但發現某些 Due 板（特別是副廠）的 ATmega16U2 firmware 在 500000 下會送出亂碼讓 host 一直 timeout。所以改回 115200 保守值。如果你的板子 16U2 跑 500000 OK，可以同步把 `.ino` 的 `UART_BAUDRATE` 跟 `binFileTransfer_core.py` 的 `BAUD` 兩邊都改回 500000。其他加速優化（CRC32 一次驗證、Data# Polling、拿掉 10s timeout、拿掉 cosmetic delay）跟 baud 無關，全部保留。

| 階段 | 方向 | 訊息字串 | 意義 |
|------|------|----------|------|
| 1 | MCU → PC | `ARDUINO_ERASE_READY` | Software ID 通過，等待 PC 下令 Erase |
| 2 | PC → MCU | `ARDUINO_ERASE_TRIGGER` | 觸發 Chip Erase |
| 3 | MCU → PC | `ARDUINO_READY_TO_RECEIVED_DATA` | Erase 驗證通過，可以開始送資料 |
| 4 | PC → MCU | （4096 bytes raw binary）| 一個 chunk 的資料 |
| 5 | MCU → PC | `ARDUINO_RECEIVED_LINE_DONE` | 該 chunk 已 Program 完成 |
| — | 重複 4–5 直到 32 個 chunk（128 KB）送完 | | |
| 6 | PC → MCU | `ARDUINO_VERIFY_REQUEST <8位 CRC32 hex>` | 要求 MCU 整片掃 CRC32 對照 |
| 7 | MCU → PC | `ARDUINO_VERIFY_OK` | CRC32 對到，整片 ROM 與 firmware.bin 一致 |
| 8 | PC → MCU | `ARDUINO_TRANSFER_DONE_SIGNAL` | 顯式通知傳輸結束（取代等 10 秒 idle）|
| 9 | MCU → PC | `Timing (ms): total=..., program=..., readback=..., compare=..., uart+idle=...` | 該次燒錄的分段耗時 |
| 10 | MCU → PC | `ARDUINO_DATA_COMPLETED` | MCU 認定傳輸結束 |
| ✗ | MCU → PC | `ARDUINO_ERROR` | 任何 fatal error（ID 不符 / Erase 失敗 / CRC32 不對）|

CRC32 是 IEEE 802.3 polynomial `0xEDB88320`，跟 Python `zlib.crc32` 相容。MCU 端是 bitwise 算法，128 KB 大約 1.3 s（read 開銷為主，CRC 計算只佔 ~150 ms）。

如果新版 PC 工具配上舊版 `.ino`，舊 MCU 不認得 `ARDUINO_VERIFY_REQUEST` / `ARDUINO_TRANSFER_DONE_SIGNAL`，會在 30 秒 PC handshake timeout 內失敗——這個 commit 後跨版本不再支援，請兩邊一起更新。

### GPIO 設定模式（GUI「GPIO 設定」tab 用）

MCU 在送出 `ARDUINO_ERASE_READY` 之後、收到 `ARDUINO_ERASE_TRIGGER` 之前，會持續等待單腳 GPIO 指令；只要還沒進入燒錄流程，host 可以反覆發 GPIO 指令做接線除錯。一旦發了 `ARDUINO_ERASE_TRIGGER`，MCU 直接進入 erase + 燒錄流程，下次想用 GPIO 必須 reset Due。

| 方向 | 訊息 | 意義 |
|------|------|------|
| PC → MCU | `GPIO_SET <pin> <OUTPUT\|INPUT> [HIGH\|LOW]` | 設定指定 Due pin 的 mode；mode=OUTPUT 時可同時帶 HIGH/LOW 值 |
| MCU → PC | `GPIO_OK` | 設定成功 |
| PC → MCU | `GPIO_READ <pin>` | 讀取指定 pin 的當前值（會先強制 INPUT mode）|
| MCU → PC | `GPIO_VALUE <pin> <0\|1>` | 該 pin 的當前數位值 |
| MCU → PC | `GPIO_ERROR <reason>` | 格式錯誤、未知 mode/value 等；例：`GPIO_ERROR bad_mode` |

⚠️ 直接拿 GPIO_SET 去改 CE#/OE#/WE#/A0–A18/DQ0–DQ7 會破壞 IDLE 匯流排狀態，之後燒錄行為未定義。reset Due 才能回到乾淨狀態。GPIO 模式適合「LED 跑馬燈測試」、「pin map 接線驗證」這類用途。

GUI 端目前用持久連線（GPIO 設定 tab 上的 [Connect] / [Disconnect] 按鈕），按一次 Connect 開 serial（等 ~2s Due reset + ARDUINO_ERASE_READY），之後每筆 GPIO_SET / GPIO_READ 只走 ~5 ms UART 來回。畫面上把 Due 的 D0–D65（54 條 digital + 12 條 analog A0–A11，共 66 條）攤成一條清單，不再依角色分組。**Connect 後不會自動讀取**，Read 欄位停在 `??` 等使用者按 [Read All]（66 根 ≈ 400 ms）。可勾選 Auto-refresh 每 X 秒自動 Read All（預設關）。

按 [Disconnect] 時 GUI 會立刻送出 abort 訊號並把已排隊但還沒跑的 GPIO 指令清空，所以即使 MCU 沒回應，最壞也只會等當前那顆 pin 的單次 serial timeout，不會卡 66 × timeout。

按下「燒錄 ROM」tab 的 Start Programming 時，如果 GPIO 還在 Connected，GUI 會自動 Disconnect 釋放 serial，再進入燒錄流程；燒完不會自動重連，要手動再按一次 Connect。

CLI 端 `gpio_set` / `gpio_read` 仍是 one-shot（每次 open/close），給 script 用簡單；要持久連線請直接用 `from binFileTransfer_core import GpioSession`。

### TDBG 波形重播模式（GUI「TDBG」tab 用）

跟 GPIO 模式一樣，**只在 MCU 收到 `ARDUINO_ERASE_TRIGGER` 之前可用**；發了 `TRIGGER` 後 TDBG 跟 GPIO 都失效，要 reset Due 才能再用。

用途：把 Acute 邏輯分析儀(或其他工具)擷取下來的波形 `.txt` 上傳到 Due，由 Due 在指定 GPIO 上以 cycle 等級時序重播。播放時 Due 用 Cortex-M3 的 DWT cycle counter 自行 busy-wait（84 MHz、~11.9 ns/tick），最小可重播脈寬約 32 cycles ≈ 380 ns。

| 階段 | 方向 | 訊息 | 意義 |
|------|------|------|------|
| 1 | PC → MCU | `TDBG_LOAD <pin> <num_events> <initial_state>` | 通告 Due 接下來會送 `num_events × 5` bytes 的事件資料；`initial_state` 是 0/1，第一個 transition 之前的腳位電位 |
| 2 | MCU → PC | `TDBG_READY` | Due 已開好 buffer，可以送 binary blob |
| 3 | PC → MCU | （`num_events × 5` bytes raw binary）| 每個 event = `uint32 little-endian delta_cycles` + `uint8 state(0/1)`；delta 是與「上一個事件」之間的 84 MHz cycle 數，第一個事件 delta 一律 0（在 playback 起始就觸發） |
| 4 | MCU → PC | `TDBG_LOADED <crc16_hex>` | Due 把收到的 blob 算 CRC-16/CCITT-FALSE 回傳；host 比對若不符要重送 |
| 5 | PC → MCU | `TDBG_PLAY` 或 `TDBG_PLAY_LOOP <n>` | 啟動播放；`n=0` 表示無限循環、`n>=1` 表示重複 N 次；單純 `TDBG_PLAY` 等同 `TDBG_PLAY_LOOP 1` |
| 6 | MCU → PC | `TDBG_PLAY_STARTED` | 播放開始 |
| 7 | PC → MCU | `TDBG_STOP` | 中止無限 / 多次播放；只在「長間隔事件」(≥10 ms gap) 期間 Due 會去 poll Serial，所以反應延遲最壞 = 一個長間隔 |
| 8 | MCU → PC | `TDBG_STOPPED` | 確認已停（僅在收到 STOP 才會送）|
| 9 | MCU → PC | `TDBG_PLAY_DONE` | 播放結束（不論是跑完還是被 STOP）|
| ✗ | MCU → PC | `TDBG_ERROR <reason>` | 各種錯誤；`bad_pin` / `bad_count` / `bad_initial` / `bad_format` / `short_read X/Y` / `not_loaded` |

時序保證：
- 直接寫 PIO `SODR/CODR` 暫存器做腳位翻轉（單 cycle store，無 `digitalWrite()` 抖動）
- 中斷只在「等下一個 deadline + 寫腳位」這段 mask，事件之間 Serial RX、SysTick、USB CDC 全部正常運作
- 預期 jitter ±10 cycles ≈ ±120 ns（spin-loop 開銷與指令預取）
- 不保證跨設備時鐘對齊：Due 與分析儀晶振各自漂移，典型 ±0.01% (1 秒擷取對應 100 µs 誤差)

容量限制：MCU 端 buffer = `TDBG_MAX_EVENTS × 5 = 20 KB`（`TDBG_MAX_EVENTS` 預設 `4096`）。原始擷取超過 4096 個 transition 時，host 端的 `parse_acute_txt` 會直接拒絕並提示。

GUI 操作：先按右上角 [連線] → 切到 **TDBG** 分頁 → 從 **輸出腳位** 下拉選一個 Due GPIO（**沒有預設值**，每次都要選；Flash 匯流排上的腳位會在標籤顯示 `(WE#)` / `(A0)` 等註記讓你警覺）→ 按 [TDBG1] / [TDBG2] / [TDBG3] 其中之一。三段波形是**燒在韌體裡的 preset**，host 只送 `TDBG_PRESET <n> <pin>`，MCU 自己 bit-bang 出對應圖樣後回 `TDBG_PRESET_OK <n>`。

要換波形：三段 preset 是 hard-coded 在 `binFileProgram.ino` 的 `sendTdbgPreset1/2/3()` 裡（三者只差最後一個 byte），改完需用 Arduino IDE 重新上傳。`TdbgSession.load()` / `play()` 的「上傳任意擷取波形」路徑仍是 library API，但 GUI 沒有對應按鈕。

CLI 端目前**沒有**對應的 sub-command；要 scripting 或載入任意波形,直接 `from binFileTransfer_core import TdbgSession, parse_acute_txt`(library 端仍支援多 channel、迭代、stop event)。

**GUI 三 tab 的 port 仲裁**：燒錄 / GPIO / TDBG 同時間最多只有一個能持有 serial port。在 TDBG 連線狀態下按 GPIO 的 Connect 會被擋；按 Start Programming 則會自動釋放 GPIO 與 TDBG 後再進入燒錄。

### 波形錄製模式（GUI「波形錄製」tab 用）

跟 GPIO / TDBG 一樣，**只在 MCU 收到 `ARDUINO_ERASE_TRIGGER` 之前可用**。發了 TRIGGER 後失效，要 reset Due 才能再用。

用途：當作小型 logic analyzer。挑 1–4 個 Due GPIO，按開始，Due 用 pin-change interrupt 攔截每個轉態並用 DWT 計時，同時每 100 ms 回報一次當前狀態給 host 顯示 live HIGH / LOW；按結束後回傳整段時序給 host 畫成波形。

| 階段 | 方向 | 訊息 | 意義 |
|------|------|------|------|
| 1 | PC → MCU | `RECORD_START <pin1> [<pin2> ...]` | 1–4 個 Due GPIO 編號；順序決定後續 mask 的 bit 位置（`pin1` = bit 0）|
| 2 | MCU → PC | `RECORD_STARTED` | ISR 已掛上，DWT 已啟動 |
| 3 | MCU → PC | `RECORD_LIVE <hex_mask>` | 約每 100 ms 一次，當前各 pin HIGH/LOW 的 bitmask |
| - | MCU → PC | `RECORD_OVERFLOW` | （一次性）buffer 滿了，後續 transition 被丟棄，但 live 心跳繼續 |
| 4 | PC → MCU | `RECORD_STOP` | 結束錄製 |
| 5 | MCU → PC | `RECORD_STOPPED` | ISR 已 detach |
| 6 | MCU → PC | `RECORD_DATA <count>` | 接下來會送 `count × 5 bytes` raw binary |
| 7 | MCU → PC | （`count × 5` bytes raw）| 每個事件 = `uint32_le delta_us` + `uint8 mask`，bit i = pin list 第 i 個的 HIGH/LOW |
| 8 | MCU → PC | `RECORD_DONE <crc16_hex>` | Blob 的 CRC-16/CCITT-FALSE，host 比對若不符表示傳輸出錯 |
| ✗ | MCU → PC | `RECORD_ERROR <reason>` | `bad_pin <n>` / `no_pins` / `already_active` / `not_active` |

時序保證：
- 中斷驅動（`attachInterrupt CHANGE`），不是 polling — 主迴圈在做別的事也照樣抓到 transition
- DWT 計數器標記絕對時間，delta 以 µs 為單位記錄
- ISR 進入延遲 + handler prologue ≈ 300–500 ns（Cortex-M3 @ 84 MHz）
- 同時發生在多 pin 上的 transition：先觸發的 ISR 進來時讀 PDSR 會看到所有相關 pin 已穩定的 mask，記到同一個 event 內

容量限制：`RECORD_MAX_EVENTS = 4096` events × 5 bytes = 20 KB MCU buffer。對活躍訊號可錄幾十毫秒到數秒，視轉態密度而定。

GUI 操作：在 **波形錄製** tab，按 [Connect] → 每個 row 的 Pin 下拉挑 Due GPIO，按 [+ 加 pin] 增加更多 row（最多 4 個），不要的 row 按 [⊖] 移除 → [開始]。錄製期間每個 pin row 的小圓點會即時變綠（HIGH）/ 灰（LOW）。按 [結束] 後 Due 把整段資料傳回，結束鍵右邊會出現一個小波形圖示 — 點下去開預覽視窗，把所有選的 pin 疊在同一個 X 軸上呈現，長間隔超過 10 ms 會自動分 cluster 顯示（像 TDBG 預覽那樣）。

CLI 端**沒有**對應 sub-command；要 scripting 請直接 `from binFileTransfer_core import RecordSession, parse_record_blob`。

**GUI 四 tab 的 port 仲裁**：燒錄 / GPIO / TDBG / 波形錄製 同時間最多一個持有 serial port。任意 tab Connect 中，其他 tab 的 Connect 會被擋。按 Start Programming 會自動依序釋放所有持有 session 的 tab 再進入燒錄。

對應字串常數：
- `.ino`：`strEraseReady` / `strEraseTrigger` / `strReadyStart` / `strLineReceivedResponse` / `strVerifyRequest` / `strVerifyOK` / `strTransferDone` / `strTransferCompleted` / `strError`
- `.py`：`MCU_ERASE_READY` / `MCU_ERASE_TRIGGER` / `MCU_READY_TO_START` / `MCU_RECEIVED_LINE_RESPONSE` / `MCU_VERIFY_REQUEST` / `MCU_VERIFY_OK` / `MCU_TRANSFER_DONE_SIGNAL` / `MCU_TRANSFER_COMPLETED` / `MCU_ERROR`，集中在 `binFileTransfer_core.py`

> 改字串時 **兩邊一定要一起改**，否則 PC 端會等到 `handshake_timeout_s`（預設 30 秒）超時並退出，MCU 端則卡在 `while(true)`。
> 改 `BAUD` 也是兩邊都改。混搭不同 baud 會收到亂碼。

### SGPIO 被動解碼模式（GUI「SGPIO」tab 用）

SFF-8485 SGPIO 的**被動解碼器**：Due 把三條訊號當**輸入**接上、全程不驅動匯流排
（RX-only，不碰 SDataIn），解出 initiator 送給背板的 LED 控制位元流。

| 訊號 | 方向 | Due 角色 |
|---|---|---|
| SClock | initiator → | 取樣時脈（中斷來源） |
| SLoad | initiator → | 分幀標記 |
| SDataOut | initiator → target | 被取樣的資料位元 |
| ~~SDataIn~~ | target → initiator | **不接**（本工具不回應） |

⚠️ 訊號需為 **3.3V**（Due 不是 5V 容忍），且必須與 SGPIO 來源**共地**。

握手：

| 方向 | 字串 |
|---|---|
| PC → MCU | `SGPIO_START <sclk> <sload> <sdata> <rising 0\|1> <sloadActiveHigh 0\|1> <frameLen>` |
| MCU → PC | `SGPIO_STARTED`，或 `SGPIO_ERROR <why>` |
| MCU → PC | `SGPIO_FRAME <bitstring>`（ASCII `0`/`1`，首字元 = 該幀第一個取樣位元） |
| MCU → PC | `SGPIO_OVERRUN`（pump 來不及排空，或位元計數飽和 = SLoad 從未 assert） |
| PC → MCU | `SGPIO_STOP` → `SGPIO_STOPPED` |

MCU 只負責**擷取原始位元**：每個 SClock 有效邊緣取樣一個 SDataOut 位元，SLoad 判定幀邊界，
並且**只在幀內容改變時**才回傳（外加約 200 ms heartbeat）—— SGPIO 是連續不停的時脈流，
全送會塞爆 115200。

**幀的語意完全在 host 端**，所以換 vendor 變體或 SLoad 慣例差一個 bit 都只是改設定、不必重燒：
分頁裡可設 drives 數、bits/drive、header bits、MSB/LSB、取樣邊緣、SLoad 極性。
`parse_sgpio_frame()` 依設定拆成每個 drive 的 Activity / Locate / Fault；
`sgpio_frame_valid()` 檢查幀長是否等於 `header_bits + drives × bits/drive` ——
**對不上就標紅，不會靜默解錯**。畫面上每個 drive 一列 A/L/F 燈號 + 原始位元 + 「已收 N 幀」。

> RECORD 與 SGPIO **互斥**（兩者都要獨佔 MCU 的中斷擷取路徑）：韌體雙向拒絕，
> GUI 也會在其中一個進行中把其他分頁灰掉。

> 不需要真的 HBA 也能自測：用本工具的 **TDBG** 在三支腳打出一段已知圖樣、接回 SGPIO 的
> 三支輸入腳，解碼結果應等於送出的內容。

---

## 7. SST39 命令序列（datasheet 整理）

下列命令都由 `binFileProgram/binFileProgram.ino` 中的 `writeByte(addr, data)` 完成（一個週期 = WE# 拉低再拉高）。

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
| `Error: Arduino Device not found.` | USB 沒接 / 接到 Native Port 而非 Programming Port | 換到 Due 靠近 DC 插座那個 Port；或在 GUI 右上角的 Port 下拉選單手動指定（CLI 用 `--port`） |
| `EEPROM ID ERROR!!` | 接線錯、IC 不在支援清單、VCC 沒供電 | 用三用電表先量 VDD/VSS；對著上面接線表逐條 check；不是 SST39SF010 / LF010 / VF010 就要改程式 |
| `CHIP ERASE FAILED!` | WE# 沒接好、VCC 不穩、IC 已經寫壞 | 先試另一顆新 IC；量 WE# 訊號 |
| `Data compare failed at 0x...` | Program 時資料線雜訊、`delayMicroseconds(30)` 不夠 | 縮短跳線、加 0.1 µF decoupling cap；必要時加大 program delay |
| handshake 卡住 | `.ino` 與 `.py` 的字串常數不同步 | 對照 §6 表格逐字檢查 |
| 10 秒就跳 `ARDUINO_DATA_COMPLETED` | UART 中斷或 PC 端跑太慢 | 確認 `RECEIVED_DATA_TIMEOUT` 是否合適；穩定環境下 32 個 chunk 應該幾秒就跑完 |

---

## 9. 重要常數一覽（修改前先看這裡）

`binFileProgram/binFileProgram.ino`：
```c
#define UART_BAUDRATE         115200
#define CHUNK_SIZE            4096
#define EXPECTED_CHUNKS       32      // 128 KB / CHUNK_SIZE
#define RECEIVED_DATA_TIMEOUT 10000   // ms，僅作為舊版 host 的 fallback
                                      // （新版 host 會送 TRANSFER_DONE_SIGNAL）
```

`binFileTransfer_core.py`（CLI 與 GUI 共用）：
```python
BAUD                        = 115200
CHUNK_SIZE                  = 4096
FILE_SIZE_SUPPORT           = 128 * 1024
TARGET_VID                  = 0x2341     # Arduino
TARGET_PID                  = 0x003D     # Due Programming Port
DEFAULT_HANDSHAKE_TIMEOUT_S = 30.0
```

> Port 與 timeout **不再寫死在原始碼**。CLI 用 `--port` / `--timeout` 指定；GUI 用畫面上的 Port 欄位。原本的 `AUTO_DETECT = 0/1` 已移除。

`binFileTransfer.py`（CLI）：
```python
FILE_NAME = "firmware.bin"   # 預設值，可被 --file 覆蓋
```

兩邊（`.ino` 與 `.py`）的 `BAUD` 與 `CHUNK_SIZE` **必須一致**；7 個握手字串也必須一致（含新增的 `ARDUINO_TRANSFER_DONE_SIGNAL`）。

---

## 10. CI / Release

`.github/workflows/build-release.yml` 會在以下情況跑 PyInstaller 打 Windows EXE：
- 任何 push 到 `main`（自動）
- 從 Actions 頁面手動 `workflow_dispatch`

純文件變更（`README.md` / `**/*.md` / `spec/**` / `.gitignore`）會被 `paths-ignore` 跳過，不浪費 runner 分鐘。

### 自動 release 規則

每個觸發成功的 build 會自動建一個 release，並標成 **Latest**（最新的會搶到 Latest 徽章，自動置頂於 Releases 頁面）：
- Tag 格式：`build-YYYYMMDD-HHMMSS-<7位commit sha>`
- Release 名稱：`Auto build build-YYYYMMDD-HHMMSS-<sha>`
- 內文：commit SHA + commit message
- Asset：`Arduino_Utility.exe`
- `make_latest: true` → 每次新 build 自動取代上一次的 Latest 標記

PyInstaller 鎖在 `==6.11.1`、Python `3.12`，避免上游升版突然壞掉。

### EXE 是 self-contained 的（給接到 EXE 的人）

`Arduino_Utility.exe` 直接 build 在 Windows runner 上，**單檔可執行，不用裝 Python、不用裝 Visual C++ Redist、不用 pip**。Build 內含：
- Python 3.12 直譯器
- PySide6/Qt GUI runtime（PyInstaller 自動包入；workflow 明確 `--exclude-module tkinter`）
- `pyserial` + Windows COM port enumeration backend（用 `--collect-submodules serial` + `--hidden-import serial.tools.list_ports_windows` 強制納入，避免 PyInstaller 漏掉動態載入的子模組）

唯一 host 端要有的東西是 **Arduino Due Programming Port 的 USB CDC driver**，這個 Windows Update 在第一次插上 Due 時會自動安裝，不用人工處理。

#### 第一次執行的 SmartScreen 警告

EXE **沒有 code signing 憑證**（憑證是要花錢買的，目前沒做），所以第一次在乾淨的 Windows 跑會跳：

> Windows protected your PC

點 **More info → Run anyway** 即可。同一台機器之後就不會再跳了。

如果之後願意花錢買 OV/EV code signing 憑證，把 PFX 設進 GitHub Secrets，build 步驟可以加 `signtool` 簽章，這個警告就會永遠消失。

#### EXE 的 Windows 版本資訊

build 時 workflow 會動態產生 `version.txt` 並用 `--version-file` 嵌進 EXE。Right-click EXE → Properties → Details 可以看到：
- `FileDescription`：SST39 Flash EEPROM Programmer
- `ProductName`：SST39 Flash Programmer
- `FileVersion` / `ProductVersion`：`build-YYYY.MM.DD-<sha>`
- `CompanyName`：cbolong

#### EXE 的 icon

`assets/icon.ico` 是一個 256/128/64/48/32/16 多解析度的 chip-style icon，build 時用 `--icon` 嵌入。Explorer thumbnail 與 taskbar 顯示用的就是這個。要換 icon 直接覆蓋這個檔案即可（保持 multi-resolution `.ico` 格式）。

### 自動 prune（保留最近 4 份 EXE）

每次 build 結束後會清理：
- 撈出所有 `build-*` 開頭的 release，按 `publishedAt` 排序
- 第 5 個（含）以後的 release 上的 `Arduino_Utility.exe` asset 會被刪掉（也一併清掉舊名 `SST39FlashProgrammer.exe` 與曾經短暫嘗試過的 `SST39FlashProgrammer.zip`，避免遺留資產）
- **Release notes、tag、source code zip 都保留**，方便回顧 commit 歷史
- 你手動發的 semver release（例如 `v0.1.0`）**不會被碰**，因為 prefix 不符

### 想停掉自動 build？

把 `.github/workflows/build-release.yml` 開頭的 `push:` 區塊整段拿掉就會回到「只在手動 dispatch 時 build」。

## 11. 待辦 / 可改進

- [ ] 加入 Sector Erase 支援，做局部更新
- [ ] 用 Toggle Bit / Data# Polling 取代固定 delay，提升相容性
- [ ] 支援 SST39xF020 / SST39xF040（接更多位址線、調整 ID 表與容量）
- [ ] 加 CRC / SHA256 末端校驗，目前是逐 byte read-back 比對（已能抓到大多數錯誤但較慢）
- [ ] CLI 加 `--no-pause` flag 給 CI 用（目前結束會 `input("Press Enter to Exit...")`）
- [ ] GUI 加 Cancel 按鈕，能在程式跑到一半中止（目前只能等 timeout）
