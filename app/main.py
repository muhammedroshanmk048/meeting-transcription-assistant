import asyncio
import logging
import re
import shutil
import tempfile
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app import audio, jobs
from app.config import get_settings
from app.export import FORMATS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("meetings")
settings = get_settings()
STATIC = Path(__file__).resolve().parent.parent / "static"
AUDIO_TYPES = {".mp3", ".m4a", ".wav", ".mp4", ".webm", ".ogg", ".opus", ".flac", ".aac", ".mov", ".mkv"}


async def cleanup_loop():
    """Drop expired results periodically: nothing about a meeting is kept longer than job_ttl_minutes."""
    while True:
        try:
            removed = jobs.purge_expired()
            if removed:
                log.info("Cleanup removed %d expired jobs", removed)
        except Exception:
            log.exception("Cleanup failed")
        await asyncio.sleep(settings.cleanup_interval_minutes * 60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not audio.ffmpeg_available():
        log.warning("ffmpeg/ffprobe not found on PATH: uploads will fail until it is installed")
    log.info("Transcription: %s · summaries: %s", settings.transcribe_provider, settings.llm_provider)
    task = asyncio.create_task(cleanup_loop())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="Meeting Transcription Assistant", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------- Sessions + rate limiting ----------

SESSION_RE = re.compile(r"^[A-Za-z0-9-]{16,64}$")


def session_id(x_session_id: Annotated[str, Header()]) -> str:
    """Each browser sends a random ID; jobs are only visible to the session that created them."""
    if not SESSION_RE.match(x_session_id):
        raise HTTPException(400, "Invalid session id")
    return x_session_id


Session = Annotated[str, Depends(session_id)]

_job_calls: dict[str, deque] = defaultdict(deque)


def client_ip(request: Request) -> str:
    # Hosting platforms sit behind a proxy, so the real client is the first X-Forwarded-For hop
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def job_rate_limit(request: Request) -> None:
    """Sliding one-hour window per client IP, to protect the shared free-tier transcription quota."""
    now = time.monotonic()
    calls = _job_calls[client_ip(request)]
    while calls and now - calls[0] > 3600:
        calls.popleft()
    if len(calls) >= settings.jobs_per_hour_per_ip:
        raise HTTPException(429, f"Limit of {settings.jobs_per_hour_per_ip} recordings per hour reached. Try later.")
    calls.append(now)
    # Drop idle clients so the table doesn't grow forever
    for ip in [ip for ip, c in _job_calls.items() if not c or now - c[-1] > 3600]:
        del _job_calls[ip]


# ---------- Routes ----------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "transcribe_provider": settings.transcribe_provider,
        "llm_provider": settings.llm_provider,
        "ffmpeg": audio.ffmpeg_available(),
    }


@app.get("/api/limits")
def limits():
    return {
        "max_upload_mb": settings.max_upload_mb,
        "max_audio_minutes": settings.max_audio_minutes,
        "job_ttl_minutes": settings.job_ttl_minutes,
        "max_jobs_per_session": settings.max_jobs_per_session,
        "formats": sorted(AUDIO_TYPES),
    }


@app.post("/api/jobs", status_code=202)
def create_job(file: UploadFile, sid: Session, request: Request):
    name = Path(file.filename or "recording").name
    if Path(name).suffix.lower() not in AUDIO_TYPES:
        raise HTTPException(400, f"Unsupported file type. Use one of: {' '.join(sorted(AUDIO_TYPES))}")
    # Check session limits before counting the upload against the IP's hourly allowance
    try:
        jobs.check_limits(sid)
    except ValueError as e:
        raise HTTPException(409, str(e))
    job_rate_limit(request)

    # Stream to a private temp dir in 1 MB pieces, stopping as soon as the size limit is passed
    work_dir = Path(tempfile.mkdtemp(prefix="meeting-"))
    upload = work_dir / f"upload{Path(name).suffix.lower()}"
    limit = settings.max_upload_mb * 1024 * 1024
    size = 0
    try:
        with upload.open("wb") as out:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"File larger than {settings.max_upload_mb} MB")
                out.write(chunk)
        if size == 0:
            raise HTTPException(400, "The file is empty")
        return jobs.create(sid, name, work_dir, upload).public()
    except ValueError as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(409, str(e))
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


@app.get("/api/jobs")
def list_jobs(sid: Session):
    return [j.public() for j in jobs.session_jobs(sid)]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, sid: Session):
    job = jobs.get(sid, job_id)
    if not job:
        raise HTTPException(404, "Job not found or expired")
    return job.public(full=True)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, sid: Session):
    if not jobs.delete(sid, job_id):
        raise HTTPException(404, "Job not found, expired or still running")
    return {"deleted": job_id}


@app.get("/api/jobs/{job_id}/export")
def export(job_id: str, sid: Session, format: Literal["md", "txt", "srt"] = "md"):
    job = jobs.get(sid, job_id)
    if not job or not job.transcript:
        raise HTTPException(404, "No transcript for this job")
    render, media_type = FORMATS[format]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(job.filename).stem)[:60] or "meeting"
    return Response(
        render(job),
        media_type=f"{media_type}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{stem}.{format}"'},
    )
