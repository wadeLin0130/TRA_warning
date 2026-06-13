# 台鐵位置偵測 App (TRA Position Spotter)

根據使用者 GPS 定位，判斷目前位於台鐵哪一條線、多少里程，並根據列車即時動態 (TrainLiveBoard) 預測再過多久、從哪個方向會有列車通過該位置。

使用 TDX 運輸資料流通服務 v3 軌道 API。

## 功能
1. GPS (lat/lon) → 台鐵線路 + 官方累積里程 (km)
2. 即時列車動態 → 預估通過時間 (ETA) 與方向

## 專案結構
```
tra-position-app/
├── app/
│   ├── main.py          # FastAPI + 簡單前端
│   ├── tdx_client.py    # TDX OAuth + API 封裝
│   └── geo.py           # 線路匹配 + 里程計算
├── scripts/
│   └── fetch_static_data.py   # 抓取靜態資料 (Line, Station, StationOfLine, Shape)
├── templates/
│   └── index.html       # Leaflet + Tailwind 單頁 UI
├── data/                # 快取的靜態 JSON (執行 script 後產生)
├── .env.example
├── requirements.txt
└── README.md
```

## 設定與執行

1. 申請 TDX API Key
   - 註冊 https://tdx.transportdata.tw/
   - 會員中心 → 資料服務 → API金鑰 取得 Client ID / Client Secret
   - 建議訂閱適合方案（基本服務即可開始）

2. 複製環境變數
   ```bash
   cp .env.example .env
   # 編輯 .env 填入你的 Client ID / Secret
   ```

3. 安裝依賴
   ```bash
   cd tra-position-app
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

4. 抓取靜態資料（線型、車站、里程等，之後可重跑更新）
   ```bash
   python scripts/fetch_static_data.py
   ```
   會產生 `data/*.json`

5. 啟動伺服器
   ```bash
   python -m uvicorn app.main:app --reload --port 8000
   ```
   開啟 http://localhost:8000

   **或者先用命令列快速測試（不用開瀏覽器）**：
   ```bash
   python test_position.py
   # 或指定座標
   python test_position.py 25.0478 121.5168 "台北車站"
   ```

6. 使用
   - 點「使用我的位置」或輸入經緯度 / 點擊地圖
   - 按「偵測線路與里程」
   - 自動或手動按「查詢即將通過列車」

## 技術說明

- **里程計算**：
  - 使用 TDX `/Shape` 軌道線型 (LineString) 做最近點投影。
  - 使用 `/StationOfLine` 的 `CumulativeDistance` 作為官方里程錨點。
  - 在線型上做線性內插得到使用者位置的里程。

- **即時列車**：
  - `/TrainLiveBoard` 取得目前運行中列車的站點與延誤。
  - 透過車站反查所屬線路 + 該線里程對照，簡單估計通過時間（平均速度假設 + 延誤）。
  - 後續可加強：搭配 DailyTrainTimetable 做更精準的站間時間內插 + 方向判斷。

- **注意事項**：
  - 部分支線或少用線型資料可能稀疏，信心度會較低。
  - 即時動態更新頻率依 TDX 為準（通常數十秒到分鐘級）。
  - 生產環境建議加 Redis 快取 + 背景定期更新 live data，並保護 Client Secret。

## 後續調整建議（給你自己）
- 改善方向判斷（從 LiveBoard 或時刻表推導上/下行）。
- 依車種給不同平均速度（太魯閣/普悠瑪更快）。
- 加入地圖上顯示即時列車位置（若 LiveBoard 有更細位置資訊）。
- 加入歷史/班次篩選、通知功能。
- 把前端獨立成 React / RN + 後端部署 (Railway / Fly.io 等)。
- 加上 shapely 做更精準的地理投影（可選）。

## 授權與資料來源
- 資料：交通部 TDX 運輸資料流通服務 (https://tdx.transportdata.tw/)
- 請遵守 TDX 使用條款與各資料集授權。

有問題或要調整功能，後面再一起測試優化！

## 部署到雲端 (給台灣手機使用)

### 推薦平台（符合實際、快速、低維護）
- **Render.com** (最簡單，適合這個規模)
  - 免費 tier 會休眠 (15分無流量) → 適合偶爾開啟使用
  - 付費 Starter (~$7/月) 可 always-on
  - 自動 HTTPS + 客製網域
- **Railway.app** 類似
- **Fly.io** (可指定東京/新加坡區域，延遲較低，pay-as-you-go)
- 避免一開始自己管 VPS (除非你熟悉 nginx + systemd + certbot)

### 步驟 (Render 範例)
1. 準備程式碼
   - 確保 .env **不要** commit (已在 .gitignore)
   - requirements 已含 gunicorn + slowapi (rate limit 保護 TDX 配額)
   - 已有 Procfile (給平台用 gunicorn 啟動)

2. 推到 GitHub
   ```bash
   cd tra-position-app
   git init
   git add .
   git commit -m "initial deploy ready"
   git remote add origin https://github.com/YOURNAME/tra-position-app.git
   git push -u origin main
   ```

3. Render 部署
   - 去 https://render.com 註冊 (用 GitHub 登入)
   - New > Web Service
   - Connect your GitHub repo (選 tra-position-app)
   - Name: tra-spotter 或 tra-warning
   - Region: 選 Oregon 或 Frankfurt (目前 Render 亞洲節點有限，東京 ping ~80-150ms 對這個 app 夠用)
   - Build Command: `pip install -r requirements.txt`
   - Start Command: 留空 (會用 Procfile)
   - Plan: Starter (always on) 或先 Free 測試
   - Advanced > Environment Variables:
     - TDX_CLIENT_ID = 你的值
     - TDX_CLIENT_SECRET = 你的值
   - Create Web Service

4. 部署完成後
   - 得到 https://tra-spotter-xxxx.onrender.com
   - 手機用 Chrome/Safari 開啟 → 「加入主畫面」 (因為有 manifest.json，已是 PWA)
   - 測試 GPS (手機一定要用 https，定位才會給)

5. 客製網域 (推薦)
   - 買便宜網域 (namecheap .com 或台灣的 .tw)
   - 在 Render Dashboard > Custom Domains 加你的域名
   - 在 DNS 設 CNAME 指向 onrender 的值
   - Render 會自動給 Let's Encrypt 憑證

### 注意事項 (實際部署重點)
- **速率限制**：已加 slowapi (位置偵測 10/min、即將通過 60/min)，保護你的 TDX 配額不被濫用。
- **TDX 金鑰**：只放環境變數，絕對不要 commit .env
- **延遲**：台灣手機連美國主機 ~150-250ms，開啟 app 後幾秒內會更新，實務可接受 (不是即時遊戲)。
- **如果要更好延遲**：之後可換 Fly.io 部署到 NRT (東京) 或用 GCP Cloud Run (asia-east1 台灣)。
- **費用**：先用 Free 測試；多人長期用建議付費 always-on，避免休眠。
- **備份**：資料夾 data/ 的靜態 JSON 已 commit，部署時會帶上。
- **監控**：Render 內建 log；可加 /health 給平台健康檢查。
- **未來擴充**：加簡單驗證 (e.g. query param token) 或 IP 白名單，避免公開濫用。

### 本機 vs 雲端
- 本機開發：用 `./start-server.sh` (已內建 auto restart)
- 雲端：平台會處理 restart、HTTPS、scaling

部署後把網址給大家用手機測試即可！

有任何部署過程的錯誤 log 貼給我，我幫你修。
