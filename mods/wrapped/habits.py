"""How a session went: the Bottleneck and the Swear Jar, worked out from a transcript's records. Only counts come out of
here. A prompt's text is read, checked against WORDS and dropped, and no text is returned, stored or printed.

Bottleneck. A turn runs from a prompt to the assistant's last record before the next prompt. Worked is the time inside
turns. Waited is the time from a turn's end to the next prompt from the person, up to WAIT_CAP_MS. A longer gap is away,
not waiting. Silence over the cap inside a turn, such as a permission prompt left overnight, is away too. A prompt from a
program (an SDK prompt, a scheduled wake-up, a background task's notice) starts a turn but isn't a person answering, so
the gap before it isn't waiting. A session's last turn has no reply, so nothing is added after it.

Swear Jar. Only prompts the person typed count: promptSource "typed" or "queued", or no promptSource at all in older
transcripts unless an SDK sent them. Tool results, system text, slash commands, accepted suggestions and everything the
assistant wrote are never read. Code blocks, inline code, quoted lines, indented lines, URLs and system tags are removed
first. Transcripts don't mark pasted text, so a prompt over PASTE_CHARS is taken for a paste and skipped whole.
"""
import re
from collections import Counter

import wrapped

WAIT_CAP_MS = 30 * 60 * 1000
PASTE_CHARS = 2000
FINE = 0.25
JAR_FULL = 100

# Each word's forms, and the word it counts under. Whole words only, so scunthorpe, assassin and scrapped don't count.
WORDS = {
    "fuck": ("fuck", "fucks", "fucked", "fucking", "fucker", "fuckers", "motherfucker", "motherfuckers"),
    "shit": ("shit", "shits", "shitty", "shitting", "bullshit"),
    "damn": ("damn", "damned", "dammit", "goddamn"),
    "crap": ("crap", "crappy"),
    "asshole": ("asshole", "assholes", "arsehole", "arseholes"),
    "bastard": ("bastard", "bastards"),
    "bitch": ("bitch", "bitches"),
    "bollocks": ("bollocks",),
    "cunt": ("cunt", "cunts"),
    "wtf": ("wtf",),
}
ROOT_OF = {form: root for root, forms in WORDS.items() for form in forms}
SWEAR = re.compile(r"(?<!\w)(%s)(?!\w)" % "|".join(sorted(ROOT_OF, key=len, reverse=True)), re.IGNORECASE)

# Lines a person didn't type, and what's left of a prompt once they're gone.
NOT_A_PROMPT = ("<local-command", "<bash-", "<system-reminder", "<user-prompt-submit-hook", "Caveat:", "[Request interrupted",
                "This session is being continued")
FROM_A_PROGRAM = ("<task-notification", "<teammate-message")
FENCED = re.compile(r"(^|\n)[ \t]*(```|~~~).*?(\n[ \t]*\2[^\n]*|\Z)", re.DOTALL)
INLINE = re.compile(r"`[^`\n]*`")
TAGGED = re.compile(r"<(system-reminder|ide_[a-z_]+|command-[a-z]+|local-command-[a-z]+)>.*?</\1>", re.DOTALL)
LINES = re.compile(r"^(?:>|    |\t).*$", re.MULTILINE)
LINK = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)


def mask(word):
    """f***: the first letter and a star for each of the rest."""
    return word[0] + "*" * (len(word) - 1)


def prompt_text(record):
    """A user record's own text, or None for a tool result or nothing to read."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    return "\n".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")


def prompt_kind(record):
    """"typed", "human" (a person's act they didn't type: a slash command, an accepted suggestion), "machine", or None
    for a record that isn't a prompt at all."""
    if (record.get("type") != "user" or record.get("isSidechain") or record.get("isMeta") or record.get("isCompactSummary")
            or record.get("isVisibleInTranscriptOnly") or "toolUseResult" in record):
        return None
    text = prompt_text(record)
    if not text or not text.strip():
        return None
    head = text.lstrip()
    if head.startswith(NOT_A_PROMPT):
        return None
    source = record.get("promptSource")
    entry = str(record.get("entrypoint") or "")
    if (head.startswith(FROM_A_PROGRAM) or source == "sdk" or entry.startswith("sdk")
            or record.get("scheduledTaskId") or record.get("wakeupSource")):
        return "machine"
    if head.startswith("<command-") or source == "suggestion_accepted":
        return "human"
    if source in ("typed", "queued") or source is None:
        return "typed"
    return "machine"


