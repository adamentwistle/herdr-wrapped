"""Made-up runs for demo mode, in the shape wrapped.parse_run gives. Nobody's history: six months of a heavy user, invented
from a fixed seed, so a demo reads the same everywhere. They go through the real stats, peak, Bottleneck and Swear Jar.
"""
import random
from datetime import datetime, timedelta, timezone

import habits
import wrapped

SEED = 20260705
FIRST_DAY = datetime(2026, 5, 1, tzinfo=timezone.utc)
DAYS = 184
SPRINT_DAY = datetime(2026, 9, 17, tzinfo=timezone.utc)
SPRINT_AGENTS = 12
MODEL = "claude-opus-4-8"
TOOLS = [("Bash", 0.36), ("Read", 0.27), ("Edit", 0.17), ("Grep", 0.11), ("Write", 0.05), ("Agent", 0.04)]
SWEARS = [("fuck", 0.5), ("shit", 0.3), ("damn", 0.2)]
MINUTE = 60000


def pick(rng, weighted):
    roll = rng.random()
    for value, weight in weighted:
        roll -= weight
        if roll <= 0:
            return value
    return weighted[-1][0]


def between(rng, low, high):
    return low + (high - low) * rng.random()


def session(rng, number, start, minutes, size):
    """One made-up session of `minutes`, about `size` tokens through the model."""
    cache_read = int(size * 0.93)
    cache_creation = int(size * 0.055)
    output = int(size * 0.012)
    inputs = size - cache_read - cache_creation - output
    calls = max(4, int(minutes * between(rng, 0.9, 1.5)))
    tools = {}
    for _ in range(calls):
        name = pick(rng, TOOLS)
        tools[name] = tools.get(name, 0) + 1
    span = int(minutes * MINUTE)
    worked = int(span * between(rng, 0.55, 0.72))
    waits = max(1, int(minutes / 14))
    waited = min(span - worked, int(waits * between(rng, 0.7, 2.3) * MINUTE))
    swears = {}
    for _ in range(2 if rng.random() < 0.012 else 1 if rng.random() < 0.03 else 0):
        word = habits.mask(pick(rng, SWEARS))
        swears[word] = swears.get(word, 0) + 1
    ended = start + timedelta(milliseconds=span)
    return {
        "run_id": "sample-%04d" % number, "project": None, "model": MODEL, "is_transcript": True, "is_subagent": False,
        "input_tokens": inputs, "output_tokens": output, "cache_read": cache_read, "cache_creation": cache_creation,
        "tokens": size, "cost_usd": round(wrapped._cost(MODEL, inputs, output, cache_read, cache_creation), 6),
        "tools": tools, "tool_calls": calls, "absolutely_right": 1 if rng.random() < 0.3 else 0,
        "started": start.isoformat(), "ended": ended.isoformat(), "span_ms": span,
        "habits": {"worked_ms": worked, "waited_ms": waited, "slowest_ms": int(between(rng, 3, 27) * MINUTE), "waits": waits,
                   "away": 1 if rng.random() < 0.2 else 0, "swears": swears},
    }


def runs():
    """About 700 sessions from May to October. Most days stay under a handful at once, and one sprint reaches twelve."""
    rng = random.Random(SEED)
    made = []
    for offset in range(DAYS):
        day = FIRST_DAY + timedelta(days=offset)
        if day == SPRINT_DAY:
            for i in range(SPRINT_AGENTS):
                start = day + timedelta(hours=14, minutes=i * 3)
                made.append(session(rng, len(made), start, between(rng, 60, 140), int(between(rng, 6e6, 14e6))))
            continue
        weekend = day.weekday() >= 5
        grow = 1 + offset / DAYS
        for _ in range(int(between(rng, 0, 2.2) if weekend else between(rng, 2.5, 3.9) * grow)):
            start = day + timedelta(hours=between(rng, 8, 21))
            minutes = between(rng, 12, 130) if rng.random() > 0.04 else between(rng, 200, 340)
            made.append(session(rng, len(made), start, minutes, int(minutes * between(rng, 0.05e6, 0.12e6))))
    return sorted(made, key=lambda r: r["started"])
