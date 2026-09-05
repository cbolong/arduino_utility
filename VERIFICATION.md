# 驗證矩陣(Verification Matrix)

每個功能 × 每種情境的驗證方式。**自動** = `python tests/run_all.py` 內的離線測試
(無硬體、offscreen);**HIL** = 需接 Arduino Due 的硬體在環手動步驟。
新增功能時:先在這裡加情境列,再補對應的自動測試;修 bug 時必須先加一條會紅的列。

## 1. 連線 / 斷線

| 情境 | 預期行為 | 驗證 |
|---|---|---|
| 正常連線(sketch 已在 idle) | fast-ping 命中,<1s 連上,不 reset 板子 | HIL;fast/fallback 分流邏輯:自動 `test_smoke_gui S3` |
| 連線(板子剛上電 / 舊 sketch) | fast-ping 0.5s miss → 自動 fallback DTR reset → 30s 內握手 | HIL |
| 沒接板子按連線 | `open_due_link` 回 None → status 顯示失敗,連線鈕可再按 | 自動 `test_stuck_states R7` |
| 連線後三個 session 建立 | gpio/tdbg/record session 就緒、側欄「已連線」 | 自動 `test_smoke_gui S3` |
| 斷線時 session close 拋例外 | callback 仍然鏈到,UI 回到未連線 | 自動 `test_smoke_gui S10` |
| 連線中亂按(mash) | `_conn_busy` 擋重入,只跑一次握手 | 自動 `test_stuck_states R8` |
| 頁面在連線後才首次點開 | 晚建頁面繼承已連線狀態 | 自動 `test_smoke_gui S2+S3` 組合 |
| 關閉視窗(閒置/連線中) | closeEvent 依序關 session→serial→workers,不掛 | 自動 `test_smoke_gui S12` |

## 2. 燒錄 ROM

| 情境 | 預期行為 | 驗證 |
|---|---|---|
| 正常燒錄 128KB | 32 chunks + CRC32 verify + DATA_COMPLETED | 自動 `test_core_protocol C10`;真晶片 HIL |
| 小於 128KB 的檔 | 補 0 到 128KB 再燒 | 自動 C10(1000B 檔) |
| 空檔 / 不存在 / >128KB | 本地拒絕,不碰序列埠 | 自動(`program_firmware` 前置檢查;C10 家族) |
| 晶片未偵測(ID 0x0) | MCU 重探一次→仍無→`ARDUINO_ERROR`;host 顯示可行動提示 | 自動 `test_core_protocol C11`;重探本體 HIL |
| CRC 驗證失敗(舊 sketch,無區塊回報) | 失敗 + 通用提示(升級 .ino 可得定位) | 自動 `test_core_protocol C12` |
| CRC 失敗 — 單一區塊損毀 | 區塊差異表點名該區塊 + 「傳輸損毀」診斷 | 自動 C13 |
| CRC 失敗 — 64KB 週期重複(位址線卡住) | 診斷指名 A16/Due D24 | 自動 C14 |
| CRC 失敗 — 全片未寫入(WE 斷) | 32 區塊全同 → 指向寫入路徑,**不得**誤報位址線 | 自動 C16 |
| 補頁位元組 | 不足 128KB 補 0xFF(leave-erased,可暴露 erase 不完全) | 自動 C15 |
| 燒錄中側欄狀態 | 「燒錄中…」→ 結束回「未連線」 | 自動 `test_smoke_gui S9` |
| 燒錄進度回報 | `on_progress` 每 chunk ACK 觸發 (1..32,32);callback 拋例外不中斷燒錄 | 自動 C18 |
| GUI 進度條 | 燒錄中顯示 chunk n/32 + %,結束隱藏 | offscreen 截圖(視覺);progSig 佈線=smoke S9 家族 |
| FF-skip 提速 | firmware 跳過 0xFF 位元組(~18s/108K 檔);CRC 掃描仍驗補頁區 | HIL(對照 Timing 報告 program 時間) |
| 燒錄前有 GPIO/TDBG 連線 | 先釋放共用連線再開始 | 自動 S9(disconnect_then 鏈) |
| 燒錄後想用 GPIO/TDBG/RECORD | 需按 Due RESET + 重新連線(訊息有提示) | HIL |
| worker 在 done 前死掉 | `_on_worker_failure` 還原按鈕+解鎖 port | 自動 `test_stuck_states R6` |
| chunk 中途 MCU 斷線 | `_wait_for_line` timeout → False → UI 恢復 | 自動(C 家族 timeout 路徑);真拔線 HIL |

## 3. GPIO

