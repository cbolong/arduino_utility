# Session Handoff — Arduino應用軟體 (SST39 Flash Programmer + TDBG/GPIO/RECORD)

> 給下一個接手 session 的完整交接文件。最後更新 commit `36cda33`。
> 開發分支:直接 commit 在 `main` 並 push(見 `CLAUDE.md` workflow rules)。

---

## 0. 一句話狀態

字型 + 統一 Connect 的 UI 改動已完成並 push,**但 Part B(統一 Connect)是大重構、
我無法用硬體驗證,等使用者實測四個 tab**。TDBG playback 經歷一長串硬體 bug 修正
(linkage / edge-trigger / retimer),**最後一個已知阻塞(`TC6_Handler` 沒 hook 到
vector)在 commit `20de7a3` 用「搬到獨立 `.c` 檔」解掉,但同樣等使用者實測確認**。

---

## 1. 專案是什麼

用 **Arduino Due**(ATSAM3X8E,Cortex-M3 @ 84 MHz)當 programmer 燒 SST39xF010
並列 NOR flash。除了燒錄,還長出三個額外功能(都在 Due 開機後的 pre-erase idle
loop 裡跑,gating 相同):

- **GPIO** — 手動 poke 任一支腳(輸出/輸入、讀值)
- **TDBG** — 把 captured 的 logic-analyzer 波形重播到某支腳(clock-burst 輸出)
- **RECORD(波形錄製)** — 中斷驅動的多腳波形錄製

檔案:
- `binFileProgram/binFileProgram.ino` — Due 上的 sketch(.ino 必須在同名子資料夾,
  Arduino IDE 2.x 要求)
- `binFileProgram/tdbg_tc_isr.c` — **新檔案**,TDBG 的 TC2 ch0 中斷處理(見 §4 為什麼
  獨立成 .c)
- `binFileTransfer_core.py` — host 端 library(所有握手協定、session 類別)
- `binFileTransfer.py` — CLI 前端
- `binFileTransferGui.py` — Tkinter GUI 前端(4 個 tab)
- `CLAUDE.md` — 給 Claude 的 repo 指南(架構、協定、「看起來像 bug 但不是」清單)

**重要**:`.ino` 改動後使用者必須用 Arduino IDE 手動 re-upload。host(.py)改動只要
`git pull` 重開 GUI。每次回報問題請先確認 banner 時間戳(`MCU: FW: arduino_utility
build <date> <time>`)是不是最新 — 使用者好幾次忘記 re-upload 跑舊韌體。

---

## 2. 本 session 的 commit(由舊到新)

