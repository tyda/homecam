from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import ssl
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx
import imageio_ffmpeg
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"


@dataclass(frozen=True)
class Camera:
    id: str
    name: str
    url: str


class ICatchRequest(BaseModel):
    host: str = Field(min_length=3, max_length=255)
    user: str = Field(default="admin", min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)
    quality: str = "sub"


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


def ffmpeg_exe() -> str:
    system_ffmpeg = Path("/usr/bin/ffmpeg")
    if system_ffmpeg.exists():
        return str(system_ffmpeg)
    return imageio_ffmpeg.get_ffmpeg_exe()


def snapshot_from_rtsp(url: str) -> bytes:
    timeout = int(float(os.getenv("SNAPSHOT_TIMEOUT_SECONDS", "12")))
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        cmd = [
            ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
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


async def _capture_icatch_h264(host: str, channel: int, high_quality: bool, seconds: float, user: str | None = None, password: str | None = None) -> bytes:
    user = user or os.getenv("ICATCH_USER", "admin")
    password = password or os.getenv("ICATCH_PASSWORD")
    if not password:
        raise HTTPException(status_code=400, detail="未設定 ICATCH_PASSWORD")

    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    ssl_ctx = ssl._create_unverified_context()
    bit = 1 << (channel - 1)
    cmd = f"vobits={bit:x},pbits={bit:x},aobits=0,hq={1 if high_quality else 0}"
    uri = f"wss://{host}/streaming"
    h264 = bytearray()
    got_keyframe = False
    deadline = time.time() + seconds

    try:
        async with websockets.connect(uri, ssl=ssl_ctx, open_timeout=8, max_size=None) as ws:
            hello = await asyncio.wait_for(ws.recv(), timeout=5)
            if not (isinstance(hello, bytes) and hello[:4] == b"wsli"):
                raise HTTPException(status_code=502, detail="iCATCH websocket 未回傳 live 初始化訊號")
            await ws.send(auth)
            await ws.send(cmd)

            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=3)
                except asyncio.TimeoutError:
                    break
                if not isinstance(msg, bytes) or len(msg) < 40:
                    continue
                done = 0
                while done + 40 <= len(msg):
                    fourcc = msg[done : done + 4]
                    data_size = struct.unpack_from("<I", msg, done + 24)[0]
                    ch = struct.unpack_from("<I", msg, done + 28)[0]
                    ex_size = struct.unpack_from("<I", msg, done + 36)[0]
                    frame_size = 40 + ex_size + data_size
                    if frame_size <= 0 or done + frame_size > len(msg):
                        break
                    frame = msg[done : done + frame_size]
                    if fourcc == b"H264" and ch == channel - 1 and data_size > 0:
                        key = struct.unpack_from("<I", frame, 56)[0] if len(frame) >= 60 else 0
                        payload = frame[-data_size:]
                        if key == 1:
                            got_keyframe = True
                        if got_keyframe:
                            h264.extend(payload)
                        if got_keyframe and len(h264) > 180_000:
                            return bytes(h264)
                    done += frame_size
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"iCATCH websocket 連線失敗：{type(exc).__name__}") from exc

    if not h264:
        raise HTTPException(status_code=502, detail="iCATCH 沒有取得可解碼 H.264 影格")
    return bytes(h264)


def normalize_icatch_host(host: str) -> str:
    host = host.strip()
    if "://" in host:
        parsed = urlparse(host)
        host = parsed.hostname or host
    host = host.strip().strip("/")
    if not host:
        raise HTTPException(status_code=400, detail="host 不可空白")
    return host


