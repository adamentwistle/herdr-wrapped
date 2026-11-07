#!/usr/bin/env python3
"""Checks for helper.py and the mod's changes to wrapped.py, on hand-made runs and the bundled fixtures only.

  python tests/test_helper.py
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "mods", "wrapped")
sys.path.insert(0, MOD)
import helper  # noqa: E402
import wrapped  # noqa: E402


# A stand-in for a headless browser: writes a blank PNG the size --window-size asks for at 2x, or not, then exits or
# keeps running. Filled in with (writes, then_waits).
FAKE_BROWSER = r'''import struct, sys, time, zlib
writes, then_waits = %r, %r
args = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
w, h = (2 * int(n) for n in args["window-size"].split(","))
def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
if writes:
    rows = (b"\x00" + b"\x00" * (3 * w)) * h
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(rows, 1)) + chunk(b"IEND", b"")
    with open(args["screenshot"], "wb") as f:
        f.write(png)
if then_waits:
    time.sleep(60)
'''


def run(run_id, started, ended):
    return {"run_id": run_id, "started": started, "ended": ended}


def at(clock):
    return "2026-07-01T%s:00+00:00" % clock


def peak(*runs):
    return helper.peak_agents(list(runs))["count"]


class Peak(unittest.TestCase):
    def test_apart(self):
        self.assertEqual(peak(run("a", at("09:00"), at("10:00")), run("b", at("11:00"), at("12:00"))), 1)

    def test_touching_sessions_do_not_overlap(self):
        self.assertEqual(peak(run("a", at("09:00"), at("10:00")), run("b", at("10:00"), at("11:00"))), 1)

    def test_a_chain_of_touching_sessions_stays_at_one(self):
        self.assertEqual(peak(run("a", at("09:00"), at("10:00")), run("b", at("10:00"), at("11:00")),
                              run("c", at("11:00"), at("12:00"))), 1)

    def test_overlapping(self):
        self.assertEqual(peak(run("a", at("09:00"), at("10:01")), run("b", at("10:00"), at("11:00"))), 2)

    def test_nested(self):
        self.assertEqual(peak(run("a", at("09:00"), at("12:00")), run("b", at("10:00"), at("11:00")),
                              run("c", at("10:30"), at("10:45"))), 3)

    def test_staggered_pairs_never_reach_three(self):
        self.assertEqual(peak(run("a", at("09:00"), at("10:00")), run("b", at("09:30"), at("10:30")),
                              run("c", at("10:15"), at("11:00"))), 2)

    def test_same_start_and_end(self):
        self.assertEqual(peak(run("a", at("09:00"), at("10:00")), run("b", at("09:00"), at("10:00"))), 2)

    def test_one_timestamp_alone(self):
        self.assertEqual(peak(run("a", at("09:00"), at("09:00"))), 1)

    def test_one_timestamp_inside_a_session(self):
        self.assertEqual(peak(run("a", at("09:00"), at("10:00")), run("b", at("09:30"), at("09:30"))), 2)

    def test_one_timestamp_touching_a_session_end(self):
        self.assertEqual(peak(run("a", at("09:00"), at("10:00")), run("b", at("10:00"), at("10:00"))), 1)

    def test_a_subagent_folds_into_its_session(self):
        self.assertEqual(peak(run("a", at("09:00"), at("10:00")), run("a", at("09:10"), at("09:20")),
                              run("a", at("09:15"), at("09:25"))), 1)

    def test_a_subagent_past_its_session_extends_it(self):
        self.assertEqual(peak(run("a", at("09:00"), at("10:00")), run("a", at("09:50"), at("11:00")),
                              run("b", at("10:30"), at("10:40"))), 2)

    def test_different_offsets_compare_as_moments(self):
        self.assertEqual(peak(run("a", "2026-07-01T09:00:00+00:00", "2026-07-01T10:00:00+00:00"),
                              run("b", "2026-07-01T10:30:00+01:00", "2026-07-01T11:30:00+01:00")), 2)

    def test_no_times(self):
        self.assertIsNone(helper.peak_agents([run("a", None, None)]))
        self.assertIsNone(helper.peak_agents([]))

    def test_says_when(self):
        got = helper.peak_agents([run("a", "2026-07-02T09:00:00+00:00", "2026-07-02T10:00:00+00:00"),
                                  run("b", "2026-07-02T09:30:00+00:00", "2026-07-02T09:40:00+00:00")])
        self.assertEqual(got, {"count": 2, "date": "2026-07-02", "label": "jul 2"})


def assistant(uuid, session, stamp, cwd="/work/secret-project"):
    return json.dumps({"type": "assistant", "uuid": uuid, "requestId": "r" + uuid, "sessionId": session, "timestamp": stamp,
                       "cwd": cwd, "message": {"model": "claude-opus-4-8", "usage": {"input_tokens": 100, "output_tokens": 50},
                                               "content": [{"type": "tool_use", "id": "t" + uuid, "name": "Bash"}]}})


class Folder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.projects = os.path.join(self.dir, "projects")
        self.cache = os.path.join(self.dir, "data", "wrapped", "runs-cache.json")

    def write(self, name, session, start, end, root=None):
        path = os.path.join(root or self.projects, "proj", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(assistant(name + "1", session, start) + "\n" + assistant(name + "2", session, end) + "\n")
        return path

    def args(self, budget=15.0, **more):
        values = {"demo": False, "projects": [self.projects], "cache": self.cache, "budget": budget, "stats": None, "window": "all", "out": None}
        return mock.Mock(**{**values, **more})

    def three(self):
        self.write("a.jsonl", "a", "2026-07-01T09:00:00Z", "2026-07-01T10:00:00Z")
        self.write("b.jsonl", "b", "2026-07-01T09:30:00Z", "2026-07-01T09:40:00Z")
        self.write("c.jsonl", "c", "2026-07-02T09:30:00Z", "2026-07-02T09:40:00Z")

    def test_second_read_parses_nothing(self):
        self.three()
        first = helper.read(self.args())
        with mock.patch.object(helper.wrapped, "parse_run", side_effect=AssertionError("parsed again")):
            second = helper.read(self.args())
        self.assertEqual(first, second)
        self.assertEqual(second["windows"]["all"]["sessions"], "3")

    def test_a_changed_file_is_parsed_again_and_only_it(self):
        self.three()
        helper.read(self.args())
        self.write("b.jsonl", "b", "2026-07-01T09:30:00Z", "2026-07-01T11:00:00Z")
        real, seen = wrapped.parse_run, []
        with mock.patch.object(helper.wrapped, "parse_run", side_effect=lambda p, *rest: seen.append(p) or real(p, *rest)):
            helper.read(self.args())
        self.assertEqual([os.path.basename(p) for p in seen], ["b.jsonl"])

    def test_a_deleted_file_leaves_the_cache(self):
        self.three()
        helper.read(self.args())
        os.remove(os.path.join(self.projects, "proj", "c.jsonl"))
        self.assertEqual(helper.read(self.args())["windows"]["all"]["sessions"], "2")

    def test_a_new_wrapped_py_starts_again(self):
        self.three()
        helper.read(self.args())
        real, seen = wrapped.parse_run, []
        with mock.patch.object(helper, "parser_id", return_value="other"), \
                mock.patch.object(helper.wrapped, "parse_run", side_effect=lambda p, *rest: seen.append(p) or real(p, *rest)):
            helper.read(self.args())
        self.assertEqual(len(seen), 3)

    def test_a_spent_budget_goes_in_passes_and_ends_the_same(self):
        self.three()
        whole = helper.read(self.args(cache=os.path.join(self.dir, "other.json")))
        progress = []
        for _ in range(5):
            reply = helper.read(self.args(budget=0))
            progress.append(reply["read"])
            if not reply["partial"]:
                break
        self.assertEqual(progress, [1, 2, 3])
        self.assertEqual(reply, whole)

    def test_a_partial_read_shows_no_numbers(self):
        self.three()
        reply = helper.read(self.args(budget=0))
        self.assertTrue(reply["partial"])
        self.assertNotIn("windows", reply)

    def test_a_copied_folder_counts_once(self):
        self.three()
        copy = os.path.join(self.dir, "copy")
        shutil.copytree(self.projects, copy)
        reply = helper.read(self.args(projects=[self.projects, copy]))
        self.assertEqual(reply["windows"]["all"]["sessions"], "3")
        self.assertEqual(reply["windows"]["all"]["peak"]["count"], 2)

    def test_two_folders_with_different_sessions_add_up(self):
        self.three()
        other = os.path.join(self.dir, "other")
        self.write("d.jsonl", "d", "2026-07-03T09:30:00Z", "2026-07-03T09:40:00Z", root=other)
        self.assertEqual(helper.read(self.args(projects=[self.projects, other]))["windows"]["all"]["sessions"], "4")

    def test_a_file_too_big_is_left_out_and_counted(self):
        self.three()
        with mock.patch.object(helper, "MAX_FILE", 1):
            reply = helper.read(self.args())
        self.assertEqual(reply["leftOut"], 3)
        self.assertTrue(reply["windows"]["all"]["empty"])

    def test_an_unreadable_file_does_not_stop_the_rest(self):
        self.three()
        real = wrapped.parse_run

        def fussy(path, *rest):
            if path.endswith("b.jsonl"):
                raise OSError("no")
            return real(path, *rest)

        with mock.patch.object(helper.wrapped, "parse_run", side_effect=fussy):
            reply = helper.read(self.args())
        self.assertEqual(reply["leftOut"], 1)
        self.assertEqual(reply["windows"]["all"]["sessions"], "2")

    def test_nothing_to_read_is_named(self):
        os.makedirs(self.projects)
        self.assertIn("No Claude Code transcripts found", helper.read(self.args())["error"])

    def test_no_names_or_paths_in_the_reply_or_the_cache(self):
        self.three()
        reply = json.dumps(helper.read(self.args()))
        for secret in ("secret-project", self.dir, "\"run_id\"", "top_project"):
            self.assertNotIn(secret, reply)
        with open(self.cache, encoding="utf-8") as f:
            self.assertNotIn("secret-project", f.read())

    def test_the_last_7_and_30_days_count_back_from_today(self):
        self.three()
        with mock.patch.object(helper, "today_of", return_value=date(2026, 7, 8)):
            windows = helper.read(self.args())["windows"]
        self.assertEqual(windows["7d"]["window"], "last 7 days")
        self.assertEqual(windows["7d"]["sessions"], "1")
        self.assertEqual(windows["30d"]["sessions"], "3")
        with mock.patch.object(helper, "today_of", return_value=date(2026, 9, 1)):
            windows = helper.read(self.args())["windows"]
        self.assertEqual(windows["7d"], {"window": "last 7 days", "empty": True})
        self.assertEqual(windows["30d"], {"window": "last 30 days", "empty": True})
        self.assertEqual(windows["all"]["sessions"], "3")

    def test_an_empty_home_finds_nothing(self):
        home = os.path.join(self.dir, "home")
        os.makedirs(home)
        env = {"HOME": home, "USERPROFILE": home}
        with mock.patch.dict(os.environ, env), mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}):
            self.assertIn("error", helper.read(self.args(projects=None)))

    def test_claude_folders_are_found_and_a_copy_adds_nothing(self):
        home = os.path.join(self.dir, "home")
        self.write("a.jsonl", "a", "2026-07-01T09:00:00Z", "2026-07-01T10:00:00Z", root=os.path.join(home, ".claude", "projects"))
        self.write("a.jsonl", "a", "2026-07-01T09:00:00Z", "2026-07-01T10:00:00Z", root=os.path.join(home, ".claude-copy", "projects"))
        os.makedirs(os.path.join(home, ".claude-empty", "projects"))
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home, "CLAUDE_CONFIG_DIR": ""}):
            reply = helper.read(self.args(projects=None))
        self.assertEqual(reply["total"], 1)
        self.assertEqual(reply["windows"]["all"]["peak"]["count"], 1)

    def test_save_falls_back_to_html_with_no_browser(self):
        self.three()
        out = os.path.join(self.dir, "Downloads")
        with mock.patch.object(helper.wrapped, "find_browser", return_value=None):
            reply = helper.save(self.args(out=out))
        self.assertEqual(reply["kind"], "html")
        self.assertTrue(reply["noBrowser"])
        self.assertIn("no chromium-family browser", reply["why"])
        with open(reply["saved"], encoding="utf-8") as f:
            self.assertIn("most agents at once", f.read())

    def browser(self, writes, then_waits):
        """A stand-in browser: it writes the screenshot or not, then exits or keeps running."""
        path = os.path.join(self.dir, "browser")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!%s\n%s" % (sys.executable, FAKE_BROWSER % (writes, then_waits)))
        os.chmod(path, 0o755)
        return path

    def test_a_hung_browser_falls_back_to_html(self):
        self.three()
        self.assertLess(wrapped._RENDER_SECONDS + helper.SAVE_BUDGET, 28)
        with mock.patch.object(helper.wrapped, "find_browser", return_value=self.browser(False, True)), \
                mock.patch.object(helper.wrapped, "_RENDER_SECONDS", 1):
            reply = helper.save(self.args(out=self.dir))
        self.assertEqual(reply["kind"], "html")
        self.assertFalse(reply["noBrowser"])
        self.assertIn("more than 1 seconds", reply["why"])

    def test_a_browser_that_keeps_running_after_its_screenshot_still_saves_the_png(self):
        # Chrome 145 on macOS writes the screenshot and doesn't exit.
        self.three()
        with mock.patch.object(helper.wrapped, "find_browser", return_value=self.browser(True, True)), \
                mock.patch.object(helper.wrapped, "_RENDER_SECONDS", 10):
            reply = helper.save(self.args(out=self.dir))
        self.assertEqual(reply["kind"], "png")
        with open(reply["saved"], "rb") as f:
            head = f.read(24)
        self.assertEqual(struct.unpack(">II", head[16:24]), (2160, 2160))

    def test_an_old_card_under_the_same_name_is_not_taken_for_the_new_one(self):
        self.three()
        with mock.patch.object(helper.wrapped, "find_browser", return_value=self.browser(True, False)):
            first = helper.save(self.args(out=self.dir))
        with mock.patch.object(helper.wrapped, "find_browser", return_value=self.browser(False, True)), \
                mock.patch.object(helper.wrapped, "_RENDER_SECONDS", 1):
            second = helper.save(self.args(out=self.dir))
        self.assertEqual(first["kind"], "png")
        self.assertEqual(second["kind"], "html")

    def test_a_project_folder_name_stays_out_of_the_cache(self):
        root = os.path.join(self.projects, "-Users-someone-Projects-secret-project")
        self.write("a.jsonl", "a", "2026-07-01T09:00:00Z", "2026-07-01T10:00:00Z", root=root)
        first = helper.read(self.args())
        with open(self.cache, encoding="utf-8") as f:
            cache = f.read()
        self.assertNotIn("secret-project", cache)
        self.assertNotIn(self.dir, cache)
        with mock.patch.object(helper.wrapped, "parse_run", side_effect=AssertionError("parsed again")):
            self.assertEqual(helper.read(self.args()), first)

    def test_save_says_so_when_the_window_is_empty(self):
        self.three()
        with mock.patch.object(helper, "today_of", return_value=date(2026, 9, 1)):
            reply = helper.save(self.args(out=self.dir, window="7d"))
        self.assertIn("Nothing to save", reply["error"])

    def test_a_png_reply_names_the_png(self):
        self.three()
        with mock.patch.object(helper.wrapped, "render_png", return_value=(True, "ok")):
            reply = helper.save(self.args(out=self.dir))
        self.assertEqual(reply["kind"], "png")
        self.assertTrue(reply["saved"].endswith(".png"))


def stats_cache(path, days, longest=0, unplaced=0):
    """A stats-cache.json like Claude Code's: days are (date, sessions, tools, tokens)."""
    placed = sum(d[3] for d in days)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 5,
                   "dailyActivity": [{"date": d, "sessionCount": n, "toolCallCount": t, "messageCount": 1} for d, n, t, _ in days],
                   "dailyModelTokens": [{"date": d, "tokensByModel": {"claude-opus-4-8": k}} for d, _, _, k in days],
                   "modelUsage": {"claude-opus-4-8": {"inputTokens": placed + unplaced, "outputTokens": 0,
                                                      "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}},
                   "longestSession": {"duration": longest}}, f)
    return path


