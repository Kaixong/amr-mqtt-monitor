# AMR MQTT 監控系統（amr-mqtt-monitor）

一個在單台 Windows 電腦上就能完整運作的 **AMR（自主移動機器人）車隊監控** 練習專案：

- 模擬 5 台 AMR 在倉庫裡接任務、移動、充電、偶爾故障
- 即時顯示**這台 Windows 電腦本身**的 CPU / 記憶體 / 磁碟 / 網路 / 電池
- 自動**偵測異常**（斷線、故障、低電量、卡住、資源過高…），寫入資料庫並在網頁上警報
- **記錄每台車的狀態變化**到 SQLite，在儀表板顯示最近 1 / 24 小時的**稼動率**
- 所有資料都透過 **MQTT 5** 傳遞（後端走 TLS 加密），網頁直接以 WebSocket 訂閱，沒有額外的後端 API

| 頁面 | 網址 |
|---|---|
| 儀表板：倉庫地圖、車隊狀態、稼動率、電腦使用狀況、Broker 狀態 | http://localhost:8080 |
| 異常記錄：篩選、確認、匯出 CSV | http://localhost:8080/alarms.html |

---

## 目錄

1. [使用技術](#1-使用技術)
2. [系統架構](#2-系統架構)
3. [目錄結構](#3-目錄結構)
4. [MQTT 設計](#4-mqtt-設計)
5. [異常偵測](#5-異常偵測)
6. [安全性](#6-安全性)
7. [MQTT 最佳實務對照表](#7-mqtt-最佳實務對照表)
8. [第一次安裝](#8-第一次安裝)
9. [開啟](#9-開啟)
10. [測試](#10-測試)
11. [關閉](#11-關閉)
12. [日常維運](#12-日常維運)
13. [疑難排解](#13-疑難排解)
14. [已知限制](#14-已知限制)

---

## 1. 使用技術

| 分類 | 技術（實測版本） | 用途 |
|---|---|---|
| 作業系統 | Windows 11 + **WSL2**（3.0.3）+ **Ubuntu 24.04** | Linux 環境跑在 Windows 上；專案放在 WSL 檔案系統（較快） |
| 容器 | **Docker Engine** 29.9 + **Docker Compose** v5.6 | 執行 broker 與網頁伺服器，`restart: unless-stopped` 會隨 Docker 自動啟動 |
| 訊息中介 | **Eclipse Mosquitto** 2.1.2（`eclipse-mosquitto:2`） | MQTT broker：TLS 8883、WebSocket 9001、Secure WebSocket 9443；帳密 + ACL、持久化、`$SYS` 統計 |
| 通訊協定 | **MQTT 5.0** | Last Will、Retained、QoS 0/1、Session / Message Expiry、Response Topic + Correlation Data、Content Type、User Properties |
| 加密 | **TLS 1.2+**，憑證由 **OpenSSL** 3.0 產生的私有 CA 簽發 | 後端連線加密並驗證 broker 身分 |
| 網頁伺服器 | **nginx** 1.31（`nginx:alpine`） | 提供 `dashboard/` 靜態檔，port 8080 |
| 後端程式 | **Python** 3.12 + **paho-mqtt** 2.1.0 | 模擬器、Windows 監控、異常偵測服務、狀態記錄服務（各自一個 MQTT 用戶端） |
| 資料庫 | **SQLite** 3.45（Python 內建） | 異常記錄 `data/alarms.db`、車輛狀態區間 `data/telemetry.db` |
| Windows 資料來源 | **Windows PowerShell** 5.1 + **CIM/WMI** 效能計數器 | 讀取 Windows 本機 CPU（含每個執行緒）、記憶體、磁碟、網路、電池 |
| 前端 | 原生 **HTML / CSS / JavaScript** + **MQTT.js** 5.10.1（CDN） + inline **SVG** | 不需要建置工具；地圖、圖表都是 SVG |
| 測試 | **pytest**（單元）、Python **unittest**（整合）、**flake8** | 單元測試（車輛狀態機、異常規則、稼動率）＋整合測試（實際連 broker） |
| CI | **GitHub Actions**（`.github/workflows/ci.yml`） | 每次 push / PR：flake8 + 單元測試；再在 runner 上啟動整套服務跑整合測試 |
| 工具 | mosquitto-clients 2.0.18、git 2.43、bash | 手動測試、版本控制、啟動腳本 |

---

## 2. 系統架構

```mermaid
flowchart LR
  subgraph WIN["Windows 11"]
    BR["瀏覽器<br/>儀表板 / 異常記錄"]
    PS["PowerShell 取樣程式<br/>(隱藏視窗，CIM 查詢)"]
  end
  subgraph WSL["WSL2 · Ubuntu 24.04"]
    subgraph DK["Docker（只開放 127.0.0.1）"]
      NG["nginx :8080<br/>dashboard/ 靜態檔"]
      MQ[("Mosquitto<br/>:8883 MQTT/TLS<br/>:9001 MQTT/WebSocket<br/>:9443 MQTT/WSS")]
    end
    SIM["simulator/main.py<br/>5 台 AMR"]
    SYS["monitor/sysmon.py"]
    ALM["alarm/alarmd.py<br/>規則引擎"]
    DB[("data/alarms.db<br/>SQLite")]
    REC["recorder/recorder.py<br/>狀態記錄 / 稼動率"]
    TDB[("data/telemetry.db<br/>SQLite")]
  end
  BR -- "HTTP 取得網頁" --> NG
  BR <-- "MQTT 5 over WebSocket :9001<br/>(帳號 dashboard)" --> MQ
  SIM <-- "MQTT 5 / TLS :8883<br/>狀態 / 指令 / 回覆" --> MQ
  SYS -- "MQTT 5 / TLS :8883<br/>電腦使用狀況" --> MQ
  ALM <-- "MQTT 5 / TLS :8883<br/>訂閱遙測、發布異常" --> MQ
  ALM --- DB
  REC <-- "MQTT 5 / TLS :8883<br/>訂閱車輛狀態、發布稼動率" --> MQ
  REC --- TDB
  PS -- "stats.json（每 2 秒）" --> SYS
  SYS -- "heartbeat" --> PS
```

### 元件說明

| 元件 | 做什麼 | 重點設計 |
|---|---|---|
| **Mosquitto** | 所有資料的集散中心 | 沒有明碼 TCP；關閉匿名；每個程式獨立帳號與最小權限 ACL；持久化讓保留訊息與 session 在重啟後還在 |
| **simulator** | 5 台 AMR，每台一個 MQTT 連線，每 0.5 秒發布狀態 | 每台有自己的 Last Will（斷線時 broker 代發 `offline`）；持久 session；以 Correlation Data 回覆指令 |
| **sysmon** | 把 Windows 使用狀況發布到 MQTT | 從 WSL 裡看到的是 WSL 虛擬機，不是 Windows；所以另外啟動一個**獨立、隱藏的 Windows PowerShell** 取樣，透過檔案交換資料（見下方） |
| **alarmd** | 訂閱所有遙測，套用規則，產生 / 恢復 / 確認異常 | 規則（`rules.py`）是純邏輯、可單元測試；每筆異常寫入 SQLite；自己斷線時暫停判斷避免誤報 |
| **recorder** | 把每台車的狀態變化記成「狀態區間」，每 10 秒發布稼動率 | 邏輯（`tracker.py`）是純邏輯、可單元測試；只在狀態改變時寫入 SQLite（不是每筆遙測），保留 30 天；用發布者的 `ts` 計時；超過 3 秒沒回報記為「離線」；忽略舊的 retained `offline`，避免重啟時誤記離線 |
| **nginx** | 只提供靜態檔 | 網頁自己用 MQTT.js 連 broker，nginx 不參與資料流 |
| **儀表板** | 即時地圖、車隊、稼動率、電腦、Broker 狀態、警報橫幅 | 指令用 MQTT 5 request/response，按鈕會顯示「已執行 / 被忽略 / 沒有回應」 |
| **異常記錄頁** | 歷史異常列表 | 讀取 retained 的 `alarms/history`；確認動作送到 `alarms/ack` |

### 資料流：一次「車輛斷線」會發生什麼

1. AMR 停止發送 → 3 秒後 **alarmd** 判定 `OFFLINE`，寫入 SQLite，發布 `alarms/event`（raised）與 `alarms/active`
2. 儀表板收到後：紅色警報橫幅、選單徽章 +1、地圖紅圈、卡片顯示「最後回報 X 秒前」並停用按鈕、（可選）提示音
3. 約 7.5 秒（keepalive 5 秒 × 1.5）後 broker 也確認斷線並代發 Last Will `amr/<id>/online = offline`
4. 車輛重新連上 → alarmd 發布 `cleared`，記錄恢復時間；使用者可在異常記錄頁「確認」

### 資料流：網頁按下「暫停」會發生什麼

1. 網頁發布 `amr/AMR-01/cmd`：內容 `{"cmd":"pause","req_id":"…"}`，屬性 **Message Expiry 5 秒**、**Response Topic** `amr/AMR-01/cmd/result`、**Correlation Data**（隨機 ID）
2. broker 依 ACL 確認 `dashboard` 可以寫這個主題，轉給 AMR-01（若 AMR-01 短暫斷線，持久 session 會先保留，超過 5 秒則丟棄）
3. AMR-01 執行後回覆到 `amr/AMR-01/cmd/result`，**原樣帶回 Correlation Data**
4. 網頁用 Correlation Data 找到對應的請求，顯示「✓ 已執行」；3 秒沒回覆則顯示「沒有回應」

### 為什麼 sysmon 要用「檔案 + 隱藏 PowerShell」

- 在 WSL 裡直接執行 `powershell.exe` 產生的 Windows 程序，會在啟動它的 `wsl.exe` 結束時被一起關掉
- 所以 sysmon 用 `Start-Process -WindowStyle Hidden` 啟動一個**獨立**的取樣程式，它每 2 秒把結果寫到 `logs/sysmon/stats.json`
- 生命週期靠 `heartbeat` 檔：sysmon 正常結束時寫入 `stop` → 取樣程式 2 秒內結束；sysmon 異常死掉 → heartbeat 15 秒沒變化，取樣程式自行結束，不會變成孤兒程序
- 判斷「有沒有變化」只比內容不比時間，所以 WSL 與 Windows 時鐘不同步也不影響

---

## 3. 目錄結構

```
amr-mqtt-monitor/
├── run.sh                   # 啟動 / 停止 / 狀態 / 測試（在 Ubuntu 裡用）
├── docker-compose.yml       # mosquitto + nginx（只綁定 127.0.0.1）
├── requirements.txt         # Python 套件
├── requirements-dev.txt     # ＋ pytest、flake8（開發 / 測試用）
├── .flake8                  # lint 設定
├── .github/workflows/ci.yml # GitHub Actions：lint + 單元測試 + 整合測試
├── simulator/
│   ├── amr.py               # 單台 AMR 的模擬邏輯（狀態機、電量、移動）
│   └── main.py              # 車隊 + MQTT（狀態發布、指令、回覆、Last Will）
├── monitor/
│   └── sysmon.py            # Windows 使用狀況（內含 PowerShell 取樣程式）
├── alarm/
│   ├── rules.py             # 異常規則（門檻在 DEFAULTS）
│   ├── store.py             # SQLite 存取
│   └── alarmd.py            # 異常偵測服務（MQTT 收發）
├── recorder/
│   ├── tracker.py           # 狀態區間與稼動率計算（純邏輯）
│   ├── statedb.py           # SQLite 存取（state_intervals）
│   └── recorder.py          # 狀態記錄服務（MQTT 收發）
├── common/
│   └── mqttutil.py          # 共用：MQTT 5 + TLS 連線、帳密、重連退避、CONNACK 錯誤、PUBLISH 屬性
├── dashboard/
│   ├── index.html           # 儀表板
│   ├── alarms.html          # 異常記錄
│   ├── common.js / .css     # 共用：連線、導覽列、警報橫幅、指令
│   └── config.js            # （自動產生，不進 git）瀏覽器用的 MQTT 帳密 / 網址
├── mosquitto/
│   ├── mosquitto.conf       # broker 設定
│   ├── acl                  # 各帳號的主題權限
│   ├── secrets/             # （自動產生，不進 git）passwd、acl、憑證，僅 broker 可讀
│   └── data/                # （不進 git）broker 持久化資料
├── scripts/
│   ├── setup-auth.sh        # 建立 / 更換 MQTT 帳號密碼、套用 ACL
│   ├── setup-tls.sh         # 建立私有 CA 與 broker 憑證
│   └── windows/
│       ├── start.ps1        # 從 Windows 一鍵啟動並開啟儀表板
│       └── stop.ps1         # 從 Windows 一鍵關閉（含關閉 WSL）
├── tests/
│   ├── test_amr.py          # 單元測試：車輛狀態機與指令規則（不需要啟動服務）
│   ├── test_rules.py        # 單元測試：異常規則與資料庫（不需要啟動服務）
│   ├── test_recorder.py     # 單元測試：狀態區間、稼動率、SQLite（不需要啟動服務）
│   └── itest_broker.py      # 整合測試（需要服務在執行）
├── certs/                   # （自動產生，不進 git）ca.crt / ca.key / server.*
├── .env                     # （自動產生，不進 git）MQTT 密碼與設定
├── data/alarms.db           # （不進 git）異常記錄
├── data/telemetry.db        # （不進 git）車輛狀態區間（稼動率）
└── logs/                    # （不進 git）各服務 log、PID、sysmon 交換檔
```

---

## 4. MQTT 設計

### 連接埠

| Port | 協定 | 使用者 | 說明 |
|---|---|---|---|
| 8883 | MQTT over **TLS** | Python 服務、命令列工具 | 驗證 broker 憑證（`certs/ca.crt`）；**沒有**明碼的 1883 |
| 9001 | MQTT over WebSocket | 瀏覽器（預設） | 只開放本機，見[已知限制](#14-已知限制) |
| 9443 | MQTT over **Secure WebSocket** | 瀏覽器（選用） | 瀏覽器信任 `certs/ca.crt` 後可改用，見[日常維運](#12-日常維運) |

所有 port 只綁定 `127.0.0.1`：這台電腦（含 Windows 端）可以連，網路上的其他裝置不行。

### 主題

| 主題 | 發布者 | QoS / 保留 | 內容 |
|---|---|---|---|
| `amr/<id>/status` | simulator | 0 / retained（24 小時過期） | JSON：位置、方向、電量、狀態、任務、錯誤、里程…，每 0.5 秒 |
| `amr/<id>/online` | simulator / broker | 1 / retained（24 小時過期） | `online`；斷線時 broker 代發 Last Will `offline` |
| `amr/<id>/cmd` | 儀表板 | 1（5 秒過期） | `pause` `resume` `charge` `clear` `go` `cancel`；純文字或 JSON `{"cmd","req_id"}` |
| `amr/<id>/cmd/result` | simulator | 1 | `{"req_id","cmd","ok","queued","queue","state","ts"}`，並帶回請求的 Correlation Data |

**指令規則（`simulator/amr.py`）**，分兩層：

1. **狀態機（`AMR.command`）**：會開始新工作的指令（`go` 派新任務、`charge` 去充電）**只有在「閒置」時才接受**，不會搶掉進行中的任務、充電或暫停中的任務。`pause`（閒置 / 移動中 / 充電中可用）、`resume`（只在暫停時）、`clear`（只在錯誤時）只改變目前工作的執行方式。
2. **排隊（`AMR.request`，MQTT 指令的入口）**：車輛忙碌時收到 `go` / `charge` **不會被拒絕，而是排隊**（最多 3 個，依序），等它下一次閒置時才交給狀態機執行；回覆 `ok: true, queued: true`。`cancel` 清空排隊。暫停、錯誤時排隊會保留，恢復後繼續。
   - `charge` 在已經充電、正在前往充電站或已經排隊時會被拒絕（沒有意義）
   - 閒置時電量低於 20% 車輛會自己去充電，此時排隊中的 `charge` 會被移除，其他指令保留
   - 儀表板卡片顯示「排隊」欄位，按鈕有「派任務 / 去充電 / 取消排隊」
| `system/<host>/stats` | sysmon | 0 / retained（24 小時過期） | Windows 使用狀況 JSON，每 2 秒 |
| `system/<host>/online` | sysmon / broker | 1 / retained（24 小時過期） | Last Will |
| `alarms/active` | alarmd | 1 / retained | 進行中的異常 |
| `alarms/history` | alarmd | 1 / retained | 最近 300 筆異常（新到舊） |
| `alarms/event` | alarmd | 1 | `{"action": "raised"/"cleared"/"acked", "alarm": {...}}` |
| `alarms/ack` | 儀表板 | 1 | `{"id": 12, "by": "..."}` 或 `{"all": true, "by": "..."}` |
| `alarms/service/online` | alarmd / broker | 1 / retained | Last Will（儀表板據此顯示「異常偵測服務離線」） |
| `stats/utilization` | recorder | 1 / retained | 每 10 秒：`{"ts","since","windows":{"3600":{...},"86400":{...}}}`，每台車 `{"total" 秒, "utilization" %, "states": {狀態: %}}` |
| `stats/recorder/online` | recorder / broker | 1 / retained | Last Will |
| `$SYS/broker/...` | mosquitto | — | broker 統計（每 5 秒），顯示在儀表板 |

所有 JSON 訊息都帶 **Content Type** `application/json` 與 **Payload Format Indicator**（UTF-8）；文字訊息帶 `text/plain`。

### 用到的 MQTT 機制與理由

| 機制 | 用在哪 | 理由 |
|---|---|---|
| **QoS 0** | 高頻遙測（status、stats） | 下一筆很快就來，掉一筆無所謂，省開銷 |
| **QoS 1** | 指令、回覆、上下線、異常 | 不能遺漏 |
| **Retained** | 最新狀態、上下線、異常清單 | 新開網頁立刻有資料，不用等下一筆 |
| **Message Expiry（保留訊息 24 小時）** | 遙測與上下線 | 下線很久的車輛 / 電腦會自動從 broker 消失，不會永遠殘留；異常清單**不**設過期 |
| **Last Will** | 每台 AMR、sysmon、alarmd | 異常斷線時由 broker 代為通知 |
| **Keepalive 5 秒** | AMR | broker 約 7.5 秒內判定斷線 |
| **Session Expiry 60 秒** | AMR | 短暫斷線期間的指令在重連後仍會送達 |
| **Message Expiry（指令 5 秒）** | 指令 | 超過 5 秒的舊指令由 broker 丟棄，不會在重連後被執行 |
| **Response Topic + Correlation Data** | 指令 | 標準的 request/response；回覆只送到自己的 `cmd/result`，網頁用 Correlation Data 對應請求 |
| **Content Type / Payload Format Indicator** | 所有訊息 | 接收端不用猜格式 |
| **User Properties** | 指令 | 標示送出者（`sender: dashboard`） |
| **CONNACK 原因碼** | 所有 Python 服務 | 被拒絕時（例如密碼錯誤）在 log 寫出原因與處理建議，並以退避方式重試 |
| **時間戳記判斷 retained 新舊** | 儀表板、alarmd、recorder | retained 可能是很久以前的資料，不能當成「剛收到」 |

---

## 5. 異常偵測

| 類型 | 等級 | 觸發條件 | 恢復條件 |
|---|---|---|---|
| 失去連線 `OFFLINE` | 嚴重 | 3 秒沒有狀態，或收到 Last Will `offline` | 重新收到狀態 |
| 故障 `ERROR` | 嚴重 | 車輛狀態為 `error` | 狀態不再是 `error` |
| 電量極低 `BATTERY_CRITICAL` | 嚴重 | ≤ 10% | > 15% |
| 電量偏低 `BATTERY_LOW` | 警告 | ≤ 20% | > 25% |
| 疑似卡住 `STUCK` | 警告 | 「移動中」但 10 秒內位移 < 0.2 m | 有位移或不再是移動中 |
| CPU 過高 `CPU_HIGH` | 警告 | ≥ 90% **持續 10 秒** | < 80% |
| 記憶體過高 `MEM_HIGH` | 警告 | ≥ 90% | < 85% |
| 磁碟空間不足 `DISK_LOW_<槽>` | 警告 | 剩餘 ≤ 10% | > 12% |
| 監控中斷 `MONITOR_OFFLINE` | 警告 | sysmon 10 秒沒有回報 | 重新收到資料 |

- **遲滯（hysteresis）**：觸發與恢復門檻不同，數值在門檻附近時不會一直跳
- **生命週期**：`raised`（發生）→ `cleared`（條件消失，自動）→ `acked`（人員確認，可在任何時候）
- **離線時保留其他異常**：車輛離線時無法判斷它的故障有沒有恢復，所以維持原狀
- **防誤報**：alarmd 與 broker 斷線期間、以及重新連上後 10 秒內暫停判斷（broker 重啟不會讓所有車同時誤報）；`./run.sh stop` 會先停 alarmd
- 門檻都在 [`alarm/rules.py`](alarm/rules.py) 的 `DEFAULTS`

### 稼動率（recorder）

- **稼動率 = 「移動中」時間 ÷ 觀測時間**。觀測時間包含閒置、充電、暫停、錯誤與離線；recorder 沒在執行的時間不算（沒有觀測）
- 每台車的狀態變化存成一列 `state_intervals(amr_id, state, started_at, ended_at)`，狀態有 `moving` `charging` `idle` `paused` `error` `offline`
- 「離線」：3 秒沒有狀態，或收到 Last Will `offline`（從最後一次回報的時間開始算）
- 儀表板的稼動率面板可切換最近 1 小時 / 24 小時；資料不足一個區間時會顯示「只有 N 分的資料」

---

## 6. 安全性

| 項目 | 做法 |
|---|---|
| 傳輸加密 | Python 服務與命令列工具走 **TLS 1.2+**（port 8883），用 `certs/ca.crt` 驗證 broker 憑證（CN / SAN = localhost、127.0.0.1）；**沒有明碼 TCP port** |
| 網路暴露 | Docker 只綁定 `127.0.0.1`，網路上的其他裝置連不到 |
| 驗證 | `allow_anonymous false`，帳號：`simulator` `sysmon` `alarmd` `recorder` `dashboard` |
| 授權（ACL） | 最小權限，見 [`mosquitto/acl`](mosquitto/acl)。例如 `dashboard` 只能讀取、對 `amr/+/cmd` 下指令、對 `alarms/ack` 確認，**不能**假冒車輛狀態或稼動率；`alarmd` 不能下指令；`recorder` 只能讀車輛狀態、寫 `stats/*` |
| 密碼保存 | 隨機產生在 `.env`（權限 600、不進 git）；broker 端是雜湊後的 `mosquitto/secrets/passwd`，只有 broker 的使用者（uid 1883）可讀 |
| 私鑰保存 | `certs/ca.key`、`certs/server.key` 權限 600、不進 git |
| 瀏覽器帳密 | `dashboard/config.js` 會被網頁讀取，等同公開，所以 `dashboard` 帳號權限刻意最小 |
| 資源限制 | 每個 listener `max_connections 50`；`max_packet_size 256KB`、`max_queued_messages 1000`、`max_keepalive 60` |
| 回覆主題 | AMR 只會回覆到自己的 `cmd/result`，不接受請求指定其他主題 |

以上每一項都有對應的整合測試（[10.2](#102-整合測試需要服務在執行)）。

---

## 7. MQTT 最佳實務對照表

依 MQTT 5.0 規格、Mosquitto 官方文件與常見的 MQTT 實務指南逐項檢查：

| 實務 | 狀態 | 在本專案 |
|---|---|---|
| 主題不以 `/` 開頭、不含空白、具體分層、包含裝置 ID | ✅ | `amr/<id>/status` 等 |
| 不訂閱 `#`（只訂閱需要的） | ✅ | 每個程式只訂閱自己需要的主題 |
| 依資料性質選 QoS | ✅ | 遙測 0、指令 / 事件 1 |
| Retained 給「最新狀態」用 | ✅ | 狀態、上下線、異常清單、稼動率 |
| 保留訊息要能自動清除 | ✅ | 遙測設 24 小時 Message Expiry |
| Last Will 偵測異常斷線 | ✅ | 每台 AMR、sysmon、alarmd、recorder |
| 適當的 Keepalive | ✅ | AMR 5 秒、其他 10 秒 |
| 持久 session 搭配 Session Expiry | ✅ | AMR 60 秒 |
| 指令設 Message Expiry，避免執行過期指令 | ✅ | 5 秒（整合測試驗證） |
| Request / Response 用 Response Topic + Correlation Data | ✅ | 指令與回覆 |
| 標示酬載格式（Content Type、Payload Format Indicator） | ✅ | 所有訊息 |
| 處理 CONNACK 原因碼 | ✅ | log 寫出原因與建議 |
| 重連退避 | ✅ | 1 → 30 秒 |
| 唯一的 Client ID | ✅ | `sim-AMR-01`、`sysmon`、`alarmd`、`recorder`、`dash-<隨機>` |
| 關閉匿名、每個用戶端獨立帳號 | ✅ | 5 個帳號 |
| 最小權限 ACL | ✅ | `mosquitto/acl` |
| TLS 加密並驗證 broker 憑證 | ✅（後端）／⚠️（瀏覽器預設 ws，見限制） | 8883、9443 |
| 不對外開放 port | ✅ | 只綁 127.0.0.1 |
| 限制連線數與封包大小 | ✅ | `max_connections`、`max_packet_size` |
| broker 持久化 | ✅ | `persistence true` |
| 監控 broker（`$SYS`） | ✅ | 儀表板 Broker 面板 |
| 自動化測試 | ✅ | 單元 77 項、整合 17 項 |
| Topic Alias | ➖ 不採用 | 只在本機傳輸，主題很短，省下的流量可忽略，反而增加重連時的狀態管理 |
| Shared Subscription（`$share/`） | ➖ 不採用 | alarmd 只有一個實例；需要水平擴充時再用 |
| Will Delay Interval | ➖ 不採用 | 斷線要立刻通知；短暫斷線已由 alarmd 的判斷時間吸收 |
| 用戶端憑證（mTLS） | ➖ 不採用 | 單機環境，帳密 + TLS 已足夠；多機部署時建議加上 |

---

## 8. 第一次安裝

> 已經安裝好的電腦可以跳過這一節。

1. **安裝 WSL 與 Ubuntu**（Windows PowerShell，系統管理員）：
   ```powershell
   wsl --install -d Ubuntu-24.04
   ```
2. **安裝 Docker、mosquitto-clients、Python 等**（Ubuntu 裡）：可使用 `D:\claude\mqtt_amr_test\setup-wsl.sh`
   ```bash
   bash /mnt/d/claude/mqtt_amr_test/setup-wsl.sh
   ```
   若它修改了 `/etc/wsl.conf`（啟用 systemd），請在 Windows 執行 `wsl --shutdown` 後重開 Ubuntu。
3. **Python 套件**（Ubuntu 裡，專案資料夾）：
   ```bash
   cd ~/amr-mqtt-monitor
   python3 -m venv .venv
   .venv/bin/pip install -r requirements-dev.txt   # 執行用的套件 + pytest
   ```
4. **MQTT 帳號密碼與 TLS 憑證**：不用手動做，第一次 `./run.sh start` 會自動執行 `scripts/setup-auth.sh` 與 `scripts/setup-tls.sh`。

---

## 9. 開啟

### 方法 A：在 Ubuntu 終端機

```bash
cd ~/amr-mqtt-monitor
./run.sh start
```

成功時會看到四個服務 `started`，最後一行是網址；若有服務起不來會顯示 `FAILED to start` 並回傳錯誤碼。

### 方法 B：在 Windows 一鍵啟動（會自動開啟瀏覽器）

```powershell
powershell -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu-24.04\home\kuma\amr-mqtt-monitor\scripts\windows\start.ps1
```

加上 `-NoBrowser` 則不開瀏覽器。這個腳本也會啟動一個隱藏的 `wsl.exe`，避免沒開終端機時 WSL 自己停掉。

### 確認有沒有開好

```bash
./run.sh status
```

應該看到 `dashboard` 與 `mosquitto` 為 `Up`，`simulator` `sysmon` `alarmd` `recorder` 為 `running`。接著打開 http://localhost:8080 ，右上角應顯示「已連線」、地圖上有 5 台車在移動、車隊下方約 10 秒後出現稼動率、右側有電腦使用狀況與 Broker 狀態。

> 從舊版更新上來：第一次 `./run.sh start` 會自動替新的 `recorder` 帳號產生密碼並重新載入 broker（其他密碼不變）。

---

## 10. 測試

以下指令都在 Ubuntu 終端機、專案資料夾裡執行。

**CI（GitHub Actions）**：每次 push / PR 會自動跑兩個 job：
1. `unit`：`flake8`（設定在 `.flake8`）＋ `./run.sh test`
2. `integration`：在 GitHub 的 Ubuntu runner 上從零執行 `./run.sh start`（自動建立帳號、憑證，用 Docker 啟動 mosquitto + nginx 與所有 Python 服務），再跑 `./run.sh itest` 並確認儀表板有回應。runner 上沒有 Windows，所以 sysmon 只會記錄「無法取樣」，不影響其他測試

本機要跑一樣的 lint：`.venv/bin/flake8`。

### 10.1 單元測試（不需要啟動服務）

```bash
./run.sh test
```

預期最後顯示 `77 passed`：車輛狀態機（只有閒置時接受新任務、暫停 / 繼續、錯誤排除、自動充電）、指令排隊（忙碌時排隊、依序執行、不搶佔、上限、取消）、異常規則、遲滯、異常資料庫、狀態區間與稼動率計算。需要 pytest（`.venv/bin/pip install -r requirements-dev.txt`）。

### 10.2 整合測試（需要服務在執行）

```bash
./run.sh itest
```

預期 `Ran 17 tests ... OK`，約 15 秒。會實際連到 broker 驗證：

| 類別 | 驗證內容 |
|---|---|
| 傳輸 | 1883 明碼 port 不存在；不信任我們 CA 的用戶端連不上 8883；憑證是發給 localhost 的；9001 與 9443（WSS）可登入 |
| 驗證 / 授權 | 匿名與錯誤密碼被拒；`dashboard` 不能假冒車輛狀態或稼動率；`alarmd` 不能下指令 |
| recorder | `stats/utilization` 有發布、每台車的狀態百分比加總約 100% |
| MQTT 5 | retained 狀態帶 Content Type / Payload Format / 24 小時過期；指令回覆帶回 Correlation Data；忙碌的車輛把 `go` 排隊（不搶佔）、`cancel` 清空排隊；不接受其他 Response Topic；**持久 session 中過期的指令被 broker 丟棄、未過期的照常送達**；異常狀態為 retained JSON；`$SYS` 可讀 |

### 10.3 用命令列手動操作 MQTT

先載入密碼，並定義一個小函式 `mq`（自動帶上 TLS 與 dashboard 帳號），之後的指令都會用到：

```bash
set -a; . ./.env; set +a
mq() { local c=$1; shift; "mosquitto_$c" -h localhost -p 8883 --cafile certs/ca.crt -u dashboard -P "$MQTT_PASS_DASHBOARD" "$@"; }
```

**匿名連線應被拒絕**（預期輸出 `Connection Refused: not authorised.`）：

```bash
mosquitto_sub -h localhost -p 8883 --cafile certs/ca.crt -t '#' -C 1 -W 3
```

**讀取車輛狀態**（預期印出 3 筆 JSON）：

```bash
mq sub -t 'amr/+/status' -C 3 -v
```

**ACL：dashboard 不能假冒車輛狀態**（預期輸出 `0`，代表假訊息被 broker 丟棄）：

```bash
mq sub -t amr/AMR-01/status -W 3 | grep -c FAKE &
sleep 0.5; mq pub -t amr/AMR-01/status -m '{"FAKE":1}'; wait
```

### 10.4 指令與回覆（request / response）

```bash
mq sub -t amr/AMR-01/cmd/result -C 1 -W 5 &
sleep 0.5; mq pub -t amr/AMR-01/cmd -m '{"cmd":"pause","req_id":"test-1"}'; wait
mq pub -t amr/AMR-01/cmd -m resume
```

預期回覆：`{"req_id": "test-1", "cmd": "pause", "ok": true, "state": "paused", ...}`。
在網頁上則是按車輛卡片的按鈕，右下角會跳出「✓ 已執行」。

### 10.5 異常警報（模擬車輛斷線）

```bash
mq sub -t alarms/event -C 1 -W 10 &
sleep 0.5; mq pub -t amr/AMR-03/cmd -m simulate_netloss; wait
```

預期約 3 秒內印出 `{"action": "raised", "alarm": {... "AMR-03 失去連線 ..."}}`。
此時網頁上方會出現紅色警報橫幅，「異常記錄」頁會多一筆「進行中」。
`simulate_netloss` 是測試用指令，AMR-03 不會自己恢復，請重新啟動服務讓它回來：

```bash
./run.sh restart
```

約 10 秒後該筆異常會變成「已恢復」。

### 10.6 查看資料庫裡的異常記錄

```bash
.venv/bin/python -c "
import sqlite3
db = sqlite3.connect('data/alarms.db')
for r in db.execute('SELECT id, datetime(raised_at, \"unixepoch\", \"localtime\"), severity, message, cleared_at IS NOT NULL FROM alarms ORDER BY id DESC LIMIT 5'):
    print(r)"
```

### 10.7 查看稼動率與狀態記錄

```bash
mq sub -t stats/utilization -C 1 | .venv/bin/python -m json.tool | head -20
.venv/bin/python -c "
import sqlite3
db = sqlite3.connect('data/telemetry.db')
for r in db.execute('SELECT amr_id, state, datetime(started_at, \"unixepoch\", \"localtime\"), round(ended_at - started_at, 1) FROM state_intervals ORDER BY id DESC LIMIT 5'):
    print(r)"
```

---

## 11. 關閉

### 方法 A：在 Ubuntu 終端機（只停 Python 服務）

```bash
./run.sh stop
```

Docker 容器（mosquitto、nginx）會繼續執行，資源用量很小；網頁會顯示車輛離線。

### 方法 B：在 Windows 一鍵全部關閉（含 WSL，釋放記憶體）

```powershell
powershell -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu-24.04\home\kuma\amr-mqtt-monitor\scripts\windows\stop.ps1
```

最後應顯示 `Ubuntu-24.04  Stopped`。注意 `wsl --shutdown` 會關閉**所有** WSL 發行版。

---

## 12. 日常維運

| 想做的事 | 指令（Ubuntu，專案資料夾） |
|---|---|
| 看最近的 log | `./run.sh logs` |
| 只看某個服務 | `tail -f logs/alarmd.log` |
| broker log | `docker compose logs -f mosquitto` |
| 重新啟動服務 | `./run.sh restart` |
| 更換全部 MQTT 密碼 | `./scripts/setup-auth.sh --rotate && ./run.sh restart`，之後重新整理網頁 |
| 修改主題權限 | 編輯 `mosquitto/acl` 後 `./scripts/setup-auth.sh --acl-only`（不需重啟） |
| 更新 broker 憑證（到期前） | `./scripts/setup-tls.sh --renew`（同一個 CA，用戶端不用改） |
| 重建 CA | `./scripts/setup-tls.sh --new-ca && ./run.sh restart`（瀏覽器若信任過舊 CA 要重新匯入） |
| 查看憑證到期日 | `openssl x509 -in certs/server.crt -noout -enddate` |
| 調整異常門檻 | 編輯 `alarm/rules.py` 的 `DEFAULTS` 後 `./run.sh restart` |
| 清空異常記錄 | `./run.sh stop && rm data/alarms.db && ./run.sh start` |
| 清空稼動率記錄 | `./run.sh stop && rm data/telemetry.db && ./run.sh start`（平常會自動只保留 30 天） |
| 改模擬車數 | 編輯 `run.sh` 裡 `start_one simulator simulator main.py` 加上 `-n 8` |

### 讓瀏覽器也走加密（wss://，選用）

瀏覽器預設用 `ws://localhost:9001`（只在本機、不出網路）。若要改用加密的 `wss://localhost:9443`：

1. **由你自己**把 `certs/ca.crt` 匯入 Windows：在 Windows 檔案總管打開 `\\wsl.localhost\Ubuntu-24.04\home\kuma\amr-mqtt-monitor\certs\ca.crt` → 安裝憑證 → 目前使用者 → 「受信任的根憑證授權單位」。這會讓這台電腦信任此 CA 簽發的所有憑證，請確認 `certs/ca.key` 沒有外流。
2. 在 `.env` 加一行 `DASHBOARD_WS_URL=wss://localhost:9443`，執行 `./scripts/setup-auth.sh`，重新整理網頁。
3. 要改回來：刪掉 `.env` 那一行，再執行一次 `./scripts/setup-auth.sh`。

---

## 13. 疑難排解

| 症狀 | 原因 / 解法 |
|---|---|
| 網頁右上角「帳號驗證失敗（config.js）」 | 密碼換過但網頁是舊的 → 重新整理；或 `dashboard/config.js` 不存在 → `./scripts/setup-auth.sh` |
| 網頁一直「重新連線中」 | broker 沒開 → `./run.sh status`、`docker compose up -d` |
| 從 Windows 的 PowerShell / 程式連 `localhost:8080` 或 `:9001` 逾時，但瀏覽器正常 | Windows 的 `localhost` 會先試 IPv6 `::1`，而 port 只綁 IPv4 `127.0.0.1`；瀏覽器會自動改用 IPv4，其他程式不一定 → 改連 `127.0.0.1` |
| log 出現 `broker refused connection: Not authorized` | 密碼換過但服務沒重啟 → `./run.sh restart` |
| `certificate verify failed` | 用了錯的 CA 或憑證過期 → 確認用 `--cafile certs/ca.crt`；`./scripts/setup-tls.sh --renew` |
| `mosquitto_sub` 連 1883 失敗 | 正常：已沒有明碼 port，請用 `-p 8883 --cafile certs/ca.crt`（或 10.3 的 `mq`） |
| 電腦使用狀況顯示「沒有收到資料」 | `tail logs/sysmon.log`；sysmon 會在 10 秒沒有新資料時自動重啟取樣程式 |
| 沒開終端機時 WSL 自己停掉 | 用[方法 B](#方法-b在-windows-一鍵啟動會自動開啟瀏覽器)啟動（它會保持一個隱藏的 `wsl.exe`） |
| `./run.sh: Permission denied` | 從 Windows 編輯過檔案會遺失執行權限 → `chmod +x run.sh scripts/*.sh` |
| `ls mosquitto/secrets` 權限不足 | 正常：該資料夾只給 broker（uid 1883）讀取 |
| broker 重啟後一堆「失去連線」 | 已處理：alarmd 會暫停判斷 10 秒；若仍發生，代表車輛真的超過 10 秒沒連回來 |

---

## 14. 已知限制

- **瀏覽器預設走未加密的 `ws://`**：只綁在 127.0.0.1、不經過網路，所以風險低；要加密請照 [12](#讓瀏覽器也走加密wss選用) 讓瀏覽器信任 CA（需要你自己匯入憑證，這是 Windows 的安全性設定）。
- **瀏覽器帳密是公開的**：任何能開網頁的人都看得到 `config.js`；`dashboard` 帳號只有最小權限。
- **不能從其他電腦 / 手機連線**：port 只綁本機。若要開放，需改 `docker-compose.yml` 的綁定、憑證加上該主機名稱、瀏覽器改走 wss，並重新檢視帳號權限。
- **模擬器沒有避碰**：車輛可能在地圖上重疊，包含開過離線車輛停住的位置。
- **單獨關掉模擬器會記成「失去連線」**：`./run.sh stop` / `restart` 會先停 alarmd 與 recorder 所以不會；但若只停模擬器或它當掉，alarmd 與 recorder 無法分辨是計畫性停機。
- **稼動率的「24 小時」只算 recorder 有在執行的時間**：電腦關機或服務停止的時間不列入分母。
- **排隊只存在模擬器的記憶體裡**：模擬器重啟後排隊會清空（指令本身也只有 5 秒有效期）。
- **異常記錄頁只顯示最近 300 筆**：完整記錄在 `data/alarms.db`。
