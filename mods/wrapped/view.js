// Draws the overlay. Colours are theme names, with `accent` as the one strong colour. Sizes are designed for mod.json's
// 940 by 680 overlay: the story on the left, and three blocks on the right. Only .frames animates here, so the overlay
// renders when its state changes and not on a timer.
import { div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { WINDOWS, bottleneckNote, bottleneckText, canAct, emptyText, jarRows, jarText, leftOutText, progressText, stripNote } from "./logic.js";

const PAD = 28;
const STRIP_H = 56;
const SIDE_W = 300;
const JAR_ROWS = 9;
const SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const tint = (colour, alpha) => `${colour.slice(0, 7)}${alpha}`;

function windowSwitch(view, t) {
  return h_flex().flex_none().p(2).gap(2).rounded(8).bg(t.canvas).children(WINDOWS.map((w) => {
    const on = w.key === view.window;
    return h_flex().id(`window-${w.key}`).h(26).px(12).items_center().rounded(6).text_size(12).font_medium().cursor_pointer()
      .text_color(on ? t.foreground : t.muted).when(on, (e) => e.bg(t.raised))
      .on_click((_e, cx) => view.pick(w.key, cx)).child(w.label);
  }));
}

function header(view, t) {
  return h_flex().flex_none().items_center().gap(10)
    .child(div().text_size(13).font_semibold().text_color(t.muted).child("Claude Code Wrapped"))
    .when(view.demo, (e) => e.child(div().px(6).py(1).rounded(4).bg(t.raised).text_size(11).text_color(t.dim).child("Demo data")))
    .child(div().flex_1())
    .child(windowSwitch(view, t));
}

function hero(w, t) {
  return v_flex().flex_1().min_w(0).gap(4)
    .child(div().text_size(12).font_medium().text_color(t.muted).child("Tokens processed"))
    .child(h_flex().items_end().gap(4)
      .child(div().text_size(112).line_height(1).font_bold().text_color(t.accent).child(w.tokens))
      .when(Boolean(w.unit), (e) => e.child(div().pb(12).text_size(56).line_height(1).font_semibold().text_color(t.accent).child(w.unit))))
    .child(div().pt(6).text_size(13).text_color(t.muted).child(`Cache included: ${w.fromCache} from cache, ${w.newTokens} new`));
}

const block = (t) => v_flex().flex_none().p(14).gap(4).rounded(12).bg(t.canvas);
const blockTitle = (text, t) => div().text_size(12).font_semibold().text_color(t.foreground).child(text);
const smallPrint = (text, t) => div().text_size(11).line_height(1.35).text_color(t.muted).child(text);

function peak(w, t) {
  return v_flex().flex_none().p(14).gap(2).rounded(12).bg(tint(t.accent, "1f"))
    .child(blockTitle("Most agents at once", t))
    .child(div().text_size(56).line_height(1.05).font_bold().text_color(t.foreground).child(w.peak ? String(w.peak.count) : "0"))
    .child(smallPrint("Sessions open at the same moment, by transcript start and end. An idle session counts as running.", t));
}

function bottleneck(w, t) {
  const b = w.bottleneck;
  if (!b) return block(t).child(blockTitle("Bottleneck", t)).child(smallPrint("No finished turns yet.", t));
  return block(t).gap(6)
    .child(blockTitle("Bottleneck", t))
    .child(div().text_size(15).line_height(1.3).font_semibold().text_color(t.foreground).child(bottleneckText(b)))
    .child(h_flex().h(6).rounded(3).overflow_hidden().bg(t.input)
      .child(div().h(6).w(Math.round(SIDE_W * b.waitedShare / 100)).bg(t.accent)))
    .child(div().text_size(12).text_color(t.muted).child(`Your slowest answer: ${b.slowest}`))
    .child(smallPrint(bottleneckNote(w.capMinutes), t));
}

function jarDrawing(level, t) {
  const rows = jarRows(level, JAR_ROWS);
  return v_flex().w(64).flex_none().items_center()
    .child(div().w(44).h(7).rounded(2).bg(t.muted))
    .child(v_flex().w(64).h(80).p(4).gap(2).justify_end().rounded(10).border_1().border_color(t.muted)
      .children(Array.from({ length: rows }, () => div().h(6).rounded(2).bg(t.accent))));
}

function shareSwitch(view, t) {
  const on = view.shareJar;
  return h_flex().id("jar-share").gap(8).items_center().cursor_pointer().on_click((_e, cx) => view.toggleJar(cx))
    .child(h_flex().w(30).h(18).flex_none().p(2).rounded(9).bg(on ? t.accent : t.input).when(on, (e) => e.justify_end())
      .child(div().size(14).rounded_full().bg(t.foreground)))
    .child(div().text_size(12).text_color(t.foreground).child(`Share on card and caption: ${on ? "On" : "Off"}`));
}

function jar(view, w, t) {
  const j = w.jar;
  return block(t).gap(8)
    .child(blockTitle("Swear Jar", t))
    .child(h_flex().gap(12).items_center()
      .child(jarDrawing(j.level, t))
      .child(v_flex().flex_1().min_w(0).gap(4)
        .child(div().text_size(28).line_height(1.1).font_bold().text_color(t.foreground).child(j.fine))
        .child(div().text_size(12).line_height(1.3).text_color(t.muted).child(jarText(j)))
        .child(div().text_size(12).line_height(1.3).text_color(t.muted).child(`"You're absolutely right": ${j.rights}`))))
    .child(shareSwitch(view, t));
}

const stat = (value, label, t) => v_flex().flex_1().min_w(0).gap(2)
  .child(div().truncate().text_size(24).font_semibold().text_color(t.foreground).child(value))
  .child(div().text_size(12).text_color(t.muted).child(label));

function stats(w, t) {
  const tool = w.topTool ? `${w.topTool.name} · ${w.topTool.count}` : "None";
  return v_flex().flex_none().gap(14)
    .child(h_flex().gap(16)
      .child(stat(w.cost, "API-equivalent cost", t))
      .child(stat(w.sessions, "Sessions", t))
      .child(stat(w.toolCalls, "Tool calls", t)))
    .child(h_flex().gap(16)
      .child(stat(tool, "Top tool", t))
      .child(stat(w.longest ?? "None", "Longest session", t))
      .child(stat(w.busiest?.label ?? "None", "Busiest day", t)));
}

function strip(w, t) {
  const s = w.strip;
  if (!s) return div();
  const bars = s.bars.map((pct, i) => div().flex_1().min_w(0).rounded(1)
    .h(pct ? Math.max(3, Math.round(pct * STRIP_H / 100)) : 1).bg(i === s.top ? t.accent : pct ? t.input : t.border));
  return v_flex().flex_none().gap(6)
    .child(h_flex().justify_between().text_size(12)
      .child(div().text_color(t.muted).child(s.days > 1 ? "Activity" : "Daily activity"))
      .child(div().text_color(t.muted).child(stripNote(w))))
    .child(h_flex().h(STRIP_H).items_end().gap(2).children(bars))
    .child(h_flex().justify_between().text_size(11).text_color(t.dim)
      .child(div().child(s.from))
      .child(div().child(s.to)));
}

function button(id, label, primary, ready, t, press) {
  return h_flex().id(id).h(34).px(14).flex_none().items_center().rounded(8).text_size(13).font_medium()
    .bg(primary ? t.solid : t.raised).text_color(primary ? t.solid_foreground : t.foreground)
    .when(ready, (e) => e.cursor_pointer().hover((s) => s.opacity(0.88)).on_click(press))
    .when(!ready, (e) => e.opacity(0.45))
    .child(label);
}

function actions(view, t) {
  const ready = canAct(view.shown());
  const note = view.note, left = leftOutText(view.leftOut);
  return h_flex().flex_none().items_center().gap(8)
    .child(button("save", view.saving ? "Saving" : "Save card", true, ready && !view.saving, t, (_e, cx) => view.save(cx)))
    .child(button("copy", "Copy caption", false, ready, t, (_e, cx) => view.copy(cx)))
    .child(button("post", "Post", false, ready, t, (_e, cx) => view.post(cx)))
    .child(div().flex_1().min_w(0).pl(6).text_size(12).line_height(1.3).text_color(note?.bad ? t.blocked : t.muted).child(note?.text ?? left ?? ""));
}

function centred(child) {
  return v_flex().flex_1().min_h(0).items_center().justify_center().gap(10).child(child);
}

function loading(view, t) {
  return centred(h_flex().items_center().gap(8).text_size(13).text_color(t.muted)
    .child(div().w(14).flex_none().text_color(t.accent).frames(SPINNER, 90).child(SPINNER[0]))
    .child(div().child(progressText(view.progress))));
}

const problem = (view, t) => centred(div().max_w(520).text_center().text_size(13).line_height(1.4).text_color(t.blocked).child(view.problem));

const empty = (view, t) => centred(div().text_size(13).text_color(t.muted).child(emptyText(view.window)));

function card(view, w, t) {
  return h_flex().flex_1().min_h(0).items_start().gap(24).overflow_y_scrollbar()
    .child(v_flex().flex_1().min_w(0).gap(22)
      .child(hero(w, t))
      .child(stats(w, t))
      .child(strip(w, t))
      .child(div().flex_none().text_size(20).line_height(1.3).font_semibold().text_color(t.foreground).child(w.punchline)))
    .child(v_flex().w(SIDE_W).flex_none().gap(10)
      .child(peak(w, t))
      .child(bottleneck(w, t))
      .child(jar(view, w, t)));
}

export function paneView(view, t) {
  const w = view.shown();
  const body = view.problem ? problem(view, t) : !view.windows ? loading(view, t) : w?.empty ? empty(view, t) : card(view, w, t);
  return v_flex().id("wrapped").size_full().min_h(0).p(PAD).gap(20).bg(t.popover)
    .child(header(view, t))
    .child(body)
    .child(actions(view, t));
}
