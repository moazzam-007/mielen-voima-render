import os
import sys
import re
import time
import uuid
import shutil
import base64
import asyncio
import logging
import tempfile
import threading
import subprocess
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException, Header, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
import requests
import edge_tts
from dotenv import load_dotenv

# Load local environment if present
load_dotenv()

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("mielen-voima-engine")

# Configuration from Environment Variables
CF_ACCOUNTS = [
    (os.getenv("CF_ACCOUNT_ID_1"), os.getenv("CF_API_TOKEN_1"), "CF_Primary"),
    (os.getenv("CF_ACCOUNT_ID_2"), os.getenv("CF_API_TOKEN_2"), "CF_Backup_1"),
    (os.getenv("CF_ACCOUNT_ID_3"), os.getenv("CF_API_TOKEN_3"), "CF_Backup_2"),
]
CF_ACCOUNTS = [acc for acc in CF_ACCOUNTS if acc[0] and acc[1]]

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "")
PUBLIC_BASE_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_BASE_URL") or "https://mielen-voima-video-engine.onrender.com"
ALLOWED_WEBHOOK_HOSTS = [
    h.strip().lower() for h in os.getenv("ALLOWED_WEBHOOK_HOSTS", "").split(",") if h.strip()
]

VOICE_DEFAULT = "fi-FI-HarriNeural"
FLUX_MODEL = "@cf/black-forest-labs/flux-1-schnell"

# Global In-Memory Job Registry and Concurrency Lock (Max 1 Render Job for 512MB RAM safety)
jobs_db = {}
JOBS_LOCK = threading.Lock()
RENDER_SLOT_LOCK = threading.Lock()
ACTIVE_RENDER_JOBS = 0
MAX_CONCURRENT_JOBS = 1
JOB_TTL_SECONDS = 1800  # 30 minutes retention

# Base working directory for jobs
BASE_TMP = Path(tempfile.gettempdir()) / "mielen_voima_jobs"
BASE_TMP.mkdir(parents=True, exist_ok=True)


# ─── CONCURRENCY & TTL HELPERS ───────────────────────────────────────────────

def acquire_render_slot() -> bool:
    global ACTIVE_RENDER_JOBS
    with RENDER_SLOT_LOCK:
        if ACTIVE_RENDER_JOBS >= MAX_CONCURRENT_JOBS:
            return False
        ACTIVE_RENDER_JOBS += 1
        return True

def release_render_slot():
    global ACTIVE_RENDER_JOBS
    with RENDER_SLOT_LOCK:
        ACTIVE_RENDER_JOBS = max(0, ACTIVE_RENDER_JOBS - 1)

def purge_expired_jobs():
    """Purges completed/failed jobs older than JOB_TTL_SECONDS to avoid memory leaks."""
    now = time.time()
    with JOBS_LOCK:
        expired = [
            jid for jid, info in jobs_db.items()
            if info.get("status") in ("completed", "failed")
            and (now - info.get("created_at", now)) > JOB_TTL_SECONDS
        ]
        for jid in expired:
            job_info = jobs_db.pop(jid, None)
            if job_info:
                job_dir = BASE_TMP / jid
                shutil.rmtree(job_dir, ignore_errors=True)
                logger.info(f"Purged expired job: {jid}")


# ─── AUTHENTICATION DEPENDENCY ───────────────────────────────────────────────

async def verify_api_key(x_api_key: Optional[str] = Header(None)):
    """Validates X-API-Key header against API_SECRET_KEY if configured."""
    if API_SECRET_KEY:
        if not x_api_key or x_api_key != API_SECRET_KEY:
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing X-API-Key header.")


# ─── SUBPROCESS HELPER WITH TIMEOUT & STDERR CAPTURE ────────────────────────