| 情境 | 預期行為 | 驗證 |
|---|---|---|
| set_pin OUTPUT HIGH/LOW、INPUT | `GPIO_SET …` → `GPIO_OK` | 自動 `test_core_protocol C1` |
| 非法 mode/value | 本地擋下,不上線 | 自動 C2 |
| read_pin 正常 | `GPIO_VALUE p 0/1` → LOW/HIGH,圈圈+綠燈更新 | 自動 C3 + `test_smoke_gui S4` |
| read_pin 無回應 | ~1s 放棄(`GPIO_READ_TIMEOUT_S`),回 None | 自動 C4 |
| Read All 全 66 腳 | 逐腳讀,每腳結果更新對應列 | 自動 S5 |
| Read All 中按斷線 | `_abort` set → 最多再等 1 腳就停 | 自動(stuck_states R5 家族);真斷線 HIL |
| 自動讀取 timer | 斷線即停、checkbox 取消 | 自動 `test_stuck_states R5` |
| 自動讀取不洗版 | auto sweep quiet(無 send/recv log,錯誤照常);手動 Read All 照常 log | 自動 C17 |
| 圈圈三態視覺 | 空心=未讀、灰實心=LOW、綠實心=HIGH | offscreen 截圖(QSS) |
| 戳 flash 匯流排腳 | 允許但匯流排狀態未定義 — 燒錄前需 RESET | HIL(行為 by design) |
| reconnect 後 UI vs MCU 腳位狀態 | fast-connect 不 reset:MCU 保留舊態,UI 歸零 — 點圈圈重讀對齊 | HIL |

## 4. TDBG

| 情境 | 預期行為 | 驗證 |
|---|---|---|
| preset 1/2/3 送出 | `TDBG_PRESET n pin` → `TDBG_PRESET_OK n` | 自動 `test_core_protocol C5` + `test_smoke_gui S6` |
| preset 中途斷線 race | done 後按鈕仍可恢復(重連即用) | 自動 S7 |
| load 波形(庫 API) | STOP-drain → LOAD → READY → blob → LOADED CRC | 自動 `test_tdbg_session T1` |
| load 前殘留播放 chatter | drain 吸收 PLAY_STARTED/PLAY_DONE,握手不錯亂 | 自動 T2 |
| load CRC 不符 | 回 False + CRC mismatch log | 自動 T3 |
| play / play_loop n | fire-and-forget 寫線即回;n=0 拒絕 | 自動 T4 |
| 校準 pattern | 可載入;非 anchor 的 delta 全 ≥ 60 cycles | 自動 T5;邏輯分析儀看四組週期 HIL |
| 本地驗證(腳號/初態/事件數) | 全部不上線直接拒 | 自動 T6 |
| worker 例外 | `_on_worker_failure` 清 busy | 自動 `test_stuck_states R4` |

## 5. 波形錄製(RECORD)

| 情境 | 預期行為 | 驗證 |
|---|---|---|
| start→live→stop 完整回合 | STARTED → LIVE 心跳驅動即時燈 → STOPPED/DATA/blob/DONE + CRC | 自動 `test_record_session RS1` + `test_smoke_gui S8` |
| MCU blob 短送 | `stop()` 失敗 + `tail_hex` 診斷(可分辨 MCU 短送 vs host 搶讀) | 自動 RS2 |
| blob CRC 不符 | `stop()` 回 None,不崩 | 自動 RS3 |
| 腳數 0 / >4 / 非法腳號 | 本地拒絕 | 自動 RS4 |
| start 拋例外(如 USB handle 失效) | failSig → 頁面重置,可再按 | 自動 `test_stuck_states R1` |
| 錄後摘要持久顯示 | 縮圖旁「N 邊緣 · X ms」,不被狀態列洗掉 | offscreen(smoke S8 家族) |
| stop 拋例外 | stopSig(None) → 頁面重置 | 自動 R2 |
| 同一腳選兩列 | `_selected_pins` 去重成一支 | 自動 `test_stuck_states R9` |
| RECORD_OVERFLOW | host log warn,錄製繼續(MCU 停累積) | HIL(需 >4096 邊緣) |
| 錄製中按斷線 | live thread 收斂、port 正常釋放 | HIL |

## 6. SGPIO 被動解碼

