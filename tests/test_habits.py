#!/usr/bin/env python3
"""Checks for real session counts, the Bottleneck and the Swear Jar, on hand-made records only.

  python3 tests/test_habits.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "mods", "wrapped")
sys.path.insert(0, MOD)
import habits  # noqa: E402
import helper  # noqa: E402
import wrapped  # noqa: E402

CAP_MIN = habits.WAIT_CAP_MS // 60000
MIN = 60000


def at(minute):
    """A timestamp `minute` minutes after 09:00, on any scale."""
    hours, minutes = divmod(int(minute), 60)
    day, hours = divmod(9 + hours, 24)
    return "2026-07-%02dT%02d:%02d:00.000Z" % (1 + day, hours, minutes)


def prompt(minute, text="go on", source="typed", **more):
    record = {"type": "user", "timestamp": at(minute), "message": {"role": "user", "content": text}, **more}
    if source:
        record["promptSource"] = source
    return record


def tool_result(minute, text="ok"):
    return {"type": "user", "timestamp": at(minute), "toolUseResult": {"stdout": text},
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": text}]}}


def answer(minute, text="done", **more):
    return {"type": "assistant", "uuid": "a%s" % minute, "timestamp": at(minute),
            "message": {"content": [{"type": "text", "text": text}]}, **more}


class Bottleneck(unittest.TestCase):
    def test_worked_runs_from_the_prompt_to_the_end_of_the_turn(self):
        got = habits.read([prompt(0), answer(1), tool_result(2), answer(5)])
        self.assertEqual(got["worked_ms"], 5 * MIN)

    def test_waited_runs_from_the_end_of_the_turn_to_the_next_prompt(self):
        got = habits.read([prompt(0), answer(5), prompt(7), answer(8)])
        self.assertEqual(got["waited_ms"], 2 * MIN)
        self.assertEqual(got["worked_ms"], 6 * MIN)
        self.assertEqual(got["waits"], 1)

    def test_a_tool_result_is_not_an_answer(self):
        got = habits.read([prompt(0), answer(1), tool_result(4), answer(5)])
        self.assertEqual(got["waited_ms"], 0)

    def test_a_gap_at_the_cap_is_waiting(self):
        got = habits.read([prompt(0), answer(1), prompt(1 + CAP_MIN)])
        self.assertEqual(got["waited_ms"], habits.WAIT_CAP_MS)
        self.assertEqual(got["away"], 0)

    def test_a_gap_over_the_cap_is_away_not_waiting(self):
        got = habits.read([prompt(0), answer(1), prompt(2 + CAP_MIN)])
        self.assertEqual(got["waited_ms"], 0)
        self.assertEqual(got["waits"], 0)
        self.assertEqual(got["away"], 1)

    def test_an_overnight_gap_adds_nothing(self):
        got = habits.read([prompt(0), answer(5), prompt(14 * 60), answer(14 * 60 + 2)])
        self.assertEqual(got["waited_ms"], 0)
        self.assertEqual(got["away"], 1)
        self.assertEqual(got["worked_ms"], 7 * MIN)

    def test_the_slowest_answer_stays_under_the_cap(self):
        got = habits.read([prompt(0), answer(1), prompt(4), answer(5), prompt(5 + CAP_MIN + 10), answer(100), prompt(112)])
        self.assertEqual(got["slowest_ms"], 12 * MIN)

    def test_a_session_ending_with_no_reply_adds_no_wait(self):
        got = habits.read([prompt(0), answer(5)])
        self.assertEqual(got["worked_ms"], 5 * MIN)
        self.assertEqual((got["waited_ms"], got["waits"], got["away"]), (0, 0, 0))

    def test_a_prompt_nobody_answered_adds_no_work(self):
        got = habits.read([prompt(0), answer(5), prompt(6)])
        self.assertEqual(got["worked_ms"], 5 * MIN)

    def test_a_program_answering_is_not_you_answering(self):
        got = habits.read([prompt(0, source="sdk"), answer(5), prompt(10, source="sdk"), answer(12)])
        self.assertEqual(got["worked_ms"], 7 * MIN)
        self.assertEqual(got["waited_ms"], 0)

    def test_a_background_notice_starts_a_turn_but_is_not_you(self):
        got = habits.read([prompt(0), answer(5), prompt(9, "<task-notification>x</task-notification>", source=None), answer(10)])
        self.assertEqual(got["waited_ms"], 0)
        self.assertEqual(got["worked_ms"], 6 * MIN)

    def test_a_slash_command_is_you_answering(self):
        got = habits.read([prompt(0), answer(5), prompt(8, "<command-name>/compact</command-name>", source=None), answer(9)])
        self.assertEqual(got["waited_ms"], 3 * MIN)

    def test_silence_over_the_cap_inside_a_turn_is_not_work(self):
        got = habits.read([prompt(0), answer(2), tool_result(2 + CAP_MIN + 300), answer(3 + CAP_MIN + 300)])
        self.assertEqual(got["worked_ms"], 3 * MIN)

    def test_subagent_records_are_left_out(self):
        got = habits.read([prompt(0), answer(1), answer(50, isSidechain=True), prompt(3)])
        self.assertEqual(got["waited_ms"], 2 * MIN)

    def test_records_with_no_time_are_skipped(self):
        got = habits.read([prompt(0), {"type": "assistant", "message": {}}, answer(1)])
        self.assertEqual(got["worked_ms"], MIN)

    def test_runs_add_up(self):
        one = {"habits": habits.read([prompt(0), answer(10), prompt(15), answer(16)])}
        two = {"habits": habits.read([prompt(0), answer(20), prompt(23)])}
        got = habits.bottleneck_of([one, two])
        self.assertEqual((got["worked"], got["waited"], got["slowest"]), ("31m", "8m", "5m"))
        self.assertEqual(got["waitedShare"], 21)

    def test_nothing_to_say(self):
        self.assertIsNone(habits.bottleneck_of([]))
        self.assertIsNone(habits.bottleneck_of([{"habits": habits.read([answer(0)])}, {"habits": None}]))

    def test_spans_read_like_the_line(self):
        for ms, want in ((41 * 3600000, "41h"), (6 * 3600000 + 12 * MIN, "6h 12m"), (28 * MIN, "28m"), (3 * 3600000, "3h"), (412 * 3600000, "412h"), (0, "0m")):
            self.assertEqual(habits.fmt_span(ms), want)


class Words(unittest.TestCase):
    def test_counts_whole_words_in_any_case(self):
        self.assertEqual(habits.swears("Oh SHIT, this is shit!"), {"s***": 2})
        self.assertEqual(habits.swears("(damn) and fucking hell"), {"d***": 1, "f***": 1})

    def test_forms_count_under_their_word(self):
        self.assertEqual(habits.swears("bullshit shitty shit-hot"), {"s***": 3})

    def test_words_inside_words_do_not_count(self):
        for text in ("Scunthorpe", "scunthorpe united", "assassin", "classic", "scrapped", "Penistone", "damnation", "shitake",
                     "cockpit", "Dickens", "bitchin", "fucktastic", "crapshoot", "pass the bass", "rewtfoo", "fuck_it", "shit2"):
            self.assertEqual(habits.swears(text), {}, text)

    def test_masking(self):
        self.assertEqual(habits.mask("fuck"), "f***")
        self.assertEqual(habits.mask("wtf"), "w**")
        self.assertEqual(habits.mask("asshole"), "a******")
        self.assertEqual(habits.swears("wtf is this"), {"w**": 1})

    def test_code_is_not_the_person_talking(self):
        for text in ("look\n```\nshit\n```\n", "```py\nshit = 1\n```", "unclosed\n```\nshit", "~~~\nshit\n~~~", "run `shit` now",
                     "> shit was quoted", "    shit in an indented line", "see https://example.com/shit/page"):
            self.assertEqual(habits.swears(text), {}, text)

    def test_words_outside_code_still_count(self):
        self.assertEqual(habits.swears("damn\n```\nshit\n```\nshit"), {"d***": 1, "s***": 1})

    def test_a_long_prompt_is_taken_for_a_paste(self):
        self.assertEqual(habits.swears("shit " + "x" * habits.PASTE_CHARS), {})

    def test_system_tags_inside_a_prompt_are_removed(self):
        self.assertEqual(habits.swears("hello <system-reminder>shit</system-reminder>"), {})


class Jar(unittest.TestCase):
    def jar(self, *records):
        return habits.read(list(records))["swears"]

    def test_typed_and_queued_prompts_count(self):
        self.assertEqual(self.jar(prompt(0, "shit"), prompt(1, "damn", source="queued")), {"s***": 1, "d***": 1})

    def test_older_transcripts_with_no_source_count(self):
        self.assertEqual(self.jar(prompt(0, "shit", source=None)), {"s***": 1})

    def test_a_block_list_prompt_counts(self):
        self.assertEqual(self.jar(prompt(0, [{"type": "text", "text": "shit"}, {"type": "image", "source": {}}])), {"s***": 1})

    def test_only_the_person_typing_counts(self):
        for record in (
            tool_result(0, "shit"),
            answer(0, "shit"),
            prompt(0, "shit", source="sdk"),
            prompt(0, "shit", source="suggestion_accepted"),
            prompt(0, "shit", entrypoint="sdk-cli", source=None),
            prompt(0, "shit", isMeta=True),
            prompt(0, "shit", isCompactSummary=True),
            prompt(0, "shit", isSidechain=True),
            prompt(0, "<local-command-stdout>shit</local-command-stdout>", source=None),
            prompt(0, "<command-name>/shit</command-name>", source=None),
            prompt(0, "<bash-stdout>shit</bash-stdout>", source=None),
            prompt(0, "<task-notification>shit</task-notification>", source=None),
            prompt(0, "This session is being continued. shit", source=None),
            prompt(0, "[Request interrupted by user] shit", source=None),
            prompt(0, "shit", scheduledTaskId="x"),
        ):
            self.assertEqual(self.jar(record), {}, record)

    def test_the_fine(self):
        runs = [{"habits": {"swears": {"f***": 3, "s***": 1}}}, {"habits": {"swears": {"s***": 1}}}, {"habits": None}]
        self.assertEqual(habits.jar_of(runs), {"count": 5, "fine": "$1.25", "top": "f***", "level": 5})

    def test_an_empty_jar(self):
        self.assertEqual(habits.jar_of([]), {"count": 0, "fine": "$0.00", "top": None, "level": 0})

    def test_the_jar_stops_filling(self):
        self.assertEqual(habits.jar_of([{"habits": {"swears": {"f***": 500}}}])["level"], 100)


class OnDisk(unittest.TestCase):
    """Real files through the real read, with a prompt that must never come back."""
    SECRET = "zebra-launch-codes"

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.projects = os.path.join(self.dir, "projects")
        self.cache = os.path.join(self.dir, "data", "cache.json")

    def write(self, name, records):
        path = os.path.join(self.projects, "proj", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def assistant(self, minute, rid, session="s1", tokens=100, text="done", **more):
        return {"type": "assistant", "uuid": rid + str(minute), "requestId": rid, "sessionId": session, "timestamp": at(minute),
                "cwd": "/work/secret-project",
                "message": {"model": "claude-opus-4-8", "usage": {"input_tokens": tokens},
                            "content": [{"type": "text", "text": text}, {"type": "tool_use", "id": "t" + rid, "name": "Bash"}]}, **more}

    def session(self):
        self.write("s1.jsonl", [dict(prompt(0, "%s, fuck this, you're absolutely right" % self.SECRET), sessionId="s1"),
                                self.assistant(5, "r1", text="you're absolutely right"),
                                dict(prompt(7, "again"), sessionId="s1"), self.assistant(9, "r2")])

    def args(self, **more):
        return mock.Mock(**{"demo": False, "projects": [self.projects], "cache": self.cache, "budget": 15.0, "stats": None, "window": "all", "out": self.dir, "jar": False, **more})

    def view(self):
        return helper.read(self.args())["windows"]["all"]

    def subagent(self, name, records):
        self.write(os.path.join("s1", "subagents", name), records)

    def test_subagent_transcripts_are_not_sessions(self):
        self.session()
        self.subagent("agent-a.jsonl", [self.assistant(1, "r3", isSidechain=True)])
        self.subagent(os.path.join("workflows", "w1", "agent-b.jsonl"), [self.assistant(2, "r4", isSidechain=True)])
        self.assertEqual(self.view()["sessions"], "1")

    def test_an_older_subagent_file_is_not_a_session(self):
        self.session()
        self.write("agent-old.jsonl", [dict(prompt(1, "x", isSidechain=True), sessionId="s1"), self.assistant(2, "r5", isSidechain=True)])
        self.assertEqual(self.view()["sessions"], "1")

    def test_their_tokens_and_tools_still_count(self):
        self.session()
        before = self.view()
        self.subagent("agent-a.jsonl", [self.assistant(1, "r3", tokens=800, isSidechain=True)])
        after = self.view()
        self.assertEqual((before["tokens"] + before["unit"], before["toolCalls"]), ("200", "2"))
        self.assertEqual((after["tokens"] + after["unit"], after["toolCalls"]), ("1.0K", "3"))

    def test_a_turn_repeated_in_a_subagent_file_counts_once(self):
        self.session()
        self.subagent("agent-a.jsonl", [self.assistant(1, "r1", isSidechain=True), self.assistant(2, "r3", tokens=800, isSidechain=True)])
        view = self.view()
        self.assertEqual((view["tokens"] + view["unit"], view["toolCalls"]), ("1.0K", "3"))

    def test_two_sessions_are_two(self):
        self.session()
        self.write("s2.jsonl", [dict(prompt(0), sessionId="s2"), self.assistant(5, "r9", session="s2")])
        self.assertEqual(self.view()["sessions"], "2")

    def test_a_window_of_only_subagents_is_not_empty(self):
        self.subagent("agent-a.jsonl", [self.assistant(1, "r3", isSidechain=True)])
        view = self.view()
        self.assertFalse(view["empty"])
        self.assertEqual(view["sessions"], "0")

    def test_the_longest_session_is_a_session(self):
        self.session()
        self.subagent("agent-a.jsonl", [self.assistant(1, "r3", isSidechain=True), self.assistant(600, "r4", isSidechain=True)])
        self.assertEqual(self.view()["longest"], "9m")

    def test_no_prompt_text_in_the_reply_or_the_cache(self):
        self.session()
        reply = json.dumps(helper.read(self.args()))
        with open(self.cache, encoding="utf-8") as f:
            cache = f.read()
        for place in (reply, cache):
            self.assertNotIn(self.SECRET, place)
            self.assertNotIn("fuck", place)
            self.assertNotIn("again", place)
        self.assertIn("f***", cache)

    def test_counted_from_the_person_only(self):
        self.session()
        view = self.view()
        self.assertEqual((view["jar"]["count"], view["jar"]["top"], view["jar"]["fine"]), (1, "f***", "$0.25"))

    def test_absolutely_right_is_the_assistant_only(self):
        self.session()
        self.assertEqual(self.view()["jar"]["rights"], "1")

    def test_the_bottleneck_reaches_the_card_and_caption(self):
        self.session()
        view = self.view()
        self.assertEqual(view["bottleneck"]["worked"], "7m")
        self.assertEqual(view["bottleneck"]["waited"], "2m")
        self.assertIn("my agents worked 7m and waited 2m for me (estimated).", view["share"]["caption"])
        s = helper.card_stats(helper.collect([self.projects], self.cache, 15.0)[0], "all time")
        self.assertIn("your agents worked 7m. they waited 2m for you.", wrapped.html_card(s))
        self.assertIn("estimated from timestamps", wrapped.html_card(s))

    def test_with_sharing_off_no_swear_data_reaches_the_card_or_caption(self):
        self.session()
        runs = helper.collect([self.projects], self.cache, 15.0)[0]
        s = helper.card_stats(runs, "all time")
        self.assertNotIn("jar", s)
        for text in (wrapped.html_card(s), wrapped.html_card(s, "wide"), *wrapped.share_text(s).values(), self.view()["share"]["caption"], self.view()["share"]["postUrl"]):
            for part in ("swear", "$0.25", "f***", "f%2A%2A%2A"):
                self.assertNotIn(part, text.lower())

    def test_with_sharing_on_it_does(self):
        self.session()
        runs = helper.collect([self.projects], self.cache, 15.0)[0]
        s = helper.card_stats(runs, "all time", jar=True)
        self.assertIn("swear jar", wrapped.html_card(s))
        self.assertIn("1 swear, top word f***", wrapped.html_card(s))
        self.assertIn("swear jar: $0.25.", self.view()["shareJar"]["caption"])

    def test_save_only_shows_the_jar_when_asked(self):
        self.session()
        with mock.patch.object(helper.wrapped, "find_browser", return_value=None):
            off = helper.save(self.args())
            with open(off["saved"], encoding="utf-8") as f:
                self.assertNotIn("swear", f.read())
            on = helper.save(self.args(jar=True))
            with open(on["saved"], encoding="utf-8") as f:
                self.assertIn("swear jar", f.read())

    def test_an_empty_jar_stays_off_the_card_even_when_on(self):
        self.write("s1.jsonl", [dict(prompt(0, "fine"), sessionId="s1"), self.assistant(5, "r1")])
        s = helper.card_stats(helper.collect([self.projects], self.cache, 15.0)[0], "all time", jar=True)
        self.assertNotIn("swear", wrapped.html_card(s))


if __name__ == "__main__":
    unittest.main()
