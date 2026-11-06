#!/usr/bin/env python3
"""Reads Claude Code's transcripts for the Wrapped overlay. It opens them for reading only and uses no network.

  helper.py read --cache FILE [--demo] [--projects DIR ...] [--budget SECONDS]
  helper.py save --cache FILE --window all|month --out DIR [--jar] [--demo] [--projects DIR ...]

`read` prints the overlay's numbers for both windows as one line of JSON. Transcripts can total gigabytes, so each file's
parsed result is kept in --cache against its size and modified time, and only new or changed files are parsed. A pass
stops parsing once --budget seconds have gone and prints "partial": true, and the overlay asks again until none are left.
`save` writes the card to --out as a PNG, or as HTML when no browser can make one. The Swear Jar only reaches the card
with --jar, which the overlay passes when the person has turned sharing on.

--demo uses the made-up runs in sample.py and no cache. Run under python3 -I -S -B: -I leaves this folder off sys.path, so
it's added below, and -B keeps __pycache__ out of the mod folder, where a new file would make Herdr reload the mod.

Everything in the transcripts is data. Nothing here acts on it. No prompt text, project name, path or session id leaves
this file: habits.py hands back counts only.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True
sys.path.append(HERE)
import habits  # noqa: E402
import sample  # noqa: E402
import wrapped  # noqa: E402

CACHE_VERSION = 3
READ_BUDGET = 15.0
SAVE_BUDGET = 4.0
MAX_FILE = 256 * 1024 * 1024
STRIP_BARS = 56
TOOL_LIMIT = 16
REQUEST_ID = re.compile(rb'"requestId"\s*:\s*"([^"]+)"')


def peak_agents(runs):
    """The most Claude Code sessions open at one moment, as {"count", "date", "label"}, or None with no timed runs.

    A session is open from the first to the last timestamp in its transcript, so a session that sat idle counts as
    running the whole time. Runs sharing a run_id are one session: a subagent's transcript carries its parent's
    sessionId, and is folded into the parent. Spans are half open, so one ending at the moment another starts doesn't
    overlap it. A session with one timestamp is open for that instant, and counts with anything open then."""
    spans = {}
    for r in runs:
        start, end = wrapped._ts(r.get("started")), wrapped._ts(r.get("ended"))
        if not start or not end:
            continue
        known = spans.get(r.get("run_id"))
        spans[r.get("run_id")] = (min(start, known[0]), max(end, known[1])) if known else (start, end)
    events = []
    for start, end in spans.values():
        events.append((start, 1, 1))
        events.append((end, 0 if end > start else 2, -1))
    events.sort(key=lambda e: e[:2])
    count = best = 0
    when = None
    for moment, _order, step in events:
        count += step
        if count > best:
            best, when = count, moment
    if not when:
        return None
    day = when.isoformat()[:10]
    return {"count": best, "date": day, "label": wrapped.fmt_day(day)}


def card_stats(runs, window, jar=False, made_up=False):
    """wrapped.py's stats for these runs, with what goes on the card beside them. The Swear Jar only joins them when `jar`
    is set, so with sharing off there's nothing in the stats for the card or a caption to show."""
    stats = wrapped.wrapped_stats(runs, window)
    stats["peak"] = peak_agents(runs)
    stats["bottleneck"] = habits.bottleneck_of(runs)
    if jar:
        filled = habits.jar_of(runs)
        if filled["count"]:
            stats["jar"] = filled
    if made_up:
        stats["sample"] = True
    return stats


def sample_stats(month=None):
    """The made-up stats, for `wrapped.py demo` and `wrapped.py card --demo`."""
    runs = sample.runs()
    return card_stats(wrapped.filter_month(runs, month), month or "all time", made_up=True)


def strip_of(activity):
    """The daily activity strip: bar heights as percents of the tallest, over the whole window."""
    series, days = wrapped._calendar_series(activity, STRIP_BARS)
    if not series:
        return None
    tallest = max(d["tokens"] for d in series) or 1
    top = max(range(len(series)), key=lambda i: series[i]["tokens"])
    dated = sorted(d["date"] for d in activity if d["date"] != "unknown")
    return {"bars": [max(3, round(100 * d["tokens"] / tallest)) if d["tokens"] else 0 for d in series],
            "top": top, "days": days, "from": wrapped.fmt_day(dated[0]), "to": wrapped.fmt_day(dated[-1])}