def run_command(cmd: list, timeout: int = 600, cwd: Optional[Path] = None) -> str:
    """Executes external shell commands with strict timeout and captures stderr on failure."""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            cwd=cwd,
            check=False
        )
        if proc.returncode != 0:
            tail_err = (proc.stderr or "")[-2000:]
            logger.error(f"Command failed (exit {proc.returncode}): {' '.join(str(c) for c in cmd[:4])}...\n{tail_err}")
            raise RuntimeError(f"FFmpeg/Subprocess failed (exit {proc.returncode}): {tail_err.strip()}")
        return proc.stdout or ""
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out after {timeout}s: {' '.join(str(c) for c in cmd[:4])}")
        raise RuntimeError(f"Process timed out after {timeout}s")


# ─── PYDANTIC MODELS ──────────────────────────────────────────────────────────

class SceneItem(BaseModel):
    scene_id: int = Field(ge=1, le=30, description="Sequential scene index")
    voiceover: str = Field(min_length=1, max_length=600, description="Finnish spoken voiceover text")
    image_prompt: str = Field(min_length=1, max_length=2000, description="Flux stickman prompt")
    keyword: Optional[str] = Field(default=None, max_length=50, description="Visual keyword overlay")

class GenerateShortRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=200)
    video_id: Optional[str] = Field(default=None, max_length=100)
    webhook_url: Optional[str] = Field(default=None, max_length=500)
    voice: Optional[str] = Field(default=VOICE_DEFAULT, max_length=100)
    scenes: List[SceneItem] = Field(min_length=1, max_length=20)

    @field_validator("webhook_url")
    def validate_webhook(cls, v):
        if not v:
            return v
        if not v.startswith("https://"):
            raise ValueError("webhook_url must use HTTPS scheme")
        if ALLOWED_WEBHOOK_HOSTS:
            host = v.split("/")[2].lower()
            if not any(allowed in host for allowed in ALLOWED_WEBHOOK_HOSTS):
                raise ValueError(f"webhook_url host '{host}' is not in ALLOWED_WEBHOOK_HOSTS")
        return v

    @field_validator("video_id")
    def sanitize_video_id(cls, v):
        if not v:
            return v
        return re.sub(r"[^a-zA-Z0-9_\-]", "_", v)[:100]

    @field_validator("scenes")
    def validate_unique_scenes(cls, v):
        ids = [s.scene_id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("All scene_id values must be unique")
        return v

class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: int
    topic: str
    duration_sec: Optional[float] = None
    download_url: Optional[str] = None
    error: Optional[str] = None


# ─── FASTAPI APPLICATION ──────────────────────────────────────────────────────

app = FastAPI(
    title="Mielen Voima - Video Generation Engine",
    description="Automated $0-cost Video Engine for Finnish YouTube Shorts on Render Web Service",
    version="1.1.0"
)

@app.get("/")
def root():
    return {
        "service": "Mielen Voima Video Engine",
        "status": "online",
        "channel": "@Mielen_Voima",
        "endpoints": {
            "health": "/health",
            "generate": "/api/generate-short",
            "status": "/api/status/{job_id}",
            "download": "/api/download/{job_id}"
        }
    }

@app.get("/health")
@app.get("/ping")
async def health_check():
    """Lightweight non-blocking health check endpoint for Render and n8n keep-awake ping."""
    purge_expired_jobs()
    with JOBS_LOCK:
        active = len([j for j in jobs_db.values() if j.get("status") not in ("completed", "failed")])
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "active_jobs": active
    }


# ─── CORE PIPELINE WORKER ────────────────────────────────────────────────────

def get_audio_duration(file_path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path)
    ]
    stdout = run_command(cmd, timeout=30)
    return float(stdout.strip())

