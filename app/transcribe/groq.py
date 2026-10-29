from functools import lru_cache
from pathlib import Path

from groq import Groq

from app import audio
from app.config import get_settings
from app.transcribe import Progress, Segment, Transcript, merge_chunks

settings = get_settings()


@lru_cache
def get_client() -> Groq:
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is not set. Add it to .env or switch TRANSCRIBE_PROVIDER to local.")
    # The SDK backs off and retries on 429s, which the free tier returns under load
    return Groq(api_key=settings.groq_api_key, max_retries=2, timeout=300)


def _field(obj, name: str):
    # verbose_json fields arrive as extra attributes, or plain dicts depending on SDK version
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def transcribe_file(path: Path) -> tuple[list[Segment], str | None]:
    kwargs = {"language": settings.language} if settings.language else {}
    result = get_client().audio.transcriptions.create(
        file=(path.name, path.read_bytes()),
        model=settings.groq_model,
        response_format="verbose_json",
        timestamp_granularities=["segment"],
        temperature=0,
        **kwargs,
    )
    segments = [
        Segment(start=_field(s, "start"), end=_field(s, "end"), text=_field(s, "text").strip())
        for s in _field(result, "segments") or []
        if _field(s, "text").strip()
    ]
    return segments, _field(result, "language")


def transcribe(path: Path, duration: float, work_dir: Path, progress: Progress) -> Transcript:
    if path.stat().st_size <= settings.groq_max_file_mb * 1024 * 1024:
        segments, language = transcribe_file(path)
        return Transcript(segments=segments, language=language, engine=f"groq/{settings.groq_model}")

    # Too big for one request: transcribe overlapping chunks and stitch them back on the original timeline
    chunks = audio.split(path, duration, work_dir)
    results, language = [], None
    for i, (offset, chunk) in enumerate(chunks, 1):
        progress(f"Transcribing part {i} of {len(chunks)}")
        segments, language = transcribe_file(chunk)
        results.append((offset, min(settings.chunk_minutes * 60, duration - offset), segments))
        chunk.unlink(missing_ok=True)
    return Transcript(segments=merge_chunks(results), language=language, engine=f"groq/{settings.groq_model}")
