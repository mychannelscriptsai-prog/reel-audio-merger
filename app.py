import os
import tempfile
import uuid
import subprocess
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl

CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME")
UPLOAD_PRESET = os.environ.get("CLOUDINARY_UPLOAD_PRESET")  # unsigned preset name
FOLDER = os.environ.get("CLOUDINARY_FOLDER", "reels_with_music")

if not CLOUD_NAME or not UPLOAD_PRESET:
    # We don't raise here to allow boot, but requests will fail clearly.
    pass

app = FastAPI()


class MergeRequest(BaseModel):
    video_url: HttpUrl
    audio_url: HttpUrl
    duration_sec: int = 7
    music_volume: float = 0.15  # 0.0 - 1.0


def _download(url: str, out_path: Path) -> None:
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def _run_ffmpeg(video_in: Path, audio_in: Path, out_mp4: Path, duration: int, volume: float) -> None:
    # -stream_loop -1 loops audio if it's shorter than duration
    # -t trims output length
    # -shortest ensures we never exceed the shortest stream
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(video_in),
        "-stream_loop", "-1",
        "-i", str(audio_in),
        "-t", str(duration),
        "-filter_complex", f"[1:a]volume={volume}[a]",
        "-map", "0:v:0",
        "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        str(out_mp4),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {p.stderr[-2000:]}")


def _upload_to_cloudinary(mp4_path: Path) -> str:
    if not CLOUD_NAME or not UPLOAD_PRESET:
        raise RuntimeError("Missing CLOUDINARY_CLOUD_NAME or CLOUDINARY_UPLOAD_PRESET env vars")

    url = f"https://api.cloudinary.com/v1_1/{CLOUD_NAME}/video/upload"
    public_id = f"{FOLDER}/{uuid.uuid4().hex}"

    with open(mp4_path, "rb") as f:
        files = {"file": f}
        data = {
            "upload_preset": UPLOAD_PRESET,
            "public_id": public_id,
            "resource_type": "video",
        }
        r = requests.post(url, files=files, data=data, timeout=120)
        r.raise_for_status()
        j = r.json()
        if "secure_url" not in j:
            raise RuntimeError(f"Cloudinary upload missing secure_url: {j}")
        return j["secure_url"]


@app.post("/merge")
def merge(req: MergeRequest):
    try:
        duration = max(1, min(int(req.duration_sec), 58))  # keep Shorts/Reels safe
        volume = max(0.0, min(float(req.music_volume), 1.0))
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            video_in = td / "in.mp4"
            audio_in = td / "music.mp3"
            out_mp4 = td / "out.mp4"

            _download(str(req.video_url), video_in)
            _download(str(req.audio_url), audio_in)
            _run_ffmpeg(video_in, audio_in, out_mp4, duration, volume)
            final_url = _upload_to_cloudinary(out_mp4)

        return {"final_url": final_url}
    except requests.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"HTTP error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