| Commit | 重點 |
|---|---|
| `504f374` | Chunk A:boot banner + 抓不到 chip 不再 halt(GPIO/TDBG/RECORD 沒插晶片也能測) |
| `5b7ff1f` | Mac-style UI 配色 + 修選中 tab 字大小 |
| `0bd6644` | `.ino` 搬進 `binFileProgram/` 子資料夾 + 顯式 `#include <Arduino.h>` |
| `dbc9544`→`814938a`→`016e309` | DWT 宣告來回:**結論** = Atmel SAM core 的 `core_cm3.h` 有 `CoreDebug` 但**沒有 `DWT`**,所以 .ino 自己 `#ifndef DWT_BASE` 手寫 DWT(只 DWT,不碰 CoreDebug) |
| `b3c43db` | Sprint 1:RECORD framing race fix(`_live_loop` 讀完第一行就 return)+ UI 全面打磨(CJK font、contrast、disabled 色、preview cleanup、DPI helper) |
| `ed29762` | Sprint 2:TDBG 改用 SAM3X TC compare-interrupt 引擎(取代 spin-loop) |
| `0cc2ab2` | (Sprint 2 之前)TDBG spin-loop 的 `noInterrupts()` 提到 cluster 級 |
| `ff37259` | GpioTab:雙欄 + 漸進建構 + toggle button(解決 66-pin panel 開啟卡頓) |
| `bb9d7d0` | CLAUDE.md 更新 |
| `90d95e0` | App 改名「Arduino應用軟體」 |
| `569599b` | **TDBG bug 1**:ISR 永遠不 fire — `RC=0` 對 fresh `CV=0` 沒有 compare edge(SAM3X TC 是 edge-triggered)。修法:setup 同步 drain `delta=0` 前綴、RC floor ≥ 1、disarm 後清 NVIC pending |
| `6797529` | TDBG host-side retimer:把 password 1 的 380 ns delta 等比例放大到 ≥ 1.5 µs(引擎 floor 以上),長 gap 不動 |
| `b1e8620`→`c39653f` | **TDBG bug 2 嘗試**:`TC6_Handler` 在 .ino(C++)被 name-mangle,沒 override vector 的 weak alias。試 `extern "C"` + forward decl + LED probe,**沒成功** |
| `7c51b35` | TDBG play 改 fire-and-forget(送出 PLAY_STARTED 就 return,不等 PLAY_DONE)— 因為使用者只要訊號打出去 |
| `20de7a3` | **TDBG bug 2 真正解**:`TC6_Handler` 搬到獨立 `tdbg_tc_isr.c`(gcc 編、天生 C linkage,繞開 IDE auto-prototype)。**等使用者實測 LED13 是否亮 / D23 是否有波形** |
| `86e33e4` | **Part A 字型**:`("TkDefaultFont", N)` tuple 是 bug(第一元素被當家族名,但它是 named font)→ 解析真正家族存 `_UI_FAMILY`、全面替換;選中 tab 12pt bold |
| `36cda33` | **Part B 統一 Connect**:三個 tab 共用 App 層級一條 serial + 一組 Connect/Disconnect |

---

## 3. 各功能目前狀態

| 功能 | 狀態 |
|---|---|
| 燒錄 ROM | 穩定(本 session 沒動核心邏輯,只動了連線釋放方式 → 待測) |
| GPIO | 功能完整;本 session 改了 panel 建構(雙欄/漸進/toggle)+ 連線架構 → 待測 |
| TDBG | **協定鏈路全通**(LOAD/CRC/PLAY_STARTED 都 OK);**ISR 能否真正 fire 並輸出 D23 波形 = 最後一個未經使用者確認的關鍵點**(commit `20de7a3` 的 .c 檔修法) |
| 波形錄製 | framing race 已修;連線架構改了 → 待測 |
| UI 字型 | 已修(Part A) |
| 統一 Connect | 已寫完(Part B)→ **完全沒硬體驗證** |

---

## 4. 關鍵踩雷與結論(下一手必讀)

這個 session 花最多時間在 TDBG。以下是血淚結論,別重蹈覆轍:

### (A) SAM core 的 `core_cm3.h` 不對稱
`<Arduino.h>` 在 Arduino SAM 1.6.x **不會**把 `<core_cm3.h>` 完整拉進來:
`CoreDebug` 有、`DWT` 沒有。所以 `.ino` 頂端自己 `#ifndef DWT_BASE { 手寫 DWT_Type }`,
**只手寫 DWT,不要碰 CoreDebug**(會撞名)。

### (B) `.ino` 是 C++ 編譯 → vector handler 必須是 C linkage
Arduino IDE 把 `.ino` 當 C++ 編、還會 run ctags auto-prototype。所以在 `.ino` 裡寫
`void TC6_Handler(void)`(或甚至 `extern "C"` 版)**都無法可靠 override** SAM core
startup 的 weak alias(symbol 被 name-mangle 成 `_Z11TC6_Handlerv`)。vector 停在
`Dummy_Handler`(`while(1);`)→ 第一個中斷觸發整顆 MCU 卡死 → PLAY_STARTED 之後
完全沒反應、後續任何命令 timeout。
**唯一可靠解**:把 ISR 放在獨立 `.c` 檔(gcc 編,天生 C linkage,不被 auto-prototype 動)。
→ 這就是 `tdbg_tc_isr.c` 存在的原因。它 `extern` 參照 `.ino` 裡的共用 state(那些
state 在 .ino 被放進 `extern "C" { }` block、且不是 static)。

