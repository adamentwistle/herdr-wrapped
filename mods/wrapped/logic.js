// What the overlay works out, kept apart from drawing. No Herdr imports, so `node --test` can load this file.
export const HELPER_MS = 28000;
export const WINDOWS = [{ key: "7d", label: "Last 7 days" }, { key: "30d", label: "Last 30 days" }, { key: "all", label: "All time" }];

// Rejects once `ms` has passed with no answer, so one hung call can't hold the overlay. `sleep` is `cx.sleep`.
export function timed(promise, ms, what, sleep) {
  const late = sleep(ms).then(() => { throw new Error(`${what} didn't answer within ${Math.round(ms / 1000)} s`); });
  return Promise.race([promise, late]);
}

// The cache sits outside mods/wrapped, because a new file under mods/ makes Herdr reload the mod.
export const cachePath = (appHome) => `${appHome}/data/wrapped/runs-cache.json`;

// -I and -S keep the user's Python setup out of it. -B keeps __pycache__ out of the mod folder, for the same reason.
export function helperArgs(appHome, action, { demo = false, window = null, out = null, jar = false } = {}) {
  const args = ["-I", "-S", "-B", `${appHome}/mods/wrapped/helper.py`, action, "--cache", cachePath(appHome)];
  if (demo) args.push("--demo");
  if (window) args.push("--window", window);
  if (out) args.push("--out", out);
  if (jar) args.push("--jar");
  return args;
}

// What the helper printed, as `{ value, error }`. A crash or bad output is named, not passed off as nothing to show.
export function helperReply(result) {
  if (result?.code !== 0) {
    const why = String(result?.stderr ?? "").trim().split("\n").at(-1)?.slice(0, 160);
    return { value: null, error: `helper.py stopped with code ${result?.code ?? "unknown"}${why ? `: ${why}` : ""}` };
  }
  let value;
  try { value = JSON.parse(result.stdout); } catch { return { value: null, error: "helper.py printed something that isn't JSON" }; }
  if (!value || typeof value !== "object") return { value: null, error: "helper.py printed something that isn't JSON" };
  return value.error ? { value: null, error: String(value.error) } : { value, error: null };
}

// Why the helper couldn't run at all: it didn't answer, or python3 couldn't start it.
export const helperFailure = (e) => {
  const why = String(e?.message ?? e);
  return /didn't answer/.test(why) ? why : `python3 couldn't start helper.py: ${why}`;
};

// What to do after a read: "done", "again" for a partial one, or "stuck" if a pass got no further than the last.
export function readStep(reply, lastRead) {
  if (!reply.partial) return "done";
  return reply.read > lastRead ? "again" : "stuck";
}

export const stuckText = "Reading stopped getting further. Check that the data folder in Herdr's home can be written to.";

export function progressText(progress) {
  if (!progress) return "Reading transcripts";
  return `Reading transcripts: ${progress.read.toLocaleString("en-US")} of ${progress.total.toLocaleString("en-US")}`;
}

// A note under the card when files were left out, so the total isn't taken for the whole history.
export function leftOutText(count) {
  if (!count) return null;
  return count === 1 ? "1 transcript couldn't be read and is left out." : `${count.toLocaleString("en-US")} transcripts couldn't be read and are left out.`;
}

export const tilde = (path, home) => (path.startsWith(home) ? `~${path.slice(home.length)}` : path);

export function savedText(reply, home) {
  const where = tilde(reply.saved, home);
  if (reply.kind === "png") return `Saved to ${where}`;
  if (reply.noBrowser) return `No Chromium browser found, so the card was saved as HTML: ${where}. Open it and take a screenshot.`;
  return `The browser couldn't make a PNG (${reply.why}), so the card was saved as HTML: ${where}. Open it and take a screenshot.`;
}

export const postedText = "Opened X. A link can't attach an image, so add the saved card to the post yourself.";

export function postFailure(result) {
  const why = result?.message ?? (String(result?.stderr ?? "").trim().split("\n").at(-1) || `code ${result?.code ?? "unknown"}`);
  return `The open command failed (${String(why).slice(0, 120)}). Copy caption still works.`;
}

const EMPTY = { "7d": "No Claude Code sessions in the last 7 days.", "30d": "No Claude Code sessions in the last 30 days." };
export const emptyText = (key) => EMPTY[key] ?? "No Claude Code sessions yet.";

// The hero's small print. Days whose transcripts Claude Code has deleted come from its own stats file, and say so.
export function tokensNote(view) {
  const cache = `Cache included: ${view.fromCache} from cache, ${view.newTokens} new`;
  if (!view.historyDays) return cache;
  const days = view.historyDays === 1 ? "1 day" : `${view.historyDays.toLocaleString("en-US")} days`;
  return `${cache}. ${days} from Claude Code's stats, as its transcripts are gone.`;
}

// How the strip's corner reads.
export function stripNote(view) {
  const busiest = view.busiest ? `Busiest ${view.busiest.label}, ${view.busiest.tokens}` : "";
  return view.strip?.days > 1 ? `${view.strip.days} days per bar. ${busiest}`.trim() : busiest;
}

// What Copy caption and Post use. The jar joins only when the person has turned sharing on.
export const shareOf = (view, jarOn) => (jarOn ? view.shareJar : view.share);

export const bottleneckText = (b) => `Your agents worked ${b.worked}. They waited ${b.waited} for you.`;

export const bottleneckNote = (minutes) => `Estimated from transcript timestamps. A gap over ${minutes} minutes counts as away, not waiting.`;

// One row of coins per step of the jar, and at least one with anything in it.
export const jarRows = (level, rows) => (level > 0 ? Math.max(1, Math.round(level * rows / 100)) : 0);

export function jarText(jar) {
  if (!jar.count) return "Nothing in the jar.";
  const times = jar.count === 1 ? "1 swear" : `${jar.count.toLocaleString("en-US")} swears`;
  return `${times} at $0.25. Top word ${jar.top}`;
}

// The buttons work once there's a card to act on.
export const canAct = (view) => Boolean(view && !view.empty);
