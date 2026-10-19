# amr-mqtt-monitor

AMR 車隊模擬 + Windows 電腦使用狀況 + 異常偵測，透過 MQTT 5 即時顯示在網頁儀表板。

## 啟動 / 停止

```bash
./run.sh start     # 第一次會自動建立 MQTT 帳號密碼（.env）
./run.sh status
./run.sh logs
./run.sh restart
./run.sh stop      # 停止 Python 服務（docker 容器保持執行）
./run.sh test      # 異常規則單元測試
```

- 儀表板：http://localhost:8080
- 異常記錄：http://localhost:8080/alarms.html

## 組成

| 元件 | 說明 |
|---|---|
| `mosquitto` (docker) | MQTT broker，TCP 1883、WebSocket 9001；帳號密碼 + ACL、持久化、`$SYS` 統計 |
| `dashboard` (docker, nginx) | 提供 `dashboard/`，port 8080 |
| `simulator/main.py` | 模擬 5 台 AMR（`-n` 調整數量） |
| `monitor/sysmon.py` | 透過隱藏的 Windows PowerShell 取樣 Windows 本機 CPU/記憶體/磁碟/網路/電池 |
| `alarm/alarmd.py` | 異常偵測服務：規則在 `alarm/rules.py`，記錄存於 `data/alarms.db`（SQLite） |

## 偵測的異常

| 類型 | 等級 | 條件（恢復條件） |
|---|---|---|
| 失去連線 OFFLINE | 嚴重 | 3 秒沒有狀態，或 broker 發出遺囑 offline |
| 故障 ERROR | 嚴重 | 車輛狀態為 error |
| 電量極低 / 偏低 | 嚴重 / 警告 | ≤ 10% / ≤ 20%（> 15% / > 25% 才恢復） |
| 疑似卡住 STUCK | 警告 | 「移動中」但 10 秒內位移 < 0.2 m |
| CPU 過高 | 警告 | ≥ 90% 持續 10 秒（< 80% 恢復） |
| 記憶體過高 | 警告 | ≥ 90%（< 85% 恢復） |
| 磁碟空間不足 | 警告 | 剩餘 ≤ 10%（> 12% 恢復） |
| 監控中斷 | 警告 | sysmon 10 秒沒有回報 |

門檻都在 `alarm/rules.py` 的 `DEFAULTS`。alarmd 自己和 broker 斷線時、以及重新連上後 10 秒內暫停判斷，避免 broker 重啟造成大量誤報。

## MQTT 主題

| 主題 | QoS / 保留 | 內容 |
|---|---|---|
| `amr/<id>/status` | 0 / retained | JSON 狀態，每 0.5 秒 |
| `amr/<id>/online` | 1 / retained | `online` / `offline`（遺囑，keepalive 5 秒 → 約 7.5 秒偵測） |
| `amr/<id>/cmd` | 1 | `pause` `resume` `charge` `clear` `go`，或 JSON `{"cmd","req_id"}`；儀表板會帶 Message Expiry 5 秒與 Response Topic |
| `amr/<id>/cmd/result` | 1 | 指令回覆 `{"req_id","cmd","ok","state"}` |
| `system/<host>/stats` | 0 / retained | Windows 使用狀況 |
| `system/<host>/online` | 1 / retained | sysmon 遺囑 |
| `alarms/active` | 1 / retained | 進行中的異常 |
| `alarms/history` | 1 / retained | 最近 300 筆異常 |
| `alarms/event` | 1 | `raised` / `cleared` / `acked` 事件 |
| `alarms/ack` | 1 | `{"id": 12}` 或 `{"all": true}` 確認異常 |
| `alarms/service/online` | 1 / retained | alarmd 遺囑 |

AMR 用持久 session（Session Expiry 60 秒）：短暫斷線期間送出的指令會在重連後送達，但超過 5 秒的指令會被 broker 丟棄。

## 安全性

- 匿名連線已關閉；帳號：`simulator` `sysmon` `alarmd` `dashboard`，權限見 `mosquitto/acl`（最小權限）
- 密碼在 `.env`（不進 git）；`./scripts/setup-auth.sh --rotate` 換新密碼，`--acl-only` 只重新套用 ACL
- `dashboard` 帳號密碼會出現在瀏覽器（`dashboard/config.js`），所以它只能讀取、下 AMR 指令、確認異常
- 尚未啟用 TLS：目前只在本機使用。若要讓其他電腦連線，請先加上 TLS（`listener 8883` + 憑證），並把 docker 的 port 綁定到需要的介面

手動測試：

```bash
set -a; . ./.env; set +a
mosquitto_sub -u dashboard -P "$MQTT_PASS_DASHBOARD" -t 'alarms/event' -v
mosquitto_pub -u dashboard -P "$MQTT_PASS_DASHBOARD" -t amr/AMR-01/cmd -m charge
mosquitto_pub -u dashboard -P "$MQTT_PASS_DASHBOARD" -t amr/AMR-03/cmd -m simulate_netloss   # 測試斷線警報
```
