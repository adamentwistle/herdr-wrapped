#!/usr/bin/env python3
# This copy is released under the MIT license by its copyright holder. See LICENSE.
"""claude-code-wrapped - spotify wrapped for your claude code usage.

claude code shows you this session's cost and forgets it. wrapped reads your
local ~/.claude transcripts (jsonl on disk, no api, no network, all local) and
turns a whole month (or all time) into one shareable card: total tokens, real
cost, busiest day, most-used tool, longest session, and the meme stat - how many
times claude told you "you're absolutely right".

every number is computed the honest way: assistant records that share a
requestId are one api turn with cumulative usage, so they are collapsed and
counted once (otherwise cost and tool counts inflate); cache reads bill at 0.1x
the input rate and cache writes at 1.25x. it proves itself on a bundled fixture
corpus, so the numbers are deterministic.

usage:
  python3 wrapped.py                          interactive setup (pick profile, format, ...)
  python3 wrapped.py profiles                  list the claude profiles it can read
  python3 wrapped.py demo                      the text wrapped, on made-up data
  python3 wrapped.py wrap [dir]                the text wrapped for a transcripts dir
  python3 wrapped.py card [dir] --out <f>      the shareable card, rendered to PNG
  python3 wrapped.py selftest                  prove the stats on the fixtures

profiles: claude code stores transcripts per profile under $CLAUDE_CONFIG_DIR
          (default ~/.claude). wrapped reads the ACTIVE profile by default, which
          is what makes it agree with `/usage` when you have several. `profiles`
          lists them; pass a dir to `wrap`/`card` to read a specific one.

card options:
  --format square|wide            aspect ratio (default square: universal, safe anywhere)
  --share                         also print ready-to-paste X / LinkedIn / Slack captions
  --html                          write the HTML instead of rasterizing to PNG
  --demo                          use the made-up data in sample.py, marked on the card
  the card auto-renders to PNG via a headless chromium browser if one is found
  (override with $CLAUDE_WRAPPED_BROWSER); otherwise it falls back to HTML.

windows:  --month YYYY-MM  (just that month)   --all-time  (default)
privacy:  the card never shows repo names or paths. nothing leaves the machine.
output:   --json emits the structured stats (on wrap).

point it at your own history:
  python3 wrapped.py wrap ~/.claude/projects
  python3 wrapped.py card ~/.claude/projects --month 2026-07 --format wide --share

this copy lives in the herdr mod. it differs from the source in a few places:
sessions counts real sessions only (parse_run's is_subagent, wrapped_stats); parse_run
takes an `extra` reader and requests to skip; the card and captions take "peak",
"bottleneck", "jar" and "sample" from the stats dict (html_card, _CARD_BODY, CARD_STYLE,
share_text), and without those keys they match the source; render_png has a time limit
and a throwaway profile; `demo` and `card --demo` use the made-up data in sample.py.
"""
import contextlib
import glob
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zlib
from datetime import date, datetime, timedelta, timezone
from html import escape

# per-model price per 1,000,000 tokens: (input, output). cache reads bill at 0.1x
# the input rate, cache writes at 1.25x. unknown models fall back to opus-tier.
PRICING = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_DEFAULT_PRICE = (5.0, 25.0)

# the meme. counts every time an assistant turn concedes the point: "you're
# absolutely right" / "you are absolutely right", any case, straight or curly
# apostrophe. per-occurrence, so a turn that says it twice counts twice.
_ABSOLUTELY_RIGHT = re.compile(r"\byou(?:['’])?re\s+absolutely\s+right\b"
                               r"|\byou\s+are\s+absolutely\s+right\b", re.IGNORECASE)

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec"]

# the "get your own" the card + text wrapped show so a reshare drives people back.
# one place to change it.
REPO = "github.com/adamentwistle/herdr-wrapped"


def _price(model):
    if model:
        for key, price in PRICING.items():
            if model == key or model.startswith(key):
                return price
    return _DEFAULT_PRICE


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _ts(raw):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    # normalize to tz-aware (assume UTC when a stamp carries no offset) so a
    # transcript mixing naive and aware timestamps can still be min/max'd.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _cost(model, inp_t, out_t, cache_r, cache_c):
    inp, out = _price(model)
    return (inp_t * inp + out_t * out + cache_r * (inp * 0.1)
            + cache_c * (inp * 1.25)) / 1e6


def _usage_total(u):
    """Sum of the four token fields in a usage dict (0 for an empty/none dict)."""
    if not isinstance(u, dict):
        return 0
    return (_int(u.get("input_tokens")) + _int(u.get("output_tokens"))
            + _int(u.get("cache_read_input_tokens")) + _int(u.get("cache_creation_input_tokens")))


def parse_run(path, extra=None, skip_requests=()):
    """Parse one transcript jsonl into a normalized per-session record. Assistant
    records that share a requestId are one api turn with cumulative usage, so they
    are collapsed (counted once) - otherwise cost and tool counts inflate.
    `extra(records)` adds to the result under "habits". `skip_requests` are api turns
    already counted elsewhere: a subagent's file can repeat one of its parent's."""
    records = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                records.append(rec)

    groups, order = {}, []
    seen_tool_ids, times, absolutely_right, cwds = set(), [], 0, []
    for r in records:
        ts = _ts(r.get("timestamp"))
        if ts:
            times.append(ts)
        msg = r.get("message")
        if not isinstance(msg, dict):
            msg = {}
        content = msg.get("content")
        if not isinstance(content, list):
            content = []
        if r.get("type") != "assistant" or not r.get("uuid"):
            continue
        if r.get("cwd"):
            cwds.append(r["cwd"])
        # the meme is what claude told YOU - count main-thread turns only, not
        # subagent (sidechain) self-talk you never saw.
        if not r.get("isSidechain"):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    absolutely_right += len(_ABSOLUTELY_RIGHT.findall(block.get("text") or ""))
        rid = r.get("requestId") or r["uuid"]
        if rid in skip_requests:
            continue
        if rid not in groups:
            groups[rid] = {"usage": {}, "model": None, "tools": []}
            order.append(rid)
        g = groups[rid]
        # claude code writes one line per content block, each repeating the reply's
        # usage. /stats adds up every line, and so does this, so the totals agree
        # with /stats. tool calls are still counted once per tool_use id.
        u = msg.get("usage")
        if isinstance(u, dict):
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                g["usage"][k] = _int(g["usage"].get(k)) + _int(u.get(k))
        if msg.get("model"):
            g["model"] = msg["model"]
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                bid = block.get("id")
                if bid in seen_tool_ids:
                    continue
                seen_tool_ids.add(bid)
                g["tools"].append(block.get("name") or "?")

    input_t = output_t = cache_r = cache_c = 0
    cost = 0.0
    model_counts, tools = {}, {}
    for rid in order:
        g = groups[rid]
        u = g["usage"]
        it, ot = _int(u.get("input_tokens")), _int(u.get("output_tokens"))
        cr, cc = _int(u.get("cache_read_input_tokens")), _int(u.get("cache_creation_input_tokens"))
        input_t += it
        output_t += ot
        cache_r += cr
        cache_c += cc
        cost += _cost(g["model"], it, ot, cr, cc)
        if g["model"]:
            model_counts[g["model"]] = model_counts.get(g["model"], 0) + 1
        for name in g["tools"]:
            tools[name] = tools.get(name, 0) + 1

    started, ended = (min(times), max(times)) if times else (None, None)
    span_ms = int((ended - started).total_seconds() * 1000) if times and ended > started else 0
    model = max(model_counts, key=model_counts.get) if model_counts else None
    run_id = next((r.get("sessionId") for r in records if r.get("sessionId")),
                  os.path.splitext(os.path.basename(path))[0])
    # a real transcript has at least one conversational turn; a bare metadata /
    # sidecar .jsonl has none and is not a session.
    is_transcript = any(r.get("type") in ("user", "assistant") for r in records)
    # a subagent gets a transcript of its own, inside its session's folder, or in older
    # versions a file of nothing but sidechain records. it's part of a session, not one.
    turns = [r for r in records if r.get("type") in ("user", "assistant")]
    is_subagent = ("subagents" in os.path.normpath(path).split(os.sep)[:-1]
                   or bool(turns) and all(r.get("isSidechain") for r in turns))
    # the project is the working directory's basename - the only name-bearing
    # field wrapped touches. it stays in the local --json output only; the card
    # and the terminal wrapped never show it.
    project = os.path.basename(max(set(cwds), key=cwds.count)) if cwds else None
    return {
        "run_id": run_id, "project": project, "model": model,
        "is_transcript": is_transcript, "is_subagent": is_subagent,
        "habits": extra(records) if extra else None,
        "input_tokens": input_t, "output_tokens": output_t,
        "cache_read": cache_r, "cache_creation": cache_c,
        # total throughput: every token the model actually processed, cache
        # included - cache reads are real context, and they are most of the bill.
        "tokens": input_t + output_t + cache_r + cache_c, "cost_usd": round(cost, 6),
        "tools": tools, "tool_calls": sum(tools.values()),
        "absolutely_right": absolutely_right,
        "started": started.isoformat() if started else None,
        "ended": ended.isoformat() if ended else None, "span_ms": span_ms,
    }


def ingest_dir(directory):
    """Parse every *.jsonl transcript under a directory into per-session records,
    skipping non-transcript sidecars, sorted by start time. Searches recursively,
    so a projects/ root that nests one dir per project works without naming each.
    Dedup is within a file: claude code writes one session per jsonl and does not
    repeat a requestId across files (verified on real history), so this is correct
    for the on-disk format. One unparseable file is skipped, not fatal."""
    paths = glob.glob(os.path.join(directory, "*.jsonl"))
    paths += glob.glob(os.path.join(directory, "**", "*.jsonl"), recursive=True)
    runs = []
    for p in sorted(set(paths)):
        try:
            r = parse_run(p)
        except Exception as e:  # a corrupt file must not take down the whole wrapped
            print("skipped %s (%s)" % (os.path.basename(p), e), file=sys.stderr)
            continue
        if r["is_transcript"]:
            runs.append(r)
    return sorted(runs, key=lambda r: r["started"] or "")


def filter_month(runs, month):
    """Keep only sessions that started in `month` (a YYYY-MM string)."""
    if not month:
        return runs
    return [r for r in runs if (r["started"] or "")[:7] == month]