class History(unittest.TestCase):
    """Claude Code deletes old transcripts, but /stats keeps a line per day in stats-cache.json. All time fills the days
    whose transcripts are gone from it. Each file self.write makes is one session of 300 tokens and 2 tool calls."""

    setUp_folder = Folder.setUp
    write = Folder.write
    args = Folder.args

    def setUp(self):
        self.setUp_folder()
        self.write("c.jsonl", "c", "2026-07-02T09:30:00Z", "2026-07-02T09:40:00Z")
        self.stats = os.path.join(self.dir, "stats-cache.json")

    def all_time(self, **more):
        with mock.patch.object(helper, "today_of", return_value=date(2026, 7, 2)):
            return helper.read(self.args(stats=self.stats, **more))["windows"]

    def test_a_day_with_no_transcripts_comes_from_the_stats_file(self):
        # 07-02 is in both, and /stats has it at twice the transcripts' tokens, so 07-01's 1200 count as 600.
        stats_cache(self.stats, [("2026-07-01", 2, 5, 1200), ("2026-07-02", 1, 2, 600)])
        view = self.all_time()["all"]
        self.assertEqual(view["sessions"], "3")
        self.assertEqual(view["toolCalls"], "7")
        self.assertEqual((view["tokens"], view["unit"]), ("900", ""))
        self.assertEqual(view["historyDays"], 1)
        self.assertEqual(view["strip"]["from"], "jul 1")
        self.assertEqual(view["busiest"]["label"], "jul 1")

    def test_a_day_the_transcripts_have_in_full_is_not_counted_twice(self):
        stats_cache(self.stats, [("2026-07-02", 1, 2, 600)])
        view = self.all_time()["all"]
        self.assertEqual((view["sessions"], view["tokens"], view["historyDays"]), ("1", "300", 0))

    def test_a_day_with_some_transcripts_gone_takes_the_fuller_count(self):
        self.write("d.jsonl", "d", "2026-07-01T09:00:00Z", "2026-07-01T09:10:00Z")
        stats_cache(self.stats, [("2026-07-01", 3, 6, 1800), ("2026-07-02", 1, 2, 600)])
        view = self.all_time()["all"]
        self.assertEqual(view["sessions"], "4")
        self.assertEqual(view["toolCalls"], "8")
        self.assertEqual((view["tokens"], view["unit"]), ("1.2", "K"))

    def test_tokens_with_no_day_still_count_toward_all_time(self):
        stats_cache(self.stats, [("2026-07-01", 2, 5, 1200), ("2026-07-02", 1, 2, 600)], unplaced=200)
        view = self.all_time()["all"]
        self.assertEqual((view["tokens"], view["unit"]), ("1.0", "K"))

    def test_the_longest_session_can_come_from_the_stats_file(self):
        stats_cache(self.stats, [("2026-06-01", 1, 0, 0), ("2026-07-02", 1, 2, 600)], longest=(4 * 24 + 23) * 3600000 + 6 * 60000)
        self.assertEqual(self.all_time()["all"]["longest"], wrapped.fmt_duration((4 * 24 + 23) * 3600000 + 6 * 60000))

    def test_only_all_time_uses_it(self):
        stats_cache(self.stats, [("2026-07-01", 2, 5, 1200), ("2026-07-02", 1, 2, 600)])
        windows = self.all_time()
        self.assertEqual(windows["7d"]["sessions"], "1")
        self.assertEqual(windows["30d"]["historyDays"], 0)

    def test_a_missing_or_broken_stats_file_changes_nothing(self):
        self.assertEqual(self.all_time()["all"]["sessions"], "1")
        with open(self.stats, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(self.all_time()["all"]["sessions"], "1")

    def test_with_no_projects_named_it_reads_the_active_profiles_file(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/somewhere/.claude-work"}):
            self.assertEqual(helper.stats_cache_path(), "/somewhere/.claude-work/stats-cache.json")


class Streamed(unittest.TestCase):
    def test_a_reply_split_into_lines_counts_its_usage_once(self):
        # Claude Code writes one line per content block, and every line repeats the reply's whole usage. /stats adds them
        # all up. A reply is counted once here.
        usage = {"input_tokens": 10, "output_tokens": 90, "cache_read_input_tokens": 900}
        lines = [{"type": "assistant", "uuid": "u%d" % i, "requestId": "req1", "sessionId": "s", "timestamp": "2026-07-01T09:00:0%dZ" % i,
                  "message": {"id": "msg1", "model": "claude-opus-4-8", "usage": usage, "content": [block]}}
                 for i, block in enumerate([{"type": "thinking"}, {"type": "text", "text": "ok"},
                                            {"type": "tool_use", "id": "t1", "name": "Bash"}])]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(json.dumps(x) for x in lines))
            run = wrapped.parse_run(path)
        self.assertEqual(run["tokens"], 1000)
        self.assertEqual(run["tool_calls"], 1)


class Demo(unittest.TestCase):
    def reply(self):
        args = mock.Mock(demo=True, projects=None, stats=None, cache=None, budget=15.0)
        return helper.read(args)

    def test_believable_for_a_heavy_user(self):
        view = self.reply()["windows"]["all"]
        self.assertEqual(view["unit"], "B")
        self.assertLess(float(view["tokens"]), 10)
        self.assertEqual(view["peak"]["count"], 12)
        self.assertTrue(view["bottleneck"])
        self.assertTrue(10 <= view["jar"]["count"] <= 60, view["jar"])

    def test_the_same_every_time(self):
        self.assertEqual(self.reply(), self.reply())

    def test_the_windows_end_on_the_samples_newest_day(self):
        windows = self.reply()["windows"]
        self.assertEqual(windows["7d"]["window"], "last 7 days")
        self.assertFalse(windows["7d"]["empty"])
        self.assertFalse(windows["30d"]["empty"])

    def test_the_card_says_it_is_sample_data(self):
        with tempfile.TemporaryDirectory() as out, mock.patch.object(helper.wrapped, "find_browser", return_value=None):
            reply = helper.save(mock.Mock(demo=True, projects=None, stats=None, cache=None, window="all", jar=False, out=out))
            with open(reply["saved"], encoding="utf-8") as f:
                self.assertIn("sample data", f.read())

    def test_a_real_card_does_not(self):
        s = helper.card_stats(wrapped.ingest_dir(os.path.join(MOD, "examples", "runs")), "all time")
        self.assertNotIn("sample data", wrapped.html_card(s))
        self.assertNotIn("sample data", wrapped.share_text(s)["x"])

    def test_the_caption_says_so_too(self):
        self.assertIn("(sample data)", self.reply()["windows"]["all"]["share"]["caption"])

    def test_wrapped_card_demo_uses_it(self):
        with tempfile.TemporaryDirectory() as out:
            page = os.path.join(out, "card.html")
            done = subprocess.run([sys.executable, "-B", os.path.join(MOD, "wrapped.py"), "card", "--demo", "--out", page],
                                  capture_output=True, text=True, cwd=out)
            self.assertEqual(done.returncode, 0, done.stderr)
            with open(page, encoding="utf-8") as f:
                self.assertIn("sample data", f.read())


class Card(unittest.TestCase):
    def stats(self, count=4):
        s = wrapped.wrapped_stats(wrapped.ingest_dir(os.path.join(MOD, "examples", "runs")))
        s["peak"] = {"count": count, "date": "2026-07-01", "label": "jul 1"}
        return s

    def test_the_tile_says_what_it_counts(self):
        card = wrapped.html_card(self.stats())
        self.assertIn("most agents at once", card)
        self.assertIn("sessions open together, idle ones count", card)
        self.assertIn('<div class="v tabnum">4</div>', card)

    def test_no_peak_is_the_source_card(self):
        s = self.stats()
        del s["peak"]
        self.assertNotIn('class="tile agents"', wrapped.html_card(s))

    def test_the_caption_says_what_it_counts(self):
        caption = wrapped.share_text(self.stats())["x"]
        self.assertIn("most agents at once: 4, idle ones included.", caption)
        self.assertIn(wrapped.REPO, caption)

    def test_a_big_history_still_fits_a_post(self):
        s = self.stats(123)
        s.update(total_tokens=123400000000, sessions=12345, tool_calls=2345678)
        s["top_tool"] = {"name": "mcp__server__read_file", "count": 1234567}
        s["bottleneck"] = {"worked": "12345h", "waited": "1234h"}
        s["jar"] = {"fine": "$1,234.50", "count": 4938, "top": "f***"}
        s["sample"] = True
        self.assertLessEqual(len(wrapped.share_text(s)["x"]), 280)

    def test_the_post_link_carries_the_caption(self):
        reply = helper.view_of(wrapped.ingest_dir(os.path.join(MOD, "examples", "runs")), "all time")
        url = reply["share"]["postUrl"]
        self.assertTrue(url.startswith("https://twitter.com/intent/tweet?text="))
        self.assertNotIn("\n", url)


class Run(unittest.TestCase):
    def test_the_way_the_mod_runs_it(self):
        made = os.path.join(MOD, "__pycache__")
        existed = os.path.isdir(made)
        done = subprocess.run([sys.executable, "-I", "-S", "-B", os.path.join(MOD, "helper.py"), "read", "--demo"],
                              capture_output=True, cwd=tempfile.gettempdir())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertLess(len(done.stdout), 8 * 1024 * 1024)
        self.assertTrue(done.stdout.isascii())
        self.assertFalse(json.loads(done.stdout)["partial"])
        self.assertEqual(os.path.isdir(made), existed)

    def test_wrapped_selftest(self):
        done = subprocess.run([sys.executable, "-B", os.path.join(MOD, "wrapped.py"), "selftest"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout)


if __name__ == "__main__":
    unittest.main()
