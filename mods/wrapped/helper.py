#!/usr/bin/env python3
"""Reads Claude Code's transcripts for the Wrapped overlay. It opens them for reading only and uses no network.

  helper.py read --cache FILE [--demo] [--projects DIR ...] [--stats FILE] [--budget SECONDS]
  helper.py save --cache FILE --window all|7d|30d --out DIR [--jar] [--demo] [--projects DIR ...] [--stats FILE]

--stats is Claude Code's own stats-cache.json, the file behind /stats. It defaults to the active profile's when no
--projects are given. Claude Code deletes old transcripts, but that file keeps a line per day, so All time fills in the
days whose transcripts are gone from it. See history_of.

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
from datetime import date, timedelta

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
WINDOW_DAYS = {"7d": 7, "30d": 30}
WINDOW_LABELS = {"all": "all time", "7d": "last 7 days", "30d": "last 30 days"}
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


def stats_cache_paths(profiles):
    """Each read profile's stats-cache.json, the file /stats reads. Every profile's, not just the active one: Herdr starts
    the helper without the shell's $CLAUDE_CONFIG_DIR, so "active" would depend on how Herdr was launched."""
    return [os.path.join(os.path.dirname(p["dir"]), "stats-cache.json") for p in profiles]


def merge_history(found):
    """Several load_history results as one: per day, the most of each count, so a copied file adds nothing. Two real
    profiles busy on the same day count as the busier one."""
    found = [h for h in found if h]
    if not found:
        return None
    days = {}
    for h in found:
        for day, d in h["days"].items():
            have = days.setdefault(day, {"sessions": 0, "tools": 0, "tokens": 0})
            for k in have:
                have[k] = max(have[k], d[k])
    return {"days": days, "unplaced": max(h["unplaced"] for h in found), "longest": max(h["longest"] for h in found)}


def load_history(path):
    """Claude Code's stats-cache.json as {"days": {date: {...}}, "unplaced", "longest"}, or None. Only counts are taken
    from it. Its tokens are counted Claude Code's way, which see history_of."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    days = {}
    for d in raw.get("dailyActivity") or []:
        if isinstance(d, dict) and isinstance(d.get("date"), str):
            days[d["date"]] = {"sessions": wrapped._int(d.get("sessionCount")), "tools": wrapped._int(d.get("toolCallCount")),
                               "tokens": 0}
    placed = 0
    for d in raw.get("dailyModelTokens") or []:
        if isinstance(d, dict) and isinstance(d.get("tokensByModel"), dict) and isinstance(d.get("date"), str):
            n = sum(wrapped._int(v) for v in d["tokensByModel"].values())
            days.setdefault(d["date"], {"sessions": 0, "tools": 0, "tokens": 0})["tokens"] = n
            placed += n
    total = 0
    for u in (raw.get("modelUsage") or {}).values():
        if isinstance(u, dict):
            total += sum(wrapped._int(u.get(k)) for k in ("inputTokens", "outputTokens", "cacheReadInputTokens",
                                                            "cacheCreationInputTokens"))
    longest = raw.get("longestSession") if isinstance(raw.get("longestSession"), dict) else {}
    return {"days": days, "unplaced": max(0, total - placed), "longest": wrapped._int(longest.get("duration"))}


def history_of(runs, history):
    """What the stats cache adds to these runs: for each day where it knows of more sessions than the transcripts do,
    because Claude Code has deleted them, its day replaces theirs. Returns None when it adds nothing.

    Tokens are counted /stats' way on both sides: a reply's usage once per streamed line. Cost and the cache share for
    the filled days follow the transcripts' own rates."""
    if not history:
        return None
    by_day = {}
    for r in runs:
        d = by_day.setdefault((r["started"] or "")[:10], {"sessions": set(), "tokens": 0, "tools": 0})
        if not r.get("is_subagent"):
            d["sessions"].add(r["run_id"])
        d["tokens"] += r["tokens"]
        d["tools"] += r["tool_calls"]
    scale = 1.0
    added = {"sessions": 0, "tokens": 0, "tools": 0, "days": {}}
    for day, h in history["days"].items():
        mine = by_day.get(day) or {"sessions": (), "tokens": 0, "tools": 0}
        if h["sessions"] <= len(mine["sessions"]):
            continue
        tokens = max(0, round(h["tokens"] * scale) - mine["tokens"])
        added["sessions"] += h["sessions"] - len(mine["sessions"])
        added["tools"] += max(0, h["tools"] - mine["tools"])
        added["tokens"] += tokens
        added["days"][day] = tokens
    if not added["days"]:
        return None
    added["tokens"] += round(history["unplaced"] * scale)
    added["longest"] = history["longest"]
    return added