def default_transcript_dir():
    """The transcripts of the ACTIVE claude profile. Claude Code stores config +
    transcripts under $CLAUDE_CONFIG_DIR (default ~/.claude), so honoring it is
    what makes wrapped agree with `/usage` when several profiles exist on one box."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg and os.path.isdir(os.path.join(cfg, "projects")):
        return os.path.join(cfg, "projects")
    return os.path.expanduser("~/.claude/projects")


def discover_profiles():
    """Every claude config dir on this machine that has transcripts, most-populated
    first, with the active one flagged. Claude Code keeps each profile in its own
    ~/.claude* directory; wrapped only ever reads one unless you point it at more."""
    home = os.path.expanduser("~")
    active = os.environ.get("CLAUDE_CONFIG_DIR")
    active_real = os.path.realpath(active) if active else None
    bases = ([active] if active else []) + sorted(glob.glob(os.path.join(home, ".claude*")))
    profiles, seen = [], set()
    for base in bases:
        if not base or not os.path.isdir(base):
            continue
        pdir = os.path.join(base, "projects")
        if not os.path.isdir(pdir):
            continue
        real = os.path.realpath(pdir)
        if real in seen:
            continue
        seen.add(real)
        files = len(glob.glob(os.path.join(pdir, "*.jsonl")))
        files += len(glob.glob(os.path.join(pdir, "**", "*.jsonl"), recursive=True))
        profiles.append({"name": os.path.basename(base.rstrip("/")), "dir": pdir,
                         "files": files, "active": os.path.realpath(base) == active_real})
    profiles.sort(key=lambda p: (not p["active"], -p["files"]))
    return profiles


def _tool_histogram(runs):
    hist = {}
    for r in runs:
        for name, count in r["tools"].items():
            hist[name] = hist.get(name, 0) + count
    return dict(sorted(hist.items(), key=lambda kv: (-kv[1], kv[0])))


def _by_day(runs):
    days = {}
    for r in runs:
        day = (r["started"] or "")[:10] or "unknown"
        d = days.setdefault(day, {"tokens": 0, "sessions": 0})
        d["tokens"] += r["tokens"]
        d["sessions"] += 1
    return days


def _by_project(runs):
    projects = {}
    for r in runs:
        name = r["project"] or "unknown"
        projects[name] = projects.get(name, 0) + r["tokens"]
    return projects


def wrapped_stats(runs, window="all time"):
    """The whole card in one structured dict: the headline stats, an honest token
    breakdown (cache reads are most of it), a daily activity series for the viz,
    and the ranked data-driven insights. `top_project` never reaches the card or
    the terminal output; it lives in the local --json only."""
    hist = _tool_histogram(runs)
    top_tool = next(iter(hist.items()), None)
    days = _by_day(runs)
    busiest = max(days.items(), key=lambda kv: (kv[1]["tokens"], kv[0])) if days else None
    sessions = [r for r in runs if not r.get("is_subagent")]
    longest = max(sessions or runs, key=lambda r: r["span_ms"]) if runs else None
    projects = _by_project(runs)
    top_project = max(projects.items(), key=lambda kv: (kv[1], kv[0])) if projects else None
    inp = sum(r["input_tokens"] for r in runs)
    out = sum(r["output_tokens"] for r in runs)
    cache_r = sum(r["cache_read"] for r in runs)
    cache_c = sum(r["cache_creation"] for r in runs)
    total = inp + out + cache_r + cache_c
    stats = {
        "window": window,
        "sessions": len({r["run_id"] for r in sessions}),
        # tokens processed = everything the model handled (cache is ~90%+ of it).
        # the breakdown keeps it honest: "from cache" (re-read context) vs "new".
        "total_tokens": total,
        "tokens_from_cache": cache_r,
        "tokens_new": inp + out + cache_c,
        "input_tokens": inp, "output_tokens": out,
        "cache_read": cache_r, "cache_creation": cache_c,
        # cost is an api-EQUIVALENT: what these tokens would bill at api rates.
        # on a flat subscription it is not what you paid.
        "cost_usd": round(sum(r["cost_usd"] for r in runs), 6),
        "tool_calls": sum(r["tool_calls"] for r in runs),
        "distinct_tools": len(hist),
        "top_tool": {"name": top_tool[0], "count": top_tool[1]} if top_tool else None,
        "absolutely_right": sum(r["absolutely_right"] for r in runs),
        "longest_session": {"run_id": longest["run_id"], "span_ms": longest["span_ms"],
                            "human": fmt_duration(longest["span_ms"])} if longest else None,
        "busiest_day": {"date": busiest[0], "tokens": busiest[1]["tokens"],
                        "label": fmt_day(busiest[0])} if busiest else None,
        "top_project": {"name": top_project[0], "tokens": top_project[1]} if top_project else None,
        "activity": [{"date": d, "tokens": v["tokens"]} for d, v in sorted(days.items())],
    }
    stats["insights"] = insights(stats)
    return stats


# ---- the insight engine (data-driven, deterministic, offline) ---------------
# each rule fires on a condition and renders a dry one-liner; the highest-scoring
# firing rule is the card's punchline. this is what makes every card different -
# your data picks your roast. no model, no network.

def _cache_share(s):
    return s["tokens_from_cache"] / s["total_tokens"] if s["total_tokens"] else 0.0


def _insight_rules():
    """(id, fires(s)->bool, score(s)->float, line(s)->str). scores decide the pick;
    a few scale with how extreme the number is, so the sharpest stat wins. lines
    stay short (one punchline per card) so the insight band never truncates."""
    return [
        ("absolutely-right-zero", lambda s: s["absolutely_right"] == 0, lambda s: 6.0,
         lambda s: "claude never once said \"you're absolutely right\". respect, or fear."),
        ("absolutely-right", lambda s: s["absolutely_right"] > 0,
         lambda s: 5.0 + min(s["absolutely_right"], 20) * 0.1,
         lambda s: "claude said \"you're absolutely right\" %s times. it meant it maybe %s."
                   % (format(s["absolutely_right"], ","), format(s["absolutely_right"] // 2, ","))),
        ("cache-heavy", lambda s: _cache_share(s) >= 0.85,
         lambda s: 5.5 + (_cache_share(s) - 0.85) * 8,
         lambda s: "%d%% of your tokens were cache reads, not fresh work." % round(100 * _cache_share(s))),
        ("marathon", lambda s: s["longest_session"] and s["longest_session"]["span_ms"] >= 4 * 3600000,
         lambda s: 5.2,
         lambda s: "your longest session ran %s. that is a hostage situation." % s["longest_session"]["human"]),
        ("big-spend", lambda s: s["cost_usd"] >= 50,
         lambda s: 4.5 + min(s["cost_usd"] / 100, 4),
         lambda s: "$%s at api-equivalent rates. your plan is either a bargain or a problem." % format(round(s["cost_usd"]), ",")),
        ("bash-brain", lambda s: s["top_tool"] and s["top_tool"]["name"] == "Bash"
         and s["tool_calls"] and s["top_tool"]["count"] / s["tool_calls"] >= 0.4,
         lambda s: 4.2,
         lambda s: "you reached for bash %s times. every problem is a shell command now." % format(s["top_tool"]["count"], ",")),
        ("many-sessions", lambda s: s["sessions"] >= 40,
         lambda s: 4.0,
         lambda s: "%s sessions. claude is your most-contacted coworker." % format(s["sessions"], ",")),
        ("tool-variety", lambda s: s["distinct_tools"] >= 8,
         lambda s: 3.5,
         lambda s: "%d different tools this year. renaissance developer, or just indecisive." % s["distinct_tools"]),
        ("busiest", lambda s: s["busiest_day"],
         lambda s: 3.0,
         lambda s: "busiest day: %s. the git log knows what happened." % s["busiest_day"]["label"]),
        ("fallback", lambda s: True, lambda s: 1.0,
         lambda s: "%s tokens, %s sessions, one you. that was the year." % (fmt_tokens(s["total_tokens"]), format(s["sessions"], ","))),
    ]


def insights(s):
    """Every firing rule, ranked by score (highest first). The card takes the top
    line as its punchline and the next as a secondary; ties break by rule order."""
    fired = []
    for i, (rid, fires, score, line) in enumerate(_insight_rules()):
        try:
            if fires(s):
                fired.append({"id": rid, "score": round(score(s), 3), "line": line(s), "_o": i})
        except Exception:
            continue
    fired.sort(key=lambda x: (-x["score"], x["_o"]))
    for f in fired:
        del f["_o"]
    return fired


# ---- formatting -------------------------------------------------------------

def fmt_tokens(n):
    """Compact token count: 128400000 -> 128.4M, 14200 -> 14.2K."""
    n = int(n)
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= size:
            return "%.1f%s" % (n / size, unit)
    return str(n)


def fmt_dollars(v):
    """$1,284.57 - two decimals, thousands separators."""
    return "$%s" % format(round(float(v), 2), ",.2f")


def fmt_duration(ms):
    """Wall-clock span: 1200000 -> 20m, 11520000 -> 3h 12m, 1019880000 -> 11d 19h.
    A "session" is one transcript file's first-to-last timestamp, so a resumed
    conversation can legitimately span days."""
    total_m = int(ms) // 60000
    d, rem = divmod(total_m, 1440)
    h, m = divmod(rem, 60)
    if d:
        return "%dd %dh" % (d, h)
    return "%dh %dm" % (h, m) if h else "%dm" % m


def fmt_day(date_str):
    """2026-06-30 -> jun 30."""
    d = _ts(date_str + "T00:00:00")
    return "%s %d" % (_MONTHS[d.month - 1], d.day) if d else date_str


def _pretty_tool(name):
    """Shorten a tool name for display: mcp__server__tool -> tool. Real transcripts
    carry mcp tool names ~40 chars long that would overflow a fixed card. Counting
    always uses the full name; this is display only."""
    if name.startswith("mcp__"):
        return name.split("__")[-1] or name
    return name


def _clip(text, n):
    return text if len(text) <= n else text[:n - 1] + "…"


# ---- the text wrapped (m1 end-to-end deliverable) ---------------------------

def _wrap(text, w):
    """Greedy word-wrap into lines of at most w chars."""
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > w:
            lines.append(cur)
            cur = word
        else:
            cur = (cur + " " + word).strip()
    if cur:
        lines.append(cur)
    return lines


def _color_on():
    """Colour + rich ascii when writing to a real terminal; plain when piped."""
    if os.environ.get("CLAUDE_WRAPPED_FORCE_COLOR"):
        return True
    return (sys.stdout.isatty() and not os.environ.get("NO_COLOR")
            and os.environ.get("TERM") != "dumb")


_RESET, _BOLD = "\x1b[0m", "\x1b[1m"


def _rgb(r, g, b):
    return "\x1b[38;2;%d;%d;%dm" % (r, g, b)


_AMBER, _ABRIGHT = _rgb(255, 180, 84), _rgb(255, 206, 133)
_INK, _MUTED, _FAINT = _rgb(238, 224, 202), _rgb(150, 133, 108), _rgb(112, 98, 80)
_CACHE = _rgb(140, 102, 55)


def _c(text, *codes):
    return ("".join(codes) + text + _RESET) if _color_on() else text


# a 5-row block font for the hero number (the centrepiece of the terminal view).
_BIGFONT = {
    "0": [" ██ ", "█  █", "█  █", "█  █", " ██ "], "1": ["  █ ", " ██ ", "  █ ", "  █ ", "████"],
    "2": ["███ ", "   █", " ██ ", "█   ", "████"], "3": ["███ ", "   █", " ██ ", "   █", "███ "],
    "4": ["█  █", "█  █", "████", "   █", "   █"], "5": ["████", "█   ", "███ ", "   █", "███ "],
    "6": [" ██ ", "█   ", "███ ", "█  █", " ██ "], "7": ["████", "   █", "  █ ", " █  ", " █  "],
    "8": [" ██ ", "█  █", " ██ ", "█  █", " ██ "], "9": [" ██ ", "█  █", " ███", "   █", " ██ "],
    ".": ["  ", "  ", "  ", "  ", "██"], "M": ["█   █", "██ ██", "█ █ █", "█   █", "█   █"],
    "K": ["█  █", "█ █ ", "██  ", "█ █ ", "█  █"], "B": ["███ ", "█  █", "███ ", "█  █", "███ "],
}


def _bignum(text):
    rows = ["", "", "", "", ""]
    for ch in text:
        g = _BIGFONT.get(ch)
        if g:
            for i in range(5):
                rows[i] += g[i] + "  "
    return rows


_SPARK = "▁▂▃▄▅▆▇█"


def _calendar_series(activity, limit):
    """The activity series made honest for a chart: zero-fill the active-days-only
    series to the full calendar span (a day off is a gap, not a missing bar), then
    when the span is longer than `limit` days, bucket consecutive days so the whole
    window still fits. Returns (buckets, days_per_bucket); each bucket is
    {"date": <start date iso>, "tokens": <sum>}. days_per_bucket == 1 means daily."""
    days = {d["date"]: d["tokens"] for d in activity
            if re.match(r"^\d{4}-\d{2}-\d{2}$", d["date"] or "")}
    if not days:
        return [], 1
    d0, d1 = date.fromisoformat(min(days)), date.fromisoformat(max(days))
    span = (d1 - d0).days + 1
    k = max(1, -(-span // limit))  # ceil(span / limit)
    buckets, cur = [], d0
    while cur <= d1:
        tokens = sum(days.get((cur + timedelta(days=i)).isoformat(), 0) for i in range(k))
        buckets.append({"date": cur.isoformat(), "tokens": tokens})
        cur += timedelta(days=k)
    return buckets, k


def _sparkline(activity, limit=56):
    """A coloured sparkline over the FULL window (peak bright), + its date span.
    Zero-filled and bucketed via _calendar_series so a long window compresses
    instead of silently dropping its start."""
    series, k = _calendar_series(activity, limit)
    if not series:
        return "", ""
    peak = max(d["tokens"] for d in series) or 1
    peak_i = max(range(len(series)), key=lambda i: series[i]["tokens"])
    wide = 2 if len(series) <= 28 else 1  # chunky bars while they fit
    bar = ""
    for i, d in enumerate(series):
        ch = _SPARK[min(7, int(round(7 * d["tokens"] / peak)))] * wide
        bar += _c(ch, _ABRIGHT, _BOLD) if i == peak_i else _c(ch, _CACHE)
    dates = sorted(d["date"] for d in activity if re.match(r"^\d{4}-\d{2}-\d{2}$", d["date"] or ""))
    span = (fmt_day(dates[0]) + "  to  " + fmt_day(dates[-1])
            if len(dates) > 1 else fmt_day(dates[0]))
    if k > 1:
        span += "  (%dd per bar)" % k
    return bar, span


def print_wrapped(s):
    """The wrapped in the terminal: a block-font hero number, a colour-coded stat
    grid, a sparkline of daily activity, and the punchline. Colour + rich ascii on
    a tty; a clean plain version when piped (NO_COLOR / not a terminal)."""
    P, W = "  ", 60
    print()
    print(P + _c("●", _FAINT) + _c(" ●", _MUTED) + _c(" ●", _AMBER) + "   "
          + _c("claude code", _INK, _BOLD) + _c(" · ", _FAINT) + _c("wrapped", _AMBER, _BOLD)
          + "     " + _c("[ %s ]" % s["window"], _FAINT))
    print(P + _c("─" * W, _FAINT))
    print()
    print(P + _c("TOKENS PROCESSED", _FAINT))
    num, unit = _split_compact(fmt_tokens(s["total_tokens"]))
    for row in _bignum(num + unit):
        print(P + _c(row, _AMBER, _BOLD))
    total = s["total_tokens"] or 1
    fill = max(0, min(30, int(round(30 * s["tokens_from_cache"] / total))))
    print(P + _c("cache included  ", _FAINT) + "["
          + _c("█" * fill, _CACHE) + _c("█" * (30 - fill), _ABRIGHT) + "]  "
          + _c(fmt_tokens(s["tokens_from_cache"]), _INK) + _c(" cache", _FAINT)
          + _c("  ·  ", _FAINT) + _c(fmt_tokens(s["tokens_new"]), _INK) + _c(" new", _FAINT))
    print()
    tool = "-"
    if s["top_tool"]:
        tool = "%s · %s" % (_clip(_pretty_tool(s["top_tool"]["name"]), 12), format(s["top_tool"]["count"], ","))
    cells = [
        (fmt_dollars(s["cost_usd"]), "cost (api-equiv)"),
        (format(s["sessions"], ","), "sessions"),
        (format(s["tool_calls"], ","), "tool calls"),
        (tool, "top tool"),
        (s["longest_session"]["human"] if s["longest_session"] else "-", "longest session"),
        (s["busiest_day"]["label"] if s["busiest_day"] else "-", "busiest day"),
    ]
    col = 20
    for triad in (cells[0:3], cells[3:6]):
        print(P + "".join(_c(v.ljust(col), _ABRIGHT, _BOLD) for v, _ in triad))
        print(P + "".join(_c(l.ljust(col), _FAINT) for _, l in triad))
        print()
    bar, span = _sparkline(s["activity"])
    if bar and s["busiest_day"]:
        note = _c("peak · %s · %s" % (s["busiest_day"]["label"], fmt_tokens(s["busiest_day"]["tokens"])), _AMBER)
        print(P + _c("DAILY ACTIVITY", _FAINT) + "    " + note)
        print(P + bar)
        print(P + _c(span, _FAINT))
        print()
    if s["insights"]:
        for i, ln in enumerate(_wrap(s["insights"][0]["line"], W - 2)):
            print(P + (_c("› ", _AMBER, _BOLD) if i == 0 else "  ") + _c(ln, _INK))
        print()
    print(P + _c("run your own  ", _FAINT) + _c(REPO, _AMBER, _BOLD))
    print()


# ---- the html share-card (v3, three formats, pinned CTA) --------------------

# a self-contained dark CRT-terminal card in three formats (square/portrait/wide),
# no network / no external assets / no javascript. the chart is pure css/html bars.
# the card is a flex column: a grid of stats that can shrink, and a CTA footer bar
# that is pinned to the bottom and can never be squeezed off (the v2 bug).
CARD_STYLE = """
:root{
  --bg-0:#0d0a07;--bg-1:#080605;--surface:#141009;--surface-2:#1b150c;
  --line:#2c2115;--line-2:#3a2c1a;
  --amber:#ffb454;--amber-bright:#ffce85;--amber-dim:#8a5e2a;
  --ink:#f3e6d1;--ink-2:#b7a488;--ink-3:#7c6c57;--glow:rgba(255,180,84,.5);
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--bg-1);overflow:hidden}
body{display:flex;align-items:center;justify-content:center;min-height:100vh;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;-webkit-font-smoothing:antialiased}
.card{position:relative;overflow:hidden;color:var(--ink);
  background:radial-gradient(120% 78% at 50% -12%,rgba(255,180,84,.11),transparent 58%),
    linear-gradient(158deg,var(--bg-0) 0%,var(--bg-1) 100%);
  display:flex;flex-direction:column;isolation:isolate}