### (C) SAM3X TC compare-match 是 edge-triggered
CPCS 中斷只在「CV 從不等於 RC 變成等於 RC」的 clock edge 觸發。開機 CV=0、若 arm
時 RC 也=0,SWTRG 把 CV reset 成 0(沒有 transition)→ 不 fire。所以:
- parser 的 anchor 慣例讓 event[0] 的 delta=0 → 不能直接拿去 arm TC
- `tdbgPlayOnceTc()` setup **同步 drain 開頭所有 delta=0 的 event**(直接寫 PIO),
  之後才用第一個 delta>0 的 event arm,保證 RC ≥ 1
- 所有寫 `TC_RC` 的地方都 floor `deadline_inc ≥ 1`

### (D) TC channel 是 16-bit
單次 RC compare 最多 65535 ticks(@ 42 MHz ≈ 1.56 ms)。長 gap(670 ms)由 ISR
**chunk**:`tdbgRemainCpu` 每次扣 `TDBG_TC_CHUNK_CPU`(65536 CPU cycles)、推進
deadline,不寫 pin、不 advance event,直到 remain 歸零才真正 fire。

### (E) TDBG 引擎的時序 floor
ISR round-trip ~50-60 CPU cycles(~25-30 TC ticks @ 42 MHz)。**沒辦法忠實重現
380 ns(32-cycle)短脈衝**。使用者澄清:這支腳是 **clock**,前 32 cycle 是 preamble
給對方 PLL 鎖頻,週期不嚴格、「盡量短」即可、目標 ~1.5 µs。所以 host 端
`tdbg_retime_for_engine()` 把整個 pattern 等比例放大到 min half-period ≥ 1.5 µs
(scale ~3.94×),長 gap 不動(總時長仍 ~1.34 s)。**要更快(<700 ns)只能改 DMA 引擎
或 TC TIOA 硬體模式 — 兩者都還沒做。**

### (F) 字型 tuple bug
`font=("TkDefaultFont", 10)` 把 `"TkDefaultFont"` 當**家族名**(無效)→ fallback 醜字。
named font 只能用字串 `font="TkDefaultFont"`(但不能改 size)。要 size/bold 必須給
真家族名 tuple。已解析成 `_UI_FAMILY`(`tkfont.nametofont("TkDefaultFont").actual("family")`)。

---

## 5. 架構:統一 Connect(Part B,commit `36cda33`)

**這是本 session 最後也最大的改動,風險最高,完全沒硬體驗證。**

- **App** 持有唯一一條 serial(`self._ser`)、一個 `threading.Lock`(`self._serial_lock`)、
  三個 session 物件(`gpio_session` / `tdbg_session` / `record_session`)。
- Port 列下方有 conn_row:`Connection: <狀態> [Connect] [Disconnect]`。
- `App._on_app_connect()` → transient thread 跑 `open_due_link()` → 成功就建立三個
  session(都 `ser=共用serial, lock=共用lock`,各自 log 到自己的 tab)→ 對三個 tab
  呼叫 `set_connected(True)`。
- `App.disconnect_then(on_done)` → transient thread 關 session(borrowed,detach 不關 port)
  + 關 serial → `set_connected(False)`。Disconnect 按鈕跟 FlashTab 都走這個。
- **session 改動**(core):`__init__` 多收 `ser` / `lock`。`ser` 給了就 borrow(open()
  是 no-op、close() 只 detach)。GPIO/TDBG 的 serial 交易包在 `_serial_guard(self._lock)`。
  `ser=None`(預設)維持原本「自己開 port」行為 → CLI 不受影響。
- **各 tab 改動**(GUI):移除自己的 conn_row / Connect / Disconnect / 連線 method;
  `_session` 變成 **property** → `self.app.<x>_session`(所以 `set_pin`/`load`/`play`/
  `start`/`stop` 等操作碼**完全沒改**);新增 `set_connected(bool)` 只開關自己的操作控件。
  Pin 選擇器留在各 tab。
- `App.set_recording(active)`:錄製中 gate 掉 GPIO/TDBG(live_loop 獨佔 serial)。
- **FlashTab**:`_on_start` 改呼叫 `app.disconnect_then(self._do_start)`(取代舊的
  `release_port_then`)。燒錄完共用連線維持 disconnected(erase 結束了 idle loop)。

