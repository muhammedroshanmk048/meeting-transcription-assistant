import shutil
import subprocess
from pathlib import Path

from app.config import get_settings

settings = get_settings()


class AudioError(ValueError):
    pass


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _run(cmd: list[str], timeout: float = 600) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise AudioError("Audio processing took too long")
    if result.returncode != 0:
        # The last stderr line usually names the real problem ("<path>: Invalid data found…"); hide the server path
        lines = result.stderr.strip().splitlines()
        reason = lines[-1].rsplit(": ", 1)[-1] if lines else "unknown error"
        raise AudioError(f"Could not read this file as audio: {reason}")
    return result.stdout


def duration(path: Path) -> float:
    """Length in seconds, read from the container metadata (no decoding)."""
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], timeout=60)
    try:
        seconds = float(out.strip())
    except ValueError:
        raise AudioError("Could not find an audio track in this file")
    if seconds <= 0:
        raise AudioError("This file has no audio")
    return seconds


def convert(src: Path, dst_dir: Path, start: float = 0, length: float | None = None, name: str = "audio") -> Path:
    """Re-encode any audio/video input to 16 kHz mono Opus: what Whisper uses internally, at a fraction of the size."""
    dst = dst_dir / f"{name}.ogg"
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if length is not None:
        cmd += ["-t", f"{length:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", settings.audio_bitrate, str(dst)]
    _run(cmd)
    return dst


def split(src: Path, total: float, dst_dir: Path) -> list[tuple[float, Path]]:
    """Cut into overlapping chunks. Returns (offset in seconds, chunk path) pairs."""
    length = settings.chunk_minutes * 60
    step = length - settings.chunk_overlap_seconds
    chunks = []
    start = 0.0
    while start < total:
        chunks.append((start, convert(src, dst_dir, start=start, length=length, name=f"chunk{len(chunks):03d}")))
        if start + length >= total:
            break
        start += step
    return chunks
