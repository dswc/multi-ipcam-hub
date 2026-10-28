# multi-ipcam-hub

用 Python + OpenCV 同時連接多隻 RTSP IP 攝影機，以可自訂的 columns × rows 分割畫面即時顯示，支援截圖、錄影與斷線自動重連。

## 安裝

```powershell
cd multi-ipcam-hub
uv venv            # 依 .python-version 使用 Python 3.12
.venv\Scripts\activate
uv pip install -r requirements.txt
```

## 設定

複製範本為 `config.yaml` 後再編輯（`config.yaml` 含密碼，已列入 `.gitignore`，不會被提交）：

```powershell
Copy-Item config.example.yaml config.yaml
```

範本中已設定：

| 名稱 | IP | 帳號 |
|------|----|------|
| cam1 | 192.168.0.2 | admin |
| cam2 | 192.168.0.3 | admin |

請在每隻攝影機的 `password` 欄位填入密碼（特殊字元不需編碼），並依廠牌確認 `path`（預設 `/stream1`）。

畫面配置（`display` 區塊）：

| 參數 | 說明 |
|------|------|
| `columns` / `rows` | 畫面分割為 columns × rows 格，攝影機依序由左到右、由上到下填入 |
| `cell_width` / `cell_height` | 每一格的像素大小 |
| `keep_aspect` | `true` 維持原始長寬比（補黑邊）；`false` 拉伸填滿整格 |

可先用 VLC（媒體 → 開啟網路串流）測試 URL 是否正確。

## 執行

```powershell
python main.py
# 或指定設定檔
python main.py -c my_config.yaml
```

| 按鍵 | 功能 |
|------|------|
| `q` / `ESC` | 離開 |
| `s` | 每隻攝影機各存一張截圖到 `snapshots/` |
| `r` | 開始／停止錄影（每隻各一個 mp4，存到 `recordings/`） |

## 掃描區域網路中的攝影機

`scanner.py` 可找出網路中的 IP 攝影機（只用標準函式庫，不需額外安裝）：

```powershell
python scanner.py                                  # 掃描本機所在的 /24 網段
python scanner.py -s 192.168.1.0/24                # 自訂網段
python scanner.py -s 192.168.1.10-50 -s 10.0.0.5   # 範圍或單一 IP，可重複指定
python scanner.py -e cameras_found.yaml            # 輸出成 config.yaml 的 cameras 區塊
```

| 參數 | 說明 |
|------|------|
| `-s` / `--subnet` | 網段，支援 CIDR（`192.168.1.0/24`）、範圍（`192.168.1.10-50`、`192.168.1.10-192.168.1.50`）、單一 IP |
| `-p` / `--ports` | 要測試的埠（預設 `554,8554,80,8080,8000,37777,34567`） |
| `-t` / `--timeout` | 每次連線逾時秒數（預設 0.5） |
| `-w` / `--workers` | 同時連線數（預設 256） |
| `--onvif-wait` | 等待 ONVIF 回應秒數，`0` 表示不做 ONVIF 探測（預設 3） |
| `-e` / `--export` | 把結果輸出成可貼進 `config.yaml` 的設定 |

判斷方式：RTSP 埠對 `OPTIONS` 請求有正確回應、回應 ONVIF WS-Discovery 群播，或開放 Hikvision（8000）／Dahua（37777）／XiongMai（34567）的私有埠。廠牌由 RTSP／HTTP 回應內容與 ONVIF 資訊推測，匯出時會依廠牌填入預設 RTSP 路徑。ONVIF 群播只能找到本機所在網段的設備。

## 架構

- `camera.py`：`RTSPCamera` 類別，每隻攝影機一個背景執行緒持續讀取，只保留最新影格（避免延遲累積），斷線時以指數退避自動重連。
- `main.py`：讀取設定、組合畫面、處理截圖與錄影。
- `scanner.py`：掃描區域網路中的 IP 攝影機，可匯出設定。

## 常見問題

- **延遲大**：改用子碼流（較低解析度），例如 Hikvision 的 `/Streaming/Channels/102`。
- **畫面花屏／灰塊**：已預設 RTSP 走 TCP；若攝影機只支援 UDP，刪除 `camera.py` 中 `OPENCV_FFMPEG_CAPTURE_OPTIONS` 那一行。
- **連不上**：確認電腦與攝影機同網段、防火牆未擋 554 埠，以及攝影機已啟用 RTSP。
