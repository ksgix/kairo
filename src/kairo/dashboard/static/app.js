// Kairo dashboard: renders what the runtime reports through the dashboard's
// operator-IPC API. Every text from Kairo is inserted as text (never as HTML),
// and labelled by where it comes from: runtime fact, cognition's own words,
// untrusted content, or operator input. The page holds only view state.
"use strict";
(() => {
  const CSRF = document.querySelector('meta[name="kairo-csrf"]').content;
  const STATUS_EVERY = 5000, PAGE_EVERY = 15000, CHAT_EVERY = 5000, MAX_BACKOFF = 60000;
  const PAGES = ["overview", "chat", "directives", "work", "activity", "context", "system"];
  const S = {
    page: PAGES.includes(location.hash.slice(1)) ? location.hash.slice(1) : "overview",
    status: null, statusErr: null, situation: null, situationErr: null,
    chat: [], chatErr: null, chatMoreBefore: false, directives: null, directivesErr: null,
    dashboard: null, outbox: null, updated: null, drafts: {},
  };

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
  const PROV = {fact: "runtime fact", interp: "cognition", untrusted: "untrusted content",
                operator: "operator"};
  const prov = (kind) => el("span", {class: `prov ${kind}`}, PROV[kind]);
  const card = (title, ...kids) => el("section", {class: "card"}, title ? el("h2", null, title) : null, kids);
  const sub = (title) => el("h3", null, title);
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
  function ago(seconds) {
    if (typeof seconds !== "number") return "";
    if (seconds < 90) return `${seconds}s ago`;
    if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
    if (seconds < 172800) return `${Math.round(seconds / 3600)}h ago`;
    return `${Math.round(seconds / 86400)}d ago`;
  }
  function when(w) {   // the situation's {"at", "age_seconds"}
    if (!w || typeof w !== "object" || !w.at || w.at === "unknown") return "unknown time";
    const age = ago(w.age_seconds);
    return age ? `${w.at.replace("T", " ").replace("Z", " UTC")} (${age})` : w.at;
  }
  function epoch(t) {  // a runtime timestamp in seconds
    if (typeof t !== "number") return "unknown time";
    return new Date(t * 1000).toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC");
  }
  function badge(text, tone) { return el("span", {class: `badge ${tone || ""}`}, text); }
  const STATE_TONE = {verified_successful: "ok", executed_unverified: "", verified_failed: "bad",
                      exited_nonzero: "bad", failed_to_execute: "bad", interrupted: "warn",
                      outcome_unknown: "warn", in_progress: "", awaiting_confirmation: "warn",
                      active: "ok", waiting: "warn", blocked: "bad", completed: "ok", abandoned: ""};
  const stateBadge = (state) => badge(state || "unknown", STATE_TONE[state]);
  function errorBox(err, what) {
    if (!err) return null;
    return el("p", {class: "error"}, `${what}: ${err.error || "failed"}`, err.code ? ` (${err.code})` : "");
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

  // -- loading ---------------------------------------------------------------------
  async function loadStatus() {
    const r = await api("/api/status");
    if (r.ok) { S.status = r.result; S.statusErr = null; } else { S.statusErr = r; }
    S.updated = new Date();
    return r.ok;
  }
  async function loadSituation() {
    const r = await api("/api/situation");
    if (r.ok) { S.situation = r.result; S.situationErr = null; } else { S.situationErr = r; }
    return r.ok;
  }
  async function loadChat() {
    const last = S.chat.length ? S.chat[S.chat.length - 1].seq : null;
    const r = await api(last === null ? "/api/chat?limit=50" : `/api/chat?after=${last}&limit=200`);
    if (!r.ok) { S.chatErr = r; return false; }
    S.chatErr = null;
    if (last === null) S.chatMoreBefore = r.result.more_before;
    S.chat = S.chat.concat(r.result.messages);
    return true;
  }
  async function loadDirectives() {
    const r = await api("/api/directives");
    if (r.ok) { S.directives = r.result; S.directivesErr = null; } else { S.directivesErr = r; }
    return r.ok;
  }
  async function loadDashboard() {
    const r = await api("/api/dashboard");
    if (r.ok) S.dashboard = r.result;
    return r.ok;
  }
  const PAGE_LOADS = {
    overview: [loadSituation], chat: [loadChat], directives: [loadDirectives, loadSituation],
    work: [loadSituation], activity: [loadSituation],
    context: [loadSituation], system: [loadSituation, loadDashboard],
  };
  async function loadPage() {
    const results = await Promise.all(PAGE_LOADS[S.page].map((f) => f()));
    return results.every(Boolean);
  }

  // Bounded polling: only while the tab is visible, slower after errors. Reads never
  // change Kairo: status, situation and chat are read operations.
  let lastStatus = 0, lastPage = 0, backoff = 0, busy = false;
  async function tick(force) {
    if (busy || (document.hidden && !force)) return;
    busy = true;
    try {
      const now = Date.now();
      const pageEvery = (S.page === "chat" ? CHAT_EVERY : PAGE_EVERY) + backoff;
      let ok = true;
      if (force || now - lastStatus >= STATUS_EVERY + backoff) {
        ok = (await loadStatus()) && ok;
        lastStatus = Date.now();
      }
      if (force || now - lastPage >= pageEvery) {
        ok = (await loadPage()) && ok;
        lastPage = Date.now();
      }
      backoff = ok ? 0 : Math.min(Math.max(backoff * 2, 5000), MAX_BACKOFF);
      render();
    } finally {
      busy = false;
    }
  }

  // -- header ------------------------------------------------------------------------
  function renderHeader() {
    const conn = document.getElementById("conn"), state = document.getElementById("state");
    const rev = document.getElementById("rev"), notice = document.getElementById("notice");
    const st = S.status, err = S.statusErr;
    notice.hidden = true;
    if (err) {
      conn.textContent = err.code === "unreachable" ? "Kairo unreachable" : `error: ${err.code || "unknown"}`;
      conn.className = "badge bad";
      state.textContent = "state unknown";
      state.className = "badge";
      notice.textContent = `${err.error || "Kairo could not be reached"}. Nothing shown is current until it answers again.`;
      notice.hidden = false;
    } else if (st) {
      conn.textContent = "connected";
      conn.className = "badge ok";
      state.textContent = st.state + (st.wake_at && st.state === "sleeping" ? ` until ${epoch(st.wake_at)}` : "");
      state.className = `badge ${st.state === "awake" ? "ok" : st.state === "sleeping" ? "" : "warn"}`;
      if ((st.protocol || 1) < 2) {
        notice.textContent = "This Kairo runtime predates operator protocol 2: only its status is available here. Deploy a newer release.";
        notice.hidden = false;
      }
    }
    rev.textContent = st && st.revision ? `revision ${st.revision.slice(0, 12)}` : "";
    document.getElementById("updated").textContent = S.updated ? `updated ${S.updated.toLocaleTimeString()}` : "";
    for (const b of document.querySelectorAll("#nav button")) b.classList.toggle("active", b.dataset.page === S.page);
  }

  // -- pages -------------------------------------------------------------------------
  function situationGuard() {
    if (S.situation) return null;
    return card("Situation unavailable", errorBox(S.situationErr, "Kairo's situation could not be read") ||
                el("p", {class: "muted"}, "Loading…"));
  }

  function attention(sit) {
    const t = sit.open_threads || {};
    const rows = [
      ["unanswered messages", (t.unanswered_human_messages || []).length],
      ["failed actions (recent)", (t.actions_failed || []).length],
      ["actions with unknown outcome", (t.actions_outcome_unknown || []).length],
      ["attempts refused last cycle", (t.attempts_refused || []).length],
      ["work requests refused last cycle", (t.work_requests_rejected || []).length],
      ["waits elapsed", (t.work_wait_elapsed || []).length],
    ];
    const failed = t.previous_cycle_failed;
    return card("Needs attention", prov("fact"), kv(rows),
      failed ? el("p", {class: "error"}, `previous cycle failed: ${failed.failure} (${when(failed.ended)})`) : null);
  }

  function renderOverview() {
    const st = S.status, sit = S.situation;
    const out = [];
    if (st) {
      const last = st.cognition_last || {};
      out.push(el("div", {class: "grid"},
        card("Kairo", prov("fact"), kv([
          ["identity", st.identity], ["state", stateBadge(st.state)], ["process id", st.pid],
          ["starts", st.starts], ["running", String(st.running)],
          ["revision", st.revision || "not reported"], ["operator protocol", st.protocol || 1],
          ["wakes at", st.wake_at ? epoch(st.wake_at) : null],
        ]),
        sub("last transition reason"),
        st.state === "sleeping" ? interp(st.reason) : el("div", null, st.reason || "—",
          el("div", {class: "muted"}, "Recorded by the runtime; a wake reason given by an operator is their own words."))),
        card("Cognition", prov("fact"), kv([
          ["providers (order)", st.cognition || "none configured"],
          ["last provider", last.provider], ["last result", last.result],
          ["last failure", last.failure], ["at", last.at ? epoch(last.at) : null],
        ])),
        card("Counts", prov("fact"), kv([
          ["active directives", st.directives], ["open work", st.open_work],
          ["implementations", (st.implementations || []).map((i) => `${i.id} (${i.state})`).join(", ") || "none"],
        ])),
        sit ? attention(sit) : null));
    } else {
      out.push(card("Kairo", errorBox(S.statusErr, "status") || el("p", {class: "muted"}, "Loading…")));
    }
    if (!sit) { out.push(situationGuard()); return out; }
    const active = (sit.directives || {}).active || [];
    const open = (sit.work || {}).open || [];
    out.push(el("div", {class: "grid"},
      card("Active directives", active.length ? el("ul", null, active.map((d) =>
        el("li", null, prov("operator"), d.statement, el("span", {class: "muted"}, ` · since ${when(d.since)}`),
          d.description ? el("div", {class: "muted description"}, d.description) : null)))
        : el("p", {class: "muted"}, "No active directive: Kairo has no lasting purpose besides messages.")),
      card("Open work", open.length ? el("ul", null, open.map((w) =>
        el("li", null, stateBadge(w.state), " ", el("span", {class: "interp-text"}, w.objective),
           el("span", {class: "muted"}, ` · updated ${when(w.updated)}`))))
        : el("p", {class: "muted"}, "No open work."))));
    out.push(activityCard(sit, 6, "Recent activity"));
    return out;
  }

  function renderChat() {
    const box = el("div", {class: "chat"});
    if (S.chatMoreBefore) box.append(el("p", {class: "muted"}, "Earlier messages exist (showing the latest 50)."));
    for (const m of S.chat) {
      if (m.unreadable) { box.append(el("div", {class: "msg"}, `#${m.seq} [unreadable record]`)); continue; }
      const human = m.from === "human";
      box.append(el("div", {class: `msg ${human ? "human" : "kairo"}`},
        el("div", {class: "meta"}, prov(human ? "operator" : "interp"),
           `${human ? "you" : "Kairo"} · ${epoch(m.at)} · #${m.seq}`,
           m.truncated_from ? ` · shortened from ${m.truncated_from} characters` : ""),
        m.text));
    }
    if (!S.chat.length) box.append(el("p", {class: "muted"}, S.chatErr ? "" : "No messages yet."));
    const last = S.chat[S.chat.length - 1];
    const waiting = last && last.from === "human";
    const text = field("textarea", "msg", {placeholder: "Message to Kairo (it answers in a later cycle)", maxlength: 20000});
    const status = el("p", {class: "muted"}, S.outbox ? S.outbox.note : "");
    const send = el("button", {type: "button", onclick: async () => {
      const value = text.value.trim();
      if (!value) return;
      // A retry of the same text reuses its client id: Kairo stores it once.
      if (!S.outbox || S.outbox.text !== value) S.outbox = {text: value, id: clientId(), note: ""};
      send.disabled = true;
      status.textContent = "Sending…";
      const r = await api("/api/message", {text: value, id: S.outbox.id});
      send.disabled = false;
      if (r.ok) {
        const note = r.result.duplicate ? "Already delivered earlier (same message id)." :
          "Accepted and stored. Kairo answers in a later cycle.";
        S.outbox = null;
        S.drafts.msg = "";
        await loadChat();
        render();
        document.getElementById("chat-note").textContent = note;
      } else {
        S.outbox.note = `Not confirmed: ${r.error} (${r.code}). Sending again is safe: it reuses the same message id.`;
        status.textContent = S.outbox.note;
      }
    }}, "Send");
    return [card("Conversation", errorBox(S.chatErr, "chat could not be read"), box,
                 waiting ? el("p", {class: "muted"}, "Your last message has no reply yet. Kairo answers when its next cycle decides to; nothing is queued in the dashboard.") : null),
            card("Send a message", el("p", {class: "muted"},
                 "A message is input to Kairo, not a command: its cognition decides what to do, and anything done is an ordinary, recorded action."),
                 el("div", {class: "row"}, text, send), status, el("p", {class: "muted", id: "chat-note"}))];
  }

  function renderDirectives() {
    const out = [];
    const linked = {};
    for (const w of ((S.situation || {}).work || {}).open || []) {
      if (w.directive_id) (linked[w.directive_id] = linked[w.directive_id] || []).push(w);
    }
    // Which implementation packages name each directive (the operator's view of the
    // catalog, from status): serving it now, or naming it without serving.
    const impls = ((S.status || {}).implementations || []);
    const statement = field("input", "directive", {placeholder: "The purpose, concisely, e.g. “Keep the backups verified and restorable”", maxlength: 500, required: true});
    const description = field("textarea", "directive-description", {placeholder: "What this purpose covers: its intent, scope, expectations and boundaries. Kairo decides the concrete work itself.", maxlength: 4000, rows: 5, required: true});
    const note = el("p", {class: "muted"});
    out.push(card("Add a directive", el("p", {class: "muted"},
      "A directive is Kairo's purpose: a lasting area of responsibility, not a task or command. Adding one executes nothing and creates no work; Kairo reassesses with it from its next cycle and decides itself what, if anything, is worth pursuing for it."),
      el("label", {for: "directive"}, "Statement (required)"),
      el("p", {class: "muted hint"}, "The enduring purpose, in one sentence: why Kairo acts."), statement,
      el("label", {for: "directive-description"}, "Description (required)"),
      el("p", {class: "muted hint"}, "What the purpose means: its scope, what is worthwhile within it, expectations and boundaries. Not a task list."),
      description,
      el("div", {class: "row"}, el("button", {type: "button", onclick: async () => {
        if (!statement.value.trim() || !description.value.trim()) {
          note.textContent = "Both a statement and a description are needed.";
          return;
        }
        const r = await api("/api/directives", {statement: statement.value, description: description.value});
        note.textContent = r.ok ? "Added." : `Refused: ${r.error} (${r.code})`;
        if (r.ok) {
          S.drafts.directive = ""; S.drafts["directive-description"] = "";
          await loadDirectives(); render(); note.textContent = "Added.";
        }
      }}, "Add directive")), note));
    if (!S.directives) { out.push(card("Directives", errorBox(S.directivesErr, "directives could not be read") || "Loading…")); return out; }
    const items = S.directives.directives.slice().reverse();
    if (!items.length) out.push(card("Directives", el("p", {class: "muted"}, "No directives yet: Kairo has no lasting purpose besides messages.")));
    for (const d of items) {
      const toggle = el("button", {type: "button", class: d.active ? "danger" : "", onclick: async () => {
        const verb = d.active ? "deactivate" : "activate";
        if (!confirm(`${verb[0].toUpperCase() + verb.slice(1)} this directive?\n\n${d.statement}`)) return;
        const r = await api(`/api/directives/${verb}`, {id: d.id});
        if (!r.ok) alert(`Refused: ${r.error} (${r.code})`);
        await loadDirectives(); render();
      }}, d.active ? "Deactivate" : "Activate");
      const named = impls.filter((i) => (i.directives || []).includes(d.id));
      const work = linked[d.id] || [];
      out.push(el("section", {class: "card directive"},
        el("div", {class: "row"},
          el("h2", null, prov("operator"), " ", d.statement),
          badge(d.active ? "active" : "inactive", d.active ? "ok" : ""), toggle),
        d.description
          ? el("p", {class: "description"}, d.description)
          : el("p", {class: "muted"}, "No description recorded (created before directives had descriptions)."),
        el("p", {class: "muted"}, `${d.origin ? "set by " + d.origin : "origin not recorded"} · created ${epoch(d.created_at)} · ${short(d.id)}`),
        el("div", {class: "grid"},
          el("div", null, sub("Open work for it"), prov("fact"),
            work.length ? el("ul", null, work.map((w) => el("li", null, stateBadge(w.state), " ", el("span", {class: "interp-text"}, w.objective))))
              : el("p", {class: "muted"}, d.active ? "None right now: Kairo decides what, if anything, is worth pursuing." : "None.")),
          el("div", null, sub("Implementations"), prov("fact"),
            named.length ? el("ul", null, named.map((i) => el("li", null, i.id, " ",
              (i.serves || []).includes(d.id) ? badge(i.state, i.state === "available" ? "ok" : "warn") : badge(i.state || "not serving", ""),
              i.reason ? el("span", {class: "muted"}, ` ${i.reason}`) : null)))
              : el("p", {class: "muted"}, "No implementation package names this directive."))),
        el("details", null, el("summary", null, `History (${(d.history || []).length})`),
          el("ul", null, (d.history || []).map((h) => el("li", {class: "muted"}, `${h.event} · ${epoch(h.at)}${h.by ? " · " + h.by : ""}`))))));
    }
    out.push(el("p", {class: "muted"}, "Directives are never edited or deleted: to change a purpose, add a new directive and deactivate the old one. Their history stays."));
    return out;
  }

  function attemptsTable(attempts) {
    if (!attempts || !attempts.length) return el("p", {class: "muted"}, "No attempts yet.");
    return el("div", {class: "scroll"}, el("table", null,
      el("thead", null, el("tr", null, ["requested", "state", "failure / exit", "external", "purpose", "problem"].map((h) => el("th", null, h)))),
      el("tbody", null, attempts.map((a) => el("tr", null,
        el("td", null, when(a.requested), el("br"), el("span", {class: "muted"}, `rev ${a.strategy_revision ?? "?"} · ${short(a.action_id)}`)),
        el("td", null, stateBadge(a.state)),
        el("td", null, a.failure || "—", a.returncode !== null && a.returncode !== undefined ? ` · exit ${a.returncode}` : ""),
        el("td", null, a.external ? `${a.external.external_outcome || "—"} · key ${short(a.external.operation_key)}` : "—"),
        el("td", null, a.purpose ? interp(a.purpose) : "—"),
        el("td", null, a.problem ? el("div", {class: "untrusted-box"}, prov("untrusted"), pre(a.problem)) : "—"))))));
  }

  function workCard(w) {
    const rec = w.recovery || {};
    const facts = kv([
      ["id", w.id], ["state", stateBadge(w.state)], ["in state since", when(w.in_state_since)],
      ["directive", w.directive_id ? short(w.directive_id) : "none"],
      ["created", when(w.created)], ["updated", when(w.updated)],
      ["strategy revision", (w.strategy || {}).revision],
      ["attempts (current strategy)", w.attempts_with_current_strategy ? `${w.attempts_with_current_strategy.attempts} (${w.attempts_with_current_strategy.failed} failed)` : null],
      ["waiting until", w.waiting_until ? when(w.waiting_until) : undefined],
      ["identical failures in a row", rec.repeated_identical_failures],
      ["diagnosis since latest failure", rec.diagnosis_since_latest_failure === null ? null : String(rec.diagnosis_since_latest_failure)],
    ]);
    const lf = rec.latest_failure;
    const unresolved = rec.unresolved_external_operations || [];
    return card(null,
      el("h2", null, stateBadge(w.state), " ", el("span", {class: "interp-text"}, w.objective)),
      el("div", {class: "grid"},
        el("div", null, sub("Runtime facts"), prov("fact"), facts,
          lf ? el("div", null, sub("Latest failure"), kv([["failure", lf.failure], ["exit", lf.returncode], ["when", when(lf)]]),
                  lf.detail ? el("div", {class: "untrusted-box"}, prov("untrusted"), pre(lf.detail)) : null) : null,
          unresolved.length ? el("div", null, sub("Unresolved external operations"), el("ul", null, unresolved.map((u) =>
            el("li", null, `${u.kind} · key ${short(u.operation_key)} · ${u.state} · ${u.resumable ? "resumable" : "settle by verification"}`)))) : null),
        el("div", null, sub("Cognition's account"), kv([
          ["why", interp(w.why)], ["strategy", interp((w.strategy || {}).text)],
          ["understanding", w.understanding_shortened
            ? el("div", null, interp(w.understanding), el("div", {class: "muted"},
                `Shown ${w.understanding_shortened.shown_chars} of ${w.understanding_shortened.full_chars} characters, as cognition sees it (context bound).`))
            : interp(w.understanding)],
          ["next step", interp(w.next_step)],
          ["state reason", w.state_reason ? interp(w.state_reason) : undefined],
        ]))),
      sub("Recent attempts"), attemptsTable(w.recent_attempts),
      (rec.revisions || []).length ? el("details", null, el("summary", null, "Strategy revisions"),
        el("table", null, el("tbody", null, rec.revisions.map((r) => el("tr", null,
          el("td", null, `#${r.revision}`), el("td", null, interp(r.strategy)),
          el("td", null, `${r.attempts} attempts · ${r.failed} failed · ${r.succeeded} succeeded · ${r.outcome_unknown} unknown`),
          el("td", null, when(r.last_attempt))))))) : null,
      (w.recent_changes || []).length ? el("details", null, el("summary", null, "Recent changes"),
        el("ul", null, w.recent_changes.map((c) => el("li", null, `${c.event}${c.to ? " → " + c.to : ""}${c.revision ? " #" + c.revision : ""} · ${when(c)}`)))) : null);
  }

  function renderWork() {
    const guard = situationGuard();
    if (guard) return [guard];
    const work = S.situation.work || {};
    const out = [el("p", {class: "muted"}, work.note || "")];
    out.push(...(work.open || []).map(workCard));
    if (!(work.open || []).length) out.push(card("Open work", el("p", {class: "muted"}, "No open work.")));
    const closed = work.recently_closed || [];
    const closedRow = (w) => el("tr", null,
      el("td", null, stateBadge(w.state)), el("td", null, interp(w.objective)),
      el("td", null,
        w.completion_basis ? badge(`basis: ${w.completion_basis}`, w.completion_basis === "verified" ? "ok" : "warn") : "",
        Array.isArray(w.evidence) ? el("div", {class: "muted"},
          `evidence: ${w.evidence.map((e) => `${short(e.action_id)} (${e.state})`).join(", ")}`) : ""),
      el("td", null, w.reason ? interp(w.reason) : "—"),
      el("td", null, when(w.closed)));
    out.push(card("Recently closed",
      closed.length ? el("div", {class: "scroll"}, el("table", null, el("tbody", null, closed.map(closedRow))))
                    : el("p", {class: "muted"}, "None."),
      el("p", {class: "muted"}, work.completion_basis || "")));
    return out;
  }

  function activityItems(sit) {
    // Newest first. The situation lists each kind oldest first, and ages are whole
    // seconds, so each list is reversed before the (stable) sort.
    const items = [];
    const newest = (list) => (list || []).slice().reverse();
    for (const c of newest(((sit.history || {}).cycles || {}).items)) items.push({t: (c.ended || {}).age_seconds, kind: "cycle", c});
    for (const a of newest(((sit.history || {}).actions || {}).items)) items.push({t: (a.requested || {}).age_seconds, kind: "action", a});
    for (const d of newest(((sit.kairo || {}).code || {}).recent_deployments)) items.push({t: d.age_seconds, kind: "deploy", d});
    return items.sort((x, y) => (x.t ?? 1e12) - (y.t ?? 1e12));
  }
  function activityRow(item) {
    if (item.kind === "cycle") {
      const c = item.c;
      return el("tr", null, el("td", null, when(c.ended)), el("td", null, "cycle"),
        el("td", null, prov("fact"), `cognition ${c.cognition || "?"}${c.provider ? " (" + c.provider + ")" : ""}${c.failure ? " · failure " + c.failure : ""} · ${c.chose_sleep ? "chose to sleep" : "stayed awake"}`,
           el("div", {class: "muted"}, `woke because: “${c.wake_reason || "—"}”`),
           c.assessment ? interp(c.assessment) : null));
    }
    if (item.kind === "deploy") {
      const d = item.d;
      return el("tr", null, el("td", null, when(d)), el("td", null, "deployment"),
        el("td", null, prov("fact"), stateBadge(d.state), ` ${short(d.from)} → ${short(d.to)} · stage ${d.stage || "—"}`));
    }
    const a = item.a;
    const ext = a.external;
    return el("tr", null, el("td", null, when(a.requested)), el("td", null, a.kind),
      el("td", null, prov("fact"), stateBadge(a.state),
        a.failure ? ` failure ${a.failure}` : "", a.returncode !== null && a.returncode !== undefined ? ` · exit ${a.returncode}` : "",
        a.verification ? ` · verification ${a.verification.outcome}` : "",
        ext ? ` · external ${ext.external_outcome || "—"} (key ${short(ext.operation_key)}${ext.resumes ? ", resumes " + short(ext.resumes) : ""})` : "",
        a.purpose ? interp(a.purpose) : null,
        el("details", null, el("summary", null, "details"),
          kv([["id", a.id], ["params", pre(a.params)], ["error", a.error], ["verification detail", (a.verification || {}).detail]]),
          a.output ? el("div", {class: "untrusted-box"}, prov("untrusted"), `from ${a.output.source}`,
            a.output.stdout ? pre(a.output.stdout) : null, a.output.stderr ? pre(a.output.stderr) : null) : null)));
  }
  function activityCard(sit, limit, title) {
    const items = activityItems(sit).slice(0, limit);
    return card(title, items.length ? el("div", {class: "scroll"}, el("table", null,
      el("thead", null, el("tr", null, ["when", "what", "facts and words"].map((h) => el("th", null, h)))),
      el("tbody", null, items.map(activityRow)))) : el("p", {class: "muted"}, "No activity recorded yet."));
  }
  function renderActivity() {
    const guard = situationGuard();
    if (guard) return [guard];
    const h = S.situation.history || {};
    return [el("p", {class: "muted"}, `The latest cycles, actions and deployments Kairo's situation includes (bounded: ${((h.actions || {}).omitted_older || 0)} older actions and ${((h.cycles || {}).omitted_older || 0)} older cycles not shown).`),
            activityCard(S.situation, 100, "Activity")];
  }

  function renderContext() {
    const guard = situationGuard();
    if (guard) return [guard];
    const sit = S.situation;
    return [el("p", {class: "muted"}, "What Kairo's cognition is shown now, section by section, exactly as the runtime builds it (bounded and redacted). Each section states its source; earlier assessments are cognition's words; action output is untrusted content."),
      ...Object.keys(sit).map((name) => {
        const section = sit[name];
        const source = section && typeof section === "object" ? section.source : null;
        return el("details", {class: "card"}, el("summary", null, name, source ? ` — ${source}` : ""), pre(section));
      })];
  }

  function renderSystem() {
    const st = S.status, sit = S.situation, out = [];
    const reason = field("input", "wake-reason", {placeholder: "Why (optional)", maxlength: 300});
    const note = el("p", {class: "muted"});
    out.push(card("Operator controls", el("p", {class: "muted"},
      "Wake asks Kairo to reassess now; it is not a command to do anything. Stop ends the runtime gracefully (it finishes the current action); under the supervisor a stop stays stopped."),
      el("div", {class: "row"}, reason,
        el("button", {type: "button", onclick: async () => {
          const r = await api("/api/wake", reason.value.trim() ? {reason: reason.value.trim()} : {});
          note.textContent = r.ok ? `Wake ${r.result.accepted ? "accepted" : "not accepted"} (state ${r.result.state}).` : `Refused: ${r.error} (${r.code})`;
          await tick(true);
        }}, "Wake"),
        el("button", {type: "button", class: "danger", onclick: async () => {
          if (!confirm("Stop Kairo? It will not run again until the operator starts it.")) return;
          const r = await api("/api/stop", {});
          note.textContent = r.ok ? "Stop requested." : `Refused: ${r.error} (${r.code})`;
          await tick(true);
        }}, "Stop Kairo")), note));
    if (st) out.push(card("Runtime", prov("fact"), pre(st)));
    if (!sit) { out.push(situationGuard()); return out; }
    const code = (sit.kairo || {}).code;
    out.push(el("div", {class: "grid"},
      card("Host (observed at the start of the last cycle)", prov("fact"), kv(Object.entries((sit.environment || {}).facts || {}))),
      code ? card("Release", prov("fact"), kv([
        ["running revision", (code.running || {}).revision], ["status", (code.running || {}).status],
        ["release", (code.running || {}).release], ["current link", code.current_link], ["previous", code.previous],
        ["repository HEAD", (code.repository || {}).head], ["HEAD is running", String((code.repository || {}).head_is_running)],
        ["uncommitted files", (code.repository || {}).dirty_files]]))
        : card("Release", el("p", {class: "muted"}, "Self-deployment is not configured for this runtime.")),
      card("Dashboard", kv([["Kairo socket", (S.dashboard || {}).socket], ["protocol expected", (S.dashboard || {}).protocol_expected],
        ["runtime protocol", st ? st.protocol || 1 : null]]))));
    const caps = sit.capabilities || {};
    const impls = (caps.implementations || {}).items || [];
    out.push(card("Capabilities", prov("fact"), el("div", {class: "scroll"}, el("table", null,
      el("thead", null, el("tr", null, ["action", "effects", "idempotency", "verified automatically"].map((h) => el("th", null, h)))),
      el("tbody", null, Object.entries(caps.actions || {}).map(([kind, spec]) => el("tr", null,
        el("td", null, kind), el("td", null, spec.effects || "—"), el("td", null, spec.idempotency || "—"),
        el("td", null, String(spec.verified_automatically))))))),
      sub("Implementations"), impls.length ? el("ul", null, impls.map((i) =>
        el("li", null, `${i.id} · ${i.state}${i.reason ? " · " + i.reason : ""}${i.digest ? " · " + String(i.digest).slice(0, 12) : ""}`)))
        : el("p", {class: "muted"}, "No implementation packages configured.")));
    return out;
  }

  const RENDER = {overview: renderOverview, chat: renderChat, directives: renderDirectives, work: renderWork,
                  activity: renderActivity, context: renderContext, system: renderSystem};
  function render() {
    renderHeader();
    const main = document.getElementById("main");
    // Re-rendering keeps what the operator is doing: typed text (drafts), focus,
    // opened details, and the window's scroll position.
    const active = document.activeElement;
    const focus = active && active.id && ["INPUT", "TEXTAREA"].includes(active.tagName)
      ? {id: active.id, start: active.selectionStart, end: active.selectionEnd} : null;
    const open = new Set(Array.from(main.querySelectorAll("details[open] > summary"), (s) => s.textContent));
    const scroll = window.scrollY;
    main.replaceChildren(...[RENDER[S.page]()].flat(Infinity).filter(Boolean));
    for (const s of main.querySelectorAll("details > summary")) if (open.has(s.textContent)) s.parentElement.open = true;
    if (focus) {
      const n = document.getElementById(focus.id);
      if (n) { n.focus(); try { n.setSelectionRange(focus.start, focus.end); } catch (e) { /* not a text field */ } }
    }
    window.scrollTo(0, scroll);
  }

  for (const b of document.querySelectorAll("#nav button")) {
    b.addEventListener("click", () => { S.page = b.dataset.page; location.hash = S.page; lastPage = 0; render(); tick(true); });
  }
  document.getElementById("refresh").addEventListener("click", () => tick(true));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(true); });
  render();
  tick(true);
  setInterval(() => tick(false), 1000);
})();
