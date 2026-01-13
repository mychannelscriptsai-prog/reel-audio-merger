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
    main_video_url: HttpUrl
    cta_video_url: HttpUrl
    audio_url: HttpUrl
    main_duration_sec: int = 7
    cta_duration_sec: int = 3
    music_volume: float = 0.15


def _download(url: str, out_path: Path) -> None:
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def _run_ffmpeg_two_videos(
    main_video: Path,
    cta_video: Path,
    audio_in: Path,
    out_mp4: Path,
    main_dur: int,
    cta_dur: int,
    volume: float
) -> None:
    fade_duration = 0.7
    fade_offset = max(0.0, main_dur - fade_duration)

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(main_video),
        "-i", str(cta_video),
        "-stream_loop", "-1",
        "-i", str(audio_in),
        "-filter_complex",
        # Use minimal memory filters
        f"[0:v]scale=1080:1920:flags=bicubic,fps=30,format=yuv420p[v0];"
        f"[1:v]scale=1080:1920:flags=bicubic,fps=30,format=yuv420p[v1];"
        f"[v0][v1]xfade=transition=fade:duration={fade_duration}:offset={fade_offset}[v];"
        f"[2:a]volume={volume}[a]",
        "-map", "[v]",
        "-map", "[a]",
        "-t", str(main_dur + cta_dur),
        # Memory-efficient video encoding
        "-c:v", "libx264",
        "-preset", "ultrafast",       # was veryfast, now ultrafast
        "-crf", "28",                 # lower quality but less memory
        "-pix_fmt", "yuv420p",
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
        # Log the URLs to debug
        print(f"Downloading main video from: {req.main_video_url}")
        print(f"Downloading CTA video from: {req.cta_video_url}")
        print(f"Downloading audio from: {req.audio_url}")

        main_duration = max(1, min(int(req.main_duration_sec), 58))
        cta_duration = max(1, min(int(req.cta_duration_sec), 58))
        volume = max(0.0, min(float(req.music_volume), 1.0))

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            main_video = td / "main.mp4"
            cta_video = td / "cta.mp4"
            audio_in = td / "music.mp3"
            out_mp4 = td / "out.mp4"

            # Download the files
            _download(req.main_video_url, main_video)
            _download(req.cta_video_url, cta_video)
            _download(req.audio_url, audio_in)

            # Merge videos
            _run_ffmpeg_two_videos(
                main_video=main_video,
                cta_video=cta_video,
                audio_in=audio_in,
                out_mp4=out_mp4,
                main_dur=main_duration,
                cta_dur=cta_duration,
                volume=volume
            )

            final_url = _upload_to_cloudinary(out_mp4)

        return {"final_url": final_url}

    except Exception as e:
        print(f"Merge failed: {e}")  # Log any errors
        raise HTTPException(status_code=500, detail=f"Merge failed: {str(e)}")
