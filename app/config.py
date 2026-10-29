from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Transcription: "groq" (Whisper via Groq's free API, used for the live demo)
    # or "local" (faster-whisper / WhisperX on this machine, see requirements-local.txt)
    transcribe_provider: Literal["groq", "local"] = "groq"
    groq_api_key: str = ""
    groq_model: str = "whisper-large-v3-turbo"
    # Empty lets Whisper detect the language; set e.g. "en" to force one
    language: str = ""

    # Local engines. Diarization (speaker labels) uses WhisperX + pyannote and needs a free
    # Hugging Face token with the pyannote model terms accepted; without it faster-whisper is used.
    local_model: str = "small"
    local_device: str = "cpu"
    local_compute_type: str = "int8"
    local_diarize: bool = False
    hf_token: str = ""

    # Summaries: "gemini" (free tier API key) or "ollama" (local, fully offline)
    llm_provider: Literal["gemini", "ollama"] = "gemini"
    google_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    # Used when the main model is overloaded (free tier returns 503 at peak times); empty disables it
    gemini_fallback_model: str = "gemini-flash-lite-latest"
    ollama_model: str = "llama3.2"
    ollama_base_url: str = "http://localhost:11434"
    # Transcripts longer than this are summarized in chunks, then merged (map-reduce)
    summary_chunk_chars: int = 60_000

    # Audio is converted to 16 kHz mono Opus at this bitrate before transcription.
    # 32 kbps is ~14 MB per hour, well under Groq's 25 MB per-request limit.
    audio_bitrate: str = "32k"
    groq_max_file_mb: float = 24
    # Only used when a converted file is still too big for one request
    chunk_minutes: float = 10
    chunk_overlap_seconds: float = 10

    # Limits for a public deployment: each browser session only sees its own jobs,
    # recordings are deleted right after processing and results expire after job_ttl_minutes
    max_upload_mb: int = 25
    max_audio_minutes: float = 45
    job_ttl_minutes: float = 60
    max_jobs_per_session: int = 5
    jobs_per_hour_per_ip: int = 5
    # Groq's free tier allows 7,200 audio seconds per hour across the whole demo
    groq_audio_seconds_per_hour: int = 7200
    cleanup_interval_minutes: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()
