import json
import logging
import time
from functools import lru_cache

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

from app.config import get_settings
from app.transcribe import Segment

log = logging.getLogger("summarize")
settings = get_settings()


class ActionItem(BaseModel):
    owner: str | None = Field(None, description="Person responsible, as named in the meeting; null if nobody was named")
    task: str = Field(description="What has to be done, as a short imperative sentence")
    due: str | None = Field(None, description="Deadline exactly as stated (e.g. 'Friday', 'end of Q3'); null if none")
    timestamp: str | None = Field(None, description="[MM:SS] or [H:MM:SS] of the transcript line where it was agreed")


class MeetingNotes(BaseModel):
    title: str = Field(description="Short descriptive title for the meeting, at most 8 words")
    summary: str = Field(description="3-6 sentence summary of what was discussed and concluded")
    key_decisions: list[str] = Field(default_factory=list, description="Decisions that were actually agreed")
    action_items: list[ActionItem] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list, description="Questions raised but left unresolved")


RULES = (
    "Use ONLY what is said in the transcript; never invent names, dates or decisions. "
    "Transcription errors are possible, so prefer the most plausible reading of garbled words. "
    "Key decisions are choices the group agreed on, not facts or status updates. "
    "Action items include tasks assigned to someone AND commitments people make themselves "
    "(\"I'll do X\", \"I can finish it by Thursday\"); use the speaker's name when the transcript reveals it. "
    "A topic explicitly left open or deferred is an open question. "
    "Copy timestamps exactly from the transcript line where an action item was agreed. "
    "Write in the language of the transcript."
)

SUMMARY_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "You are a meeting assistant that writes accurate meeting notes. " + RULES),
    ("human", "{part_note}Transcript (each line starts with its timestamp):\n\n{transcript}"),
])

MERGE_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        "You are a meeting assistant. You receive notes written separately for consecutive parts of ONE meeting. "
        "Merge them into a single set of notes for the whole meeting: one summary, deduplicated decisions and "
        "action items (keep their timestamps), and drop an open question only if a later part clearly resolved it. " + RULES,
    ),
    ("human", "Notes per part, in order:\n\n{parts}"),
])


@lru_cache
def get_models() -> list[tuple[str, Runnable]]:
    """(name, model) pairs in the order to try them. Built once: client setup costs seconds on a 0.1 CPU host."""
    if settings.llm_provider == "ollama":
        from langchain_ollama import ChatOllama

        llm = ChatOllama(model=settings.ollama_model, base_url=settings.ollama_base_url, temperature=0.2)
        return [(settings.ollama_model, llm.with_structured_output(MeetingNotes))]

    from langchain_google_genai import ChatGoogleGenerativeAI

    if not settings.google_api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set. Add it to .env or switch LLM_PROVIDER to ollama.")

    def gemini(model: str, retries: int) -> Runnable:
        llm = ChatGoogleGenerativeAI(
            model=model, google_api_key=settings.google_api_key, temperature=0.2, max_retries=retries, timeout=90
        )
        return llm.with_structured_output(MeetingNotes)

    if not settings.gemini_fallback_model:
        return [(settings.gemini_model, gemini(settings.gemini_model, 1))]
    # No retries on the main model: with a fallback ready, a 503 or quota error should switch over at once
    return [
        (settings.gemini_model, gemini(settings.gemini_model, 0)),
        (settings.gemini_fallback_model, gemini(settings.gemini_fallback_model, 1)),
    ]


# Free-tier daily quotas are small (20 requests/day on the main Flash model), so once one is used up,
# skip that model for a while instead of paying for a failed call on every job
_skip_until: dict[str, float] = {}
QUOTA_COOLDOWN_SECONDS = 3600


def clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def transcript_lines(segments: list[Segment]) -> list[str]:
    return [f"[{clock(s.start)}] {s.speaker + ': ' if s.speaker else ''}{s.text}" for s in segments]


def chunk_lines(lines: list[str], max_chars: int) -> list[str]:
    chunks, current, size = [], [], 0
    for line in lines:
        if current and size + len(line) > max_chars:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _invoke(messages) -> MeetingNotes:
    models = get_models()
    available = [(n, m) for n, m in models if _skip_until.get(n, 0) < time.time()] or models[-1:]
    for i, (name, llm) in enumerate(available):
        started = time.time()
        try:
            notes = llm.invoke(messages)
            # Structured output returns None when the model's reply doesn't match the schema
            if not isinstance(notes, MeetingNotes):
                raise RuntimeError("The summary model returned an unexpected format")
        except Exception as e:
            log.warning("%s failed after %.1fs: %s", name, time.time() - started, str(e)[:200])
            if "RESOURCE_EXHAUSTED" in str(e) and len(models) > 1:
                _skip_until[name] = time.time() + QUOTA_COOLDOWN_SECONDS
            if i == len(available) - 1:
                raise
            continue
        log.info("Summary by %s in %.1fs", name, time.time() - started)
        return notes
    raise RuntimeError("No summary model available")


def summarize(segments: list[Segment], progress=lambda _: None) -> MeetingNotes:
    if not segments:
        return MeetingNotes(title="Empty recording", summary="No speech was detected in this recording.")
    chunks = chunk_lines(transcript_lines(segments), settings.summary_chunk_chars)
    if len(chunks) == 1:
        return _invoke(SUMMARY_PROMPT.format_messages(part_note="", transcript=chunks[0]))

    # Map-reduce: notes per part, then one merge call over the (much shorter) partial notes
    parts = []
    for i, chunk in enumerate(chunks, 1):
        progress(f"Summarizing part {i} of {len(chunks)}")
        note = f"This is part {i} of {len(chunks)} of a longer meeting.\n\n"
        parts.append(_invoke(SUMMARY_PROMPT.format_messages(part_note=note, transcript=chunk)))
    progress("Merging notes")
    merged = "\n\n".join(f"Part {i}:\n{json.dumps(p.model_dump(), ensure_ascii=False)}" for i, p in enumerate(parts, 1))
    return _invoke(MERGE_PROMPT.format_messages(parts=merged))