async def generate_scene_audio(text: str, voice: str, output_path: Path):
    """Generates scene voiceover audio with Edge TTS and automatic retry."""
    for attempt in range(1, 4):
        try:
            if output_path.exists():
                output_path.unlink()
            communicate = edge_tts.Communicate(text, voice, rate="+5%")
            await asyncio.wait_for(communicate.save(str(output_path)), timeout=45)
            if output_path.exists() and output_path.stat().st_size > 500:
                return
        except Exception as e:
            logger.warning(f"Audio attempt {attempt} failed for {output_path.name}: {e}")
            await asyncio.sleep(2)
    raise RuntimeError(f"Edge TTS failed for text: {text[:40]}...")

async def generate_all_scenes_audio(scenes: List[SceneItem], voice: str, audio_dir: Path):
    """Generates all scene audio files in a single async session to avoid loop re-creation overhead."""
    semaphore = asyncio.Semaphore(2)
    async def worker(scene: SceneItem):
        async with semaphore:
            aud_path = audio_dir / f"scene_{scene.scene_id:03d}.mp3"
            await generate_scene_audio(scene.voiceover, voice, aud_path)
    await asyncio.gather(*[worker(s) for s in scenes])

def generate_scene_image(prompt: str, output_path: Path):
    """
    Generates stickman image via Cloudflare Workers AI Flux-1-schnell.
    Primary account used by default; automatically falls back on 429 / quota limit.
    """
    if not CF_ACCOUNTS:
        raise RuntimeError("No Cloudflare credentials found in environment variables.")

    payload = {"prompt": prompt, "steps": 4}
    last_error = None

    for account_id, api_token, label in CF_ACCOUNTS:
        url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{FLUX_MODEL}"
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }
        try:
            logger.info(f"Requesting image from {label}...")
            r = requests.post(url, headers=headers, json=payload, timeout=60)
            
            if r.status_code == 429:
                logger.warning(f"{label} hit HTTP 429 rate limit. Trying next account...")
                last_error = f"{label} HTTP 429 Rate Limit"
                continue

            if r.status_code != 200:
                logger.warning(f"{label} returned HTTP {r.status_code}: {r.text[:200]}")
                last_error = f"{label} HTTP {r.status_code}: {r.text[:150]}"
                continue

            # Parse image bytes
            if "image" in r.headers.get("content-type", ""):
                img_bytes = r.content
            else:
                data = r.json()
                b64_str = data.get("result", {}).get("image", "")
                if not b64_str:
                    last_error = f"{label} returned empty image data"
                    continue
                if b64_str.startswith("data:"):
                    b64_str = b64_str.split(",", 1)[-1]
                img_bytes = base64.b64decode(b64_str)

            with open(output_path, "wb") as f:
                f.write(img_bytes)
            logger.info(f"Image successfully saved: {output_path.name} ({len(img_bytes)/1024:.1f} KB)")
            return
            
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Error connecting to {label}: {e}")
            continue

    raise RuntimeError(f"All Cloudflare accounts failed to generate image. Last error: {last_error}")

def render_scene_clip(img_path: Path, aud_path: Path, out_clip: Path, scene_id: int):
    """
    Renders a single scene into a 1080x1920 MP4 clip.
    Pads 1:1 image onto 9:16 vertical canvas (1080x1920) before zoompan to ensure ZERO STRETCH / ZERO DISTORTION.
    Audio and video durations match exactly to prevent scene drift.
    """
    dur = get_audio_duration(aud_path)
    frames = max(int(dur * 30), 1)

    # Alternate subtle zoom in vs zoom out (mild 1.05 max for stability)
    if scene_id % 2 == 1:
        zoom_expr = "min(zoom+0.0004,1.06)"
    else:
        zoom_expr = "max(1.06-0.0004*on,1.0)"

    # Filter: Pad 1024x1024 onto 1080x1920 (9:16) canvas directly.
    # Because input canvas is already 9:16, zoompan samples with identical 9:16 aspect ratio, eliminating distortion.
    vf = (
        "scale=1080:1080:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=white,"
        "setsar=1,"
        f"zoompan=z='{zoom_expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1080x1920:fps=30"
    )

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(img_path),
        "-i", str(aud_path),
        "-vf", vf,
        "-c:v", "libx264", "-tune", "stillimage", "-preset", "veryfast", "-crf", "22",
        "-threads", "1",  # Strictly bounded to 1 thread for 512MB RAM stability
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-pix_fmt", "yuv420p",
        "-t", f"{dur:.3f}",
        str(out_clip)
    ]
    run_command(cmd, timeout=300)

