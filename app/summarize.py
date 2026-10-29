import json

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

from app.config import get_settings
from app.transcribe import Segment

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


def get_llm() -> Runnable:
    if settings.llm_provider == "ollama":
        from langchain_ollama import ChatOllama

        llm = ChatOllama(model=settings.ollama_model, base_url=settings.ollama_base_url, temperature=0.2)
        return llm.with_structured_output(MeetingNotes)

    from langchain_google_genai import ChatGoogleGenerativeAI

    if not settings.google_api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set. Add it to .env or switch LLM_PROVIDER to ollama.")

    def gemini(model: str) -> Runnable:
        # Fail fast on free-tier 503s instead of retrying for minutes
        llm = ChatGoogleGenerativeAI(
            model=model, google_api_key=settings.google_api_key, temperature=0.2, max_retries=1, timeout=120
        )
        return llm.with_structured_output(MeetingNotes)

    llm = gemini(settings.gemini_model)
    if settings.gemini_fallback_model:
        llm = llm.with_fallbacks([gemini(settings.gemini_fallback_model)])
    return llm


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


def _invoke(llm: Runnable, messages) -> MeetingNotes:
    notes = llm.invoke(messages)
    # Structured output returns None when the model's reply doesn't match the schema
    if not isinstance(notes, MeetingNotes):
        raise RuntimeError("The summary model returned an unexpected format")
    return notes


def summarize(segments: list[Segment], progress=lambda _: None) -> MeetingNotes:
    if not segments:
        return MeetingNotes(title="Empty recording", summary="No speech was detected in this recording.")
    llm = get_llm()
    chunks = chunk_lines(transcript_lines(segments), settings.summary_chunk_chars)
    if len(chunks) == 1:
        return _invoke(llm, SUMMARY_PROMPT.format_messages(part_note="", transcript=chunks[0]))

    # Map-reduce: notes per part, then one merge call over the (much shorter) partial notes
    parts = []
    for i, chunk in enumerate(chunks, 1):
        progress(f"Summarizing part {i} of {len(chunks)}")
        note = f"This is part {i} of {len(chunks)} of a longer meeting.\n\n"
        parts.append(_invoke(llm, SUMMARY_PROMPT.format_messages(part_note=note, transcript=chunk)))
    progress("Merging notes")
    merged = "\n\n".join(f"Part {i}:\n{json.dumps(p.model_dump(), ensure_ascii=False)}" for i, p in enumerate(parts, 1))
    return _invoke(llm, MERGE_PROMPT.format_messages(parts=merged))
