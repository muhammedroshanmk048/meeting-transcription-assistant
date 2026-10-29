"""faster-whisper (CTranslate2) on CPU. Local only: install requirements-local.txt."""
from functools import lru_cache
from pathlib import Path

from app.config import get_settings
from app.transcribe import Progress, Segment, Transcript

settings = get_settings()


@lru_cache
def get_model():
    from faster_whisper import WhisperModel

    return WhisperModel(settings.local_model, device=settings.local_device, compute_type=settings.local_compute_type)


def transcribe(path: Path, duration: float, work_dir: Path, progress: Progress) -> Transcript:
    progress(f"Loading Whisper {settings.local_model}")
    segments, info = get_model().transcribe(
        str(path), language=settings.language or None, beam_size=5, vad_filter=True
    )
    out = []
    # Segments are generated lazily while decoding, so progress can follow the audio position
    for s in segments:
        if s.text.strip():
            out.append(Segment(start=s.start, end=s.end, text=s.text.strip()))
        progress(f"Transcribing locally · {min(100, round(s.end / duration * 100))}%")
    return Transcript(segments=out, language=info.language, engine=f"faster-whisper/{settings.local_model}")
