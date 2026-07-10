# HomeCam v1

自製家用監視器第一版：保留現有 DVR，用 RTSP/HTTP snapshot 取得畫面，提供手機網頁、截圖 API、四格合成圖與 Telegram 通知鉤子。

## 功能

- FastAPI 後端
- 手機友善 Web UI
- `/api/cameras`：列出鏡頭
- `/api/camera/{id}/snapshot`：取得單路 JPG 截圖
- `/api/grid/snapshot`：取得四格合成 JPG
- `/api/telegram/send-grid`：傳四格截圖到 Telegram（需設定 bot token/chat id）
- 支援 RTSP、HTTP/HTTPS 圖片 URL、範例測試圖
- DVR 密碼不寫死在程式碼，可用 `.env` 或環境變數設定

## 快速啟動

```bash
cd /data/user-data/homecam
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8765
```

打開：

```text
http://你的主機IP:8765/
```

## 接 iCATCH DVR

先進 DVR 或路由器確認 DVR 的內網 IP，例如 `192.168.1.50`。

然後用 VLC/ffmpeg 測 RTSP。iCATCH 常見格式可能需要逐一測：

```text
rtsp://帳號:密碼@DVR_IP:554/ch01.264
rtsp://帳號:密碼@DVR_IP:554/ch1
rtsp://帳號:密碼@DVR_IP:554/Streaming/Channels/101
rtsp://帳號:密碼@DVR_IP:554/cam/realmonitor?channel=1&subtype=0
```

測試指令：

```bash
ffmpeg -rtsp_transport tcp -y -i 'rtsp://admin:你的密碼@192.168.1.50:554/ch01.264' -frames:v 1 /tmp/ch1.jpg
```

成功後，把 `.env` 的 `HOME_CAM_CAMERAS_JSON` 改成實際 URL。

## 安全建議

不要把 DVR 的 80 port 直接開到外網。建議 DVR 只留內網，外部觀看用 Tailscale/WireGuard/Cloudflare Access 進來。
