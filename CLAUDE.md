# CLAUDE.md

## What this project is

**Meeting Transcription Assistant**: a mini portfolio project. Users upload a meeting recording; the app returns a timestamped transcript, a summary, key decisions, and action items (owner, task, due date).

Skills it must demonstrate: speech recognition, audio processing, transcription, LLM summarization.
Required stack: Python, FastAPI, Whisper / WhisperX.

Owner: Muhammed Roshan M K, backend engineer (Python, FastAPI, GCP). Public portfolio project.
- Portfolio site: sibling repo `../project-zap` (live at https://muhammedroshanmk.vercel.app). Add a project card once this is deployed.
- Previous mini project, same conventions: `../rag-document-assistant` (DocChat, live at https://docchat-ft5u.onrender.com). Reuse its patterns: config, UI style, Dockerfile, render.yaml, session isolation, rate limiting.

## Hard constraint: everything must stay free

The owner will not use any paid service or credit card. Any dependency that could later force a payment must have a free fallback.

### Agreed architecture

- **Two transcription engines, switched with `TRANSCRIBE_PROVIDER`:**
  - `groq`: Whisper large-v3 (or large-v3-turbo) via Groq's free API. Used for the live demo. Free limits as reported (verify on Groq's limits page): 20 req/min, 2,000 req/day, 7,200 audio-sec/hour, 28,800/day, 25 MB per file.
  - `local`: faster-whisper (CTranslate2, int8, CPU) for plain transcription; WhisperX for word-level alignment and speaker diarization. Runs on the owner's PC only. Diarization needs a free Hugging Face token plus accepting the pyannote model terms.
- **Summarization: Gemini free tier** (`langchain-google-genai` or the google-genai SDK), returning structured JSON validated with Pydantic: summary, key decisions, action items `{owner, task, due, timestamp}`, open questions. Same model setup as DocChat: `GEMINI_MODEL=gemini-flash-latest`, `GEMINI_FALLBACK_MODEL=gemini-flash-lite-latest`, `max_retries=1`. `gemini-2.5-flash` is retired for new keys, and the free tier returns 503 at peak times. Optional `ollama` provider for fully offline summaries.
- **Audio processing: ffmpeg.** Convert any input (mp3, m4a, wav, mp4, webm) to 16 kHz mono, compressed (e.g. 32 kbps mp3/opus), to fit Groq's 25 MB limit. Get duration via ffprobe. If still too big, split into overlapping chunks and merge the transcripts with offset timestamps.
- **Long transcripts:** if a transcript exceeds what one summary call handles well, summarize in chunks then merge (map-reduce).
- **Privacy:** never store recordings. Process them in a temp dir and delete them. Keep results only in memory or for a short TTL.

### Hosting: Render free web service (Docker)

- Limits: about 512 MB RAM, 0.1 CPU. Sleeps after 15 minutes idle (about 1 minute cold start). Temporary disk.
- The deployed image must NOT include torch, WhisperX or faster-whisper. Keep local-only engines in `requirements-local.txt` and lazy-import them. Deployed `requirements.txt` stays lightweight. Dockerfile installs ffmpeg via apt.
- Not Hugging Face Spaces: since mid-2026, Docker/Gradio Spaces need PRO for new accounts. Not Vercel: serverless can't run ffmpeg jobs or long requests well.
- Set `GROQ_API_KEY` and `GOOGLE_API_KEY` as Render env vars / secrets.

### Public-demo safety (copy DocChat's approach)

- No login. A browser-generated random `X-Session-Id` scopes each user's jobs.
- Max upload size (≤ 25 MB), max audio duration (e.g. 30–45 min), per-IP rate limit on transcription, job results auto-expire.
- Long work runs as a background job: `POST /api/jobs` returns a job id, then `GET /api/jobs/{id}` polls status (`queued → converting → transcribing → summarizing → done/error`), or SSE progress. Don't hold one HTTP request open for minutes.

## Suggested layout

- `app/config.py`: pydantic-settings reading `.env`
- `app/audio.py`: ffmpeg/ffprobe conversion, duration, chunking
- `app/transcribe/`: `groq.py`, `local_whisper.py` (faster-whisper), `whisperx_engine.py` (alignment + diarization), common segment model `{start, end, text, speaker?}`
- `app/summarize.py`: prompt, Pydantic output schema, map-reduce for long transcripts
- `app/jobs.py`: in-memory job store with TTL + background worker
- `app/main.py`: FastAPI routes
- `static/`: plain HTML/CSS/JS UI. The owner asked for a look distinct from DocChat: same layout and style as DocChat (full-height sidebar, Inter / JetBrains Mono, dark only) but its own palette: emerald `#10b981` accent, amber `#fbbf24` highlights/timestamps, green-tinted dark background. Owner tried and rejected a serif "notebook", a pastel glass "bento" and a Linear-style mono theme. Upload, progress, transcript with clickable timestamps, summary, action-item checklist, export to Markdown / .txt / .srt
- `Dockerfile`, `render.yaml`, `requirements.txt`, `requirements-local.txt`, `.env.example`, `.gitignore`, `.dockerignore`, `README.md`
- `samples/`: a short public-domain or self-recorded test clip, plus a sample local WhisperX output with speaker labels to show in the README

## Plan / status (as of 2026-10-29)

Done:
- Steps 1–2: repo exists (`main`), full layout scaffolded (Groq + Gemini path, plus untested local engines). UI brand name: **MeetScribe**.
- `.venv` (Python 3.13) has the deployable deps; ffmpeg 9 installed via winget (open a new shell for PATH).
- Offline smoke test passed with a fake engine/LLM: ffprobe/ffmpeg conversion, overlapping chunk split + merge, job lifecycle, session isolation, one-active-job limit, md/txt/srt export, junk-file error, temp dir cleanup.
- `samples/demo-meeting.wav`: 68 s synthetic two-voice meeting (Windows SAPI TTS) with decisions, action items and an open question.

- Step 3: `.env` has both keys (the owner pasted them in chat; recommend rotating them before deploy).
- Real Groq transcription verified (68 s clip in ~4 s), including the chunked path (forced via `GROQ_MAX_FILE_MB=0.05 CHUNK_MINUTES=0.5`). Merge rule fixed after a duplicated phrase at a boundary; default overlap is now 10 s.
- Real Gemini structured output verified, single call and map-reduce (forced via `SUMMARY_CHUNK_CHARS=400`). Prompt tuned so self-commitments count as action items and deferred topics stay open questions. One run took ~5 min, probably a free-tier stall with 120 s timeout × retry × fallback.
- Docker image builds (988 MB, mostly ffmpeg libs) and imports fine.

- Owner tested the UI locally (2026-10-29) and approved it. Sample is now `samples/demo-meeting.mp3` (48 kbps).
- Step 7: first commit made; remote `origin` = github.com/muhammedroshanmk048/meeting-transcription-assistant.

- Step 8: deployed at https://meetscribe-5j70.onrender.com (Blueprint). Owner kept the original API keys (declined rotating). Live end-to-end works: health ok, ffmpeg present, sample clip → 8 segments, summary, 3 action items, exports.
- Open issue (2026-10-29): live summary step takes 70–100 s for the 68 s sample (locally 2–14 s); upload + convert + transcribe take ~10 s. Cause found so far: `gemini-flash-latest` (= gemini-3.8-flash) free tier is only 20 requests/day, used up by testing. Commit `4847744` (live) caches model clients, warms them at startup, skips retries on the main model and skips it for 1 h after RESOURCE_EXHAUSTED; Render logs confirm the 429 is now caught in 0.2 s, so the remaining time is in the `gemini-flash-lite-latest` call or CPU on Render. Re-test on 2026-10-30 after the quota reset: the full live job took 16 s (upload + convert + transcribe ~12 s, summary ~4 s), all exports 200. So the slow summaries only happen once the main model's 20/day quota is used up and every summary goes to the lite model. If that keeps happening, consider making lite the primary model.

- Step 9: MeetScribe card is live in `../project-zap/index.html` (commit `54165af`), linking the live demo and the repo.

Not yet verified: Render memory use, local engines (step 5 skipped for now; README marks local mode as untested, `requirements-local.txt` unpinned).

Steps:
1. Create the repo folder (`../meeting-transcription-assistant` unless the owner names it otherwise), `git init -b main`.
2. Scaffold the layout above; Groq engine + Gemini summarizer first, so the deployable path works end to end.
3. Owner creates `.env` from `.env.example` with `GROQ_API_KEY` (console.groq.com) and `GOOGLE_API_KEY` (aistudio.google.com/apikey). Never commit `.env`, never print keys, don't ask the owner to paste keys in chat.
4. Test locally with a real recording: conversion, transcription, summary, action items, export, limits.
5. Add the local engines (faster-whisper, then WhisperX + diarization) behind `TRANSCRIBE_PROVIDER=local`. Test on the owner's PC and save a sample output.
6. Pin working versions in both requirements files.
7. First commit, then publish to GitHub as `muhammedroshanmk048/<repo>`. `gh` CLI is NOT installed; the owner uses VS Code "Publish to GitHub".
8. Deploy on Render via `render.yaml` (Blueprint). Verify live under the memory limit.
9. Add a project card to `../project-zap/index.html` and push; Vercel auto-deploys.

## Environment notes

- Windows 11, Python 3.13 locally (Docker image: python:3.11-slim). Shell: PowerShell or Git Bash. Install ffmpeg locally (`winget install Gyan.FFmpeg`) for testing.
- Large pip installs (torch, WhisperX) are slow; run them in the background.
- Commit messages end with a `Co-Authored-By` line. Ask the owner before pushing.