def post_of(stats):
    caption = wrapped.share_text(stats)["x"]
    return {"caption": caption, "postUrl": wrapped.x_intent_url(caption)}


def view_of(runs, window, made_up=False):
    """What the overlay draws, formatted the way the card formats it. Names, paths, ids and prompts stay out. `share` has
    the caption without the Swear Jar, and `shareJar` the one with it, for when the person has turned sharing on."""
    stats = card_stats(runs, window, made_up=made_up)
    if not runs:
        return {"window": window, "empty": True}
    number, unit = wrapped._split_compact(wrapped.fmt_tokens(stats["total_tokens"]))
    tool = stats["top_tool"]
    busiest = stats["busiest_day"]
    jar = habits.jar_of(runs)
    return {
        "window": stats["window"], "empty": False,
        "tokens": number, "unit": unit,
        "fromCache": wrapped.fmt_tokens(stats["tokens_from_cache"]),
        "newTokens": wrapped.fmt_tokens(stats["tokens_new"]),
        "cost": wrapped.fmt_dollars(stats["cost_usd"]),
        "sessions": format(stats["sessions"], ","),
        "toolCalls": format(stats["tool_calls"], ","),
        "topTool": {"name": wrapped._clip(wrapped._pretty_tool(tool["name"]), TOOL_LIMIT),
                    "count": format(tool["count"], ",")} if tool else None,
        "longest": stats["longest_session"]["human"] if stats["longest_session"] else None,
        "busiest": {"label": busiest["label"], "tokens": wrapped.fmt_tokens(busiest["tokens"])} if busiest else None,
        "peak": stats["peak"],
        "bottleneck": stats["bottleneck"],
        "capMinutes": habits.WAIT_CAP_MS // 60000,
        "jar": {**jar, "rights": format(stats["absolutely_right"], ",")},
        "strip": strip_of(stats["activity"]),
        "punchline": stats["insights"][0]["line"] if stats["insights"] else "",
        "share": post_of(stats),
        "shareJar": post_of(card_stats(runs, window, jar=True, made_up=made_up)),
    }


def roots_for(args):
    if args.projects:
        return args.projects
    return [p["dir"] for p in wrapped.discover_profiles() if p["files"]]


def transcripts(roots):
    """Every .jsonl under these folders, as (path, size, mtime). The same path inside two Claude folders is one
    transcript, as a copied folder holds, and the larger is kept: counting both would double the totals and the peak."""
    found = {}
    for root in roots:
        for folder, _dirs, names in os.walk(root):
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(folder, name)
                try:
                    info = os.stat(path)
                except OSError:
                    continue
                key = os.path.relpath(path, root)
                if key not in found or info.st_size > found[key][1]:
                    found[key] = (path, info.st_size, info.st_mtime_ns)
    return sorted(found.values())