| 情境 | 預期行為 | 驗證 |
|---|---|---|
| 標準幀解碼(N drives × 3 bits) | 每 drive 正確 Activity/Locate/Fault | 自動 `test_sgpio G1` |
| 幀長不符 framing | `sgpio_frame_valid` False + parse raise + UI 標紅 | 自動 G2、G10 |
| 非二進位字元 | 拒絕 | 自動 G3 |
| header bits | 前導 bits 跳過,drive 欄位不錯位 | 自動 G4 |
| MSB/LSB order | 翻轉欄位顯著性 | 自動 G5 |
| bits/drive ≠ 3 | 只給 value,無具名欄位 | 自動 G6 |
| framing 設定不合理 | `validate_config` 擋下 | 自動 G7 |
| session start→frame→stop | fake serial 全回合 | 自動 G8 |
| 腳位重複 / 超範圍 / 壞 framing | 本地拒絕不上線 | 自動 G9 |
| GUI 分頁(第 5 項)即時解碼 + 標紅 + 停止 | offscreen 全流程 | 自動 G10 |
| frame 分組顯示 | `100 010 001 111`(header 以 `|` 隔開) | 自動 G11 |
| heartbeat 同幀跳過重繪 | 幀數遞增但不重 parse;新幀才 parse | 自動 G10(spy) |
| RECORD/SGPIO 互斥 | firmware 雙向拒絕 + GUI 灰化 sibling 分頁 | firmware HIL;GUI 灰化=自動(set_recording origin) |
| **真實 SGPIO 來源解碼** | 接 HBA/backplane → drive 表格對應實際 LED | HIL |
| **TDBG loopback 自測** | TDBG 產生已知 SGPIO 圖樣 → 接回 SGPIO 3 腳 → 解碼應等於送出 | HIL(免真 initiator) |
| 電平/共地 | 確認 3.3V、共地;非 3.3V 加 shifter | HIL |

## 7. 波形預覽

| 情境 | 預期行為 | 驗證 |
|---|---|---|
| 秒級慢訊號 | fit-to-width:整段塞進視窗,HIGH/LOW 全部可見 | 自動 `test_waveform W1+W2` |
| 放大/縮小/整體 | zoom 變寬可捲、整體回 fit | 自動 W3 |
| 恆 HIGH / 恆 LOW channel | 壓在對應軌上,一眼可辨 | 自動 W4 |
| 多 channel 軌線 | 每 channel 2 虛線軌 + 1/0 標記 + 置中標籤 | 自動 W5 |
| 縮圖 | 無軌線、點擊開預覽 | 自動 W6 |
| 放大後拖曳平移 | 左鍵拖 = 捲動,游標手勢回饋 | 自動 W7 |

## 8. 窮舉驗證(test_exhaustive.py)

| 情境 | 預期行為 | 驗證 |
|---|---|---|
| `set_pin` mode × value 全 12 組 | 合法組合上線且 wire 格式正確;非法組合本地拒絕不上線 | 自動 E1 |
| `parse_sgpio_frame` bits 1..8 × MSB/LSB 全 16 組 | value 算術正確;具名欄位僅 bits==3 | 自動 E2 |
| `validate_config` 各參數 min-1/min/max/max+1 + frame_len 上限 | 邊界內外皆正確 | 自動 E3 |
| `parse_record_blob` 事件數 0/1/多 × pin 數 1..4 | mask bit i → pins[i];畸零長度 raise | 自動 E4 |
| `play` iterations {-1,0,1,2,5} | <1 拒絕;==1 送 PLAY;>=2 送 PLAY_LOOP | 自動 E5 |
| 五個 session 進入點 × pin {-1,0,65,66} | 越界一律本地拒絕、不上線 | 自動 E6 |
| capture 中「首次」建立的頁面 | 必須繼承鎖定;capture 結束後恢復;斷線清除鎖 | 自動 E7 |
| 連線指示器 + 停用按鈕對比 | 啟動時與所有狀態皆 ≥4.5:1(程式化計算) | 自動 E8 |

## 9. Worker / 基礎設施

| 情境 | 預期行為 | 驗證 |
|---|---|---|
| worker job 拋任意例外 | 執行緒不死、busy 歸位、log 有類型+traceback | 自動 `test_stuck_states R3` |
| on_error 自己壞掉 | worker 依然存活,下一個 job 照跑 | 自動 R3 |
| on_failure hook | 頁面 `_on_worker_failure` 收到並重置狀態 | 自動 R4/R6 |

## 已知尚未自動化(待補清單)

1. 燒錄 chunk 中途斷線的專屬模擬(目前只涵蓋 timeout 家族)。
2. Firmware 端行為全部只能 HIL(無法在 host 端編譯/模擬 SAM3X)。

(連線 mash 與 RECORD 同腳去重已自動化:`test_stuck_states R8/R9`。)

## HIL 快速清單(換版必跑)

1. 燒 sketch → GUI 連線(看 fast vs fallback log)→ 燒一顆已知 `firmware.bin` → CRC OK。
2. GPIO:任一腳圈圈讀取、Read All、自動讀取 1s。
3. TDBG:preset 1 用邏輯分析儀看 64-bit pattern;校準 pattern 看四組週期。
4. RECORD:錄 D22 手動切 HIGH/LOW → 預覽看方波 → 放大/拖曳。
5. SGPIO:接三支腳(SClock/SLoad/SDataOut)→ 開始 → drive 表格對應實際燈號;或用 TDBG loopback 自測。
6. 拔線/插回、按 RESET、再連線 — 全程 UI 不卡死。