**已移除的舊 API**(若有殘留參照要注意):`tab.is_connected()`、
`app.any_other_tab_holding_port()`、`app.release_port_then()`、各 tab 的
`disconnect_for_other()` / `_do_close_session()` / `_on_connect*` / `_on_disconnect*`。

---

## 6. 待辦 / 待驗證(優先序)

### P0 — 等使用者硬體實測回報
1. **TDBG ISR 是否真的 fire**(commit `20de7a3` 的 .c 檔修法是否生效):
   - re-upload `.ino`(會多帶 `tdbg_tc_isr.c`,Arduino IDE 自動掃同資料夾)
   - Connect → 按「校準」→ 看 Due 板上 **D13 LED 是否亮**(ISR 探針)+ LA 看 **D23
     是否有 4 段不同週期方波**
   - LED 亮 + D23 有波形 = TDBG 終於通了
   - LED 不亮 = vector 還是沒 hook(極不可能,但若如此要查 Arduino SAM core 版本)
2. **Part B 統一 Connect 四個 tab 實測**(GPIO 設讀、TDBG 送出/校準、RECORD 開始/結束、
   Flash 燒錄)— 純 host 改動,不用 re-upload,但連線 plumbing 整個重寫了。

### P1 — 已知小瑕疵
- 連線 banner(FW 版本、chip detect)目前只 log 到 **TDBG tab**(因為 `open_due_link`
  用 `tdbg_tab._log_callback_threadsafe`)。使用者在別的 tab 看不到。可考慮改成寫到
  status bar 或廣播到所有 tab。
- 燒錄完成後沒有明確 log 提示「需 reset Due + 重新 Connect 才能再用 GPIO/TDBG/RECORD」。

### P2 — 未做的大功能
- **TDBG <700 ns 精度**:若使用者之後需要忠實重現原始 380 ns(目前是 retimer 放大到
  1.5 µs),要做 DMA-driven 引擎(DMAC 餵 PIO state buffer)或 TC TIOA 硬體模式
  (限特定 pin)。`CLAUDE.md` 跟舊 plan 有討論。預估 2-3 天。

---

## 7. 驗證 / 測試方式

- **沒有自動測試**。一切 hardware-in-the-loop。
- host 端 retimer / calibration pattern 可用純 Python 驗證(不需硬體):
  ```bash
  python3 -c "import binFileTransfer_core as c; print(c.tdbg_calibration_pattern()[1][:5])"
  ```
- 改完 `.py` 後一定 `python3 -c "import ast; ast.parse(open('檔名').read())"` 過 syntax。
- `binFileTransfer_core.py` import 時有 CRC self-test(`assert tdbg_crc16(b"123456789")==0x29B1`)。
- GUI 沒辦法在無 X display 的環境跑(tkinter),只能 ast.parse + 靜態檢查 + 使用者實測。

---

## 8. 跟使用者互動的注意事項

- 使用者用繁體中文。回報常附 GUI log 截圖 + LA 截圖。
- **每次 TDBG 問題先確認 banner 時間戳**確定跑的是最新韌體(踩過好幾次舊韌體的坑)。
- 使用者要求風格:**先仔細分析再給方案、不要亂猜**;大改動先給 plan 再做。
- TDBG 那支腳是 **clock**,不是 data;對方靠 32-cycle preamble 鎖頻後 latch 後續 data。
- 板子上沒有(或看不到)外接 LED;D13 是內建 LED,目前被當 ISR 探針用
  (`tdbg_tc_isr.c` 進 ISR 點亮、disarm 熄滅)。功能穩定後可拆掉探針。

---

## 9. 一鍵抓現況

```bash
git log --oneline -20          # 本 session 的足跡
git show 36cda33 --stat        # 最新(統一 Connect)
git show 20de7a3 --stat        # TDBG .c 檔修法
sed -n '1,60p' binFileProgram/tdbg_tc_isr.c   # ISR + 為什麼獨立成 .c 的完整說明
```