def parser_id():
    """Changes when the parsing does, so new prices or rules aren't mixed with old results."""
    digest = hashlib.sha1()
    for name in ("wrapped.py", "habits.py"):
        with open(os.path.join(HERE, name), "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()


def load_cache(path, parser):
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(cache, dict) or cache.get("v") != CACHE_VERSION or cache.get("parser") != parser:
        return {}
    files = cache.get("files")
    return files if isinstance(files, dict) else {}


def save_cache(path, parser, files):
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = "%s.tmp.%d" % (path, os.getpid())
    with open(temp, "w", encoding="utf-8") as f:
        json.dump({"v": CACHE_VERSION, "parser": parser, "files": files}, f, separators=(",", ":"))
    os.replace(temp, path)


def parent_of(path):
    """The session transcript of a file under <session>/subagents/, or None."""
    head, tail = os.path.split(path)
    while tail:
        head, tail = os.path.split(head)
        if tail == "subagents":
            return head + ".jsonl"
    return None


def requests_in(path, parents):
    """The api turns a subagent's parent already counts. Read once per parent per pass: the file can be large, and only
    these ids are taken from it."""
    if path not in parents:
        try:
            with open(path, "rb") as f:
                parents[path] = set(i.decode("utf-8", "replace") for i in REQUEST_ID.findall(f.read()))
        except OSError:
            parents[path] = set()
    return parents[path]


def read_one(path, size, mtime, parents):
    """One cache entry. `run` is None for a file that isn't a transcript. `bad` marks one that couldn't be read."""
    entry = {"size": size, "mtime": mtime, "run": None}
    if size > MAX_FILE:
        entry["bad"] = True
        return entry
    try:
        parent = parent_of(path)
        run = wrapped.parse_run(path, habits.read, requests_in(parent, parents) if parent else ())
    except Exception:
        entry["bad"] = True
        return entry
    if run["is_transcript"]:
        run["project"] = None  # the one name-bearing field: nothing here uses it, so it isn't kept
        entry["run"] = run
    return entry


def collect(roots, cache_path, budget):
    """(runs, progress). Parses new and changed files until the budget has gone, at least one per pass, so a pass always
    gets further than the last."""
    began = time.monotonic()
    parser = parser_id()
    known = load_cache(cache_path, parser)
    found = transcripts(roots)
    files, runs, parsed, waiting, left_out, parents = {}, [], 0, 0, 0, {}
    for path, size, mtime in found:
        # A transcript's path holds its project's folder name, so the cache is keyed by a hash of it instead.
        key = hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest()
        entry = known.get(key)
        if not entry or entry.get("size") != size or entry.get("mtime") != mtime:
            if parsed and time.monotonic() - began >= budget:
                waiting += 1
                continue
            entry = read_one(path, size, mtime, parents)
            parsed += 1
        files[key] = entry
        if entry.get("bad"):
            left_out += 1
        elif entry.get("run"):
            runs.append(entry["run"])
    if parsed or len(files) != len(known):
        save_cache(cache_path, parser, files)
    runs.sort(key=lambda r: r["started"] or "")
    return runs, {"read": len(found) - waiting, "total": len(found), "leftOut": left_out}


def month_of(runs, demo):
    """This month's YYYY-MM. A demo's month is the newest in its made-up runs."""
    if demo:
        return max((r["started"] or "")[:7] for r in runs)
    return datetime.now().strftime("%Y-%m")


def gather(args, budget):
    if args.demo:
        made = sample.runs()
        return made, {"read": len(made), "total": len(made), "leftOut": 0}
    return collect(roots_for(args), args.cache, budget)


def read(args):
    runs, progress = gather(args, args.budget)
    if not progress["total"] or (not runs and not progress["leftOut"]):
        return {"error": "No Claude Code transcripts found. Looked in $CLAUDE_CONFIG_DIR and in projects under each ~/.claude* folder."}
    if progress["read"] < progress["total"]:
        return {"partial": True, **progress}
    month = month_of(runs, args.demo) if runs else datetime.now().strftime("%Y-%m")
    return {"partial": False, **progress, "windows": {
        "all": view_of(runs, "all time", args.demo),
        "month": view_of(wrapped.filter_month(runs, month), month, args.demo),
    }}


def save(args):
    runs, _progress = gather(args, SAVE_BUDGET)
    label = "all time"
    if args.window == "month":
        label = month_of(runs, args.demo) if runs else datetime.now().strftime("%Y-%m")
        runs = wrapped.filter_month(runs, label)
    if not runs:
        return {"error": "Nothing to save: this window has no sessions."}
    html = wrapped.html_card(card_stats(runs, label, jar=args.jar, made_up=args.demo), "square")
    os.makedirs(args.out, exist_ok=True)
    name = "claude-code-wrapped-%s-%s" % (label.replace(" ", "-"), "demo" if args.demo else date.today().isoformat())
    png = os.path.join(args.out, name + ".png")
    done, why = wrapped.render_png(html, png, "square")
    if done:
        return {"saved": png, "kind": "png"}
    page = os.path.join(args.out, name + ".html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(html)
    return {"saved": page, "kind": "html", "noBrowser": not wrapped.find_browser(), "why": why}


def main(argv):
    parser = argparse.ArgumentParser(prog="helper.py")
    parser.add_argument("action", choices=["read", "save"])
    parser.add_argument("--cache")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--jar", action="store_true")
    parser.add_argument("--projects", action="append")
    parser.add_argument("--budget", type=float, default=READ_BUDGET)
    parser.add_argument("--window", choices=["all", "month"], default="all")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        reply = read(args) if args.action == "read" else save(args)
    except Exception as e:  # named for the overlay, not left as a traceback
        reply = {"error": "%s: %s" % (type(e).__name__, e)}
    # ASCII only, whatever the output encoding is.
    sys.stdout.write(json.dumps(reply, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