def typed_words(text):
    """A typed prompt with everything that isn't the person's own words taken out."""
    if len(text) > PASTE_CHARS:
        return ""
    for pattern in (FENCED, TAGGED, INLINE, LINES, LINK):
        text = pattern.sub(" ", text)
    return text


def swears(text):
    """{"f***": 2}: how many times each word appears in a typed prompt, masked."""
    found = Counter(ROOT_OF[m.lower()] for m in SWEAR.findall(typed_words(text)))
    return {mask(root): count for root, count in found.items()}


def read(records):
    """One transcript's counts. Runs inside the read of a file, so prompts go no further than this."""
    worked = waited = slowest = waits = away = 0
    jar = Counter()
    open_turn = False
    mark = pending = 0
    ended = None
    for r in records:
        kind = r.get("type")
        if kind not in ("user", "assistant") or r.get("isSidechain"):
            continue
        stamp = wrapped._ts(r.get("timestamp"))
        if not stamp:
            continue
        now = stamp.timestamp() * 1000
        prompt = prompt_kind(r) if kind == "user" else None
        if prompt:
            if prompt == "typed":
                jar.update(swears(prompt_text(r)))
            if ended is not None and prompt != "machine":
                gap = now - ended
                if gap > WAIT_CAP_MS:
                    away += 1
                elif gap >= 0:
                    waited += gap
                    waits += 1
                    slowest = max(slowest, gap)
            open_turn, mark, pending, ended = True, now, 0, None
            continue
        if not open_turn:
            ended = now if kind == "assistant" else ended
            continue
        step = now - mark
        if 0 <= step <= WAIT_CAP_MS:
            pending += step
        mark = now
        if kind == "assistant":
            worked += pending
            pending = 0
            ended = now
    return {"worked_ms": int(worked), "waited_ms": int(waited), "slowest_ms": int(slowest), "waits": waits, "away": away,
            "swears": dict(jar)}


def fmt_span(ms):
    """41h, 6h 12m, 28m: hours and minutes that keep adding up past a day."""
    minutes = int(round(ms / 60000))
    hours, rest = divmod(minutes, 60)
    if hours >= 10:
        return "%dh" % round(ms / 3600000)
    if hours:
        return "%dh %dm" % (hours, rest) if rest else "%dh" % hours
    return "%dm" % minutes


def bottleneck_of(runs):
    """The window's Bottleneck, or None with no finished turn. Parallel sessions add up: it's agent time, not clock time."""
    seen = [r["habits"] for r in runs if r.get("habits")]
    worked = sum(h["worked_ms"] for h in seen)
    waited = sum(h["waited_ms"] for h in seen)
    if not worked and not waited:
        return None
    slowest = max(h["slowest_ms"] for h in seen)
    return {"worked": fmt_span(worked), "waited": fmt_span(waited), "slowest": fmt_span(slowest),
            "waitedShare": round(100 * waited / (worked + waited)), "away": sum(h["away"] for h in seen)}


def jar_of(runs):
    """The window's Swear Jar: counts and a masked word, nothing else."""
    words = Counter()
    for r in runs:
        words.update((r.get("habits") or {}).get("swears") or {})
    count = sum(words.values())
    top = min(words, key=lambda w: (-words[w], w)) if words else None
    return {"count": count, "fine": wrapped.fmt_dollars(count * FINE), "top": top, "level": round(100 * min(count, JAR_FULL) / JAR_FULL)}
