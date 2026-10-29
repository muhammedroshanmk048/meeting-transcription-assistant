from app.jobs import Job
from app.summarize import clock, transcript_lines


def srt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    return f"{ms // 3_600_000:02d}:{ms % 3_600_000 // 60_000:02d}:{ms % 60_000 // 1000:02d},{ms % 1000:03d}"


def to_srt(job: Job) -> str:
    blocks = []
    for i, s in enumerate(job.transcript.segments, 1):
        text = f"{s.speaker}: {s.text}" if s.speaker else s.text
        blocks.append(f"{i}\n{srt_time(s.start)} --> {srt_time(s.end)}\n{text}\n")
    return "\n".join(blocks)


def to_txt(job: Job) -> str:
    return "\n".join(transcript_lines(job.transcript.segments)) + "\n"


def to_markdown(job: Job) -> str:
    n = job.notes
    out = [f"# {n.title if n else job.filename}", ""]
    out.append(f"*{job.filename} · {clock(job.duration or 0)} · transcribed with {job.transcript.engine}*")
    if n:
        out += ["", "## Summary", "", n.summary]
        if n.key_decisions:
            out += ["", "## Key decisions", ""] + [f"- {d}" for d in n.key_decisions]
        if n.action_items:
            out += ["", "## Action items", ""]
            for a in n.action_items:
                owner = f"**{a.owner}**: " if a.owner else ""
                due = f" (due {a.due})" if a.due else ""
                at = f" `{a.timestamp.strip('[]')}`" if a.timestamp else ""
                out.append(f"- [ ] {owner}{a.task}{due}{at}")
        if n.open_questions:
            out += ["", "## Open questions", ""] + [f"- {q}" for q in n.open_questions]
    out += ["", "## Transcript", ""] + [f"{line}  " for line in transcript_lines(job.transcript.segments)]
    return "\n".join(out) + "\n"


FORMATS = {
    "md": (to_markdown, "text/markdown"),
    "txt": (to_txt, "text/plain"),
    "srt": (to_srt, "application/x-subrip"),
}