.card::before{content:"";position:absolute;inset:0;z-index:5;pointer-events:none;
  background:repeating-linear-gradient(0deg,rgba(0,0,0,.15) 0 1px,transparent 1px 3px);
  mix-blend-mode:multiply;opacity:.55}
.card::after{content:"";position:absolute;inset:0;z-index:6;pointer-events:none;
  background:radial-gradient(135% 105% at 50% 42%,transparent 58%,rgba(0,0,0,.5) 100%)}
.card > *{position:relative;z-index:2;min-width:0}
.grid{flex:1 1 auto;min-height:0;overflow:hidden;display:grid}
.tabnum{font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1;letter-spacing:-.01em}
.head{grid-area:head;display:flex;align-items:center;gap:20px;border-bottom:1px solid var(--line)}
.dots{display:flex;gap:10px;flex:none}
.dots i{width:14px;height:14px;border-radius:50%;display:block}
.dots i:nth-child(1){background:#6b4a24}
.dots i:nth-child(2){background:#a9712f}
.dots i:nth-child(3){background:var(--amber)}
.head .title{color:var(--ink-2);letter-spacing:.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.head .title b{color:var(--ink);font-weight:600}
.head .win{margin-left:auto;flex:none;color:var(--amber);border:1px solid var(--line-2);
  border-radius:999px;background:rgba(255,180,84,.06);letter-spacing:.06em}
.hero{grid-area:hero;display:flex;flex-direction:column;justify-content:center}
.hero .kicker{color:var(--ink-3);letter-spacing:.09em;text-transform:uppercase}
.hero .num{font-weight:700;line-height:.84;color:var(--amber);text-shadow:0 0 34px var(--glow);white-space:nowrap}
.hero .num .unit{color:var(--amber-bright);font-weight:600}
.hero .lead{color:var(--ink-2)}
.hero .lead b{color:var(--ink);font-weight:600}
.cache{display:flex;flex-direction:column}
.cache .track{width:100%;display:flex;border-radius:6px;overflow:hidden;background:var(--bg-1);border:1px solid var(--line)}
.cache .seg-cache{background:linear-gradient(180deg,#7a5326,#573b1c);height:100%}
.cache .seg-new{background:linear-gradient(180deg,var(--amber-bright),var(--amber));height:100%;box-shadow:0 0 16px var(--glow)}
.cache .legend{display:flex;gap:26px;color:var(--ink-2);flex-wrap:wrap}
.cache .legend b{color:var(--ink);font-weight:600}
.cache .legend .mk{display:inline-block;width:11px;height:11px;border-radius:3px;vertical-align:middle;margin-right:9px}
.cache .legend .mk.c{background:#6b4a24}
.cache .legend .mk.n{background:var(--amber);box-shadow:0 0 9px var(--glow)}
.activity{grid-area:activity;display:flex;flex-direction:column;justify-content:center}
.activity .lbl{color:var(--ink-3);letter-spacing:.09em;text-transform:uppercase;display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.activity .lbl .peaknote{color:var(--amber);text-transform:none;letter-spacing:0;white-space:nowrap;flex:none}
.plot{position:relative;display:flex;align-items:flex-end;gap:2.4%;border-bottom:1px solid var(--line-2)}
.plot .grid-line{position:absolute;left:0;right:0;border-top:1px dashed var(--line);opacity:.55}
.bar{position:relative;flex:1 1 0;min-width:0;display:flex;align-items:flex-end;justify-content:center;height:100%}
.bar .fill{width:100%;border-radius:5px 5px 0 0;background:linear-gradient(180deg,#8a6636,#4f3a20)}
.bar.peak .fill{background:linear-gradient(180deg,var(--amber-bright),var(--amber) 58%,#b9812f);box-shadow:0 0 26px var(--glow),inset 0 1px 0 rgba(255,255,255,.35)}
.bar .cap{position:absolute;top:0;transform:translateY(-135%);color:var(--amber);white-space:nowrap;font-weight:600}
.bar.peak:first-child .cap{left:0}
.bar.peak:last-child .cap{right:0}
.axis{display:flex;gap:2.4%;color:var(--ink-3)}
.axis span{flex:1 1 0;min-width:0;display:flex;justify-content:center;white-space:nowrap}
.axis span.lft{justify-content:flex-start}
.axis span.rgt{justify-content:flex-end}
.axis span.on{color:var(--amber)}
.stats{grid-area:stats;display:grid;gap:14px}
.tile{background:linear-gradient(180deg,var(--surface-2),var(--surface));border:1px solid var(--line);
  border-radius:12px;padding:16px 22px;display:flex;flex-direction:column;gap:7px;justify-content:center}
.tile .v{color:var(--ink);font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile .v .mut{color:var(--ink-3);font-weight:400}
.tile .k{color:var(--ink-3);text-transform:uppercase;letter-spacing:.07em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile.hot{border-color:var(--line-2);background:linear-gradient(180deg,#241a0e,#1a130a)}
.tile.hot .v{color:var(--amber)}
.tile.agents{grid-column:1/-1;flex-direction:row;align-items:center;justify-content:flex-start;gap:20px;
  border-color:var(--line-2);background:linear-gradient(180deg,#241a0e,#1a130a)}
.tile.agents .v{flex:none;color:var(--amber)}
.tile.agents .k{flex:none;color:var(--ink)}
.tile.agents .n{margin-left:auto;min-width:0;color:var(--ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile.agents.say .v{color:var(--ink);font-weight:500}
.head .title .sample{color:var(--ink-3);font-size:.72em}
.insight{grid-area:insight;background:linear-gradient(180deg,rgba(255,180,84,.055),rgba(255,180,84,.02));
  border:1px solid var(--line-2);border-left:3px solid var(--amber);border-radius:12px;
  display:flex;flex-direction:column;justify-content:center;gap:11px;overflow:hidden}
.insight .p{color:var(--ink);line-height:1.32;letter-spacing:.004em}
.insight .p .em{color:var(--amber);text-shadow:0 0 18px var(--glow)}
.insight .p::before{content:"> ";color:var(--amber);font-weight:700}
.insight .s{color:var(--ink-2);line-height:1.3}
.insight .s .em{color:var(--amber)}
.cta{flex:none;display:flex;align-items:center;gap:14px;color:var(--ink);
  background:linear-gradient(180deg,var(--surface-2),var(--surface));
  border:1px solid var(--line-2);border-radius:12px}
.cta .p{color:var(--amber);flex:none;font-weight:700}
.cta .u{color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cta .u b{color:var(--amber-bright);font-weight:600}
.cta .caret{margin-left:auto;flex:none;color:var(--amber)}
.card.square{width:1080px;height:1080px;padding:44px 54px 40px;gap:18px}
.card.square .grid{gap:20px 46px;grid-template-columns:minmax(0,1.04fr) minmax(0,0.96fr);
  grid-template-rows:auto auto auto auto;align-content:space-between;
  grid-template-areas:"head head" "hero activity" "stats stats" "insight insight"}
.card.square .head{padding-bottom:20px;font-size:23px}
.card.square .head .win{padding:7px 18px;font-size:19px}
.card.square .hero{gap:16px;justify-content:center}
.card.square .hero .kicker{font-size:20px}
.card.square .hero .num{font-size:132px}
.card.square .hero .num .unit{font-size:74px}
.card.square .hero .lead{font-size:21px;margin-top:-4px}
.card.square .cache{gap:12px;margin-top:6px}
.card.square .cache .track{height:20px}
.card.square .cache .legend{font-size:19px}
.card.square .activity .lbl{font-size:18px;margin-bottom:30px}
.card.square .plot{height:184px;gap:2.6%}
.card.square .bar .cap{font-size:20px}
.card.square .axis{font-size:16px;margin-top:12px}
.card.square .stats{grid-template-columns:repeat(3,1fr);grid-template-rows:1fr 1fr}
.card.square .tile .v{font-size:32px}
.card.square .tile.wide-v .v{font-size:28px}
.card.square .tile .k{font-size:15px}
.card.square .tile.agents{padding:12px 22px}
.card.square .tile.agents .n{font-size:15px}
.card.square .tile.agents.say .v{font-size:21px}
.card.square.packed .grid{gap:12px 46px}
.card.square.packed .stats{gap:10px}
.card.square.packed .plot{height:128px}
.card.square.packed .hero .num{font-size:112px}
.card.square.packed .hero .num .unit{font-size:64px}
.card.square.packed .tile{padding:10px 22px;gap:4px}
.card.square.packed .tile .v{font-size:28px}
.card.square.packed .tile.wide-v .v{font-size:25px}
.card.square.packed .tile.agents{padding:7px 22px;gap:20px}
.card.square.packed .tile.agents.say .v{font-size:21px}
.card.square.packed .insight{padding:14px 26px}
.card.square.packed .insight .p{font-size:24px}
.card.square.packed .cta{padding:13px 26px;font-size:22px}
.card.square .insight{padding:22px 30px}
.card.square .insight .p{font-size:27px}
.card.square .insight .s{font-size:19px}
.card.square .cta{padding:18px 26px;font-size:24px}
.card.wide{width:1600px;height:900px;padding:40px 54px 38px;gap:16px}
.card.wide .grid{gap:22px 54px;grid-template-columns:minmax(0,1fr) minmax(0,1fr);
  grid-template-rows:auto 1fr 1fr;
  grid-template-areas:"head head" "hero activity" "insight stats"}
.card.wide .head{padding-bottom:20px;font-size:22px}
.card.wide .head .win{padding:6px 18px;font-size:19px}
.card.wide .hero{gap:18px}
.card.wide .hero .kicker{font-size:20px}
.card.wide .hero .num{font-size:148px}
.card.wide .hero .num .unit{font-size:84px}
.card.wide .hero .lead{font-size:22px;margin-top:-4px}
.card.wide .cache{gap:12px;margin-top:14px}
.card.wide .cache .track{height:22px}
.card.wide .cache .legend{font-size:20px}
.card.wide .activity{justify-content:flex-start}
.card.wide .activity .lbl{font-size:18px;margin-bottom:24px}
.card.wide .plot{height:196px}
.card.wide .bar .cap{font-size:18px}
.card.wide .axis{font-size:15px;margin-top:11px}
.card.wide .stats{grid-template-columns:repeat(3,1fr);grid-template-rows:1fr 1fr;gap:14px}
.card.wide .tile{padding:16px 22px;gap:6px}
.card.wide .tile .v{font-size:30px}
.card.wide .tile.wide-v .v{font-size:26px}
.card.wide .tile .k{font-size:15px}
.card.wide .tile.agents{padding:8px 22px}
.card.wide .tile.agents .n{font-size:14px}
.card.wide .stats{grid-template-rows:1fr 1fr auto auto auto}
.card.wide .tile{padding:10px 22px}
.card.wide .tile.agents .v{font-size:22px}
.card.wide .tile.agents.say .v{font-size:15px}
.card.wide .tile.agents.say .n{display:none}
.card.wide .insight{padding:24px 30px}
.card.wide .insight .p{font-size:25px}
.card.wide .insight .s{font-size:18px}
.card.wide .cta{padding:16px 26px;font-size:22px}
"""

_CARD_BODY = """<div class="card {fmt}{packed}">
  <div class="grid">
    <div class="head">
      <span class="dots"><i></i><i></i><i></i></span>
      <span class="title"><b>claude code</b> &middot; wrapped{sample_tag}</span>
      <span class="win tabnum">{window}</span>
    </div>
    <div class="hero">
      <div class="kicker">tokens processed</div>
      <div class="num tabnum">{hero_num}<span class="unit">{hero_unit}</span></div>
      <div class="lead">cache included</div>
      <div class="cache">
        <div class="track">
          <div class="seg-cache" style="width:{cache_pct}%"></div>
          <div class="seg-new" style="width:{new_pct}%"></div>
        </div>
        <div class="legend tabnum">
          <span><span class="mk c"></span><b>{from_cache}</b> from cache</span>
          <span><span class="mk n"></span><b>{new_tokens}</b> new</span>
        </div>
      </div>
    </div>
    <div class="activity">
      <div class="lbl"><span>{act_label}</span><span class="peaknote tabnum">{peaknote}</span></div>
      <div class="plot">
        <div class="grid-line" style="bottom:50%"></div>
        <div class="grid-line" style="bottom:75%"></div>
        {bars}
      </div>
      <div class="axis tabnum">{axis}</div>
    </div>
    <div class="stats">
      <div class="tile hot"><div class="v tabnum">{cost}</div><div class="k">api-equivalent cost</div></div>
      <div class="tile"><div class="v tabnum">{sessions}</div><div class="k">sessions</div></div>
      <div class="tile"><div class="v tabnum">{tool_calls}</div><div class="k">tool calls</div></div>
      <div class="tile wide-v"><div class="v tabnum">{top_tool}</div><div class="k">top tool</div></div>
      <div class="tile"><div class="v tabnum">{longest}</div><div class="k">longest session</div></div>
      <div class="tile"><div class="v tabnum">{busiest}</div><div class="k">busiest day</div></div>
      {peak_block}
    </div>
    <div class="insight">
      <div class="p">{primary}</div>
      {secondary_block}
    </div>
  </div>
  <div class="cta">
    <span class="p">$</span>
    <span class="u">run your own: <b>{repo}</b></span>
    <span class="caret">&#9613;</span>
  </div>
</div>"""


def _split_compact(text):
    """'128.4M' -> ('128.4','M'); '342' -> ('342','')."""
    if text and text[-1] in "KMB":
        return text[:-1], text[-1]
    return text, ""


def _emphasize(text):
    """Escape an insight line, then amber-highlight quoted phrases and percentages.
    The inserted markup is fixed (not from data), so this stays injection-safe."""
    esc = escape(text)
    esc = re.sub(r"(&quot;.*?&quot;)", r'<span class="em">\1</span>', esc)
    esc = re.sub(r"(?<![\w>])(\d[\d,]*%)", r'<span class="em">\1</span>', esc)
    return esc


def _axis_label_indices(n, peak_i):
    """Which bars get a date label: the peak, the ends, and a few evenly spaced
    fillers, each kept >=2 bars from an already-chosen one (a label is wider than
    a bar, so labelling every bar overflows the card, and adjacent labels overlap).
    The peak wins over an end label when they collide - it is the amber one."""
    chosen = {peak_i}
    for end in (0, n - 1):
        if all(abs(end - c) >= 2 for c in chosen):
            chosen.add(end)
    stride = max(2, -(-n // 4))
    for i in range(0, n, stride):
        if all(abs(i - c) >= 2 for c in chosen):
            chosen.add(i)
    return chosen


def _activity_html(activity, limit=14):
    """The activity bar chart (bars + axis) over the FULL window: zero-filled
    calendar days, bucketed to at most `limit` bars (so an all-time card shows
    all time, not its last two weeks). Returns (bars, axis, peaknote, label)."""
    series, k = _calendar_series(activity, limit)
    if not series:
        return "", "", "", "daily activity"
    peak_tokens = max(d["tokens"] for d in series) or 1
    peak_i = max(range(len(series)), key=lambda i: series[i]["tokens"])
    labeled = _axis_label_indices(len(series), peak_i)
    last_day = max(d["date"] for d in activity
                   if re.match(r"^\d{4}-\d{2}-\d{2}$", d["date"] or ""))
    bars = axis = ""
    for i, d in enumerate(series):
        pct = max(4, round(100 * d["tokens"] / peak_tokens))
        is_peak = i == peak_i
        cap = '<span class="cap tabnum">%s</span>' % fmt_tokens(d["tokens"]) if is_peak else ""
        bars += ('<div class="bar%s">%s<div class="fill" style="height:%d%%"></div></div>'
                 % (" peak" if is_peak else "", cap, pct))
        cls = (["on"] if is_peak else []) + (["lft"] if i == 0 else []) \
            + (["rgt"] if i == len(series) - 1 else [])
        # the end tick shows the true last day of the window, not the last
        # bucket's start, so the axis reads as the real date range.
        text = fmt_day(last_day) if i == len(series) - 1 else fmt_day(d["date"])
        axis += ('<span class="%s">%s</span>'
                 % (" ".join(cls), escape(text) if i in labeled else ""))
    if k == 1:
        label = "daily activity"
        peaknote = "peak &middot; %s &middot; %s" % (escape(fmt_day(series[peak_i]["date"])),
                                                     fmt_tokens(series[peak_i]["tokens"]))
    else:  # bucketed: say so where there is room (the label row wraps if too long)
        label = "activity"
        peaknote = "%dd bars &middot; peak %s &middot; %s" % (k, escape(fmt_day(series[peak_i]["date"])),
                                                              fmt_tokens(series[peak_i]["tokens"]))
    return bars, axis, peaknote, label


def html_card(s, fmt="square"):
    """Render wrapped_stats() into one self-contained dark HTML share-card in the
    chosen format (square/wide). Every dynamic value is escaped, and no project
    name ever reaches the card. The CTA is a pinned footer bar (flex:none)."""
    if fmt not in FORMATS:
        fmt = "square"
    total = s["total_tokens"] or 1
    hero_num, hero_unit = _split_compact(fmt_tokens(s["total_tokens"]))
    cache_pct = 100.0 * s["tokens_from_cache"] / total
    new_pct = max(0.0, 100.0 - cache_pct)
    if s["top_tool"]:
        top_tool = ('%s <span class="mut">&middot; %s</span>'
                    % (escape(_clip(_pretty_tool(s["top_tool"]["name"]), 16)),
                       format(s["top_tool"]["count"], ",")))
    else:
        top_tool = "none"
    longest = escape(s["longest_session"]["human"]) if s["longest_session"] else "0m"
    busiest = escape(s["busiest_day"]["label"]) if s["busiest_day"] else "-"
    bars, axis, peaknote, act_label = _activity_html(s["activity"])
    # one punchline on the card (the top-ranked insight), kept short by design so
    # the band never truncates; the clamp is a safety net only. the rest of the
    # ranked insights are still in `wrap --json` for anyone who wants them.
    ins = s["insights"]
    primary = _emphasize(_clip(ins[0]["line"], 96)) if ins else ""
    secondary_block = ""
    # the herdr mod adds s["peak"]. the note says what the number is, so the card
    # can't be read as more than it is.
    peak_block = ""
    if s.get("peak"):
        peak_block = ('<div class="tile agents"><div class="v tabnum">%s</div>'
                      '<div class="k">most agents at once</div>'
                      '<div class="n">sessions open together, idle ones count</div></div>'
                      % format(s["peak"]["count"], ","))
    if s.get("bottleneck"):
        peak_block += ('<div class="tile agents say"><div class="v tabnum">your agents worked %s. they waited %s for you.</div>'
                       '<div class="n">estimated from timestamps</div></div>'
                       % (escape(s["bottleneck"]["worked"]), escape(s["bottleneck"]["waited"])))
    # the swear jar is opt-in: the mod only puts "jar" in the stats when the person says so.
    if s.get("jar"):
        peak_block += ('<div class="tile agents"><div class="v tabnum">%s</div><div class="k">swear jar</div>'
                       '<div class="n">%s %s, top word %s</div></div>'
                       % (escape(s["jar"]["fine"]), format(s["jar"]["count"], ","),
                          "swear" if s["jar"]["count"] == 1 else "swears", escape(s["jar"]["top"] or "none")))
    sample_tag = ' <span class="sample">&middot; sample data</span>' if s.get("sample") else ""
    body = _CARD_BODY.format(
        fmt=fmt, window=escape(s["window"]),
        hero_num=escape(hero_num), hero_unit=escape(hero_unit),
        cache_pct="%.1f" % cache_pct, new_pct="%.1f" % new_pct,
        from_cache=fmt_tokens(s["tokens_from_cache"]), new_tokens=fmt_tokens(s["tokens_new"]),
        peaknote=peaknote, bars=bars, axis=axis, act_label=act_label,
        cost=escape(fmt_dollars(s["cost_usd"])), sessions=format(s["sessions"], ","),
        tool_calls=format(s["tool_calls"], ","), top_tool=top_tool,
        longest=longest, busiest=busiest, primary=primary, secondary_block=secondary_block,
        peak_block=peak_block, sample_tag=sample_tag, packed=" packed" if s.get("jar") else "",
        repo=escape(REPO))
    return ('<!doctype html>\n<html><head><meta charset="utf-8">'
            '<title>claude code wrapped</title>\n<style>%s</style></head>\n'
            '<body>%s</body></html>\n' % (CARD_STYLE, body))


# ---- rasterizing + sharing (m4) ---------------------------------------------

# aspect ratios. square is the default and the universal one (safe on X, LinkedIn,
# anywhere); wide suits X-landscape and blogs. (portrait was dropped: it cropped.)
FORMATS = {"square": (1080, 1080), "wide": (1600, 900)}

# where a headless browser might live, so `card` can emit a PNG directly instead
# of leaving the user to screenshot an HTML file by hand.
_BROWSER_CANDIDATES = [
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "microsoft-edge", "brave-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser():
    """A path to a chromium-family browser for headless rendering, or None.
    Honors $CLAUDE_WRAPPED_BROWSER / $CHROME_PATH first."""
    for env in ("CLAUDE_WRAPPED_BROWSER", "CHROME_PATH"):
        p = os.environ.get(env)
        if p and (os.path.exists(p) or shutil.which(p)):
            return p
    for cand in _BROWSER_CANDIDATES:
        found = shutil.which(cand) if os.path.basename(cand) == cand else (cand if os.path.exists(cand) else None)
        if found:
            return found
    return None


def _png_unfilter(ftype, row, prev, bpp):
    """Reverse one PNG scanline filter (types 0-4) in place."""
    if ftype == 0:
        return
    if ftype == 1:  # sub
        for i in range(bpp, len(row)):
            row[i] = (row[i] + row[i - bpp]) & 0xFF
    elif ftype == 2:  # up
        for i in range(len(row)):
            row[i] = (row[i] + prev[i]) & 0xFF
    elif ftype == 3:  # average
        for i in range(len(row)):
            left = row[i - bpp] if i >= bpp else 0
            row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
    elif ftype == 4:  # paeth
        for i in range(len(row)):
            a = row[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            row[i] = (row[i] + (a if (pa <= pb and pa <= pc) else b if pb <= pc else c)) & 0xFF
    else:
        raise ValueError("unknown png filter type %d" % ftype)


def _crop_png(path, want_w, want_h):
    """Crop a screenshot PNG to its top-left want_w x want_h box, in place,
    stdlib only. Handles what chromium emits (8-bit grey/RGB/RGBA,
    non-interlaced). Returns (ok, message)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return False, "not a png"
        pos, ihdr, idat = 8, None, []
        while pos + 8 <= len(data):
            ln = int.from_bytes(data[pos:pos + 4], "big")
            typ = data[pos + 4:pos + 8]
            chunk = data[pos + 8:pos + 8 + ln]
            pos += 12 + ln
            if typ == b"IHDR":
                ihdr = chunk
            elif typ == b"IDAT":
                idat.append(chunk)
            elif typ == b"IEND":
                break
        if not ihdr:
            return False, "no IHDR"
        w, hgt, bitd, ctype, _comp, _filt, inter = struct.unpack(">IIBBBBB", ihdr)
        if bitd != 8 or inter != 0 or ctype not in (0, 2, 6):
            return False, "unsupported png format (bitdepth %d, colour %d, interlace %d)" % (bitd, ctype, inter)
        if w < want_w or hgt < want_h:
            return False, "capture is %dx%d, smaller than the wanted %dx%d" % (w, hgt, want_w, want_h)
        if w == want_w and hgt == want_h:
            return True, "already exact"
        ch = {0: 1, 2: 3, 6: 4}[ctype]
        raw = zlib.decompress(b"".join(idat))
        stride = w * ch
        rows, prev, rpos = [], bytearray(stride), 0
        for _y in range(want_h):  # only the rows we keep need unfiltering
            ftype = raw[rpos]
            rpos += 1
            row = bytearray(raw[rpos:rpos + stride])
            rpos += stride
            _png_unfilter(ftype, row, prev, ch)
            rows.append(bytes(row[:want_w * ch]))
            prev = row
        body = zlib.compress(b"".join(b"\x00" + r for r in rows), 6)

        def _chunk(typ, payload):
            return (struct.pack(">I", len(payload)) + typ + payload
                    + struct.pack(">I", zlib.crc32(typ + payload) & 0xFFFFFFFF))

        ihdr2 = struct.pack(">IIBBBBB", want_w, want_h, 8, ctype, 0, 0, 0)
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr2)
                    + _chunk(b"IDAT", body) + _chunk(b"IEND", b""))
        return True, "cropped %dx%d -> %dx%d" % (w, hgt, want_w, want_h)
    except Exception as e:  # a crop failure must never eat the render
        return False, "%s: %s" % (type(e).__name__, e)


# chrome's NEW headless reserves ~90px of phantom window chrome, so the real
# viewport is SHORTER than --window-size and a window-sized capture cuts the
# bottom of the card off (measured: innerHeight 993 for a 1080 window - this
# was the missing-CTA bug). render with generous height headroom, top-anchored,
# then crop the png to the exact card box.
_CAPTURE_HEADROOM = 240
_CAPTURE_ANCHOR = ("<style>body{align-items:flex-start;justify-content:flex-start;"
                   "min-height:0}</style>")

# the mod runs this under exec's 30 s limit, so a browser that hangs has to give
# up first, and the caller still gets its html fallback.
_RENDER_SECONDS = 20


def _png_done(path):
    """True once the file is a whole PNG: it ends with the IEND chunk."""
    try:
        with open(path, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return False
            f.seek(-12, os.SEEK_END)
            return f.read(12) == b"\x00\x00\x00\x00IEND\xaeB`\x82"
    except OSError:
        return False


def _screenshot(cmd, out_abs):
    """Runs the browser until it exits or its screenshot is whole, and gives its exit code, or None past the limit.
    Chrome 145 on macOS writes the screenshot and then keeps running, so a whole PNG counts as done and the browser
    is stopped."""
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + _RENDER_SECONDS
        while True:
            code = proc.poll()
            if code is not None:
                return code
            if _png_done(out_abs):
                return 0
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def render_png(html, out_path, fmt="square"):
    """Rasterize the card HTML to a PNG via headless chrome at 2x, cropped to
    the exact card size. Returns (ok, message). Falls back with ok=False
    (caller then keeps the HTML)."""
    w, h = FORMATS.get(fmt, FORMATS["square"])
    browser = find_browser()
    if not browser:
        return False, "no chromium-family browser found (set $CLAUDE_WRAPPED_BROWSER)"
    tmp = tempfile.mkdtemp()
    html_path = os.path.join(tmp, "card.html")
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html.replace("</head>", _CAPTURE_ANCHOR + "</head>"))
        out_abs = os.path.abspath(out_path)
        # a throwaway profile, so a browser already open can't take the job over.
        cmd = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
               "--force-device-scale-factor=2", "--user-data-dir=" + os.path.join(tmp, "profile"),
               "--window-size=%d,%d" % (w, h + _CAPTURE_HEADROOM),
               "--screenshot=" + out_abs, "file://" + html_path]
        # an older card under the same name would read as this one being done.
        with contextlib.suppress(FileNotFoundError):
            os.remove(out_abs)
        code = _screenshot(cmd, out_abs)
        if code is None:
            return False, "the browser took more than %d seconds" % _RENDER_SECONDS
        if not _png_done(out_abs):
            return False, "the browser did not produce an image (exit %d)" % code
        ok, msg = _crop_png(out_abs, w * 2, h * 2)
        if not ok:
            return False, "rendered, but could not crop to the card box: %s" % msg
        return True, "%s (%dx%d @2x)" % (out_path, w, h)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_POST_LIMIT = 280


def share_text(s):
    """Ready-to-paste captions per platform, written for the moment of posting
    the card image: lead with your own numbers, invite a comparison, point at
    the repo. Dry, lowercase, no hashtags, no recycled punchline (the card
    already carries it). The user edits before posting; nothing auto-posts."""
    tokens = fmt_tokens(s["total_tokens"])
    sessions = format(s["sessions"], ",")
    calls = format(s["tool_calls"], ",")
    if s["top_tool"]:
        tool_bit = "i reached for %s %s times" % (_pretty_tool(s["top_tool"]["name"]).lower(),
                                                  format(s["top_tool"]["count"], ","))
    else:
        tool_bit = "i made %s tool calls" % calls
    # the herdr mod adds these. each says what the number is, and no more. the jar only
    # gets here when the person has opted in.
    peak_bit = ""
    if s.get("peak"):
        peak_bit += " most agents at once: %s, idle ones included." % format(s["peak"]["count"], ",")
    if s.get("bottleneck"):
        peak_bit += " my agents worked %s and waited %s for me (estimated)." % (s["bottleneck"]["worked"], s["bottleneck"]["waited"])
    if s.get("jar"):
        peak_bit += " swear jar: %s." % s["jar"]["fine"]
    if s.get("sample"):
        peak_bit += " (sample data)"
    x = ("just ran my claude code wrapped and i can't believe it: %s tokens processed "
         "across %s sessions, and %s.%s\n\nrun yours and see whose is scarier: %s"
         % (tokens, sessions, tool_bit, peak_bit, REPO))
    if len(x) > _POST_LIMIT:  # a post has room for the numbers or the patter, not both
        x = ("my claude code wrapped: %s tokens, %s sessions.%s\n\nrun yours: %s"
             % (tokens, sessions, peak_bit, REPO))
    linkedin = ("i just ran claude code wrapped over my own history. it reads the local "
                "transcripts, nothing leaves the machine, and the verdict is on the card: "
                "%s tokens processed, %s sessions, %s tool calls.%s\n\n"
                "it is one command and it works on your own history. run yours and see "
                "whose is scarier: %s"
                % (tokens, sessions, calls, peak_bit, REPO))
    slack = ("just ran my claude code wrapped: %s tokens, %s sessions, and %s.%s "
             "all local, nothing uploaded. run yours and post the card: %s"
             % (tokens, sessions, tool_bit, peak_bit, REPO))
    return {"x": x, "linkedin": linkedin, "slack": slack}


def x_intent_url(text):
    """A twitter/X web-intent compose URL with the caption prefilled. Images can't
    be pre-attached via a url (platform security), so the caller notes to drag the
    saved PNG in."""
    return "https://twitter.com/intent/tweet?text=" + urllib.parse.quote(text)


# ---- selftest ---------------------------------------------------------------

def _fixture_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples", "runs")


def selftest():
    problems = []
    runs = ingest_dir(_fixture_dir())
    by = {r["run_id"]: r for r in runs}
    s = wrapped_stats(runs)

    if s["sessions"] != 3:
        problems.append("expected 3 sessions, got %d" % s["sessions"])
    # s1: two records share reqA (cumulative usage) -> collapsed to ONE turn, so
    # tokens are 6000, each line's usage counted as /stats does, and the Bash tool_use (deduped by id) counts once.
    r1 = by.get("s1", {})
    if r1.get("tokens") != 6000:
        problems.append("s1 should count every line's usage, 6000 tokens, got %r" % r1.get("tokens"))
    if abs(r1.get("cost_usd", 0) - 0.07) > 1e-9:
        problems.append("s1 cost expected 0.07 (per line), got %s" % r1.get("cost_usd"))
    if r1.get("tools") != {"Bash": 1}:
        problems.append("s1 tools expected {Bash:1}, got %r" % r1.get("tools"))
    if r1.get("absolutely_right") != 1:
        problems.append("s1 absolutely_right expected 1, got %r" % r1.get("absolutely_right"))

    # total tokens (throughput incl cache): s1 6000 + s2 8500 + s3 10000 = 24500;
    # cost priced with cache (read 0.1x, write 1.25x): 0.07 + 0.083 + 0.1055 = 0.2585.
    if s["total_tokens"] != 24500:
        problems.append("total tokens expected 24500, got %d" % s["total_tokens"])
    if abs(s["cost_usd"] - 0.2585) > 1e-9:
        problems.append("total cost expected 0.2585, got %s" % s["cost_usd"])

    # the meme stat counts per-occurrence and is case + apostrophe insensitive:
    # s1 "you're" x1, s2 "You're...you're" x2, s3 x0 -> 3.
    if s["absolutely_right"] != 3:
        problems.append("absolutely_right expected 3, got %d" % s["absolutely_right"])

    # top tool across the corpus is Bash x3 (one per session); 6 tool calls total.
    if s["top_tool"] != {"name": "Bash", "count": 3}:
        problems.append("top_tool expected Bash x3, got %r" % s["top_tool"])
    if s["tool_calls"] != 6:
        problems.append("tool_calls expected 6, got %d" % s["tool_calls"])

    # longest session is s2 (09:00 -> 12:12 = 3h 12m); busiest day is 2026-07-01
    # (6700 tokens); both derived, not hardcoded.
    if not s["longest_session"] or s["longest_session"]["run_id"] != "s2" \
            or s["longest_session"]["human"] != "3h 12m":
        problems.append("longest_session expected s2 / 3h 12m, got %r" % s["longest_session"])
    if not s["busiest_day"] or s["busiest_day"]["date"] != "2026-07-01" \
            or s["busiest_day"]["label"] != "jul 1":
        problems.append("busiest_day expected 2026-07-01 / jul 1, got %r" % s["busiest_day"])

    # top project by tokens (incl cache) is alpha (s1 6000 + s3 10000 = 16000 > beta s2 8500).
    if not s["top_project"] or s["top_project"]["name"] != "alpha" \
            or s["top_project"]["tokens"] != 16000:
        problems.append("top_project expected alpha / 16000, got %r" % s["top_project"])

    # --month windows to just that month; july keeps only s3 (10000 tokens incl cache).
    july = filter_month(runs, "2026-07")
    if {r["run_id"] for r in july} != {"s3"}:
        problems.append("--month 2026-07 should keep only s3, got %r" % [r["run_id"] for r in july])
    if wrapped_stats(july)["total_tokens"] != 10000:
        problems.append("july total tokens expected 10000, got %d" % wrapped_stats(july)["total_tokens"])

    # formatting helpers are deterministic.
    checks = [(fmt_tokens(14200), "14.2K"), (fmt_tokens(128400000), "128.4M"),
              (fmt_dollars(0.17075), "$0.17"), (fmt_dollars(1284.5), "$1,284.50"),
              (fmt_duration(11520000), "3h 12m"), (fmt_duration(1200000), "20m"),
              (fmt_duration(1019880000), "11d 19h"),
              (fmt_day("2026-06-30"), "jun 30"),
              (_pretty_tool("mcp__victoriametrics__query_range"), "query_range"),
              (_pretty_tool("Bash"), "Bash"), (_clip("query_range", 8), "query_r…")]
    for got, want in checks:
        if got != want:
            problems.append("format expected %r, got %r" % (want, got))

    # the html card is self-contained (no network/script), deterministic, dash-free,
    # renders in both formats, shows the honest breakdown + api-equivalent
    # label + the data-driven insight, and NEVER contains a project name.
    card = html_card(s)  # default square
    if "<style>" not in card or "http://" in card or "https://" in card or "<script" in card:
        problems.append("the card should be self-contained (inline style, no remote, no script)")
    for needed in ('class="card square"', "24.5", "$0.26", "Bash", "jul 1", "all time",
                   "from cache", "api-equivalent cost", 'class="bar', "absolutely right"):
        if needed not in card:
            problems.append("the card is missing %r" % needed)
    if "\u2014" in card or "\u2013" in card:
        problems.append("the card must have no em/en dashes")
    for name in ("alpha", "beta"):  # the fixture project names must never leak
        if name in card:
            problems.append("the card leaked a project name (%s); names never reach the card" % name)
    for fmt in ("square", "wide"):
        if ('class="card %s"' % fmt) not in html_card(s, fmt):
            problems.append("card format %s did not apply its class" % fmt)

    # the insight engine: ranked highest-score-first, and x0 gives a funny line
    # (not a blank stat), which was the whole point of replacing the fixed meme.
    ins = s["insights"]
    if not ins or any(ins[i]["score"] < ins[i + 1]["score"] for i in range(len(ins) - 1)):
        problems.append("insights should be ranked highest-score first, got %r" % ins)
    z = insights({"absolutely_right": 0, "tokens_from_cache": 10, "total_tokens": 100,
                  "longest_session": None, "cost_usd": 0, "top_tool": None, "tool_calls": 0,
                  "sessions": 1, "distinct_tools": 1, "busiest_day": None})
    if not any(x["id"] == "absolutely-right-zero" for x in z):
        problems.append("an x0 concession count should trigger the zero-insight, got %r" % z)

    # hardening guards (adversarial review 2026-07-09): regex boundaries, and a
    # deliberately messy corpus that must not crash and must count honestly.
    if _ABSOLUTELY_RIGHT.findall("you're absolutely rightly proud of that"):
        problems.append("meme regex should not match 'absolutely rightly'")
    if len(_ABSOLUTELY_RIGHT.findall("You are absolutely right! and you’re absolutely right")) != 2:
        problems.append("meme regex should match 'you are' + curly-apostrophe variants (2)")
    tmp = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmp, "z.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "uuid": "z", "requestId": "rz", "sessionId": "z",
                "timestamp": "2026-07-01T10:00:00.000Z", "message": {"model": "claude-opus-4-8",
                "usage": {"input_tokens": 5000, "output_tokens": 2000, "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0}, "content": [{"type": "text", "text": "you're absolutely right"}]}}) + "\n")
            # same requestId, EMPTY usage + a NAIVE timestamp: must not zero or crash
            f.write(json.dumps({"type": "assistant", "uuid": "z2", "requestId": "rz", "sessionId": "z",
                "timestamp": "2026-07-01T10:05:00", "message": {"model": "claude-opus-4-8", "usage": {}, "content": []}}) + "\n")
            f.write("{ not valid json at all\n")  # bad line: skipped
            # message is a string, not a dict: must not crash
            f.write(json.dumps({"type": "assistant", "uuid": "z3", "sessionId": "z",
                "timestamp": "2026-07-01T10:06:00.000Z", "message": "not a dict"}) + "\n")
            # a SUBAGENT concession: counts toward tokens but NOT the meme stat
            f.write(json.dumps({"type": "assistant", "uuid": "z4", "requestId": "rzs", "isSidechain": True,
                "sessionId": "z", "timestamp": "2026-07-01T10:07:00.000Z", "message": {"model": "claude-opus-4-8",
                "usage": {"input_tokens": 10, "output_tokens": 10, "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0}, "content": [{"type": "text", "text": "you're absolutely right"}]}}) + "\n")
        rr = ingest_dir(tmp)
        if len(rr) != 1:
            problems.append("robustness: expected 1 session from the messy corpus, got %d" % len(rr))
        else:
            zz = rr[0]
            if zz["tokens"] != 7020:  # 7000 main (empty trailing usage did not zero it) + 20 subagent
                problems.append("robustness: empty-usage/tz handling wrong, tokens=%r" % zz["tokens"])
            if zz["absolutely_right"] != 1:
                problems.append("robustness: subagent meme text should be excluded, got %r" % zz["absolutely_right"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --json is serializable and carries the headline stats.
    try:
        parsed = json.loads(json.dumps(s))
    except ValueError:
        parsed = None
    if not parsed or "insights" not in parsed or "tokens_from_cache" not in parsed:
        problems.append("--json output should carry the headline stats + insights")

    # v5 regressions (2026-07-09 real-data debug):
    # (1) the stylesheet must be structurally sound. orphaned declarations at the
    # top level (leftovers from the portrait removal) made css error-recovery eat
    # everything up to the next '{' - which silently DROPPED the .card.wide sizing
    # rule and shipped a frameless wide card.
    depth = 0
    for ch in CARD_STYLE:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == ";" and depth == 0:
            problems.append("CARD_STYLE has a stray declaration at the stylesheet top level")
            break
        if depth not in (0, 1):
            problems.append("CARD_STYLE braces went unbalanced (depth %d)" % depth)
            break
    else:
        if depth != 0:
            problems.append("CARD_STYLE braces do not balance (final depth %d)" % depth)
    if ".card.wide{width:1600px" not in CARD_STYLE.replace(" ", ""):
        problems.append("the .card.wide sizing rule is missing from CARD_STYLE")

    # (2) the chart covers the FULL window: zero-filled calendar days, bucketed
    # when the span outgrows the bar budget (an all-time card must not silently
    # show only its last two weeks).
    act = [{"date": "2026-01-01", "tokens": 100},
           {"date": "2026-01-03", "tokens": 300},    # jan 2 idle -> a zero bar, not a missing one
           {"date": "2026-02-10", "tokens": 900}]    # 41-day span -> 3d buckets
    buckets, k = _calendar_series(act, 14)
    if k != 3 or len(buckets) != 14 or buckets[0]["date"] != "2026-01-01":
        problems.append("41-day span should bucket to 14 bars of 3d from jan 1, got k=%r n=%r" % (k, len(buckets)))
    if sum(b["tokens"] for b in buckets) != 1300:
        problems.append("bucketing must conserve tokens (expected 1300, got %r)" % sum(b["tokens"] for b in buckets))
    daily, k1 = _calendar_series(act[:2], 14)
    if k1 != 1 or [b["tokens"] for b in daily] != [100, 0, 300]:
        problems.append("short spans should stay daily and zero-fill gaps, got %r" % daily)

    # (3) axis labels are sparse (a label is wider than a bar; labelling every bar
    # was the clipped-right-edge bug) but keep both ends + the peak, and the bar
    # geometry stays one span per bar.
    bars_h, axis_h, note_h, lbl_h = _activity_html(act, 14)
    if lbl_h != "activity" or not note_h.startswith("3d bars"):
        problems.append("bucketed chart should declare its granularity, got %r / %r" % (lbl_h, note_h))
    if axis_h.count("<span") != 14:
        problems.append("axis should keep one span per bar, got %d" % axis_h.count("<span"))
    texted = re.findall(r"<span[^>]*>([^<]+)</span>", axis_h)
    if not (2 <= len(texted) <= 7):
        problems.append("axis should carry 2..7 sparse labels, got %d (%r)" % (len(texted), texted))
    if "jan 1" not in texted or "feb 10" not in texted:
        problems.append("axis must label the true date range ends, got %r" % texted)
    for n, p, want in ((14, 5, {0, 5, 13}), (2, 0, {0}), (10, 9, {0, 9}),
                       (31, 30, {0, 30}), (12, 1, {1, 11})):
        lab = _axis_label_indices(n, p)
        if not want <= lab or len(lab) > 7:
            problems.append("label indices broken for n=%d peak=%d: %r" % (n, p, lab))
        if any(abs(a - b) < 2 for a in lab for b in lab if a != b):
            problems.append("adjacent axis labels overlap for n=%d peak=%d: %r" % (n, p, sorted(lab)))

    # (4) `card --out x.html` writes the html (it used to point a png screenshot
    # at an .html path and fail confusingly).
    tmp2 = tempfile.mkdtemp()
    try:
        outp = os.path.join(tmp2, "c.html")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["card", _fixture_dir(), "--out", outp])
        if rc != 0 or not os.path.exists(outp) or "<style>" not in open(outp, encoding="utf-8").read():
            problems.append("card --out x.html should write the html card directly")
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)

    # (5) share captions are written for posting the card: your numbers + the
    # compare CTA + the repo - and never the insight (the card carries the
    # punchline already; the caption should not repeat it).
    st = share_text(s)
    for key, cap in st.items():
        if REPO not in cap:
            problems.append("share caption %r must point at the repo" % key)
        if s["insights"] and s["insights"][0]["line"] in cap:
            problems.append("share caption %r recycles the insight" % key)
        if "\u2014" in cap or "\u2013" in cap:
            problems.append("share caption %r has an em/en dash" % key)
    if "24.5K" not in st["x"] or "whose is scarier" not in st["x"]:
        problems.append("x caption should lead with the numbers and invite comparison, got %r" % st["x"])
    if "bash 3 times" not in st["x"]:
        problems.append("x caption should carry the top-tool stat, got %r" % st["x"])

    # (6) the png cropper: chrome's new headless viewport is SHORTER than
    # --window-size (~90px of phantom window chrome; measured innerHeight 993
    # for a 1080 window), which used to cut the CTA off the delivered png. the
    # render now captures with headroom and crops to the exact card box.
    def _mini_png(w, hgt):
        rows = b""
        for y in range(hgt):
            row = bytearray((x + y * 16) & 0xFF for x in range(w * 4))
            if y == 1:  # store row 1 with the 'up' filter so unfiltering is exercised
                prevrow = bytes((x + (y - 1) * 16) & 0xFF for x in range(w * 4))
                row = bytearray((row[i] - prevrow[i]) & 0xFF for i in range(len(row)))
                rows += b"\x02" + bytes(row)
            else:
                rows += b"\x00" + bytes(row)
        def ck(t, p):
            return struct.pack(">I", len(p)) + t + p + struct.pack(">I", zlib.crc32(t + p) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n" + ck(b"IHDR", struct.pack(">IIBBBBB", w, hgt, 8, 6, 0, 0, 0))
                + ck(b"IDAT", zlib.compress(rows)) + ck(b"IEND", b""))
    tmp3 = tempfile.mkdtemp()
    try:
        pth = os.path.join(tmp3, "c.png")
        with open(pth, "wb") as f:
            f.write(_mini_png(6, 8))
        okc, msgc = _crop_png(pth, 4, 2)
        out = open(pth, "rb").read()
        if not okc or struct.unpack(">II", out[16:24]) != (4, 2):
            problems.append("png crop failed: %s" % msgc)
        else:
            i0 = out.index(b"IDAT")
            ln = int.from_bytes(out[i0 - 4:i0], "big")
            raw = zlib.decompress(out[i0 + 4:i0 + 4 + ln])
            want = b"\x00" + bytes(range(16)) + b"\x00" + bytes((x + 16) & 0xFF for x in range(16))
            if raw != want:
                problems.append("png crop returned wrong pixels (unfiltering broken)")
            okc2, msgc2 = _crop_png(pth, 4, 2)
            if not okc2 or "exact" not in msgc2:
                problems.append("cropped png should re-parse as already exact, got %r" % msgc2)
    finally:
        shutil.rmtree(tmp3, ignore_errors=True)

    if problems:
        print("FAIL")
        for p in problems:
            print("  - " + p)
        return 1
    print("PASS: 3 fixture sessions, deterministic. total 24.5K tokens (incl cache) "
          "/ $0.2585 (usage per line as /stats counts, cache priced); \"you're absolutely right\" "
          "x3 (case + apostrophe insensitive, per-occurrence); top tool Bash x3; "
          "longest session s2 3h 12m; busiest day jul 1; top project alpha (16000); "
          "--month windows to s3; formatters check out; stylesheet sound (wide rule "
          "present); chart covers the full window (zero-filled, bucketed, sparse "
          "end+peak labels); no project name ever reaches the card; captions are "
          "stats + compare-CTA (no recycled insight); png cropper exact (the "
          "headless viewport-shave fix); card --out x.html writes html")
    return 0


# ---- cli --------------------------------------------------------------------

def _pull_flag(argv, name):
    if name in argv:
        i = argv.index(name)
        value = argv[i + 1] if i + 1 < len(argv) else None
        return value, argv[:i] + argv[i + 2:]
    return None, argv


def _load(directory, month):
    return filter_month(ingest_dir(directory), month)


def _sample_stats(month):
    """The made-up stats for `demo` and `card --demo`. helper.py holds the peak and the rest."""
    import helper
    return helper.sample_stats(month)


def _window_label(month):
    return month if month else "all time"


def _print_share(s):
    """Print ready-to-paste captions + an X compose link. Honest limit: platforms
    do not let a url pre-attach an image, so the user drags the saved png in."""
    st = share_text(s)
    print("\n  " + _c("─" * 60, _FAINT))
    print("  " + _c("SHARE", _AMBER, _BOLD) + _c("  edit to taste, then attach the saved image", _FAINT))
    print("\n  " + _c("[ x ]", _AMBER) + "\n" + st["x"])
    print("\n  " + _c("compose: ", _FAINT) + _c(x_intent_url(st["x"]), _MUTED))
    print("\n  " + _c("[ linkedin ]", _AMBER) + "\n" + st["linkedin"])
    print("\n  " + _c("[ slack ]", _AMBER) + "\n" + st["slack"])
    print("\n  " + _c("(a url cannot pre-attach an image, so drag the saved png in yourself.)", _FAINT))


def cmd_profiles():
    """List the claude profiles wrapped can read (and which one is active)."""
    profiles = discover_profiles()
    if not profiles:
        print("no claude transcript directories found under your home folder.")
        return 0
    _wiz_banner("profiles")
    for p in profiles:
        tag = _c("   ← active ($CLAUDE_CONFIG_DIR)", _AMBER) if p["active"] else ""
        print("  " + _c(p["name"].ljust(24), _INK, _BOLD) + _c("%6d transcripts" % p["files"], _FAINT) + tag)
        print("    " + _c(p["dir"], _FAINT))
    print("\n  " + _c("wrapped reads the active one by default. point it at another:", _FAINT))
    print("  " + _c("python3 wrapped.py card '%s'" % profiles[0]["dir"], _MUTED))
    return 0


def _wiz_banner(subtitle):
    print()
    print("  " + _c("●", _FAINT) + _c(" ●", _MUTED) + _c(" ●", _AMBER) + "   "
          + _c("claude code", _INK, _BOLD) + _c(" · ", _FAINT) + _c("wrapped", _AMBER, _BOLD)
          + "   " + _c(subtitle, _FAINT))
    print("  " + _c("─" * 60, _FAINT))
    print()


def _ask(prompt, default=""):
    hint = _c(" [%s]" % default, _FAINT) if default else ""
    try:
        v = input("  " + _c("›", _AMBER, _BOLD) + " " + _c(prompt, _INK) + hint + _c("  ", _FAINT)).strip()
    except EOFError:
        v = ""
    return v or default


def run_wizard():
    """Interactive setup: pick a profile, window, format, and share options, then
    render the card. The flags (card/wrap/--format/...) are the scriptable fallback."""
    _wiz_banner("setup")
    profiles = discover_profiles()
    directory = default_transcript_dir()
    if profiles:
        print("  " + _c("profiles found", _FAINT))
        for i, p in enumerate(profiles, 1):
            tag = _c("   ← active", _AMBER) if p["active"] else ""
            print("  " + _c("%d)" % i, _AMBER, _BOLD) + " " + _c(p["name"].ljust(22), _INK)
                  + _c("%6d transcripts" % p["files"], _FAINT) + tag)
        print()
        default_i = next((str(i) for i, p in enumerate(profiles, 1) if p["active"]), "1")
        try:
            directory = profiles[int(_ask("pick a profile", default_i)) - 1]["dir"]
        except (ValueError, IndexError):
            pass
    month = _ask("month YYYY-MM (blank = all time)", "")
    if month and not re.match(r"^\d{4}-\d{2}$", month):
        print("  " + _c("(not YYYY-MM, using all time)", _FAINT))
        month = ""
    fmt = _ask("format square/wide", "square")
    fmt = fmt if fmt in FORMATS else "square"
    want_share = _ask("print share captions? y/n", "y").lower().startswith("y")
    out = _ask("output png path", "wrapped.png")
    print("\n  " + _c("reading %s ..." % directory, _FAINT))
    s = wrapped_stats(_load(directory, month), _window_label(month))
    print_wrapped(s)
    ok, msg = render_png(html_card(s, fmt), out, fmt)
    if ok:
        print("  " + _c("✓ wrote ", _AMBER, _BOLD) + _c(msg, _INK))
    else:
        hout = os.path.splitext(out)[0] + ".html"
        with open(hout, "w", encoding="utf-8") as f:
            f.write(html_card(s, fmt))
        print("  " + _c("could not render a png (%s); wrote %s - open and screenshot it." % (msg, hout), _MUTED))
    if want_share:
        _print_share(s)
    return 0


def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if not argv:  # bare invocation -> the wizard when interactive, else the help
        return run_wizard() if sys.stdin.isatty() else (print(__doc__) or 0)
    if argv[0] == "selftest":
        return selftest()
    if argv[0] == "wizard":
        return run_wizard()
    if argv[0] == "profiles":
        return cmd_profiles()

    wanted_month = "--month" in argv
    month, argv = _pull_flag(argv, "--month")
    if wanted_month and not (month and re.match(r"^\d{4}-\d{2}$", month)):
        print("--month needs a YYYY-MM value (e.g. --month 2026-07)", file=sys.stderr)
        return 2
    argv = [a for a in argv if a != "--all-time"]
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    fmt, argv = _pull_flag(argv, "--format")
    fmt = fmt if fmt in FORMATS else "square"
    want_html = "--html" in argv
    argv = [a for a in argv if a != "--html"]
    want_share = "--share" in argv
    argv = [a for a in argv if a != "--share"]
    want_sample = "--demo" in argv
    argv = [a for a in argv if a != "--demo"]
    default_dir = default_transcript_dir()

    if not argv:  # flags but no subcommand
        return run_wizard() if sys.stdin.isatty() else (print(__doc__) or 0)
    if argv[0] == "demo":
        print_wrapped(_sample_stats(month))
        return 0
    if argv[0] == "wrap":
        directory = argv[1] if len(argv) >= 2 else default_dir
        s = wrapped_stats(_load(directory, month), _window_label(month))
        print(json.dumps(s, indent=2)) if as_json else print_wrapped(s)
        return 0
    if argv[0] == "card":
        out, argv = _pull_flag(argv, "--out")
        directory = argv[1] if len(argv) >= 2 else default_dir
        s = _sample_stats(month) if want_sample else wrapped_stats(_load(directory, month), _window_label(month))
        html = html_card(s, fmt)
        w, h = FORMATS[fmt]
        if out and out.lower().endswith((".html", ".htm")):
            want_html = True  # asking for an .html file IS asking for --html
        if want_html:
            out = out or "wrapped.html"
            with open(out, "w", encoding="utf-8") as f:
                f.write(html)
            print("wrote %s (%d bytes, self-contained). screenshot it at %dx%d." % (out, len(html), w, h))
        else:
            out = out or "wrapped.png"
            ok, msg = render_png(html, out, fmt)
            if ok:
                print("wrote %s" % msg)
            else:  # keep the html so the user still gets an artifact
                hout = os.path.splitext(out)[0] + ".html"
                with open(hout, "w", encoding="utf-8") as f:
                    f.write(html)
                print("could not render a png: %s" % msg, file=sys.stderr)
                print("wrote %s instead - open it and screenshot at %dx%d, "
                      "or set $CLAUDE_WRAPPED_BROWSER to a chrome path." % (hout, w, h))
        if want_share:
            _print_share(s)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