def clean_ass_text(text: str) -> str:
    """Sanitizes text for ASS subtitles to prevent syntax corruption."""
    return text.replace("{", "(").replace("}", ")").replace("\\", "").replace("\n", " ").strip()

def generate_ass_subtitles(audio_path: Path, ass_path: Path):
    """
    Transcribes audio via Groq Whisper (word timestamps) and generates
    high-readability ASS subtitles with Amber Gold (#F59E0B) active word highlight.
    """
    if not GROQ_API_KEY:
        logger.warning("GROQ_API_KEY not set. Subtitles will be omitted.")
        return False

    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    with open(audio_path, "rb") as f:
        files = {"file": ("audio.mp3", f, "audio/mpeg")}
        data = {
            "model": "whisper-large-v3",
            "response_format": "verbose_json",
            "timestamp_granularities[]": "word",
            "language": "fi"
        }
        r = requests.post(url, headers=headers, files=files, data=data, timeout=60)

    if r.status_code != 200:
        logger.error(f"Groq Whisper transcription failed: {r.text[:200]}")
        return False

    words = r.json().get("words", [])
    if not words:
        return False

    def fmt_time(sec):
        sec = max(0.0, float(sec))
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,92,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,3,2,60,60,320,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogues = []
    chunk_size = 3
    for i in range(0, len(words), chunk_size):
        chunk = words[i:i+chunk_size]
        for active_idx, target_w in enumerate(chunk):
            start_t = fmt_time(target_w.get("start", 0))
            end_t = fmt_time(target_w.get("end", 0))
            
            phrase_parts = []
            for j, w in enumerate(chunk):
                w_text = clean_ass_text(w.get("word", ""))
                if j == active_idx:
                    # Highlight active word in Amber Gold (#F59E0B -> ASS: &H000B9EF5)
                    phrase_parts.append(f"{{\\c&H000B9EF5&\\fscx108\\fscy108}}{w_text}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}")
                else:
                    phrase_parts.append(w_text)
            line_text = " ".join(phrase_parts)
            dialogues.append(f"Dialogue: 0,{start_t},{end_t},Default,,0,0,0,,{line_text}")

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(dialogues))
    return True


# ─── BACKGROUND EXECUTION PIPELINE ───────────────────────────────────────────

