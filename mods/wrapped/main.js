// The Wrapped overlay: a person's Claude Code stats as a card to share. This file runs the helper and holds the state.
// Working things out is in logic.js, drawing in view.js, and helper.py reads the transcripts through wrapped.py.
import { View } from "gpui-kit";
import { appHome, exec, home, props, theme } from "herdr";
import { HELPER_MS, canAct, helperArgs, helperFailure, helperReply, postFailure, postedText, readStep, savedText, shareOf, stuckText, timed } from "./logic.js";
import { paneView } from "./view.js";

export default class Wrapped extends View {
  init(initial, cx) {
    this.demo = Boolean((initial ?? props())?.demo);
    this.window = "all";
    this.windows = null; this.leftOut = 0;
    this.problem = null; this.progress = null;
    this.note = null; this.saving = false; this.closed = false;
    // Off every time it opens: sharing the jar is a choice made on the card in front of you.
    this.shareJar = false;
    this.load(cx);
  }

  async close() { this.closed = true; }

  // One helper call, with what went wrong named. `sleep` is `cx.sleep`.
  async helper(action, options, sleep) {
    let result;
    try {
      result = await timed(exec("python3", helperArgs(appHome(), action, { demo: this.demo, ...options })), HELPER_MS, "helper.py", sleep);
    } catch (e) {
      return { value: null, error: helperFailure(e) };
    }
    return helperReply(result);
  }

  // A long history takes several passes the first time. Each pass keeps what it read, so the next starts from there.
  load(cx) {
    cx.spawn(async (cx) => {
      let last = -1;
      while (!this.closed) {
        const { value, error } = await this.helper("read", {}, (ms) => cx.sleep(ms));
        if (error) { this.problem = error; break; }
        const step = readStep(value, last);
        if (step === "done") { this.windows = value.windows; this.leftOut = value.leftOut; this.progress = null; break; }
        if (step === "stuck") { this.problem = stuckText; break; }
        last = value.read;
        this.progress = value;
        cx.notify();
      }
      cx.notify();
    });
  }

  shown() { return this.windows?.[this.window] ?? null; }

  pick(key, cx) {
    if (key === this.window) return;
    this.window = key; this.note = null;
    cx.notify();
  }

  toggleJar(cx) {
    this.shareJar = !this.shareJar; this.note = null;
    cx.notify();
  }

  save(cx) {
    if (this.saving || !canAct(this.shown())) return;
    this.saving = true; this.note = { text: "Saving the card" };
    cx.notify();
    cx.spawn(async (cx) => {
      const { value, error } = await this.helper("save", { window: this.window, out: `${home()}/Downloads`, jar: this.shareJar }, (ms) => cx.sleep(ms));
      this.saving = false;
      this.note = error ? { text: error, bad: true } : { text: savedText(value, home()), bad: value.kind !== "png" };
      cx.notify();
    });
  }

  copy(cx) {
    const view = this.shown();
    if (!canAct(view)) return;
    cx.write_to_clipboard(shareOf(view, this.shareJar).caption);
    this.note = { text: "Caption copied." };
    cx.notify();
  }

  post(cx) {
    const view = this.shown();
    if (!canAct(view)) return;
    cx.spawn(async (cx) => {
      try {
        const result = await exec("open", [shareOf(view, this.shareJar).postUrl]);
        this.note = result.code === 0 ? { text: postedText } : { text: postFailure(result), bad: true };
      } catch (e) {
        this.note = { text: postFailure(e), bad: true };
      }
      cx.notify();
    });
  }

  render() {
    return paneView(this, theme());
  }
}
