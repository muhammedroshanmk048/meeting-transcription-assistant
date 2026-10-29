# MeetScribe: Meeting Transcription Assistant

Upload a meeting recording and get back a **timestamped transcript**, a **summary**, **key decisions**, **action items** (owner, task, due date) and **open questions**. You can export everything as Markdown, .txt or .srt subtitles.

Built to run **100% free**: Whisper through Groq's free API (or fully local with faster-whisper / WhisperX), summaries from the Gemini free tier (or a local Ollama model), and ffmpeg for audio processing.

## How it works

```
Upload (mp3 · m4a · wav · mp4 · webm …)
        │
        ▼
 ffprobe duration check ─► ffmpeg → 16 kHz mono Opus, 32 kbps (~14 MB/hour)
        │                       └─ still > 24 MB? split into overlapping chunks
        ▼
 Whisper large-v3-turbo (Groq)  ─or─  faster-whisper / WhisperX + pyannote (local)
        │                                          └─ word alignment + speaker labels
        ▼
 Segments {start, end, text, speaker?} ─► chunk timestamps re-offset and de-duplicated
        │
        ▼
 Gemini → structured JSON (Pydantic): title, summary, decisions, action items, open questions
          long transcripts: summarize per part, then merge (map-reduce)
```

The work runs as a background job: `POST /api/jobs` returns right away, and the UI polls `GET /api/jobs/{id}` as the job moves through `queued → converting → transcribing → summarizing → done`.

**Skills covered:** speech recognition, audio processing (ffmpeg), transcription with timestamps, speaker diarization (local), LLM summarization with structured output, background jobs.

## Tech stack

| Layer | Tool |
|---|---|
| API | Python, FastAPI, Pydantic |
| Audio | ffmpeg / ffprobe |
| Speech recognition | Whisper large-v3-turbo via Groq (free), or faster-whisper / WhisperX locally |
| Diarization | pyannote via WhisperX (local, free Hugging Face token) |
| Summaries | Gemini (free tier) via LangChain structured output, or Ollama (local) |
| Frontend | Plain HTML/CSS/JS: clickable timestamps, playback sync, transcript search, action-item checklist |
| Deploy | Docker, Render (free tier) |

## Run locally

Requires [ffmpeg](https://ffmpeg.org/) on PATH (Windows: `winget install Gyan.FFmpeg`).

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt

copy .env.example .env          # then add GROQ_API_KEY and GOOGLE_API_KEY
uvicorn app.main:app --reload
```

Open http://localhost:8000 · API docs at http://localhost:8000/docs

Free keys: [Groq console](https://console.groq.com/keys) · [Google AI Studio](https://aistudio.google.com/apikey).

A short test clip is in [`samples/demo-meeting.mp3`](samples/demo-meeting.mp3): a synthetic two-voice stand-up with decisions, action items and an open question.

### Fully local (no API keys, not yet tested)

```bash
pip install -r requirements-local.txt   # faster-whisper + WhisperX (pulls in PyTorch, large download)
ollama pull llama3.2
```

In `.env`, set `TRANSCRIBE_PROVIDER=local` and `LLM_PROVIDER=ollama`. For speaker labels, also set `LOCAL_DIARIZE=true` and an `HF_TOKEN`, and accept the terms of the pyannote models on Hugging Face.

## API

| Method | Path | |
|---|---|---|
| `POST` | `/api/jobs` | multipart `file`; returns `{id, status}` (202) |
| `GET` | `/api/jobs` | this session's jobs |
| `GET` | `/api/jobs/{id}` | status, plus `transcript` and `notes` when done |
| `GET` | `/api/jobs/{id}/export?format=md\|txt\|srt` | download |
| `DELETE` | `/api/jobs/{id}` | remove a finished job |

Every `/api/jobs` call needs an `X-Session-Id` header (the UI generates a random one per browser).

## Privacy and public-demo limits

- **Recordings are never stored.** Uploads go to a private temp dir that is deleted when the job ends. Results live only in memory and expire after 60 minutes.
- No login: each browser only sees its own jobs.
- Per recording: 25 MB and 45 minutes max. One job at a time per browser, 5 per IP per hour, plus a demo-wide hourly audio budget that keeps usage inside Groq's free quota.

## Deploy (Render free tier)

The deployed image has no PyTorch: transcription runs on Groq, so it fits in 512 MB.

1. Push this repo to GitHub.
2. Render → **New → Blueprint** → pick the repo (uses `render.yaml`).
3. Enter `GROQ_API_KEY` and `GOOGLE_API_KEY` when prompted.
