# Cloudflare Named Tunnel 設定指南（讓你的 tra-position-app 更穩定）

## 為什麼要換 named tunnel？
- 你現在用的是 **quick tunnel**（不用登入的臨時 tunnel）。
- Cloudflare 官方明確說 "account-less Tunnels have no uptime guarantee"。
- 偶爾會出現 Error 1033、connection timeout、突然斷（我們之前 log 常看到）。
- Named tunnel（登入帳號後建立）：
  - 跟你 Cloudflare 帳號綁定，更穩。
  - 重連機制更好。
  - 可以從 Cloudflare Dashboard 管理。
  - 支援開機自動啟動（launchd）。
  - 還是完全免費（不需要付費方案）。

## 架構提醒（為什麼「本地 index.html 還是抓不到」？）
即使資料都在 RTDB，**本機 server 還是核心**：

- `POST /api/position`（從 GPS lat/lon 算出「這是哪條線、K幾+幾」）：只有 Python geo.py + StationOfLine 資料能做。瀏覽器 JS 拿不到。
- Worker 每次都要呼叫 `GET /api/upcoming`（本機）去算即時 ETA（混合 LiveBoard + Timetable）。
- RTDB 只負責「誰在監控哪裡」（watched_positions） + 「把算好的結果推給大家」（user_results）。
- 瀏覽器 listener 拿到 user_results 後就直接 render，不再一直輪詢你的 API（這部分比較不受 tunnel 影響）。

**「本地 index.html 抓不到」最常見原因：**
- 你直接在 Finder 雙擊 `templates/index.html` 開 → 這是 `file://` 協議，fetch('/api/position') 一定失敗，一定跳「偵測失敗：無法連線後端」。
- 正確本地測試方式：瀏覽器輸入 `http://127.0.0.1:8000`（由 uvicorn 同時提供 HTML 跟 API）。

一旦你成功「設定位置」（GPS 或地圖點擊），寫進 watched_positions 之後，頁面主要靠 Firebase 即時更新，偶爾 tunnel 抖一下也不會完全壞掉。

## 設定步驟（我已經幫你啟動 login 了）

### 1. 完成 Cloudflare 登入（我剛剛在 terminal 幫你跑 `cloudflared tunnel login`）
- 你的 Mac 應該會跳出瀏覽器視窗，URL 大概長這樣：
  https://dash.cloudflare.com/argotunnel?...
- 如果沒跳，請手動複製 terminal 裡印出來的 URL 去開。
- 用 email 或 Google 登入（還沒帳號就註冊，免費即可）。
- 授權 Cloudflare Tunnel 後，它會自動下載 `cert.pem` 到 `~/.cloudflared/cert.pem`。
- 完成後告訴我「登入好了」或貼 terminal 新的輸出，我繼續幫你執行後續指令。

### 2. 建立 named tunnel（登入完成後執行）
```bash
cloudflared tunnel create tra-position
```
- 記下輸出的 **Tunnel ID**（一串像 12345678-... 的 UUID）和 token。
- 它會產生 `~/.cloudflared/<tunnel-id>.json`

### 3. 建立 config（我可以幫你寫）
典型 `~/.cloudflared/config.yml` 內容：

```yaml
tunnel: <你的 tunnel id>
credentials-file: /Users/weidilin/.cloudflared/<tunnel id>.json

ingress:
  - service: http://localhost:8000
```

### 4. 測試執行 named tunnel
```bash
cloudflared tunnel run tra-position
```
它會給你一個穩定的 URL，例如：
https://<tunnel-id>.cfargotunnel.com
或你可以之後再綁自訂子網域（如果有自己的 domain）。

### 5. 讓它開機 / 登入自動啟動（Mac launchd）
我會幫你產生 `~/Library/LaunchAgents/com.weidilin.tra-position-tunnel.plist`

內容大概：
- 執行 `cloudflared tunnel run tra-position`
- 設定 KeepAlive、RunAtLoad
- 標準 log 路徑

安裝方式：
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.weidilin.tra-position-tunnel.plist
```

之後 Mac 開機或你登入就會自動把 tunnel 拉起來，不用再靠我們這個 monitor hack。

## 之後的日常使用
- server 還是用你原本的 `./start-server.sh` 或 monitor。
- worker 還是用 `python rtdb_worker.py`。
- tunnel 換成 named + launchd。
- 給同事的網址就是 named tunnel 的那個 https://...cfargotunnel.com
- 如果以後要換電腦或搬到 VPS，只要把 cert 和 tunnel 設定複製過去即可。

## 想更穩的下一步（可選）
- 給 tunnel 綁你自己的 domain（例如 warning.你的公司.com），完全專業。
- 把整個 app（server + worker）搬到免費/低價的雲端（Render, Fly.io, Railway, 甚至 Cloudflare Pages + Worker + D1/R2 未來擴充）。
- 但對你現在「本機電腦一直開 + 10-20 同事」的需求，named tunnel + launchd 已經是性價比最高的解法。

需要我現在幫你產生 plist 範本、或等你說「登入好了」再繼續嗎？
把 terminal 新的輸出或 browser 步驟的結果貼給我，我馬上接下一步。
