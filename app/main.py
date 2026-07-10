from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"


@dataclass(frozen=True)
class Camera:
    id: str
    name: str
    url: str


def load_cameras() -> list[Camera]:
    raw = os.getenv("HOME_CAM_CAMERAS_JSON", "")
    if not raw:
        raw = '[{"id":"ch1","name":"門口","url":"demo://ch1"},{"id":"ch2","name":"車庫","url":"demo://ch2"},{"id":"ch3","name":"曬衣區","url":"demo://ch3"},{"id":"ch4","name":"路邊","url":"demo://ch4"}]'
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"HOME_CAM_CAMERAS_JSON 不是合法 JSON: {exc}") from exc
    cams = [Camera(id=str(x["id"]), name=str(x.get("name", x["id"])), url=str(x["url"])) for x in data]
    if not cams:
        raise RuntimeError("至少要設定一台 camera")
    return cams


def camera_public(cam: Camera) -> dict:
    return {"id": cam.id, "name": cam.name}


def jpeg_bytes(img: Image.Image, quality: int = 86) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def demo_image(cam: Camera, width: int = 960, height: int = 540) -> bytes:
    colors = {"ch1": (42, 92, 150), "ch2": (56, 125, 82), "ch3": (145, 94, 42), "ch4": (118, 70, 140)}
    bg = colors.get(cam.id, (72, 72, 72))
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    draw.rectangle((0, 0, width, 54), fill=(0, 0, 0))
    draw.text((18, 16), f"{cam.id}  {cam.name}  DEMO", fill=(255, 255, 255))
    draw.text((18, height - 42), now, fill=(255, 255, 255))
    for x in range(0, width, 80):
        draw.line((x, 60, x + 120, height), fill=(255, 255, 255), width=1)
    draw.rectangle((width // 2 - 80, height // 2 - 50, width // 2 + 80, height // 2 + 50), outline=(255, 255, 255), width=3)
    return jpeg_bytes(img)


def snapshot_from_http(url: str) -> bytes:
    timeout = float(os.getenv("SNAPSHOT_TIMEOUT_SECONDS", "12"))
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "image" not in ctype and not r.content.startswith(b"\xff\xd8"):
            raise HTTPException(status_code=502, detail=f"來源不是圖片：{ctype}")
        return r.content


def snapshot_from_rtsp(url: str) -> bytes:
    timeout = int(float(os.getenv("SNAPSHOT_TIMEOUT_SECONDS", "12")))
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-y", "-i", url,
            "-frames:v", "1", "-q:v", "2", f.name,
        ]
        try:
            subprocess.run(cmd, check=True, timeout=timeout, capture_output=True, text=True)
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=504, detail=f"RTSP 截圖逾時：{timeout}s") from exc
        except subprocess.CalledProcessError as exc:
            msg = (exc.stderr or exc.stdout or "ffmpeg failed").strip()[-500:]
            raise HTTPException(status_code=502, detail=f"RTSP 截圖失敗：{msg}") from exc
        data = Path(f.name).read_bytes()
        if not data:
            raise HTTPException(status_code=502, detail="RTSP 截圖空白")
        return data


def get_snapshot(cam: Camera) -> bytes:
    if cam.url.startswith("demo://"):
        return demo_image(cam)
    if cam.url.startswith(("rtsp://", "rtsps://")):
        return snapshot_from_rtsp(cam.url)
    if cam.url.startswith(("http://", "https://")):
        return snapshot_from_http(cam.url)
    raise HTTPException(status_code=400, detail=f"不支援的 camera URL scheme：{cam.url.split(':', 1)[0]}")


def make_grid(cameras: list[Camera]) -> bytes:
    snaps: list[Image.Image] = []
    for cam in cameras[:4]:
        try:
            img = Image.open(io.BytesIO(get_snapshot(cam))).convert("RGB")
        except Exception:
            img = Image.new("RGB", (960, 540), (45, 45, 45))
            draw = ImageDraw.Draw(img)
            draw.text((24, 24), f"{cam.id} {cam.name}\n無法取得畫面", fill=(255, 120, 120))
        img.thumbnail((960, 540))
        canvas = Image.new("RGB", (960, 540), (15, 15, 15))
        canvas.paste(img, ((960 - img.width) // 2, (540 - img.height) // 2))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((0, 0, 960, 42), fill=(0, 0, 0))
        draw.text((14, 12), f"{cam.id}  {cam.name}", fill=(255, 255, 255))
        snaps.append(canvas)
    while len(snaps) < 4:
        snaps.append(Image.new("RGB", (960, 540), (20, 20, 20)))
    grid = Image.new("RGB", (1920, 1080), (0, 0, 0))
    for img, pos in zip(snaps, [(0, 0), (960, 0), (0, 540), (960, 540)]):
        grid.paste(img, pos)
    draw = ImageDraw.Draw(grid)
    draw.rectangle((0, 1032, 1920, 1080), fill=(0, 0, 0))
    draw.text((16, 1048), time.strftime("HomeCam %Y-%m-%d %H:%M:%S"), fill=(255, 255, 255))
    return jpeg_bytes(grid, quality=84)


app = FastAPI(title="HomeCam", version="0.1.0")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "camera_count": len(load_cameras()), "ffmpeg": bool(Path("/usr/bin/ffmpeg").exists())}


@app.get("/api/cameras")
def cameras() -> dict:
    return {"cameras": [camera_public(c) for c in load_cameras()]}


@app.get("/api/camera/{camera_id}/snapshot")
def camera_snapshot(camera_id: str) -> Response:
    cams = load_cameras()
    cam = next((c for c in cams if c.id == camera_id), None)
    if not cam:
        raise HTTPException(status_code=404, detail="camera not found")
    return Response(get_snapshot(cam), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/grid/snapshot")
def grid_snapshot() -> Response:
    return Response(make_grid(load_cameras()), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/telegram/send-grid")
def telegram_send_grid() -> dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise HTTPException(status_code=400, detail="未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    image = make_grid(load_cameras())
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    with httpx.Client(timeout=20) as client:
        r = client.post(url, data={"chat_id": chat_id, "caption": "HomeCam 四格快照"}, files={"photo": ("homecam.jpg", image, "image/jpeg")})
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=r.text[-500:])
        return {"ok": True, "telegram": r.json()}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
