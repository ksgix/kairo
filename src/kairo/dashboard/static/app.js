// Kairo dashboard: one page that renders what the runtime reports through the
// dashboard's operator-IPC API. Every text from Kairo is inserted as text (never as
// HTML), and labelled by where it comes from: runtime fact, Kairo's own words,
// untrusted content, or operator input. The page holds only view state.
"use strict";
(() => {
  const CSRF = document.querySelector('meta[name="kairo-csrf"]').content;
  const MAX_BACKOFF = 60000, PAGE = 50, LONG_MESSAGE = 700, WINDOW_DAYS = 14;
  const S = {
    status: null, situation: null, directives: null, metrics: null, dashboard: null, err: {},
    chat: [], chatMoreBefore: false, chatLimit: 50, expanded: {},
    activity: [], activityNext: null, filter: "all", loadingEarlier: false,
    outbox: null, updated: null, drafts: {}, notes: {},
  };
  const byId = (id) => document.getElementById(id);

  // -- DOM helpers: text only --------------------------------------------------------
  function el(tag, props, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") n.className = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : String(v));
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return n;
  }
  const PROV = {fact: "runtime fact", interp: "Kairo's words", untrusted: "untrusted content",
                operator: "operator"};
  const prov = (kind) => el("span", {class: `prov ${kind}`}, PROV[kind]);
  function kv(pairs) {
    const dl = el("dl", {class: "kv"});
    for (const [k, v] of pairs) {
      if (v === undefined) continue;
      dl.append(el("dt", null, k), el("dd", null, v === null || v === "" ? "—" : v));
    }
    return dl;
  }
  // An input whose text survives re-rendering (drafts are view state only).
  function field(tag, id, props) {
    const n = el(tag, {id, ...props});
    n.value = S.drafts[id] || "";
    n.addEventListener("input", () => { S.drafts[id] = n.value; });
    return n;
  }
  const pre = (value) => el("pre", null, typeof value === "string" ? value : JSON.stringify(value, null, 1));
  const interp = (text) => (text ? el("div", {class: "interp-text"}, prov("interp"), text) : el("span", {class: "muted"}, "—"));
  const short = (id) => (typeof id === "string" ? id.slice(0, 8) : "—");
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const num = (n) => (typeof n === "number" ? n.toLocaleString("en-US") : "—");
  const money = (n) => (typeof n === "number" ? `$${n.toFixed(2)}` : "—");
  function badge(text, tone) { return el("span", {class: `badge ${tone || ""}`}, text); }
  const STATE_TONE = {verified_successful: "ok", executed_unverified: "", verified_failed: "bad",
                      exited_nonzero: "bad", failed_to_execute: "bad", interrupted: "warn",
                      outcome_unknown: "warn", in_progress: "", awaiting_confirmation: "warn",
                      active: "ok", waiting: "warn", blocked: "bad", completed: "ok", abandoned: ""};
  const stateBadge = (state) => badge(String(state || "unknown").replace(/_/g, " "), STATE_TONE[state]);
  const FAILED_STATES = ["verified_failed", "exited_nonzero", "failed_to_execute", "interrupted", "outcome_unknown"];
  function errorBox(err, what) {
    if (!err) return null;
    return el("p", {class: "error"}, `${what}: ${err.error || "failed"}`, err.code ? ` (${err.code})` : "");
  }
  function sized(n, property, value) { n.style[property] = value; return n; }

  // -- time: always UTC, always absolute ------------------------------------------------
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function date(t) {   // a runtime timestamp in seconds, an ISO string, or the situation's {"at"}
    if (t && typeof t === "object") t = t.at;
    const ms = typeof t === "number" ? t * 1000 : typeof t === "string" ? Date.parse(t) : NaN;
    return Number.isFinite(ms) ? new Date(ms) : null;
  }
  const two = (n) => String(n).padStart(2, "0");
  function clock(t) { const d = date(t); return d ? `${two(d.getUTCHours())}:${two(d.getUTCMinutes())}:${two(d.getUTCSeconds())}` : "--:--:--"; }
  function day(t) { const d = date(t); return d ? `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}` : "unknown day"; }
  function stamp(t) { const d = date(t); return d ? `${day(t)} ${two(d.getUTCHours())}:${two(d.getUTCMinutes())} UTC` : "unknown time"; }
  function span(seconds) {
    if (typeof seconds !== "number") return "";
    if (seconds < 90) return `${Math.round(seconds)} s`;
    if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
    if (seconds < 172800) return `${(seconds / 3600).toFixed(1)} h`;
    return `${(seconds / 86400).toFixed(1)} d`;
  }

  // -- API: one request, one operator operation ----------------------------------------
  async function api(path, body) {
    const opts = {credentials: "same-origin", headers: {}};
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.headers["X-Kairo-CSRF"] = CSRF;
      opts.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, opts);
    } catch (e) {
      return {ok: false, code: "dashboard_unreachable", error: "the dashboard itself is not reachable"};
    }
    if (response.status === 401) {
      location.href = "/login";
      return {ok: false, code: "unauthorized", error: "log in"};
    }
    try {
      return await response.json();
    } catch (e) {
      return {ok: false, code: "bad_response", error: `HTTP ${response.status}`};
    }
  }
  function clientId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }
  // Whether the runtime has an operation (an older release may not).
  const supports = (op) => !S.status || !Array.isArray(S.status.ops) || S.status.ops.includes(op);

  // -- loading ---------------------------------------------------------------------
  async function read(name, path) {
    const r = await api(path);
    if (r.ok) { S[name] = r.result; S.err[name] = null; } else { S.err[name] = r; }
    return r.ok;
  }
  async function loadStatus() {
    const ok = await read("status", "/api/status");
    S.updated = Date.now() / 1000;
    return ok;
  }
  const loadSituation = () => read("situation", "/api/situation");
  const loadDirectives = () => read("directives", "/api/directives");
  const loadDashboard = () => read("dashboard", "/api/dashboard");
  const loadMetrics = () => (supports("metrics") ? read("metrics", "/api/metrics") : true);
  async function loadChat() {
    const last = S.chat.length ? S.chat[S.chat.length - 1].seq : null;
    const r = await api(last === null ? `/api/chat?limit=${S.chatLimit}` : `/api/chat?after=${last}&limit=200`);
    if (!r.ok) { S.err.chat = r; return false; }
    S.err.chat = null;
    if (last === null) S.chatMoreBefore = r.result.more_before;
    S.chat = S.chat.concat(r.result.messages);
    return true;
  }
  async function loadActivity() {   // the newest page; earlier pages already loaded are kept
    if (!supports("activity")) return true;
    const r = await api(`/api/activity?limit=${PAGE}`);
    if (!r.ok) { S.err.activity = r; return false; }
    S.err.activity = null;
    const items = r.result.items;
    const lowest = items.length ? items[items.length - 1].seq : null;
    const known = S.activity.length ? S.activity[0].seq : null;
    if (known === null || (lowest !== null && lowest > known)) {  // first read, or a gap since the last
      S.activity = items;
      S.activityNext = r.result.next_before;
    } else {
      S.activity = items.concat(S.activity.filter((i) => lowest === null || i.seq < lowest));
    }
    return true;
  }
  async function loadEarlier() {
    if (!S.activityNext || S.loadingEarlier) return;
    S.loadingEarlier = true;
    const r = await api(`/api/activity?limit=${PAGE}&before=${S.activityNext}`);
    S.loadingEarlier = false;
    if (r.ok) { S.activity = S.activity.concat(r.result.items); S.activityNext = r.result.next_before; S.err.activity = null; }
    else S.err.activity = r;
    render();
  }

  // Bounded polling: only while the tab is visible, slower after errors. Every loader
  // is a read operation: polling never wakes or changes Kairo.
  const LOADS = [[loadStatus, 5000], [loadChat, 5000], [loadActivity, 15000], [loadSituation, 20000],
                 [loadDirectives, 30000], [loadMetrics, 60000], [loadDashboard, 60000]];
  const loaded = new Map();
  let backoff = 0, busy = false;
  async function tick(force) {
    if (busy || (document.hidden && !force)) return;
    const now = Date.now();
    const due = LOADS.filter(([load, every]) => force || now - (loaded.get(load) || 0) >= every + backoff);
    if (!due.length) return;
    busy = true;
    try {
      const results = await Promise.all(due.map(([load]) => load()));
      for (const [load] of due) loaded.set(load, Date.now());
      backoff = results.every(Boolean) ? 0 : Math.min(Math.max(backoff * 2, 5000), MAX_BACKOFF);
      render();
    } finally {
      busy = false;
    }
  }

  // -- painting: a panel is rebuilt only when what it shows has changed -----------------
  // So scrolling, typed text, opened details and selected text survive polling.
  const VOLATILE = new Set(["age_seconds", "due_in_seconds", "passed_seconds_ago"]);
  const painted = {}, scrollers = {};
  let afterPaint = [];
  function paint(id, data, build) {
    const key = JSON.stringify(data, (k, v) => (VOLATILE.has(k) ? undefined : v));
    if (painted[id] === key) return;
    painted[id] = key;
    const host = byId(id);
    const active = document.activeElement;
    const focus = active && active.id && ["INPUT", "TEXTAREA"].includes(active.tagName)
      ? {id: active.id, start: active.selectionStart, end: active.selectionEnd} : null;
    const open = new Set(Array.from(host.querySelectorAll("details[open] > summary"), (s) => s.textContent));
    afterPaint = [];
    host.replaceChildren(...[build(data)].flat(Infinity).filter(Boolean));
    for (const s of host.querySelectorAll("details > summary")) if (open.has(s.textContent)) s.parentElement.open = true;
    for (const restore of afterPaint) restore();
    if (focus && document.activeElement !== active) {
      const n = byId(focus.id);
      if (n) { n.focus(); try { n.setSelectionRange(focus.start, focus.end); } catch (e) { /* not a text field */ } }
    }
  }
  // A scrolling box that keeps its position across repaints; ``stick`` keeps it at
  // the end while the reader is at the end (a chat).
  function scroller(name, cls, stick, ...kids) {
    const before = scrollers[name];
    const atEnd = !before || before.scrollTop + before.clientHeight >= before.scrollHeight - 24;
    const top = before ? before.scrollTop : 0;
    const n = el("div", {class: cls}, kids);
    scrollers[name] = n;
    afterPaint.push(() => { n.scrollTop = stick && atEnd ? n.scrollHeight : top; });
    return n;
  }
  const head = (title, ...kids) => el("div", {class: "panel-head"}, el("h2", null, title), kids);

  // -- header ------------------------------------------------------------------------
  function renderHeader() {
    const st = S.status, err = S.err.status;
    const pill = byId("state"), text = byId("state-text"), notice = byId("notice");
    notice.hidden = true;
    if (err) {
      pill.className = "pill bad";
      text.textContent = err.code === "unreachable" ? "Kairo unreachable" : `error: ${err.code || "unknown"}`;
      notice.textContent = `${err.error || "Kairo could not be reached"}. Nothing shown is current until it answers again.`;
      notice.hidden = false;
    } else if (st) {
      pill.className = `pill ${st.state === "awake" ? "awake" : st.state === "sleeping" ? "sleeping" : "warn"}`;
      const until = st.state !== "sleeping" ? "" : st.wake_at ? ` · wakes ${stamp(st.wake_at)}` : " · until woken";
      text.textContent = String(st.state).replace(/^./, (c) => c.toUpperCase()) + until;
      if ((st.protocol || 1) < 2) {
        notice.textContent = "This Kairo runtime predates operator protocol 2: only its status is available here. Deploy a newer release.";
        notice.hidden = false;
      }
    }
    byId("rev").textContent = st && st.revision ? `release ${st.revision.slice(0, 7)}` : "";
    byId("updated").textContent = S.updated ? `updated ${clock(S.updated)} UTC` : "";
    byId("flash").textContent = S.notes.header || "";
  }

  // -- monitors: one tile per thing that can be wrong ------------------------------------
  const GLYPH = {ok: "✓", check: "!", bad: "×", unknown: "?"};
  const WORD = {ok: "OK", check: "Check", bad: "Problem", unknown: "Unknown"};
  function monitor(name, tone, title, sub) {
    return el("div", {class: `monitor ${tone}`},
      el("div", {class: "monitor-head"}, el("span", {class: `glyph ${tone}`, "aria-hidden": true}, GLYPH[tone]),
         el("span", {class: "monitor-name"}, name), el("span", {class: "monitor-word"}, WORD[tone])),
      el("div", {class: "monitor-title"}, title), sub ? el("div", {class: "monitor-sub"}, sub) : null);
  }
  function runtimeMonitor(d) {
    if (d.statusErr) return monitor("Runtime", "bad", d.statusErr.code === "unreachable" ? "Not reachable" : "Not answering", d.statusErr.error);
    const st = d.st;
    if (!st) return monitor("Runtime", "unknown", "Loading…");
    const spans = (d.running || {}).spans || [];
    const current = spans.length && spans[spans.length - 1].end === "running" ? spans[spans.length - 1] : null;
    const facts = [current ? `Up since ${stamp(current.from)}` : null, `start ${st.starts ?? "?"}`,
                   st.quiet_wakes ? `${plural(st.quiet_wakes, "timer wake")} without a model call` : null];
    const tone = st.state === "awake" || st.state === "sleeping" ? "ok" : "check";
    return monitor("Runtime", tone, st.state === "sleeping" ? "Running, asleep" : `Running, ${st.state}`, facts.filter(Boolean).join(" · "));
  }
  function modelMonitor(d) {
    const st = d.st;
    if (!st) return monitor("Model", "unknown", "Loading…");
    if (!st.cognition) return monitor("Model", "check", "No model configured", "Kairo runs cycles but decides nothing.");
    const last = st.cognition_last, today = d.today || {};
    const failures = today.failed ? `${plural(today.failed, "failed call")} today` : "no failures today";
    if (!last) return monitor("Model", "unknown", "No call yet", st.cognition);
    if (last.result === "failed") return monitor("Model", "bad", `Last call failed: ${last.failure || "unknown"}`, `${stamp(last.at)} · ${failures}`);
    return monitor("Model", today.failed ? "check" : "ok", last.result === "decided" ? "Last call decided" : `Last cycle: ${last.result}`,
                   `${stamp(last.at)} · ${last.provider || st.cognition} · ${failures}`);
  }
  function releaseMonitor(d) {
    if (!d.sit) return monitor("Release", "unknown", "Loading…");
    const code = d.code;
    if (!code || !(code.running || {}).revision) return monitor("Release", "unknown", "Not running from a release", "Self-deployment is not configured for this runtime.");
    const status = code.running.status, rev = code.running.revision.slice(0, 7);
    const previous = code.previous ? `Previous: ${code.previous.slice(0, 7)}` : "No previous release";
    const repo = code.repository || {};
    const ahead = repo.head && !repo.head_is_running ? ` · dev HEAD ${repo.head.slice(0, 7)} is not deployed` : "";
    const dirty = repo.dirty_files ? ` · ${plural(repo.dirty_files, "uncommitted file")}` : "";
    if (status === "confirmed") return monitor("Release", "ok", `${rev} confirmed`, previous + ahead + dirty);
    if (status === "operator_selected") return monitor("Release", "ok", `${rev} selected by the operator`, previous + ahead + dirty);
    return monitor("Release", "check", `${rev} ${String(status || "unknown").replace(/_/g, " ")}`, previous + ahead + dirty);
  }
  function dashboardMonitor(d) {
    const dash = d.dash, running = (d.st || {}).revision;
    if (!dash) return monitor("Dashboard", "unknown", "Loading…");
    if (!dash.revision || !running) return monitor("Dashboard", "ok", "Serving", `Started ${stamp(dash.started_at)}, not from a release.`);
    if (dash.revision === running) return monitor("Dashboard", "ok", "Serving the running release", `Started ${stamp(dash.started_at)} on ${dash.revision.slice(0, 7)}.`);
    return monitor("Dashboard", "check", "Serving another release", `Started ${stamp(dash.started_at)} on ${dash.revision.slice(0, 7)}; Kairo runs ${running.slice(0, 7)}. Restart kairo-dashboard to match.`);
  }
  function purposeMonitor(d) {
    if (!d.st) return monitor("Purpose", "unknown", "Loading…");
    if (!d.st.directives) return monitor("Purpose", "check", "No active directive", "Kairo acts only on your messages.");
    return monitor("Purpose", "ok", plural(d.st.directives, "active directive"), d.firstDirective ? el("span", null, prov("operator"), d.firstDirective) : null);
  }
  function attentionMonitor(d) {
    if (!d.sit) return monitor("Needs you", "unknown", "Loading…");
    const t = d.threads || {};
    const items = [
      [(t.unanswered_human_messages || []).length, "unanswered message"],
      [d.blocked, "blocked work item"],
      [(t.actions_outcome_unknown || []).length, "action with unknown outcome"],
      [(t.work_wait_elapsed || []).length, "elapsed wait"],
    ].filter(([n]) => n).map(([n, word]) => plural(n, word));
    if (t.previous_cycle_failed) items.push(`last cycle failed (${t.previous_cycle_failed.failure})`);
    if (!items.length) return monitor("Needs you", "ok", "Nothing waiting", `${plural(d.open, "open work item")}.`);
    return monitor("Needs you", "check", items[0].replace(/^./, (c) => c.toUpperCase()), items.slice(1).join(" · "));
  }
  function renderMonitors() {
    const st = S.status, sit = S.situation, days = (S.metrics || {}).days || [];
    const open = ((sit || {}).work || {}).open || [];
    const active = ((sit || {}).directives || {}).active || [];
    const d = {
      st: st && {state: st.state, starts: st.starts, quiet_wakes: st.quiet_wakes, cognition: st.cognition,
                 cognition_last: st.cognition_last, directives: st.directives, revision: st.revision},
      statusErr: S.err.status, sit: !!sit, code: ((sit || {}).kairo || {}).code, threads: (sit || {}).open_threads,
      blocked: open.filter((w) => w.state === "blocked").length, open: open.length,
      firstDirective: active.length ? active[0].statement : null,
      dash: S.dashboard, running: (S.metrics || {}).running, today: days[days.length - 1],
    };
    paint("monitors", d, () => [runtimeMonitor(d), modelMonitor(d), releaseMonitor(d), dashboardMonitor(d),
                                purposeMonitor(d), attentionMonitor(d)]);
  }

  // -- runtime timeline -----------------------------------------------------------------
  function unavailable(op, err) {
    if (!supports(op) || (err && err.code === "unknown_op")) {
      return el("p", {class: "muted"}, "This Kairo runtime predates this view. Deploy a newer release to see it.");
    }
    return errorBox(err, "could not be read") || el("p", {class: "muted"}, "Loading…");
  }
  function segments(spans, start, now) {
    const out = [];
    spans.forEach((s, i) => {
      const until = i + 1 < spans.length ? spans[i + 1].from : now;
      const ran = s.end === "running" ? now : s.end === "stopped" ? s.to
        : Math.min(Math.max(s.last_record_at || s.from, s.from), until);
      const about = `${s.revision ? "release " + s.revision.slice(0, 7) + " · " : ""}${s.start_reason || ""}`;
      if (ran > start) out.push({kind: "running", from: Math.max(s.from, start), to: ran, about});
      if (s.end !== "running" && until > Math.max(ran, start)) {
        out.push({kind: s.end === "stopped" ? "stopped" : "unknown", from: Math.max(ran, start), to: until,
                  about: s.end === "stopped" ? `${s.stop_reason || ""}${s.exit_code ? " (exit " + s.exit_code + ")" : ""}`
                    : "no stop was recorded: killed, crashed or power lost at some point in this stretch"});
      }
    });
    return out.filter((g) => g.to > g.from);
  }
  function renderTimeline() {
    const m = S.metrics;
    const d = {running: m && m.running, deployments: m && m.deployments, now: m && m.now, err: S.err.metrics, ok: supports("metrics")};
    paint("timeline", d, () => {
      const spans = (d.running || {}).spans || [];
      if (!d.running) return [head("Runtime"), unavailable("metrics", d.err)];
      if (!spans.length) return [head("Runtime"), el("p", {class: "muted"}, "No start has been recorded yet.")];
      const now = d.now, start = Math.max(spans[0].from, now - WINDOW_DAYS * 86400), total = Math.max(now - start, 1e-6);
      const segs = segments(spans, start, now);
      const ran = segs.filter((g) => g.kind === "running").reduce((sum, g) => sum + g.to - g.from, 0);
      const marks = (d.deployments || []).filter((p) => typeof p.at === "number" && p.at >= start && p.at <= now);
      const kinds = new Set(segs.map((g) => g.kind));
      const label = {running: "Running", stopped: "Stopped", unknown: "Not recorded"};
      const said = segs.map((g) => `${label[g.kind]} ${stamp(g.from)} to ${stamp(g.to)}`).join("; ");
      const track = el("div", {class: "track", role: "img", "aria-label": `${said}. ${plural(marks.length, "deployment")}.`},
        segs.map((g, i) => sized(el("div", {class: `seg ${g.kind}${i === segs.length - 1 ? " last" : ""}`,
          title: `${label[g.kind]}: ${stamp(g.from)} to ${i === segs.length - 1 && g.kind === "running" ? "now" : stamp(g.to)} (${span(g.to - g.from)})${g.about ? " · " + g.about : ""}`}),
          "flex", `${g.to - g.from} 1 0`)),
        marks.map((p) => sized(el("div", {class: "mark", title: `Deployment ${stamp(p.at)}: ${short(p.from)} → ${short(p.to)} (${String(p.state).replace(/_/g, " ")})`}),
          "left", `${((p.at - start) / total * 100).toFixed(2)}%`)));
      const ticks = [];
      const step = total > 10 * 86400 ? 2 : 1;
      for (let t = (Math.floor(start / 86400) + 1) * 86400, i = 0; t < now; t += 86400, i += 1) {
        const at = (t - start) / total * 100;
        if (at > 12 && at < 90 && i % step === 0) ticks.push(sized(el("span", null, day(t)), "left", `${at.toFixed(2)}%`));
      }
      return [
        el("div", {class: "timeline-head"},
          el("h2", null, `Runtime since ${stamp(start)}`),
          el("span", {class: "small muted"}, `Running ${(ran / total * 100).toFixed(1)}% of ${span(total)}`),
          el("span", {class: "spacer"}),
          el("div", {class: "legend"},
            el("span", null, el("i", {class: "swatch", "aria-hidden": true}), "Running"),
            kinds.has("stopped") ? el("span", null, el("i", {class: "swatch stopped", "aria-hidden": true}), "Stopped") : null,
            kinds.has("unknown") ? el("span", null, el("i", {class: "swatch unknown", "aria-hidden": true}), "Not recorded") : null,
            marks.length ? el("span", null, el("i", {class: "swatch tick", "aria-hidden": true}), "Deployment") : null)),
        track,
        el("div", {class: "axis", "aria-hidden": true}, el("span", null, day(start)), ticks, el("span", {class: "end"}, "now")),
        d.running.omitted_older ? el("p", {class: "muted small"}, "Earlier starts and stops exist and are not drawn.") : null,
      ];
    });
  }

  // -- key figures ----------------------------------------------------------------------
  const BAR = 64;  // pixels of the tallest bar
  function bars(days, parts, describe) {
    const max = Math.max(1, ...days.map((x) => parts.reduce((sum, [key]) => sum + (x[key] || 0), 0)));
    return el("div", {class: "bars", role: "img", "aria-label": days.map(describe).join("; ")},
      days.map((x) => el("div", {class: "bar", title: describe(x)},
        parts.map(([key, cls]) => (x[key] ? sized(el("i", {class: cls}), "height", `${Math.max(2, Math.round(x[key] / max * BAR))}px`) : null)))));
  }
  function figure(name, body, foot) {
    return el("div", {class: "figure"}, el("div", {class: "figure-name"}, name), body, foot ? el("div", {class: "figure-foot"}, foot) : null);
  }
  const label = (x) => { const [, m, dd] = x.day.split("-"); return `${Number(dd)} ${MONTHS[Number(m) - 1]}`; };
  function renderFigures() {
    const m = S.metrics, sit = S.situation;
    const d = {days: m && m.days, complete: m && m.days_complete, context: m && m.last_context, work: m && m.work,
               trimmed: ((sit || {}).context || {}).trimmed_for_budget, shortened: ((sit || {}).context || {}).long_texts_shortened_for_budget,
               err: S.err.metrics, ok: supports("metrics")};
    paint("figures", d, () => {
      if (!d.days) return el("div", {class: "figure"}, el("div", {class: "figure-name"}, "Key figures"), unavailable("metrics", d.err));
      const days = d.days.slice(-WINDOW_DAYS), today = days[days.length - 1] || {};
      const peak = days.reduce((a, b) => (b.calls > a.calls ? b : a), days[0] || {calls: 0});
      const costly = days.filter((x) => typeof x.cost_usd === "number");
      const dearest = costly.reduce((a, b) => (b.cost_usd > a.cost_usd ? b : a), costly[0]);
      const anyFailed = days.some((x) => x.failed);
      const calls = figure("Model calls per day",
        el("div", {class: "figure-body"},
          el("div", null, el("div", {class: "big"}, num(today.calls)), el("div", {class: "unit"}, "today so far (UTC)")),
          bars(days, [["decided", ""], ["failed", "failed"]], (x) => `${label(x)}: ${plural(x.calls, "call")}${x.failed ? `, ${x.failed} failed (${Object.entries(x.failures).map(([k, n]) => `${n} ${k}`).join(", ")})` : ""}`)),
        [el("span", null, peak && peak.calls ? `Peak ${num(peak.calls)} on ${label(peak)}` : "No calls recorded"),
         anyFailed ? el("span", {class: "legend"}, el("span", null, el("i", {class: "swatch", "aria-hidden": true}), "decided"),
                        el("span", null, el("i", {class: "swatch failed", "aria-hidden": true}), "failed")) : null]);
      const cost = figure("Model cost per day, as the provider reports it",
        el("div", {class: "figure-body"},
          el("div", null, el("div", {class: "big"}, money(today.cost_usd)), el("div", {class: "unit"}, "today so far (UTC)")),
          bars(costly.length ? days : [], [["cost_usd", ""]], (x) => `${label(x)}: ${typeof x.cost_usd === "number" ? money(x.cost_usd) + ` over ${plural(x.costed, "call")}` : "no cost reported"}`)),
        [el("span", null, dearest ? `Peak ${money(dearest.cost_usd)} on ${label(dearest)}` : "No cost reported yet"),
         today.costed ? el("span", null, `${money(today.cost_usd / today.costed)} per call today`) : null]);
      const c = d.context;
      const share = c ? c.chars / c.budget : 0;
      const context = figure("Context sent to the model, last call",
        c ? [el("div", {class: "big"}, num(c.chars), " ", el("small", null, `of ${num(c.budget)} characters`)),
             el("div", {class: `meter${share >= 0.95 ? " near" : ""}`, role: "img", "aria-label": `Context is at ${(share * 100).toFixed(1)} percent of its budget`},
                sized(el("i"), "width", `${Math.min(100, share * 100).toFixed(1)}%`))]
          : el("div", {class: "muted"}, "No model call has reported its context size yet."),
        [c ? el("span", null, `${share >= 0.95 ? "At the budget" : `${(share * 100).toFixed(0)}% of the budget`} · ${stamp(c.at)}`) : null,
         d.trimmed || d.shortened ? el("span", null, `Now: ${plural(d.trimmed || 0, "history item")} left out, ${plural(d.shortened || 0, "long text")} shortened to fit`) : null]);
      const states = (d.work || {}).by_state || {}, basis = (d.work || {}).completed_by_basis || {};
      const count = (n, word) => el("div", null, el("div", {class: "big"}, num(n || 0)), el("div", {class: "unit"}, word));
      const work = figure("Work",
        el("div", {class: "counts"}, count((states.active || 0) + (states.waiting || 0) + (states.blocked || 0), "open"),
           count(states.completed, "completed"), count(states.abandoned, "abandoned")),
        states.completed ? el("span", null, `Completions: ${basis.verified || 0} verified by the runtime, ${basis.checked || 0} by their own check, ${(basis.unverified || 0) + (basis.unknown || 0)} on Kairo's judgment`) : null);
      const table = el("details", {class: "figure"}, el("summary", null, "Daily figures as a table"),
        el("div", {class: "scroll"}, el("table", null,
          el("thead", null, el("tr", null, el("th", null, "Day (UTC)"), ["Cycles", "Decided", "Failed", "Cost"].map((h) => el("th", {class: "num"}, h)), el("th", null, "Failures"))),
          el("tbody", null, days.slice().reverse().map((x) => el("tr", null, el("td", null, x.day),
            [num(x.cycles), num(x.decided), num(x.failed), money(x.cost_usd)].map((v) => el("td", {class: "num"}, v)),
            el("td", null, Object.entries(x.failures).map(([k, n]) => `${n} ${k}`).join(", ") || "—")))))),
        d.complete ? null : el("p", {class: "muted small"}, "More cycles fall inside these days than were counted: the earliest days are incomplete."));
      return [calls, cost, context, work, table];
    });
  }

  // -- directives -----------------------------------------------------------------------
  function renderDirectives() {
    const linked = {};
    for (const w of ((S.situation || {}).work || {}).open || []) {
      if (w.directive_id) (linked[w.directive_id] = linked[w.directive_id] || []).push({state: w.state, objective: w.objective});
    }
    const d = {directives: S.directives && S.directives.directives, omitted: S.directives && S.directives.omitted_older,
               err: S.err.directives, linked, note: S.notes.directive};
    paint("directives", d, () => {
      const statement = field("input", "directive", {placeholder: "The purpose, concisely, e.g. “Keep the backups verified and restorable”", maxlength: 500, required: true});
      const description = field("textarea", "directive-description", {placeholder: "What this purpose covers: its intent, scope, expectations and boundaries. Kairo decides the concrete work itself.", maxlength: 4000, rows: 5, required: true});
      const note = el("p", {class: "muted small"}, d.note || "");
      const form = el("details", null, el("summary", null, "Add a directive"),
        el("p", {class: "hint"}, "A directive is Kairo's purpose: a lasting area of responsibility, not a task or command. Adding one executes nothing and creates no work; Kairo reassesses with it from its next cycle and decides itself what, if anything, is worth pursuing for it."),
        el("label", {for: "directive"}, "Statement (required)"),
        el("p", {class: "hint"}, "The enduring purpose, in one sentence: why Kairo acts."), statement,
        el("label", {for: "directive-description"}, "Description (required)"),
        el("p", {class: "hint"}, "What the purpose means: its scope, what is worthwhile within it, expectations and boundaries. Not a task list."),
        description,
        el("div", {class: "row"}, el("button", {type: "button", class: "primary", onclick: async () => {
          if (!statement.value.trim() || !description.value.trim()) {
            note.textContent = S.notes.directive = "Both a statement and a description are needed.";
            return;
          }
          const r = await api("/api/directives", {statement: statement.value, description: description.value});
          S.notes.directive = r.ok ? "Added." : `Refused: ${r.error} (${r.code})`;
          note.textContent = S.notes.directive;
          if (r.ok) {
            S.drafts.directive = ""; S.drafts["directive-description"] = "";
            await Promise.all([loadDirectives(), loadStatus()]);
            render();
          }
        }}, "Add directive")), note);
      if (!d.directives) return [head("Directives"), errorBox(d.err, "directives could not be read") || el("p", {class: "muted"}, "Loading…"), form];
      const items = d.directives.slice().reverse().sort((a, b) => Number(b.active) - Number(a.active));
      const active = items.filter((x) => x.active).length;
      const out = [head("Directives"), el("p", {class: "muted small"}, `${active} active · ${items.length - active} inactive${d.omitted ? ` · ${d.omitted} older not shown` : ""}`), form];
      if (!items.length) out.push(el("p", {class: "muted"}, "No directives yet: Kairo has no lasting purpose besides messages."));
      for (const x of items) {
        const toggle = el("button", {type: "button", class: x.active ? "danger" : "", onclick: async () => {
          const verb = x.active ? "deactivate" : "activate";
          if (!confirm(`${verb[0].toUpperCase() + verb.slice(1)} this directive?\n\n${x.statement}`)) return;
          const r = await api(`/api/directives/${verb}`, {id: x.id});
          if (!r.ok) alert(`Refused: ${r.error} (${r.code})`);
          await Promise.all([loadDirectives(), loadStatus()]);
          render();
        }}, x.active ? "Deactivate" : "Activate");
        const work = d.linked[x.id] || [];
        out.push(el("section", {class: "item directive"},
          el("div", {class: "item-head"}, badge(x.active ? "active" : "inactive", x.active ? "ok" : ""),
             el("span", {class: "item-title"}, prov("operator"), x.statement)),
          x.description
            ? el("div", {class: "description"}, x.description)
            : el("p", {class: "muted small"}, "No description recorded (created before directives had descriptions)."),
          el("p", {class: "muted small"}, `${x.origin ? "set by " + x.origin : "origin not recorded"} · created ${stamp(x.created_at)} · ${short(x.id)}`),
          el("div", null, el("div", {class: "muted small"}, "Open work for it"),
            work.length ? work.map((w) => el("div", null, stateBadge(w.state), " ", el("span", {class: "interp-text"}, w.objective)))
              : el("div", {class: "muted small"}, x.active ? "None right now: Kairo decides what, if anything, is worth pursuing." : "None.")),
          el("details", null, el("summary", null, `History (${(x.history || []).length})`),
            (x.history || []).map((h) => el("div", {class: "muted small"}, `${h.event} · ${stamp(h.at)}${h.by ? " · " + h.by : ""}`))),
          el("div", {class: "row"}, toggle)));
      }
      out.push(el("p", {class: "hint"}, "Directives are never edited or deleted: to change a purpose, add a new directive and deactivate the old one. Their history stays."));
      return out;
    });
  }

  // -- work -----------------------------------------------------------------------------
  function attemptsTable(attempts) {
    if (!attempts || !attempts.length) return el("p", {class: "muted small"}, "No attempts yet.");
    return el("div", {class: "scroll"}, el("table", null,
      el("thead", null, el("tr", null, ["requested", "state", "failure / exit", "purpose", "problem"].map((h) => el("th", null, h)))),
      el("tbody", null, attempts.map((a) => el("tr", null,
        el("td", null, stamp(a.requested), el("br"), el("span", {class: "muted"}, `rev ${a.strategy_revision ?? "?"} · ${short(a.action_id)}`)),
        el("td", null, stateBadge(a.state)),
        el("td", null, a.failure || "—", a.returncode !== null && a.returncode !== undefined ? ` · exit ${a.returncode}` : "",
           a.external ? ` · external ${a.external.external_outcome || "—"}` : ""),
        el("td", null, a.purpose ? interp(a.purpose) : "—"),
        el("td", null, a.problem ? el("div", {class: "untrusted-box"}, prov("untrusted"), pre(a.problem)) : "—"))))));
  }
  function workCard(w) {
    if (w.unreadable) return el("div", {class: "item"}, el("span", {class: "error"}, `Work ${short(w.id)}: unreadable record`));
    const rec = w.recovery || {}, lf = rec.latest_failure, now = w.attempts_with_current_strategy;
    const unresolved = rec.unresolved_external_operations || [];
    return el("div", {class: "item"},
      el("div", {class: "item-head"}, stateBadge(w.state), el("span", {class: "item-title interp-text"}, w.objective)),
      w.next_step ? el("div", {class: "small"}, el("span", {class: "muted"}, "Next: "), w.next_step) : null,
      w.state !== "active" && w.state_reason ? el("div", {class: "small"}, el("span", {class: "muted"}, `${w.state}: `), w.state_reason) : null,
      el("div", {class: "muted small"}, `updated ${stamp(w.updated)}${now ? ` · ${plural(now.attempts, "attempt")} with this strategy, ${now.failed} failed` : ""}${w.waiting_until ? ` · waiting until ${stamp(w.waiting_until)}` : ""}`),
      el("details", null, el("summary", null, "Details"),
        prov("fact"), kv([
          ["id", w.id], ["in state since", stamp(w.in_state_since)], ["directive", w.directive_id ? short(w.directive_id) : "none"],
          ["created", stamp(w.created)], ["strategy revision", (w.strategy || {}).revision],
          ["completion check", w.completion_check ? w.completion_check.join(" ") : undefined],
          ["identical failures in a row", rec.repeated_identical_failures],
          ["diagnosis since latest failure", rec.diagnosis_since_latest_failure === null || rec.diagnosis_since_latest_failure === undefined ? undefined : String(rec.diagnosis_since_latest_failure)],
        ]),
        lf ? el("div", null, el("h3", null, "Latest failure"), kv([["failure", lf.failure], ["exit", lf.returncode], ["when", stamp(lf)]]),
                lf.detail ? el("div", {class: "untrusted-box"}, prov("untrusted"), pre(lf.detail)) : null) : null,
        unresolved.length ? el("div", null, el("h3", null, "Unresolved external operations"), unresolved.map((u) =>
          el("div", {class: "small"}, `${u.kind} · key ${short(u.operation_key)} · ${u.state} · ${u.resumable ? "resumable" : "settle by verification"}`))) : null,
        el("h3", null, "Kairo's account"), kv([
          ["why", interp(w.why)], ["strategy", interp((w.strategy || {}).text)],
          ["understanding", w.understanding_shortened
            ? el("div", null, interp(w.understanding), el("div", {class: "muted"},
                `Shown ${w.understanding_shortened.shown_chars} of ${w.understanding_shortened.full_chars} characters, as the model sees it.`))
            : interp(w.understanding)],
        ]),
        el("h3", null, "Recent attempts"), attemptsTable(w.recent_attempts)));
  }
  function closedCard(w) {
    return el("div", {class: "item"},
      el("div", {class: "item-head"}, stateBadge(w.state),
        w.completion_basis ? badge(`basis: ${w.completion_basis}`, ["verified", "checked"].includes(w.completion_basis) ? "outline" : "warn") : null),
      el("div", {class: "item-title interp-text"}, w.objective),
      w.reason ? el("div", {class: "small muted"}, w.reason) : null,
      el("div", {class: "muted small"}, `closed ${stamp(w.closed)}`,
        Array.isArray(w.evidence) && w.evidence.length ? ` · evidence: ${w.evidence.map((e) => `${short(e.action_id)} (${String(e.state).replace(/_/g, " ")})`).join(", ")}` : ""));
  }
  function renderWork() {
    const work = (S.situation || {}).work;
    const d = {work, err: S.err.situation, has: !!S.situation};
    paint("work", d, () => {
      if (!d.has) return [head("Work"), errorBox(d.err, "Kairo's situation could not be read") || el("p", {class: "muted"}, "Loading…")];
      const open = (d.work || {}).open || [], closed = (d.work || {}).recently_closed || [];
      const recent = closed.slice().reverse();
      return [head("Work"),
        open.length ? open.map(workCard) : el("p", {class: "muted"}, "Nothing open."),
        closed.length ? [el("h3", null, "Recently closed"), recent.slice(0, 3).map(closedCard),
          recent.length > 3 ? el("details", null, el("summary", null, `${recent.length - 3} more closed`), recent.slice(3).map(closedCard)) : null] : null,
        el("p", {class: "hint"}, "Basis: verified means a runtime verifier confirmed a cited action; checked means the work's own check passed; unverified means Kairo judged results that exited 0."),
        (d.work || {}).omitted ? el("p", {class: "muted small"}, `${d.work.omitted} older work items are not shown.`) : null];
    });
  }

  // -- activity: a log stream ------------------------------------------------------------
  const FILTERS = [["all", "All"], ["decisions", "Decisions"], ["actions", "Actions"], ["deployments", "Deployments"], ["failures", "Failures"]];
  const failedItem = (i) => (i.type === "cycle" ? i.result === "failed" : i.type === "action" ? FAILED_STATES.includes(i.state) : i.unreadable === true);
  const SHOWN = {
    all: () => true, decisions: (i) => i.type === "cycle", actions: (i) => i.type === "action" && !i.deploy,
    deployments: (i) => !!i.deploy || i.type === "process", failures: failedItem,
  };
  function entry(item, tag, bad, ...body) {
    return el("div", {class: `entry${bad ? " bad" : ""}`}, el("div", {class: "entry-time"}, clock(item.at)),
      el("div", {class: "entry-tag"}, tag), el("div", {class: "entry-body"}, body));
  }
  function cycleEntry(c) {
    if (c.result === "failed") {
      return entry(c, "DECISION", true,
        el("div", {class: "entry-line"}, `Model call failed: ${c.failure || "unknown"}`,
           c.retry_after_seconds ? ` · retry in ${span(c.retry_after_seconds)} (failure ${c.consecutive_failures} in a row)` : ""),
        c.failure_detail ? el("div", {class: "entry-sub"}, c.failure_detail) : null,
        el("div", {class: "entry-sub"}, `woke: ${c.wake_reason || "—"}`));
    }
    if (c.result !== "decided") {
      return entry(c, "CYCLE", false, el("div", {class: "entry-line"}, `Cycle without a model (${c.result || "unknown"})`),
        el("div", {class: "entry-sub"}, `woke: ${c.wake_reason || "—"}`));
    }
    const did = [(c.actions || []).length ? plural(c.actions.length, "action") : null, c.replies ? (c.replies === 1 ? "1 reply" : `${c.replies} replies`) : null,
                 c.work_applied ? plural(c.work_applied, "work change") : null,
                 c.work_rejected ? `${plural(c.work_rejected, "request")} refused` : null].filter(Boolean);
    const then = c.rested_by_runtime ? `rested by the runtime for ${span(c.rested_by_runtime.seconds)}`
      : c.ended_in_state === "sleeping" ? `slept${typeof c.wake_after_seconds === "number" ? " for up to " + span(c.wake_after_seconds) : ""}` : "stayed awake";
    return entry(c, "DECISION", false,
      el("div", {class: "entry-line"}, `${did.length ? did.join(", ") : "Nothing to do"} · ${then}`),
      el("div", {class: "entry-sub"}, [`woke: ${c.wake_reason || "—"}`, c.provider, typeof c.cost_usd === "number" ? money(c.cost_usd) : null,
        typeof c.situation_chars === "number" ? `${num(c.situation_chars)} characters sent` : null,
        typeof c.seconds === "number" ? span(c.seconds) : null].filter(Boolean).join(" · ")),
      c.assessment ? interp(c.assessment) : null);
  }
  function stageRow(s) {
    const counts = typeof s.tests_run === "number" ? `${num(s.tests_run)} run${s.skipped ? `, ${s.skipped} skipped` : ""}` : "";
    const verdict = s.passed === true ? "passed" : s.passed === false ? (s.role === "evidence" ? "did not pass (evidence only: did not block)" : "failed") : "—";
    return [el("dt", null, `${String(s.stage).replace(/_/g, " ")}${s.role ? ` (${s.role})` : ""}`),
            el("dd", null, [verdict, counts].filter(Boolean).join(" · "),
               s.summary ? el("details", null, el("summary", null, "what it printed"), el("div", {class: "untrusted-box"}, prov("untrusted"), pre(s.summary))) : null)];
  }
  function actionEntry(a) {
    const dep = a.deploy, ext = a.external;
    const bad = FAILED_STATES.includes(a.state);
    const took = typeof a.finished_at === "number" && typeof a.at === "number" ? ` · ran ${span(a.finished_at - a.at)}` : "";
    return entry(a, dep ? "DEPLOY" : "ACTION", bad,
      el("div", {class: "entry-line"}, stateBadge(a.state), " ", dep ? `${short(dep.from)} → ${short(dep.to)}` : a.kind,
         a.returncode !== null && a.returncode !== undefined ? ` · exit ${a.returncode}` : "", a.failure && a.failure !== a.state ? ` · ${a.failure}` : "", took,
         ext ? ` · external ${ext.external_outcome || "—"}` : ""),
      a.request && !dep ? el("div", {class: "command"}, a.request) : null,
      a.purpose ? interp(a.purpose) : null,
      dep ? [el("dl", {class: "stages"}, (dep.stages || []).map(stageRow),
               dep.snapshot ? [el("dt", null, "snapshot"), el("dd", {class: "mono"}, dep.snapshot)] : null,
               typeof dep.files_changed === "number" ? [el("dt", null, "changes"), el("dd", null, `${plural(dep.files_changed, "file")}, ${(dep.trust_critical_changed || []).length} trust-critical`)] : null),
             dep.error ? el("div", {class: "error"}, `Refused at ${dep.stage}: ${dep.error}`) : null] : null,
      a.verification ? el("div", {class: "entry-sub"}, `verification ${a.verification.outcome}: ${a.verification.detail || "—"}`) : null,
      a.error && !dep ? el("div", {class: "entry-sub"}, a.error) : null,
      a.output ? el("details", null, el("summary", null, "output"),
        el("div", {class: "untrusted-box"}, prov("untrusted"), `from ${a.output.source}`,
          a.output.stdout ? pre(a.output.stdout) : null, a.output.stderr ? pre(a.output.stderr) : null)) : null);
  }
  function processEntry(p) {
    const started = p.event === "started";
    return entry(p, started ? "START" : "STOP", false,
      el("div", {class: "entry-line"}, `${started ? "Started" : "Stopped"}: ${p.reason || "—"}`,
         p.exit_code ? ` (exit ${p.exit_code})` : "", p.revision ? ` · release ${p.revision.slice(0, 7)}` : ""));
  }
  function activityEntry(i) {
    if (i.unreadable) return entry(i, String(i.type || "record").toUpperCase(), true, el("div", {class: "entry-line"}, `Unreadable record #${i.seq}`));
    return i.type === "cycle" ? cycleEntry(i) : i.type === "action" ? actionEntry(i) : processEntry(i);
  }
  function renderActivity() {
    const d = {items: S.activity, next: S.activityNext, filter: S.filter, err: S.err.activity, ok: supports("activity"),
               loaded: loaded.has(loadActivity), busy: S.loadingEarlier};
    paint("activity", d, () => {
      const buttons = FILTERS.map(([key, text]) => el("button", {type: "button", "aria-pressed": String(d.filter === key),
        onclick: () => { S.filter = key; render(); }}, text));
      if (!d.ok || (!d.items.length && (d.err || !d.loaded))) return [head("Activity, newest first"), unavailable("activity", d.err)];
      const shown = d.items.filter(SHOWN[d.filter]);
      const rows = [];
      let current = null;
      for (const i of shown) {
        const on = day(i.at);
        if (on !== current) { rows.push(el("div", {class: "log-day"}, `${on} (UTC)`)); current = on; }
        rows.push(activityEntry(i));
      }
      const oldest = d.items.length ? d.items[d.items.length - 1] : null;
      return [head("Activity, newest first", buttons), errorBox(d.err, "activity could not be refreshed"),
        scroller("activity", "log", false, rows.length ? rows : el("p", {class: "muted"}, d.items.length ? "Nothing of this kind in what is loaded." : "Nothing recorded yet.")),
        el("div", {class: "log-foot"},
          el("span", {class: "muted small spacer"}, oldest ? `Showing ${shown.length} of ${d.items.length} loaded, back to ${stamp(oldest.at)}.` : ""),
          d.next ? el("button", {type: "button", disabled: d.busy, onclick: loadEarlier}, d.busy ? "Loading…" : "Load earlier")
                 : oldest ? el("span", {class: "muted small"}, "This is the whole record.") : null)];
    });
  }

  // -- chat -----------------------------------------------------------------------------
  async function send(text, status, button) {
    const value = text.value.trim();
    if (!value) return;
    // A retry of the same text reuses its client id: Kairo stores it once.
    if (!S.outbox || S.outbox.text !== value) S.outbox = {text: value, id: clientId()};
    button.disabled = true;
    status.textContent = "Sending…";
    const r = await api("/api/message", {text: value, id: S.outbox.id});
    button.disabled = false;
    if (r.ok) {
      S.notes.chat = r.result.duplicate ? "Already delivered earlier (same message id)." : "Accepted and stored. Kairo answers in a later cycle.";
      S.outbox = null;
      S.drafts.msg = "";
      text.value = "";
      await Promise.all([loadChat(), loadStatus()]);
      render();
    } else {
      S.notes.chat = `Not confirmed: ${r.error} (${r.code}). Sending again is safe: it reuses the same message id.`;
      status.textContent = S.notes.chat;
    }
  }
  function renderChat() {
    const d = {chat: S.chat, more: S.chatMoreBefore, limit: S.chatLimit, expanded: S.expanded, err: S.err.chat, note: S.notes.chat};
    paint("chat", d, () => {
      const box = [];
      if (d.more) {
        box.push(el("p", {class: "muted small"}, `Earlier messages exist (showing the latest ${d.limit}). `,
          d.limit < 200 ? el("button", {type: "button", class: "link", onclick: () => { S.chatLimit = 200; S.chat = []; tick(true); }}, "Show up to 200") : null));
      }
      for (const m of d.chat) {
        if (m.unreadable) { box.push(el("div", {class: "msg"}, `#${m.seq} [unreadable record]`)); continue; }
        const human = m.from === "human", long = m.text.length > LONG_MESSAGE, open = !!d.expanded[m.seq];
        box.push(el("div", {class: `msg ${human ? "human" : "kairo"}`},
          el("div", {class: "meta"}, prov(human ? "operator" : "interp"),
             `${human ? "You" : "Kairo"} · ${stamp(m.at)} · #${m.seq}`,
             m.truncated_from ? ` · shortened from ${m.truncated_from} characters` : ""),
          long && !open ? m.text.slice(0, LONG_MESSAGE) + "…" : m.text,
          long ? el("div", null, el("button", {type: "button", class: "link", onclick: () => { S.expanded[m.seq] = !open; render(); }},
            open ? "Show less" : "Show the full message")) : null));
      }
      if (!d.chat.length) box.push(el("p", {class: "muted"}, d.err ? "" : "No messages yet."));
      const last = d.chat[d.chat.length - 1];
      const text = field("textarea", "msg", {rows: 3, maxlength: 20000});
      const status = el("p", {class: "muted small", id: "chat-note"}, d.note || "");
      const button = el("button", {type: "button", class: "primary", onclick: () => send(text, status, button)}, "Send");
      return [head("Chat"), errorBox(d.err, "chat could not be read"), scroller("chat", "chat", true, box),
        last && last.from === "human" ? el("p", {class: "muted small"}, "Your last message has no reply yet. Kairo answers when its next cycle decides to; nothing is queued in the dashboard.") : null,
        el("div", {class: "composer"},
          el("label", {for: "msg"}, "Message to Kairo. It is input, not a command: Kairo reads it at its next cycle and decides what to do."),
          text, el("div", {class: "row"}, button), status)];
    });
  }

  // -- system and debug: what the old System and Context pages held ---------------------
  function renderSystem() {
    const sit = S.situation, st = S.status;
    const d = {sit, st, dash: S.dashboard, err: S.err.situation};
    paint("system", d, () => {
      if (!d.sit) return [head("System and debug"), errorBox(d.err, "Kairo's situation could not be read") || el("p", {class: "muted"}, "Loading…")];
      const code = (d.sit.kairo || {}).code, caps = d.sit.capabilities || {};
      const impls = (caps.implementations || {}).items || [];
      return [head("System and debug"),
        el("details", null, el("summary", null, "Host, as observed at the start of the last cycle"), prov("fact"),
          kv(Object.entries((d.sit.environment || {}).facts || {}).map(([k, v]) => [k, typeof v === "object" && v !== null ? JSON.stringify(v) : String(v)]))),
        el("details", null, el("summary", null, "Release and repository"), prov("fact"),
          code ? kv([
            ["running revision", (code.running || {}).revision], ["status", (code.running || {}).status],
            ["release", (code.running || {}).release], ["current link", code.current_link], ["previous", code.previous],
            ["repository HEAD", (code.repository || {}).head], ["branch", (code.repository || {}).branch],
            ["HEAD is running", String((code.repository || {}).head_is_running)],
            ["uncommitted files", (code.repository || {}).dirty_files]])
            : el("p", {class: "muted"}, "Self-deployment is not configured for this runtime.")),
        el("details", null, el("summary", null, "Capabilities and implementation packages"), prov("fact"),
          el("div", {class: "scroll"}, el("table", null,
            el("thead", null, el("tr", null, ["action", "effects", "idempotency", "verified automatically"].map((h) => el("th", null, h)))),
            el("tbody", null, Object.entries(caps.actions || {}).map(([kind, spec]) => el("tr", null,
              el("td", null, kind), el("td", null, spec.effects || "—"), el("td", null, spec.idempotency || "—"),
              el("td", null, String(spec.verified_automatically === true))))))),
          impls.length ? impls.map((i) => el("div", {class: "small"}, `${i.id} · ${i.state}${i.reason ? " · " + i.reason : ""}${i.digest ? " · " + String(i.digest).slice(0, 12) : ""}`))
            : el("p", {class: "muted small"}, "No implementation packages configured.")),
        el("details", null, el("summary", null, "What the model is shown now"),
          el("p", {class: "hint"}, "Kairo's situation, section by section, exactly as the runtime builds it (bounded and redacted). Earlier assessments are Kairo's words; action output is untrusted content."),
          Object.keys(d.sit).map((name) => {
            const section = d.sit[name];
            const source = section && typeof section === "object" ? section.source : null;
            return el("details", null, el("summary", null, name, source ? ` — ${source}` : ""), pre(section));
          })),
        el("details", null, el("summary", null, "Runtime status and dashboard"), prov("fact"), d.st ? pre(d.st) : null,
          kv([["Kairo socket", (d.dash || {}).socket], ["protocol expected", (d.dash || {}).protocol_expected],
              ["runtime protocol", d.st ? d.st.protocol || 1 : null], ["dashboard release", (d.dash || {}).revision || "not a release"]]))];
    });
  }

  function render() {
    renderHeader();
    renderMonitors();
    renderTimeline();
    renderFigures();
    renderDirectives();
    renderWork();
    renderActivity();
    renderChat();
    renderSystem();
  }

  // -- operator controls ----------------------------------------------------------------
  // Wake asks Kairo to reassess now; it is not a command to do anything. Stop ends the
  // runtime gracefully; under the supervisor a stop stays stopped.
  byId("wake").addEventListener("click", async () => {
    const r = await api("/api/wake", {reason: "wake requested from the dashboard"});
    S.notes.header = r.ok ? `Wake ${r.result.accepted ? "accepted" : "not accepted"} (state ${r.result.state}).` : `Refused: ${r.error} (${r.code})`;
    await tick(true);
    render();
  });
  byId("stop").addEventListener("click", async () => {
    if (!confirm("Stop Kairo? It will not run again until the operator starts it.")) return;
    const r = await api("/api/stop", {});
    S.notes.header = r.ok ? "Stop requested." : `Refused: ${r.error} (${r.code})`;
    await tick(true);
    render();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(true); });
  render();
  tick(true);
  setInterval(() => tick(false), 1000);
})();
