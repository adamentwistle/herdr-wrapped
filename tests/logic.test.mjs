import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import * as logic from "../mods/wrapped/logic.js";

const root = fileURLToPath(new URL("..", import.meta.url));
const mod = join(root, "mods", "wrapped");
const home = "/Users/me", appHome = "/Users/me/.config/herdr-app";

test("the helper runs isolated, and its cache stays out of mods/", () => {
  const args = logic.helperArgs(appHome, "read");
  assert.deepEqual(args.slice(0, 5), ["-I", "-S", "-B", `${appHome}/mods/wrapped/helper.py`, "read"]);
  assert.equal(args[args.indexOf("--cache") + 1], `${appHome}/data/wrapped/runs-cache.json`);
  assert.ok(!logic.cachePath(appHome).includes("/mods/"));
  assert.ok(!args.includes("--demo"));
});

test("a save passes its window and folder, and demo reaches the helper", () => {
  const args = logic.helperArgs(appHome, "save", { demo: true, window: "30d", out: `${home}/Downloads` });
  assert.equal(args[4], "save");
  assert.ok(args.includes("--demo"));
  assert.equal(args[args.indexOf("--window") + 1], "30d");
  assert.equal(args[args.indexOf("--out") + 1], `${home}/Downloads`);
});

test("the jar reaches a save only when asked", () => {
  assert.ok(!logic.helperArgs(appHome, "save", { window: "all" }).includes("--jar"));
  assert.ok(logic.helperArgs(appHome, "save", { window: "all", jar: true }).includes("--jar"));
});

test("sharing follows the toggle", () => {
  const view = { share: "plain", shareJar: "with jar" };
  assert.equal(logic.shareOf(view, false), "plain");
  assert.equal(logic.shareOf(view, true), "with jar");
});

test("the Bottleneck's words", () => {
  assert.equal(logic.bottleneckText({ worked: "41h", waited: "6h 12m" }), "Your agents worked 41h. They waited 6h 12m for you.");
  assert.match(logic.bottleneckNote(30), /^Estimated from transcript timestamps\. A gap over 30 minutes counts as away, not waiting\.$/);
});

test("the jar's words and level", () => {
  assert.equal(logic.jarText({ count: 0 }), "Nothing in the jar.");
  assert.equal(logic.jarText({ count: 1, top: "f***" }), "1 swear at $0.25. Top word f***");
  assert.equal(logic.jarText({ count: 1200, top: "s***" }), "1,200 swears at $0.25. Top word s***");
  assert.equal(logic.jarRows(0, 9), 0);
  assert.equal(logic.jarRows(1, 9), 1);
  assert.equal(logic.jarRows(100, 9), 9);
});