def with_history(stats, added):
    """Folds history_of's days into wrapped.py's stats, and works the punchlines out again."""
    total = stats["total_tokens"]
    share = stats["tokens_from_cache"] / total if total else 0.0
    rate = stats["cost_usd"] / total if total else 0.0
    stats["total_tokens"] += added["tokens"]
    stats["tokens_from_cache"] += round(added["tokens"] * share)
    stats["tokens_new"] = stats["total_tokens"] - stats["tokens_from_cache"]
    stats["cost_usd"] = round(stats["cost_usd"] + added["tokens"] * rate, 6)
    stats["sessions"] += added["sessions"]
    stats["tool_calls"] += added["tools"]
    days = {d["date"]: d["tokens"] for d in stats["activity"]}
    for day, tokens in added["days"].items():
        days[day] = days.get(day, 0) + tokens
    stats["activity"] = [{"date": d, "tokens": n} for d, n in sorted(days.items())]
    top = max(days.items(), key=lambda kv: (kv[1], kv[0]))
    stats["busiest_day"] = {"date": top[0], "tokens": top[1], "label": wrapped.fmt_day(top[0])}
    if added["longest"] > (stats["longest_session"] or {}).get("span_ms", 0):
        stats["longest_session"] = {"run_id": None, "span_ms": added["longest"], "human": wrapped.fmt_duration(added["longest"])}
    stats["history_days"] = len(added["days"])
    stats["insights"] = wrapped.insights(stats)
    return stats


def card_stats(runs, window, jar=False, made_up=False, history=None):
    """wrapped.py's stats for these runs, with what goes on the card beside them. The Swear Jar only joins them when `jar`
    is set, so with sharing off there's nothing in the stats for the card or a caption to show. `history` is
    load_history's, given for All time only: the peak, the Bottleneck, the jar and the top tool stay the transcripts'."""
    stats = wrapped.wrapped_stats(runs, window)
    added = history_of(runs, history)
    if added:
        stats = with_history(stats, added)
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


def filter_days(runs, days, today):
    """The runs that started in the last `days` days, today included, by the UTC date wrapped.py files a session under."""
    first = (today - timedelta(days=days - 1)).isoformat()
    return [r for r in runs if (r["started"] or "")[:10] >= first]


def today_of(runs, demo):
    """Today. A demo's today is the newest day in its made-up runs, so its windows aren't empty."""
    if demo and runs:
        return date.fromisoformat(max((r["started"] or "")[:10] for r in runs))
    return date.today()


def window_runs(runs, key, demo):
    return runs if key == "all" else filter_days(runs, WINDOW_DAYS[key], today_of(runs, demo))


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


def view_of(runs, window, made_up=False, history=None):
    """What the overlay draws, formatted the way the card formats it. Names, paths, ids and prompts stay out. `share` has
    the caption without the Swear Jar, and `shareJar` the one with it, for when the person has turned sharing on."""
    stats = card_stats(runs, window, made_up=made_up, history=history)
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
        "historyDays": stats.get("history_days", 0),
        "punchline": stats["insights"][0]["line"] if stats["insights"] else "",
        "share": post_of(stats),
        "shareJar": post_of(card_stats(runs, window, jar=True, made_up=made_up, history=history)),
    }


BACKUP_WORDS = ("copy", "backup", "bak")


def is_backup(name):
    """A copied Claude folder, by its name: ".claude copy", ".claude-copy", ".claude-BACKUP", ".claude-backup-20260212".
    Real profiles are named for what they're for, like ".claude-work" or ".claude-eval"."""
    return any(word in name.lower() for word in BACKUP_WORDS)


def settings_of(cache_path):
    """settings.json beside the cache, in Herdr's data folder: {"includeBackups": true} reads backup copies too."""
    if not cache_path:
        return {}
    try:
        with open(os.path.join(os.path.dirname(cache_path), "settings.json"), encoding="utf-8") as f:
            found = json.load(f)
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def profiles_for(args):
    backups = settings_of(args.cache).get("includeBackups") is True
    return [p for p in wrapped.discover_profiles() if p["files"] and (backups or p["active"] or not is_backup(p["name"]))]


def roots_for(args):
    if args.projects:
        return args.projects
    return [p["dir"] for p in profiles_for(args)]


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


def history_for(args):
    if args.demo:
        return None
    if args.stats:
        return load_history(args.stats)
    if args.projects:
        return None
    return merge_history([load_history(path) for path in stats_cache_paths(profiles_for(args))])


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
    history = history_for(args)
    return {"partial": False, **progress, "windows": {
        key: view_of(window_runs(runs, key, args.demo), WINDOW_LABELS[key], args.demo, history if key == "all" else None)
        for key in ("7d", "30d", "all")
    }}


def save(args):
    runs, _progress = gather(args, SAVE_BUDGET)
    label = WINDOW_LABELS[args.window]
    history = history_for(args) if args.window == "all" else None
    runs = window_runs(runs, args.window, args.demo)
    if not runs:
        return {"error": "Nothing to save: this window has no sessions."}
    html = wrapped.html_card(card_stats(runs, label, jar=args.jar, made_up=args.demo, history=history), "square")
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
    parser.add_argument("--stats")
    parser.add_argument("--budget", type=float, default=READ_BUDGET)
    parser.add_argument("--window", choices=["all", "7d", "30d"], default="all")
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