def process_video_job(job_id: str, payload: GenerateShortRequest):
    job_dir = BASE_TMP / job_id
    audio_dir = job_dir / "audio"
    images_dir = job_dir / "images"
    clips_dir = job_dir / "clips"
    
    try:
        audio_dir.mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(parents=True, exist_ok=True)
        clips_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"[{job_id}] Starting pipeline for topic: '{payload.topic}'")
        with JOBS_LOCK:
            jobs_db[job_id]["status"] = "generating_media"
            jobs_db[job_id]["progress"] = 15

        # 1. Generate Voiceovers in a single async gather session
        asyncio.run(generate_all_scenes_audio(payload.scenes, payload.voice, audio_dir))
        with JOBS_LOCK:
            jobs_db[job_id]["progress"] = 35

        # 2. Generate Stickman Images sequentially (rate-limit safe)
        for scene in payload.scenes:
            img_path = images_dir / f"scene_{scene.scene_id:03d}.jpg"
            generate_scene_image(scene.image_prompt, img_path)
        with JOBS_LOCK:
            jobs_db[job_id]["progress"] = 55

        # 3. Render Individual Scene Clips (with aspect-safe 9:16 canvas)
        with JOBS_LOCK:
            jobs_db[job_id]["status"] = "assembling_video"
        scene_clips = []
        for scene in payload.scenes:
            logger.info(f"[{job_id}] Rendering video clip for scene {scene.scene_id}/{len(payload.scenes)}...")
            img_p = images_dir / f"scene_{scene.scene_id:03d}.jpg"
            aud_p = audio_dir / f"scene_{scene.scene_id:03d}.mp3"
            clip_p = clips_dir / f"scene_{scene.scene_id:03d}.mp4"
            render_scene_clip(img_p, aud_p, clip_p, scene.scene_id)
            scene_clips.append(clip_p)
            logger.info(f"[{job_id}] Scene {scene.scene_id}/{len(payload.scenes)} video clip ready.")
        with JOBS_LOCK:
            jobs_db[job_id]["progress"] = 75

        # 4. Stream-Copy Concatenation
        logger.info(f"[{job_id}] Concatenating all {len(scene_clips)} scene clips into raw video...")
        concat_txt = job_dir / "concat_list.txt"
        with open(concat_txt, "w", encoding="utf-8") as f:
            for c in scene_clips:
                f.write(f"file 'clips/{c.name}'\n")

        raw_combined = job_dir / "raw_combined.mp4"
        cmd_concat = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", "concat_list.txt",
            "-c", "copy",
            "raw_combined.mp4"
        ]
        run_command(cmd_concat, timeout=300, cwd=job_dir)
        with JOBS_LOCK:
            jobs_db[job_id]["progress"] = 85

        # 5. Word-by-word Subtitles
        temp_audio = job_dir / "full_audio.mp3"
        cmd_extract_audio = [
            "ffmpeg", "-y", "-i", "raw_combined.mp4",
            "-vn", "-acodec", "libmp3lame", "-b:a", "192k",
            "full_audio.mp3"
        ]
        run_command(cmd_extract_audio, timeout=120, cwd=job_dir)

        ass_file = job_dir / "subtitles.ass"
        has_subtitles = generate_ass_subtitles(temp_audio, ass_file)

        final_video = job_dir / "final_short.mp4"
        if has_subtitles and ass_file.exists():
            cmd_burn = [
                "ffmpeg", "-y",
                "-i", "raw_combined.mp4",
                "-vf", "subtitles=subtitles.ass",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-threads", "1",
                "-c:a", "copy",
                "final_short.mp4"
            ]
            run_command(cmd_burn, timeout=600, cwd=job_dir)
        else:
            shutil.copy(raw_combined, final_video)

        dur_sec = get_audio_duration(final_video)
        abs_download_url = f"{PUBLIC_BASE_URL.rstrip('/')}/api/download/{job_id}"

        with JOBS_LOCK:
            jobs_db[job_id]["status"] = "completed"
            jobs_db[job_id]["progress"] = 100
            jobs_db[job_id]["duration_sec"] = dur_sec
            jobs_db[job_id]["final_file"] = str(final_video.resolve())
            jobs_db[job_id]["download_url"] = abs_download_url
        logger.info(f"[{job_id}] Video successfully rendered! Duration: {dur_sec:.1f}s")

        # Cleanup intermediate files immediately to keep disk clear
        shutil.rmtree(clips_dir, ignore_errors=True)
        shutil.rmtree(audio_dir, ignore_errors=True)
        shutil.rmtree(images_dir, ignore_errors=True)
        for intermediate in [raw_combined, temp_audio, ass_file, concat_txt]:
            if intermediate.exists():
                try:
                    intermediate.unlink()
                except Exception:
                    pass

        # Webhook callback to n8n if provided
        if payload.webhook_url:
            try:
                callback_data = {
                    "job_id": job_id,
                    "status": "completed",
                    "video_id": payload.video_id or job_id,
                    "topic": payload.topic,
                    "duration_sec": dur_sec,
                    "download_url": abs_download_url
                }
                headers = {}
                if API_SECRET_KEY:
                    headers["X-API-Key"] = API_SECRET_KEY
                resp = requests.post(payload.webhook_url, json=callback_data, headers=headers, timeout=30)
                if resp.status_code >= 400:
                    logger.warning(f"[{job_id}] Webhook responded with HTTP {resp.status_code}")
                else:
                    logger.info(f"[{job_id}] Webhook callback sent to {payload.webhook_url}")
            except Exception as we:
                logger.warning(f"[{job_id}] Webhook callback failed: {we}")

    except Exception as e:
        logger.error(f"[{job_id}] Pipeline failed: {e}", exc_info=True)
        with JOBS_LOCK:
            jobs_db[job_id]["status"] = "failed"
            jobs_db[job_id]["error"] = str(e)
        
        # Complete cleanup of failed job artifacts
        shutil.rmtree(job_dir, ignore_errors=True)

        if payload.webhook_url:
            try:
                headers = {}
                if API_SECRET_KEY:
                    headers["X-API-Key"] = API_SECRET_KEY
                requests.post(
                    payload.webhook_url,
                    json={"job_id": job_id, "status": "failed", "error": str(e)},
                    headers=headers,
                    timeout=10
                )
            except Exception:
                pass
    finally:
        release_render_slot()


