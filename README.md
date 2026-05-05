# Arduino Utility — SST39 Flash EEPROM Programmer

用 **Arduino Due** 來燒錄並驗證 SST39 系列並列介面 Flash EEPROM 的工具組。
PC 端用 Python 透過 USB Serial 把 `firmware.bin` 傳給 Arduino，Arduino 直接驅動 EEPROM 的位址/資料/控制腳位完成 Erase → Program → Read-Back Verify。

---

## 1. 專案結構

```
arduino_utility/
├── binFileProgram.ino       # Arduino 端燒錄程式（燒入 Arduino Due）
├── binFileTransfer_core.py  # 共用核心：握手協定 + 傳輸主流程
├── binFileTransfer.py       # CLI 入口（argparse 包 core）
├── binFileTransferGui.py    # Tkinter GUI 入口（包 core）
├── requirements.txt         # PC 端 Python 相依（pyserial）
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

- Python 3.8+（程式內已加 `from __future__ import annotations`，舊版 Python 可用）
- 套件：用 `requirements.txt` 安裝
  ```bash
  pip install -r requirements.txt
  ```
  目前只有 `pyserial>=3.5`。`tkinter` 是 Python 標準庫，GUI 不額外裝。
- Arduino IDE（用來燒 `binFileProgram.ino` 進 Arduino Due）
- *（可選）*想自己打 Windows EXE：`pip install pyinstaller==6.11.1`

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
或從 GitHub Releases 下載 `SST39FlashProgrammer.exe`（Windows 單檔執行）。

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

對應字串常數：
- `.ino`：`strEraseReady` / `strEraseTrigger` / `strReadyStart` / `strLineReceivedResponse` / `strVerifyRequest` / `strVerifyOK` / `strTransferDone` / `strTransferCompleted` / `strError`
- `.py`：`MCU_ERASE_READY` / `MCU_ERASE_TRIGGER` / `MCU_READY_TO_START` / `MCU_RECEIVED_LINE_RESPONSE` / `MCU_VERIFY_REQUEST` / `MCU_VERIFY_OK` / `MCU_TRANSFER_DONE_SIGNAL` / `MCU_TRANSFER_COMPLETED` / `MCU_ERROR`，集中在 `binFileTransfer_core.py`

> 改字串時 **兩邊一定要一起改**，否則 PC 端會等到 `handshake_timeout_s`（預設 30 秒）超時並退出，MCU 端則卡在 `while(true)`。
> 改 `BAUD` 也是兩邊都改。混搭不同 baud 會收到亂碼。

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
- Asset：`SST39FlashProgrammer.exe`
- `make_latest: true` → 每次新 build 自動取代上一次的 Latest 標記

PyInstaller 鎖在 `==6.11.1`、Python `3.12`，避免上游升版突然壞掉。

### EXE 是 self-contained 的（給接到 EXE 的人）

`SST39FlashProgrammer.exe` 直接 build 在 Windows runner 上，**單檔可執行，不用裝 Python、不用裝 Visual C++ Redist、不用 pip**。Build 內含：
- Python 3.12 直譯器
- `tkinter` GUI runtime（標準庫，PyInstaller 自動包入）
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
- 第 5 個（含）以後的 release 上的 `SST39FlashProgrammer.exe` asset 會被刪掉
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
