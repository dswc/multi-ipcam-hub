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

## 架構

- `camera.py`：`RTSPCamera` 類別，每隻攝影機一個背景執行緒持續讀取，只保留最新影格（避免延遲累積），斷線時以指數退避自動重連。
- `main.py`：讀取設定、組合畫面、處理截圖與錄影。

## 常見問題

- **延遲大**：改用子碼流（較低解析度），例如 Hikvision 的 `/Streaming/Channels/102`。
- **畫面花屏／灰塊**：已預設 RTSP 走 TCP；若攝影機只支援 UDP，刪除 `camera.py` 中 `OPENCV_FFMPEG_CAPTURE_OPTIONS` 那一行。
- **連不上**：確認電腦與攝影機同網段、防火牆未擋 554 埠，以及攝影機已啟用 RTSP。
