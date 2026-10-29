from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from app.config import get_settings

settings = get_settings()

Progress = Callable[[str], None]


class Segment(BaseModel):
    start: float
    end: float
    text: str
    speaker: str | None = None


class Transcript(BaseModel):
    segments: list[Segment]
    language: str | None = None
    engine: str


def transcribe(path: Path, duration: float, work_dir: Path, progress: Progress) -> Transcript:
    """Run the configured engine. Local engines are imported lazily so the deployed image never needs torch."""
    if settings.transcribe_provider == "local":
        if settings.local_diarize:
            from app.transcribe.whisperx_engine import transcribe as run
        else:
            from app.transcribe.local_whisper import transcribe as run
    else:
        from app.transcribe.groq import transcribe as run
    return run(path, duration, work_dir, progress)


def merge_chunks(chunks: list[tuple[float, float, list[Segment]]]) -> list[Segment]:
    """Join transcripts of overlapping chunks: (offset, chunk length, segments relative to the chunk).

    Neighbours share an overlap, cut in its middle: a chunk keeps the segments that start before the cut,
    the next one those that start after it. A sentence running across the cut is already in the earlier
    chunk, so the next chunk also skips anything that starts before the last kept segment ended.
    """
    merged: list[Segment] = []
    for i, (offset, length, segments) in enumerate(chunks):
        lo = offset + settings.chunk_overlap_seconds / 2 if i > 0 else float("-inf")
        hi = offset + length - settings.chunk_overlap_seconds / 2 if i < len(chunks) - 1 else float("inf")
        if merged:
            lo = max(lo, merged[-1].end - 0.5)
        for s in segments:
            start, end = s.start + offset, s.end + offset
            if lo <= start < hi:
                merged.append(s.model_copy(update={"start": start, "end": end}))
    return merged