# ─── API ENDPOINTS ────────────────────────────────────────────────────────────

@app.post("/api/generate-short", status_code=202, dependencies=[Depends(verify_api_key)])
def start_short_generation(payload: GenerateShortRequest, background_tasks: BackgroundTasks):
    """
    Asynchronous endpoint: Initiates video generation in background.
    Protected by X-API-Key header.
    Bounded to 1 concurrent job to prevent Render 512MB RAM OOM.
    """
    purge_expired_jobs()

    # Enforce concurrency lock
    if not acquire_render_slot():
        raise HTTPException(
            status_code=429,
            detail="A video rendering job is currently in progress. Please wait for it to complete."
        )

    job_id = f"mv_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    with JOBS_LOCK:
        jobs_db[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 5,
            "topic": payload.topic,
            "video_id": payload.video_id or job_id,
            "created_at": time.time(),
            "final_file": None,
            "download_url": None,
            "error": None
        }

    # Dispatch to background task (slot will be released in process_video_job's finally block)
    background_tasks.add_task(process_video_job, job_id, payload)

    return {
        "status": "queued",
        "job_id": job_id,
        "message": "Video rendering started. Monitor via /api/status/{job_id} or wait for webhook callback."
    }

@app.get("/api/status/{job_id}", response_model=JobStatusResponse, dependencies=[Depends(verify_api_key)])
def get_job_status(job_id: str):
    """Poll job status and download URL."""
    purge_expired_jobs()
    with JOBS_LOCK:
        job = jobs_db.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job ID not found.")
        job_copy = dict(job)

    return JobStatusResponse(
        job_id=job_copy["job_id"],
        status=job_copy["status"],
        progress=job_copy.get("progress", 0),
        topic=job_copy["topic"],
        duration_sec=job_copy.get("duration_sec"),
        download_url=job_copy.get("download_url"),
        error=job_copy.get("error")
    )

@app.get("/api/download/{job_id}", dependencies=[Depends(verify_api_key)])
def download_video(job_id: str):
    """Streams the completed MP4 video file."""
    with JOBS_LOCK:
        job = jobs_db.get(job_id)
        if not job or job.get("status") != "completed":
            raise HTTPException(status_code=404, detail="Video is not ready or does not exist.")
        file_path = job.get("final_file")
        video_id = job.get("video_id", job_id)

    if not file_path or not Path(file_path).exists():
        raise HTTPException(status_code=404, detail="File not found on disk.")

    return FileResponse(
        path=file_path,
        media_type="video/mp4",
        filename=f"mielen_voima_{video_id}.mp4"
    )
