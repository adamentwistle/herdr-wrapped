// Runs main.js and view.js against the stand-ins in stubs.mjs: every state the overlay can be in, and every button.
import assert from "node:assert/strict";
import test from "node:test";
import { fakeCx, world } from "./stubs.mjs";

const { default: Wrapped } = await import("../mods/wrapped/main.js");

const card = (window, more = {}) => ({
  window, empty: false, tokens: "51.9", unit: "M", fromCache: "49.5M", newTokens: "2.4M", cost: "$42.18", sessions: "8", toolCalls: "43",
  topTool: { name: "Bash", count: "17" }, longest: "4h 45m", busiest: { label: "jul 5", tokens: "22.5M" }, peak: { count: 3, date: "2026-07-05", label: "jul 5" },
  bottleneck: { worked: "41h", waited: "6h 12m", slowest: "28m", waitedShare: 13, away: 4 }, capMinutes: 30, jar: { count: 17, fine: "$4.25", top: "f***", level: 17, rights: "21" },
  strip: { bars: [10, 100, 0, 40], top: 1, days: 1, from: "jul 2", to: "jul 25" }, punchline: "claude said it 21 times.",
  share: { caption: `my ${window} caption`, postUrl: `https://twitter.com/intent/tweet?text=${window}` },
  shareJar: { caption: `my ${window} caption with the jar`, postUrl: `https://twitter.com/intent/tweet?text=${window}-jar` }, ...more,
});
const done = (more = {}) => ({ partial: false, read: 8, total: 8, leftOut: 0, windows: { all: card("all time"), "30d": card("last 30 days", { tokens: "9.1" }), "7d": card("last 7 days", { tokens: "2.2" }) }, ...more });
const reply = (value) => ({ code: 0, stdout: JSON.stringify(value), stderr: "" });

// Answers each exec in turn and keeps what it was asked.
function script(...answers) {
  const asked = [];
  world.exec = async (command, args) => {
    asked.push({ command, args });
    const next = answers.shift();
    if (next instanceof Error) throw next;
    return next ?? reply({ error: "asked once too often" });
  };
  return asked;
}

async function open(props = {}) {
  world.props = props;
  const view = new Wrapped(), cx = fakeCx();
  view.init(props, cx);
  await cx.settled();
  return { view, cx, tree: () => view.render() };
}

async function press(view, tree, id, cx) {
  const target = tree().find(id);
  assert.ok(target, `${id} is on screen`);
  assert.ok(target.press, `${id} can be pressed`);
  target.press({}, cx);
  await cx.settled();
}

test("the story is on screen", async () => {
  script(reply(done()));
  const { tree } = await open();
  const text = tree().text();
  for (const part of ["51.9", "M", "$42.18", "8", "43", "Bash · 17", "4h 45m", "jul 5", "Most agents at once", "3", "An idle session counts as running", "Your agents worked 41h. They waited 6h 12m for you.", "Your slowest answer: 28m", "A gap over 30 minutes counts as away, not waiting.", "Swear Jar", "$4.25", "17 swears at $0.25. Top word f***", "\"You're absolutely right\": 21", "Share on card and caption: Off", "claude said it 21 times.", "Last 7 days", "Last 30 days", "All time", "Save card", "Copy caption", "Post"]) assert.ok(text.includes(part), part);
  assert.ok(!text.includes("Demo data"));
});

test("the helper runs as python3 -I -S -B", async () => {
  const asked = script(reply(done()));
  await open();
  assert.equal(asked[0].command, "python3");
  assert.deepEqual(asked[0].args.slice(0, 3), ["-I", "-S", "-B"]);
});

test("the switch changes the numbers with no second read", async () => {
  const asked = script(reply(done()));
  const { view, cx, tree } = await open();
  await press(view, tree, "window-30d", cx);
  assert.ok(tree().text().includes("9.1"));
  assert.equal(asked.length, 1);
});

test("a long history reads in passes and shows how far it's got", async () => {
  const asked = script(reply({ partial: true, read: 100, total: 300, leftOut: 0 }), reply({ partial: true, read: 200, total: 300, leftOut: 0 }), reply(done()));
  const { tree } = await open();
  assert.equal(asked.length, 3);
  assert.ok(tree().text().includes("51.9"));
});

test("while loading it says so, and the buttons wait", async () => {
  world.exec = () => new Promise(() => {});
  const view = new Wrapped(), cx = fakeCx();
  view.init({}, cx);
  view.progress = { read: 12, total: 40 };
  const tree = view.render();
  assert.ok(tree.text().includes("Reading transcripts: 12 of 40"));
  assert.equal(tree.find("save").press, null);
  assert.equal(tree.find("post").press, null);
});

test("a pass that gets no further stops and says so", async () => {
  script(reply({ partial: true, read: 5, total: 9, leftOut: 0 }), reply({ partial: true, read: 5, total: 9, leftOut: 0 }));
  const { tree } = await open();
  assert.ok(tree().text().includes("Reading stopped getting further"));
});

test("no python3 is named", async () => {
  script(new Error("program not found"));
  const { tree } = await open();
  assert.ok(tree().text().includes("python3 couldn't start helper.py: program not found"));
});

test("a helper crash is named", async () => {
  script({ code: 1, stdout: "", stderr: "MemoryError\n" });
  const { tree } = await open();
  assert.ok(tree().text().includes("helper.py stopped with code 1: MemoryError"));
});

test("no transcripts is named", async () => {
  script(reply({ error: "No Claude Code transcripts found." }));
  const { tree } = await open();
  assert.ok(tree().text().includes("No Claude Code transcripts found."));
});

