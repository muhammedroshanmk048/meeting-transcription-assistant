"""WhisperX: transcription + word-level alignment + speaker diarization (pyannote). Local only."""
from functools import lru_cache
from pathlib import Path

from app.config import get_settings
from app.transcribe import Progress, Segment, Transcript

settings = get_settings()


@lru_cache
def get_model():
    import whisperx

    return whisperx.load_model(
        settings.local_model,
        settings.local_device,
        compute_type=settings.local_compute_type,
        language=settings.language or None,
    )


@lru_cache
def get_diarizer():
    try:
        from whisperx.diarize import DiarizationPipeline
    except ImportError:  # older WhisperX releases export it at the top level
        from whisperx import DiarizationPipeline

    if not settings.hf_token:
        raise RuntimeError("LOCAL_DIARIZE needs HF_TOKEN (free Hugging Face token with pyannote terms accepted)")
    return DiarizationPipeline(use_auth_token=settings.hf_token, device=settings.local_device)


def transcribe(path: Path, duration: float, work_dir: Path, progress: Progress) -> Transcript:
    import whisperx

    audio = whisperx.load_audio(str(path))
    progress(f"Transcribing with WhisperX {settings.local_model}")
    result = get_model().transcribe(audio, batch_size=8)
    language = result["language"]

    progress("Aligning words to the audio")
    align_model, metadata = whisperx.load_align_model(language_code=language, device=settings.local_device)
    result = whisperx.align(result["segments"], align_model, metadata, audio, settings.local_device)

    progress("Identifying speakers")
    result = whisperx.assign_word_speakers(get_diarizer()(audio), result)

    segments = [
        Segment(start=s["start"], end=s["end"], text=s["text"].strip(), speaker=s.get("speaker"))
        for s in result["segments"]
        if s.get("text", "").strip() and "start" in s
    ]
    return Transcript(segments=segments, language=language, engine=f"whisperx/{settings.local_model}+pyannote")
