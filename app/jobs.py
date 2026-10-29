import logging
import shutil
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from app import audio, summarize
from app.config import get_settings
from app.summarize import MeetingNotes
from app.transcribe import Transcript, transcribe

log = logging.getLogger("jobs")
settings = get_settings()

ACTIVE = {"queued", "converting", "transcribing", "summarizing"}


@dataclass
class Job:
    id: str
    session_id: str
    filename: str
    work_dir: Path
    status: str = "queued"
    message: str = "Waiting for a free worker"
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    duration: float | None = None
    transcript: Transcript | None = None
    notes: MeetingNotes | None = None
    summary_error: str | None = None
    error: str | None = None

    def public(self, full: bool = False) -> dict:
        data = {
            "id": self.id,
            "filename": self.filename,
            "status": self.status,
            "message": self.message,
            "created_at": int(self.created_at),
            "expires_at": int(self.expires_at) if self.expires_at else None,
            "duration": self.duration,
            "error": self.error,
        }
        if self.status == "queued":
            data["position"] = queue_position(self)
        if full:
            data["transcript"] = self.transcript.model_dump() if self.transcript else None
            data["notes"] = self.notes.model_dump() if self.notes else None
            data["summary_error"] = self.summary_error
        return data


_jobs: dict[str, Job] = {}
_lock = threading.Lock()
# One job at a time: the free host has a fraction of a CPU, and Groq's quota is shared anyway
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="job")
# (time, audio seconds) sent to Groq, to stay inside its free hourly audio budget
_audio_usage: deque[tuple[float, float]] = deque()


def queue_position(job: Job) -> int:
    with _lock:
        return sum(1 for j in _jobs.values() if j.status == "queued" and j.created_at <= job.created_at)


def session_jobs(session_id: str) -> list[Job]:
    with _lock:
        return sorted((j for j in _jobs.values() if j.session_id == session_id), key=lambda j: -j.created_at)


def get(session_id: str, job_id: str) -> Job | None:
    with _lock:
        job = _jobs.get(job_id)
    return job if job and job.session_id == session_id else None


def check_limits(session_id: str) -> None:
    jobs = session_jobs(session_id)
    if any(j.status in ACTIVE for j in jobs):
        raise ValueError("You already have a recording in progress. Wait for it to finish first.")
    if len(jobs) >= settings.max_jobs_per_session:
        raise ValueError(f"Limit of {settings.max_jobs_per_session} recordings reached. Delete one first.")


def create(session_id: str, filename: str, work_dir: Path, upload: Path) -> Job:
    """Register a job for an already-saved upload and queue it. Raises ValueError when a limit is hit."""
    check_limits(session_id)
    job = Job(id=uuid.uuid4().hex[:12], session_id=session_id, filename=filename, work_dir=work_dir)
    with _lock:
        _jobs[job.id] = job
    _worker.submit(run, job, upload)
    return job


def delete(session_id: str, job_id: str) -> bool:
    job = get(session_id, job_id)
    if not job or job.status in ACTIVE:
        return False
    with _lock:
        _jobs.pop(job_id, None)
    return True


def purge_expired() -> int:
    now = time.time()
    with _lock:
        expired = [j.id for j in _jobs.values() if j.expires_at and j.expires_at < now]
        for job_id in expired:
            del _jobs[job_id]
    return len(expired)


def reserve_audio_budget(seconds: float) -> None:
    if settings.transcribe_provider != "groq":
        return
    now = time.time()
    with _lock:
        while _audio_usage and now - _audio_usage[0][0] > 3600:
            _audio_usage.popleft()
        used = sum(s for _, s in _audio_usage)
        if used + seconds > settings.groq_audio_seconds_per_hour:
            wait = int(3600 - (now - _audio_usage[0][0])) // 60 + 1 if _audio_usage else 60
            raise ValueError(f"The demo's free transcription quota for this hour is used up. Try again in ~{wait} min.")
        _audio_usage.append((now, seconds))


def _set(job: Job, status: str, message: str) -> None:
    job.status, job.message = status, message
    log.info("Job %s: %s (%s) at +%.1fs", job.id, status, message, time.time() - job.created_at)


def run(job: Job, upload: Path) -> None:
    try:
        _set(job, "converting", "Reading audio")
        job.duration = audio.duration(upload)
        if job.duration > settings.max_audio_minutes * 60:
            raise ValueError(
                f"Recording is {job.duration / 60:.0f} min long; the limit is {settings.max_audio_minutes:.0f} min."
            )
        reserve_audio_budget(job.duration)
        _set(job, "converting", "Converting to 16 kHz mono")
        converted = audio.convert(upload, job.work_dir)
        upload.unlink(missing_ok=True)

        _set(job, "transcribing", f"Transcribing {job.duration / 60:.1f} min of audio")
        job.transcript = transcribe(converted, job.duration, job.work_dir, lambda m: _set(job, "transcribing", m))
        converted.unlink(missing_ok=True)

        _set(job, "summarizing", "Writing summary and action items")
        try:
            job.notes = summarize.summarize(job.transcript.segments, lambda m: _set(job, "summarizing", m))
        except Exception as e:
            # The transcript is still useful on its own, so a failed summary doesn't fail the job
            log.exception("Summary failed for job %s", job.id)
            job.summary_error = f"Summary failed: {e}"
        _set(job, "done", "Done")
    except ValueError as e:
        job.error = str(e)
        _set(job, "error", "Failed")
    except Exception as e:
        log.exception("Job %s failed", job.id)
        job.error = f"Processing failed: {e}"
        _set(job, "error", "Failed")
    finally:
        # Recordings are never kept: delete the upload and every converted file
        shutil.rmtree(job.work_dir, ignore_errors=True)
        job.expires_at = time.time() + settings.job_ttl_minutes * 60