test("an empty window says so and the buttons wait", async () => {
  script(reply(done({ windows: { all: card("all time"), "30d": { window: "last 30 days", empty: true } } })));
  const { view, cx, tree } = await open();
  await press(view, tree, "window-30d", cx);
  assert.ok(tree().text().includes("No Claude Code sessions in the last 30 days."));
  assert.equal(tree().find("copy").press, null);
});

test("transcripts left out are mentioned", async () => {
  script(reply(done({ leftOut: 2 })));
  const { tree } = await open();
  assert.ok(tree().text().includes("2 transcripts couldn't be read"));
});

test("Save card saves the window in view and says where", async () => {
  const asked = script(reply(done()), reply({ saved: "/h/Downloads/card.png", kind: "png" }));
  const { view, cx, tree } = await open();
  await press(view, tree, "window-30d", cx);
  await press(view, tree, "save", cx);
  const args = asked[1].args;
  assert.equal(args[4], "save");
  assert.equal(args[args.indexOf("--window") + 1], "30d");
  assert.equal(args[args.indexOf("--out") + 1], "/h/Downloads");
  assert.ok(tree().text().includes("Saved to ~/Downloads/card.png"));
});

test("Save card falls back to HTML and says why", async () => {
  script(reply(done()), reply({ saved: "/h/Downloads/card.html", kind: "html", noBrowser: true, why: "none" }));
  const { view, cx, tree } = await open();
  await press(view, tree, "save", cx);
  assert.ok(tree().text().includes("No Chromium browser found, so the card was saved as HTML: ~/Downloads/card.html"));
});

test("Copy caption copies the caption of the window in view", async () => {
  script(reply(done()));
  const { view, cx, tree } = await open();
  await press(view, tree, "window-30d", cx);
  await press(view, tree, "copy", cx);
  assert.equal(cx.copied, "my last 30 days caption");
});

test("Post opens the link with open", async () => {
  const asked = script(reply(done()), { code: 0, stdout: "", stderr: "" });
  const { view, cx, tree } = await open();
  await press(view, tree, "post", cx);
  assert.deepEqual(asked[1], { command: "open", args: ["https://twitter.com/intent/tweet?text=all time"] });
  assert.ok(tree().text().includes("add the saved card to the post"));
});

test("a missing open is named", async () => {
  script(reply(done()), new Error("program not found"));
  const { view, cx, tree } = await open();
  await press(view, tree, "post", cx);
  assert.ok(tree().text().includes("The open command failed (program not found)"));
});

test("sharing starts off, and nothing of the jar goes out", async () => {
  const asked = script(reply(done()), reply({ saved: "/h/Downloads/card.png", kind: "png" }), { code: 0, stdout: "", stderr: "" });
  const { view, cx, tree } = await open();
  await press(view, tree, "copy", cx);
  assert.equal(cx.copied, "my all time caption");
  await press(view, tree, "save", cx);
  assert.ok(!asked[1].args.includes("--jar"));
  await press(view, tree, "post", cx);
  assert.equal(asked[2].args[0], "https://twitter.com/intent/tweet?text=all time");
});

test("the toggle sends the jar to the card and caption", async () => {
  const asked = script(reply(done()), reply({ saved: "/h/Downloads/card.png", kind: "png" }), { code: 0, stdout: "", stderr: "" });
  const { view, cx, tree } = await open();
  await press(view, tree, "jar-share", cx);
  assert.ok(tree().text().includes("Share on card and caption: On"));
  await press(view, tree, "copy", cx);
  assert.equal(cx.copied, "my all time caption with the jar");
  await press(view, tree, "save", cx);
  assert.ok(asked[1].args.includes("--jar"));
  await press(view, tree, "post", cx);
  assert.equal(asked[2].args[0], "https://twitter.com/intent/tweet?text=all time-jar");
});

test("the toggle turns off again", async () => {
  script(reply(done()));
  const { view, cx, tree } = await open();
  await press(view, tree, "jar-share", cx);
  await press(view, tree, "jar-share", cx);
  await press(view, tree, "copy", cx);
  assert.equal(cx.copied, "my all time caption");
});

test("a window with no finished turns says so", async () => {
  script(reply(done({ windows: { all: card("all time", { bottleneck: null, jar: { count: 0, fine: "$0.00", top: null, level: 0, rights: "0" } }), "30d": card("x") } })));
  const { tree } = await open();
  assert.ok(tree().text().includes("No finished turns yet."));
  assert.ok(tree().text().includes("Nothing in the jar."));
});

test("demo reaches the helper and is marked", async () => {
  const asked = script(reply(done()));
  const { tree } = await open({ demo: true });
  assert.ok(asked[0].args.includes("--demo"));
  assert.ok(tree().text().includes("Demo data"));
});

test("closing stops the reading", async () => {
  const asked = script(reply({ partial: true, read: 1, total: 9, leftOut: 0 }), reply({ partial: true, read: 2, total: 9, leftOut: 0 }));
  const view = new Wrapped(), cx = fakeCx();
  view.init({}, cx);
  await view.close();
  await cx.settled();
  assert.ok(asked.length <= 1);
});

test("the switch reaches all three windows", async () => {
  script(reply(done()));
  const { view, cx, tree } = await open();
  await press(view, tree, "window-7d", cx);
  assert.ok(tree().text().includes("2.2"));
  await press(view, tree, "window-all", cx);
  assert.ok(tree().text().includes("51.9"));
});

test("days filled from Claude Code's stats are named under the hero", async () => {
  script(reply(done({ windows: { all: card("all time", { historyDays: 97 }), "30d": card("x"), "7d": card("y") } })));
  const { tree } = await open();
  assert.ok(tree().text().includes("97 days from Claude Code's stats, as its transcripts are gone."));
});