async def _probe_icatch_stream(req: ICatchRequest, seconds: float = 5) -> dict:
    host = normalize_icatch_host(req.host)
    auth = "Basic " + base64.b64encode(f"{req.user}:{req.password}".encode()).decode()
    ssl_ctx = ssl._create_unverified_context()
    high_quality = req.quality.lower() in {"main", "high", "hq", "1"}
    cmd = f"vobits=f,pbits=f,aobits=0,hq={1 if high_quality else 0}"
    stats = {i: {"frames": 0, "keyframes": 0, "bytes": 0, "width": None, "height": None, "codec": None} for i in range(1, 5)}
    uri = f"wss://{host}/streaming"
    deadline = time.time() + seconds

    try:
        async with websockets.connect(uri, ssl=ssl_ctx, open_timeout=8, max_size=None) as ws:
            hello = await asyncio.wait_for(ws.recv(), timeout=5)
            if not (isinstance(hello, bytes) and hello[:4] == b"wsli"):
                raise HTTPException(status_code=502, detail="沒有收到 iCATCH live 初始化訊號")
            await ws.send(auth)
            await ws.send(cmd)
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=3)
                except asyncio.TimeoutError:
                    break
                if not isinstance(msg, bytes) or len(msg) < 40:
                    continue
                done = 0
                while done + 40 <= len(msg):
                    fourcc = msg[done : done + 4]
                    data_size = struct.unpack_from("<I", msg, done + 24)[0]
                    ch = struct.unpack_from("<I", msg, done + 28)[0]
                    ex_size = struct.unpack_from("<I", msg, done + 36)[0]
                    frame_size = 40 + ex_size + data_size
                    if frame_size <= 0 or done + frame_size > len(msg):
                        break
                    frame = msg[done : done + frame_size]
                    if fourcc in {b"H264", b"H265"} and 0 <= ch <= 3:
                        key = struct.unpack_from("<I", frame, 56)[0] if len(frame) >= 60 else 0
                        width = struct.unpack_from("<I", frame, 60)[0] if len(frame) >= 68 else None
                        height = struct.unpack_from("<I", frame, 64)[0] if len(frame) >= 68 else None
                        st = stats[ch + 1]
                        st["frames"] += 1
                        st["keyframes"] += 1 if key == 1 else 0
                        st["bytes"] += data_size
                        st["width"] = width
                        st["height"] = height
                        st["codec"] = fourcc.decode()
                    done += frame_size
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"連線失敗：{type(exc).__name__}") from exc

    channels = [{"id": f"ch{ch}", **st, "ok": st["frames"] > 0} for ch, st in stats.items()]
    return {"ok": any(c["ok"] for c in channels), "host": host, "quality": "main" if high_quality else "sub", "channels": channels}


def h264_to_jpeg(h264: bytes) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".h264") as src, tempfile.NamedTemporaryFile(suffix=".jpg") as dst:
        src.write(h264)
        src.flush()
        cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "h264", "-i", src.name, "-frames:v", "1", dst.name]
        try:
            subprocess.run(cmd, check=True, timeout=12, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            msg = (exc.stderr or exc.stdout or "ffmpeg failed").strip()[-500:]
            raise HTTPException(status_code=502, detail=f"iCATCH H.264 轉 JPG 失敗：{msg}") from exc
        data = Path(dst.name).read_bytes()
        if not data:
            raise HTTPException(status_code=502, detail="iCATCH JPG 空白")
        return data


def snapshot_from_icatch_request(req: ICatchRequest, channel: int) -> bytes:
    host = normalize_icatch_host(req.host)
    high_quality = req.quality.lower() in {"main", "high", "hq", "1"}
    seconds = float(os.getenv("ICATCH_CAPTURE_SECONDS", "8"))
    h264 = asyncio.run(_capture_icatch_h264(host, channel, high_quality, seconds, req.user, req.password))
    return h264_to_jpeg(h264)


def snapshot_from_icatch(url: str) -> bytes:
    parsed = urlparse(url)
    host = normalize_icatch_host(parsed.hostname or os.getenv("ICATCH_HOST") or "")
    qs = parse_qs(parsed.query)
    channel_text = qs.get("channel", [None])[0]
    if channel_text is None:
        # Allow icatch://host/ch1 style.
        digits = "".join(c for c in parsed.path if c.isdigit())
        channel_text = digits or "1"
    channel = int(channel_text)
    if channel < 1 or channel > 16:
        raise HTTPException(status_code=400, detail="iCATCH channel 必須介於 1..16")
    high_quality = qs.get("quality", ["sub"])[0].lower() in {"main", "high", "hq", "1"}
    seconds = float(os.getenv("ICATCH_CAPTURE_SECONDS", "8"))
    h264 = asyncio.run(_capture_icatch_h264(host, channel, high_quality, seconds))
    return h264_to_jpeg(h264)


def get_snapshot(cam: Camera) -> bytes:
    if cam.url.startswith("demo://"):
        return demo_image(cam)
    if cam.url.startswith("icatch://"):
        return snapshot_from_icatch(cam.url)
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
    return {"ok": True, "camera_count": len(load_cameras()), "ffmpeg": bool(ffmpeg_exe())}


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


@app.post("/api/icatch/test")
async def icatch_test(req: ICatchRequest) -> dict:
    # Password is used only for this request; do not store it anywhere.
    return await _probe_icatch_stream(req)


@app.post("/api/icatch/snapshot/{channel}")
def icatch_snapshot(channel: int, req: ICatchRequest) -> Response:
    if channel < 1 or channel > 4:
        raise HTTPException(status_code=400, detail="channel 必須是 1..4")
    image = snapshot_from_icatch_request(req, channel)
    return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
