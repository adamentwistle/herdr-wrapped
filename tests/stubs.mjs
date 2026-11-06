// Stand-ins for Herdr's modules, so the mod's own files can run under `node --test`. An element records every call, so a
// test can read its text and press what's pressable. This proves the mod's logic, not that the real app accepts its
// calls: `herdr-app check` does that.
import { registerHooks } from "node:module";

export class El {
  constructor(tag) { this.tag = tag; this.kids = []; this.calls = []; this.press = null; this.elementId = null; }
  child(value) { if (value !== undefined && value !== null) this.kids.push(value); return this; }
  children(list) { for (const value of list) this.child(value); return this; }
  when(on, change) { return on ? change(this) : this; }
  on_click(press) { this.press = press; return this; }
  id(value) { this.elementId = value; return this; }
  hover(style) { style(element("hover")); return this; }
  text() { return this.kids.map((k) => (k instanceof El ? k.text() : String(k))).join(" "); }
  find(id) {
    if (this.elementId === id) return this;
    for (const k of this.kids) {
      const found = k instanceof El ? k.find(id) : null;
      if (found) return found;
    }
    return null;
  }
}

export const element = (tag) => new Proxy(new El(tag), {
  get(target, name, self) {
    if (name in target || typeof name === "symbol") {
      const value = target[name];
      return typeof value === "function" ? value.bind(self) : value;
    }
    return (...args) => { target.calls.push([name, args]); return self; };
  },
});

// What the fake `herdr` module reads. A test sets these.
export const world = { exec: async () => ({ code: 0, stdout: "{}", stderr: "" }), props: {} };

export const theme = Object.fromEntries(["background", "canvas", "surface", "popover", "raised", "border", "input", "foreground", "muted", "dim", "accent", "solid", "solid_foreground", "blocked"]
  .map((name, i) => [name, `#${String(i + 10).repeat(3)}`]));

const stubs = {
  "gpui-kit": `import { element } from ${JSON.stringify(import.meta.url)};
export class View {}
export const div = () => element("div");`,
  "gpui-base": `import { element } from ${JSON.stringify(import.meta.url)};
export const v_flex = () => element("v_flex");
export const h_flex = () => element("h_flex");`,
  herdr: `import { world, theme as colours } from ${JSON.stringify(import.meta.url)};
export const appHome = () => "/h/.config/herdr-app";
export const home = () => "/h";
export const exec = (...args) => world.exec(...args);
export const props = () => world.props;
export const theme = () => colours;`,
};

registerHooks({
  resolve(specifier, context, next) {
    if (specifier in stubs) return { url: `stub:${specifier}`, shortCircuit: true };
    return next(specifier, context);
  },
  load(url, context, next) {
    if (url.startsWith("stub:")) return { format: "module", source: stubs[url.slice(5)], shortCircuit: true };
    return next(url, context);
  },
});

// A cx whose sleep never ends, so `timed` only ever sees the real answer unless a test says otherwise.
export function fakeCx() {
  const tasks = [];
  const cx = {
    copied: null, notified: 0,
    notify() { this.notified += 1; },
    sleep: () => new Promise(() => {}),
    spawn(body) { tasks.push(body(cx)); },
    write_to_clipboard(text) { this.copied = text; },
    settled: async () => { while (tasks.length) await tasks.shift(); },
  };
  return cx;
}