test("a helper reply is read, and every way it can go wrong is named", () => {
  assert.deepEqual(logic.helperReply({ code: 0, stdout: '{"partial":false}' }), { value: { partial: false }, error: null });
  assert.equal(logic.helperReply({ code: 0, stdout: '{"error":"No transcripts"}' }).error, "No transcripts");
  assert.match(logic.helperReply({ code: 0, stdout: "oops" }).error, /isn't JSON/);
  assert.match(logic.helperReply({ code: 0, stdout: "null" }).error, /isn't JSON/);
  assert.equal(logic.helperReply({ code: 1, stderr: "a\nModuleNotFoundError: x\n" }).error, "helper.py stopped with code 1: ModuleNotFoundError: x");
  assert.equal(logic.helperReply(null).error, "helper.py stopped with code unknown");
});

test("a helper that can't run says why", () => {
  assert.equal(logic.helperFailure(new Error("helper.py didn't answer within 28 s")), "helper.py didn't answer within 28 s");
  assert.equal(logic.helperFailure(new Error("No such file")), "python3 couldn't start helper.py: No such file");
});

test("a read goes on while it gets further", () => {
  assert.equal(logic.readStep({ partial: false }, -1), "done");
  assert.equal(logic.readStep({ partial: true, read: 10 }, -1), "again");
  assert.equal(logic.readStep({ partial: true, read: 20 }, 10), "again");
  assert.equal(logic.readStep({ partial: true, read: 20 }, 20), "stuck");
});

test("timed gives up when the time runs out", async () => {
  const quick = (ms) => new Promise((done) => done(ms));
  await assert.rejects(logic.timed(new Promise(() => {}), 28000, "helper.py", quick), /didn't answer within 28 s/);
  assert.equal(await logic.timed(Promise.resolve(7), 28000, "helper.py", () => new Promise(() => {})), 7);
});

test("the words for progress and left-out files", () => {
  assert.equal(logic.progressText(null), "Reading transcripts");
  assert.equal(logic.progressText({ read: 1204, total: 6000 }), "Reading transcripts: 1,204 of 6,000");
  assert.equal(logic.leftOutText(0), null);
  assert.match(logic.leftOutText(1), /^1 transcript couldn't/);
  assert.match(logic.leftOutText(1200), /^1,200 transcripts couldn't/);
});

test("a save says where it went, and home is shown as ~", () => {
  assert.equal(logic.tilde("/Users/me/Downloads/a.png", home), "~/Downloads/a.png");
  assert.equal(logic.tilde("/tmp/a.png", home), "/tmp/a.png");
  assert.equal(logic.savedText({ kind: "png", saved: `${home}/Downloads/a.png` }, home), "Saved to ~/Downloads/a.png");
  const none = logic.savedText({ kind: "html", noBrowser: true, saved: `${home}/Downloads/a.html` }, home);
  assert.match(none, /^No Chromium browser found, so the card was saved as HTML: ~\/Downloads\/a\.html/);
  const slow = logic.savedText({ kind: "html", noBrowser: false, why: "the browser took more than 20 seconds", saved: `${home}/Downloads/a.html` }, home);
  assert.match(slow, /took more than 20 seconds.*saved as HTML/);
});

test("a failed open says so", () => {
  assert.match(logic.postFailure(new Error("program not found")), /program not found.*Copy caption still works/);
  assert.match(logic.postFailure({ code: 1, stderr: "" }), /code 1/);
});

test("the strip's note follows its bars", () => {
  assert.equal(logic.stripNote({ strip: { days: 1 }, busiest: { label: "jul 5", tokens: "22.5M" } }), "Busiest jul 5, 22.5M");
  assert.equal(logic.stripNote({ strip: { days: 3 }, busiest: { label: "jul 5", tokens: "22.5M" } }), "3 days per bar. Busiest jul 5, 22.5M");
});

test("the buttons wait for a card", () => {
  assert.equal(logic.canAct(null), false);
  assert.equal(logic.canAct({ empty: true }), false);
  assert.equal(logic.canAct({ empty: false }), true);
});

test("the mod's own JS uses no timers Herdr doesn't have", () => {
  for (const name of readdirSync(mod).filter((f) => f.endsWith(".js"))) {
    const source = readFileSync(join(mod, name), "utf8");
    assert.doesNotMatch(source, /\b(setTimeout|clearTimeout|setInterval|clearInterval|require\(|process\.)/, name);
  }
});

test("the overlay opens by key, with no layout change", () => {
  const manifest = JSON.parse(readFileSync(join(mod, "mod.json"), "utf8"));
  assert.equal(manifest.place, "none");
  assert.equal(manifest.title, "Wrapped");
  assert.ok(manifest.overlay.width > 0 && manifest.overlay.height > 0);
  const keys = JSON.parse(readFileSync(join(root, "keys.json"), "utf8"));
  assert.deepEqual(Object.keys(keys), ["global"]);
  const [[key, action], ...rest] = Object.entries(keys.global);
  assert.equal(rest.length, 0);
  assert.equal(action, "mod.open:wrapped");
  assert.ok(!["ctrl-shift-j", "ctrl-shift-y", "cmd-shift-u"].includes(key));
  const defaults = "bcdeklnoptvwxz".split("").filter((l) => l !== "d").map((l) => `ctrl-shift-${l}`);
  assert.ok(!defaults.includes(key));
});

test("the windows are the last 7 days, the last 30 days and all time", () => {
  assert.deepEqual(logic.WINDOWS.map((w) => [w.key, w.label]), [["7d", "Last 7 days"], ["30d", "Last 30 days"], ["all", "All time"]]);
  assert.equal(logic.emptyText("7d"), "No Claude Code sessions in the last 7 days.");
});

test("the hero names days that came from Claude Code's stats", () => {
  const view = { fromCache: "1B", newTokens: "2M" };
  assert.equal(logic.tokensNote(view), "Cache included: 1B from cache, 2M new");
  assert.equal(logic.tokensNote({ ...view, historyDays: 1 }), "Cache included: 1B from cache, 2M new. 1 day from Claude Code's stats, as its transcripts are gone.");
});
