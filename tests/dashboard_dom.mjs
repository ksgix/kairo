// Runs the dashboard's real app.js against a live dashboard, in a minimal DOM:
// enough to render the page, fill fields and click buttons, not a browser (no layout,
// no CSS, no browser security model). Used by test_directive_purpose and test_dashboard.
//
//   node dashboard_dom.mjs PORT TOKEN_FILE STEPS_JSON
//
// STEPS: [{"fill": {ID: TEXT}} | {"click": BUTTON_TEXT} | {"wait": MS} |
//         {"snapshot": LABEL}]. Prints {"snapshots": {...}, "errors": [...]}.
// A snapshot has the whole page's text, each panel's text by id, the form controls,
// labels and directive cards.
import fs from "node:fs";

const [port, tokenFile, stepsJson] = process.argv.slice(2);
const base = `http://127.0.0.1:${port}`;
const token = fs.readFileSync(tokenFile, "utf8").trim();
const login = await fetch(base + "/login", {
  method: "POST", redirect: "manual", body: new URLSearchParams({token}),
  headers: {"Content-Type": "application/x-www-form-urlencoded", Origin: base}});
const cookie = (login.headers.get("set-cookie") || "").split(";")[0];
const page = await (await fetch(base + "/", {headers: {cookie}})).text();
const csrf = (page.match(/name="kairo-csrf" content="([^"]+)"/) || [])[1];
const appJs = await (await fetch(base + "/static/app.js", {headers: {cookie}})).text();

class Node_ {}
class Text extends Node_ { constructor(t) { super(); this.data = String(t); } get textContent() { return this.data; } }
class El extends Node_ {
  constructor(tag) {
    super(); this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {};
    this.listeners = {}; this.className = ""; this.hidden = false; this.value = "";
    this.open = false; this.parentElement = null; this._text = null; this.dataset = {};
    this.style = {}; this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0;
    this.classList = {toggle: (c, on) => { this.className = on ? c : ""; }};
  }
  setAttribute(k, v) { this.attrs[k] = v; if (k === "id") this.id = v; }
  addEventListener(k, f) { (this.listeners[k] = this.listeners[k] || []).push(f); }
  append(...nodes) { for (const n of nodes) { n.parentElement = this; this.children.push(n); } }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  set textContent(t) { this._text = String(t); this.children = []; }
  get textContent() {
    return this.children.length ? this.children.map((c) => c.textContent).join("") : (this._text ?? "");
  }
  walk(f) { f(this); for (const c of this.children) if (c instanceof El) c.walk(f); }
  querySelectorAll(sel) {
    const out = [];
    this.walk((n) => {
      if (n !== this && n.tagName === "SUMMARY"
          && (sel === "details > summary" || (sel === "details[open] > summary" && n.parentElement.open))) out.push(n);
    });
    return out;
  }
  focus() {}
  setSelectionRange() {}
}
globalThis.Node = Node_;
// Every element the page gives an id exists; what the code renders goes inside them.
const ids = {}, root = new El("body");
for (const m of page.matchAll(/<(\w+)[^>]*\sid="([\w-]+)"/g)) { ids[m[2]] = new El(m[1]); ids[m[2]].id = m[2]; root.append(ids[m[2]]); }
const find = (pred) => { let hit = null; root.walk((n) => { if (!hit && pred(n)) hit = n; }); return hit; };
globalThis.document = {
  hidden: false, activeElement: null,
  querySelector: () => ({content: csrf}),
  querySelectorAll: () => [],
  getElementById: (id) => ids[id] || find((n) => n.id === id),
  createElement: (tag) => new El(tag), createTextNode: (t) => new Text(t), addEventListener() {},
};
globalThis.location = {hash: "", href: ""};
globalThis.window = {scrollY: 0, scrollTo() {}};
globalThis.confirm = () => true;
globalThis.alert = () => {};
globalThis.setInterval = () => 0;
const realFetch = fetch;
let inFlight = 0;
globalThis.fetch = (path, opts = {}) => {
  inFlight += 1;
  return realFetch(base + path, {...opts, headers: {...(opts.headers || {}), cookie, Origin: base}})
    .finally(() => { inFlight -= 1; });
};
const errors = [];
process.on("unhandledRejection", (e) => errors.push(String(e)));
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
// Wait for the page to come to rest, however slow the machine: no request in
// flight and nothing still loading, several checks in a row (at most 20 s).
async function settle() {
  let calm = 0;
  for (let i = 0; i < 400 && calm < 4; i += 1) {
    await wait(50);
    calm = inFlight === 0 && !root.textContent.includes("Loading…") ? calm + 1 : 0;
  }
  if (calm < 4) errors.push("the page did not settle");
}

new Function(appJs)();  // the dashboard's own code, unmodified
await settle();
const snapshots = {};
for (const step of JSON.parse(stepsJson)) {
  if (step.wait) {
    await wait(step.wait);
    await settle();
  } else if (step.fill) {
    for (const [id, text] of Object.entries(step.fill)) {
      const field = document.getElementById(id);
      field.value = text;
      for (const f of field.listeners.input || []) f();
    }
  } else if (step.click) {
    const button = find((n) => n.tagName === "BUTTON" && n.textContent === step.click);
    if (!button) { errors.push(`no button ${step.click}`); continue; }
    await button.listeners.click[0]();
    await settle();
  } else if (step.snapshot) {
    const controls = [], labels = [], cards = [];
    root.walk((n) => {
      if (n.tagName === "INPUT" || n.tagName === "TEXTAREA") {
        controls.push({tag: n.tagName.toLowerCase(), id: n.id || null, required: "required" in n.attrs,
                       maxlength: n.attrs.maxlength ?? null, placeholder: n.attrs.placeholder ?? null});
      } else if (n.tagName === "LABEL") {
        labels.push({for: n.attrs.for ?? null, text: n.textContent});
      } else if (n.tagName === "SECTION" && n.className.includes("directive")) {
        const heading = n.children.length ? n.children[0].textContent : "";
        const description = (() => { let d = null; n.walk((c) => { if (!d && c.className === "description") d = c.textContent; }); return d; })();
        cards.push({heading, description, text: n.textContent});
      }
    });
    snapshots[step.snapshot] = {controls, labels, cards, text: root.textContent,
                                panels: Object.fromEntries(Object.entries(ids).map(([id, n]) => [id, n.textContent])),
                                header: {state: ids["state-text"].textContent, notice: ids.notice.hidden ? null : ids.notice.textContent}};
  }
}
// Exit only once the whole report is written: a pipe takes large output in pieces.
process.stdout.write(JSON.stringify({snapshots, errors}) + "\n", () => process.exit(0));
