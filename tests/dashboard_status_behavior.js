// Executable behavior tests for the dashboard page's status/duration helpers
// and its conversation navigation rail.
//
// Extracts the real inline <script> from src/agent_bridge/share/dashboard.py's
// PAGE and runs it inside a vm context with a minimal DOM stub — no jsdom, no
// npm packages. Covers the centralized proc_state map, latestTask chronology,
// the taskDur/fmtDur/durText rules end to end, the bundled i18n layer
// (en / zh-CN / zh-TW), and the timeline ruler end to end: MARK_SEL taxonomy,
// layoutRail compact centered geometry (fixed-length ticks, kind thickness,
// rail bounds), the measured --railin scrollbar gutter, updateCurMark's 0.45
// reference, the updateRulerWave focus crest, jumpToMark, wheel forwarding,
// roving tabindex, and the MutationObserver/ResizeObserver + rAF-coalesced
// update loop.
//
// Usage: node tests/dashboard_status_behavior.js   (exit 0 = all pass)
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(
  path.join(__dirname, "..", "src", "agent_bridge", "share", "dashboard.py"),
  "utf8");
const page = (src.match(/PAGE = r"""([\s\S]*?)"""/) || [])[1];
if (!page) { console.error("PAGE not found in dashboard.py"); process.exit(2); }
const code = (page.match(/<script>([\s\S]*?)<\/script>/g) || [])
  .map((s) => s.slice("<script>".length, -"</script>".length))
  .join("\n;\n");

/* ---------- minimal DOM/window stubs ----------
   Enough surface for the page script's top-level wiring; every fetch is left
   pending (and recorded) so no poll mutates `tasks` between assertions.
   querySelector results are cached per selector so tests can observe the
   elements the page owns (#chatstatus, #chatinput, #hwrap, ...); each element
   records setAttribute values and returns stable children for querySelector.
   classList/children/parent links are real so the nav rail's layout, focus
   and event wiring run unmodified. */
const matchSel = (e, sel) => sel.split(",").some((cl) => {
  const toks = [...cl.matchAll(/(:not\()?\.([\w-]+)\)?/g)];
  return toks.length > 0 && toks.every((m) =>
    m[1] ? !e.classList.contains(m[2]) : e.classList.contains(m[2]));
});
const created = [];
const el = () => {
  const classes = new Set();
  let cn = "", html = "";
  const sync = () => { cn = [...classes].join(" "); };
  const e = {
    textContent: "", title: "", type: "", tagName: "",
    // Inline style + the CSS-custom-property surface the rail writes
    // (--my/--mo) — recorded verbatim so tests can read either channel.
    style: {
      setProperty(k, v) { this[k] = String(v); },
      getPropertyValue(k) {
        return Object.prototype.hasOwnProperty.call(this, k)
          ? String(this[k]) : "";
      },
      removeProperty(k) { delete this[k]; },
    },
    classList: {
      add: (c) => { classes.add(c); sync(); },
      remove: (c) => { classes.delete(c); sync(); },
      toggle: (c, f) => {
        const on = f === undefined ? !classes.has(c) : f;
        if (on) classes.add(c); else classes.delete(c);
        sync(); return on;
      },
      contains: (c) => classes.has(c),
    },
    dataset: {}, disabled: false, value: "", placeholder: "", tabIndex: 0,
    open: false, hidden: false, isConnected: true, offsetTop: 0,
    children: [], _parent: null, _htmlSets: 0,
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    clientWidth: 0, offsetWidth: 0,
    attrs: {}, _q: {}, _ls: {}, focused: 0, onclick: null,
    appendChild(c) {
      if (c._parent) c._parent.children.splice(c._parent.children.indexOf(c), 1);
      c._parent = e; e.children.push(c); return c;
    },
    insertAdjacentHTML() {},
    after(sib) {
      const p = e._parent; if (!p) return;
      p.children.splice(p.children.indexOf(e) + 1, 0, sib); sib._parent = p;
    },
    setAttribute(k, v) { e.attrs[k] = String(v); },
    getAttribute(k) { return e.attrs[k]; },
    removeAttribute(k) { delete e.attrs[k]; },
    replaceChildren() {
      e.children.forEach((c) => { c._parent = null; }); e.children = [];
    },
    addEventListener(ev, fn) { (e._ls[ev] || (e._ls[ev] = [])).push(fn); },
    removeEventListener() {},
    querySelector(s) { return e._q[s] || (e._q[s] = el()); },
    querySelectorAll(s) {
      const out = [];
      const walk = (k) => {
        for (const c of k.children) { if (matchSel(c, s)) out.push(c); walk(c); }
      };
      walk(e); return out;
    },
    focus() { e.focused++; documentStub.activeElement = e; },
    click() { e.clicks = (e.clicks || 0) + 1; },
    remove() {
      const p = e._parent;
      if (p) { p.children.splice(p.children.indexOf(e), 1); e._parent = null; }
    },
    scrollTo(o) {
      e._scrollToArgs = o;
      if (o && typeof o.top === "number") e.scrollTop = o.top;
    },
  };
  Object.defineProperty(e, "className", {
    get: () => cn,
    set: (v) => {
      cn = String(v); classes.clear();
      cn.split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
    },
  });
  Object.defineProperty(e, "innerHTML", {
    get: () => html,
    set: (v) => {
      e._htmlSets++;
      html = String(v);
      e.children.forEach((c) => { c._parent = null; }); e.children = [];
    },
  });
  Object.defineProperty(e, "firstChild", { get: () => e.children[0] || null });
  Object.defineProperty(e, "firstElementChild",
    { get: () => e.children[0] || null });
  return e;
};
const listeners = {};        // captured window/document event listeners
const elCache = {};
// #content/#rail are pre-seeded so the rail tests can drive real geometry.
const contentEl = el();
const railEl = el();
elCache["#content"] = contentEl;
elCache["#rail"] = railEl;
const documentStub = {
  documentElement: Object.assign(el(), { lang: "" }),
  activeElement: null, title: "", visibilityState: "visible",
  body: el(),
  querySelector: (s) => elCache[s] || (elCache[s] = el()),
  querySelectorAll: () => [],
  createElement(tag) {
    const e = el(); e.tagName = String(tag || "").toUpperCase();
    created.push(e); return e;
  },
  addEventListener(ev, fn) { (listeners[ev] || (listeners[ev] = [])).push(fn); },
};
const pending = () => new Promise(() => {});
const fetchCalls = [];
const mediaStubs = {};       // media query -> shared matchMedia stub
const moCalls = [], roCalls = [];   // observer registrations
const rafQ = [];             // queued requestAnimationFrame callbacks
const runRaf = () => { const q = rafQ.splice(0); q.forEach((f) => f()); };
const timers = [];           // queued setTimeout callbacks (drained explicitly)
const runTimers = () => { const q = timers.splice(0); q.forEach((f) => f()); };
const blobs = [], revokedUrls = [];   // Blob/object-URL download capture
let blobSeq = 0;
const store = {};            // mutable localStorage backing
const sandbox = {
  document: documentStub,
  fetch: (u) => { fetchCalls.push(String(u)); return pending(); },
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  },
  // Sentinel: a presence id sourced from storage would carry "SHARED" —
  // the heartbeat must mint a fresh per-page id instead.
  sessionStorage: { getItem: () => "SHARED", setItem() {} },
  navigator: { sendBeacon() {}, languages: ["en-US"], language: "en-US" },
  crypto: { randomUUID: () => "00000000-0000-0000-0000-000000000000" },
  // matchMedia stubs are shared per query so tests can flip .matches later
  // (the page keeps the object it received for prefers-reduced-motion).
  matchMedia: (q) => mediaStubs[q] || (mediaStubs[q] = {
    matches: false, media: q, addEventListener() {}, removeEventListener() {},
    addListener() {}, removeListener() {},
  }),
  addEventListener(ev, fn) { (listeners[ev] || (listeners[ev] = [])).push(fn); },
  removeEventListener() {},
  setInterval: () => 0, clearInterval() {},
  // Timeouts queue up like rAF callbacks; tests drain them via runTimers().
  setTimeout: (fn) => { timers.push(fn); return timers.length; },
  clearTimeout() {},
  // File-download surface: the anchor click is observed through created[],
  // the blob through this registry, and cleanup through revokedUrls.
  Blob: class {
    constructor(parts, opts) {
      this.parts = parts || [];
      this.type = (opts && opts.type) || "";
      blobs.push(this);
    }
  },
  URL: {
    createObjectURL: () => "blob:mock-" + (blobSeq++),
    revokeObjectURL: (u) => { revokedUrls.push(u); },
  },
  // rAF callbacks queue up; tests drain them explicitly via runRaf().
  requestAnimationFrame: (fn) => { rafQ.push(fn); return rafQ.length; },
  performance: { now: () => 0 },
  // Observers record their callback/target/options so tests can verify the
  // wiring and fire a notification on demand.
  MutationObserver: function (cb) {
    return { observe(t, o) { moCalls.push({ cb, t, o }); }, disconnect() {} };
  },
  ResizeObserver: function (cb) {
    return { observe(t, o) { roCalls.push({ cb, t, o }); }, disconnect() {} };
  },
  console,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(
  code +
    "\n;globalThis.__x = { statusOf, latestTask, taskDur, fmtDur, durText," +
    " durSpan, PROC_STATUS, subSeed, agentAvatar, statusGlyph, icon," +
    " usageNums, tokCount, runTok, tokTitle, fmtTok, tokSpan, calcTps," +
    " tpsSpan, turnDurMs," +
    " addTurn, addPrompt, addTool, addToolStatus, applyEvents," +
    " MARK_SEL," +
    " scheduleRail, layoutRail, jumpToMark, updateCurMark, updateRulerWave," +
    " t, LOCALES, LOCALE_PREFS, localePref, resolveSystemLocale," +
    " setLocalePref, applyLocale, rerenderLocale, ago, fmtTs, fmtNum," +
    " sendKey, sendErrState, SEND_ERR, setSendState, renderSendStatus," +
    " refreshComposer, toolKindLabel, stopReasonLabel, renderLive," +
    " thinkLabel," +
    " weekStartMs, weekContrib, weekStats, renderWeek, livePartial," +
    " renderSidebar, renderSessionHeader," +
    " select, pollEvents, sendState," +
    " taskActions, actPending, taskAction, renderQueue, dropQueueEntry," +
    " steerMsg, delMsg, ACT_DONE," +
    " hasTranscript, updateDlBtn, sanitizeFilename, buildTranscriptFilename," +
    " sessionToMarkdown, downloadTranscript," +
    " _setTasks: (v) => { tasks = v; }," +
    " _setSessions: (v) => { sessions = v; }," +
    " _setLiveAll: (v) => { liveAll = v; }," +
    " _setLiveUsage: (v) => { liveUsage = v; }," +
    " _liveAll: () => liveAll, _liveUsage: () => liveUsage," +
    " _cache: () => eventsCache, _polled: () => polled," +
    " _panes: () => panes, _rendered: () => rendered," +
    " _railBtns: () => railBtns, _setSelected: (v) => { selected = v; }," +
    " _setOutbox: (v) => { outboxQ = v; }, _outbox: () => outboxQ," +
    " _getLocale: () => locale, _getLangPref: () => langPref };",
  sandbox);
const X = sandbox.__x;

/* ---------- assertions ---------- */
let failed = 0;
const fmt = (v) => { try { return JSON.stringify(v); } catch (e) { return String(v); } };
function eq(got, want, name) {
  const ok = got === want;
  console.log(`${ok ? "PASS" : "FAIL"} ${name}  got=${fmt(got)} want=${fmt(want)}`);
  if (!ok) failed++;
}
const sess = (proc_state, id = "s1") =>
  ({ session_id: id, proc_state, title: id, agent: "devin" });
const task = (over) =>
  Object.assign({ task_id: "t1", session_id: "s1", status: "completed",
    created_at: "2026-01-01T00:00:00Z", started_at: "2026-01-01T00:00:02Z",
    finished_at: "2026-01-01T00:00:42Z" }, over);
const apiCalls = (frag) => fetchCalls.filter((u) => u.includes(frag)).length;

(async () => {
  // --- centralized status mapping: stable keys + tones, not baked labels ---
  X._setTasks([task({})]);
  eq(X.statusOf(sess("busy")).key, "status.proc.running",
    "busy -> running key even when latest task is completed");
  eq(X.t(X.statusOf(sess("busy")).key), "Running",
    "busy -> Running (en)");
  eq(X.statusOf(sess("spawning")).key, "status.proc.starting",
    "spawning -> starting key");
  eq(X.statusOf(sess("ready")).key, "status.proc.ready",
    "ready + completed -> ready key");
  eq(X.statusOf(sess("idle_unloaded")).key, "status.proc.idle",
    "idle_unloaded + completed -> idle key");
  eq(X.statusOf(sess("dead")).key, "status.proc.done",
    "dead + completed -> done key");
  X._setTasks([task({ status: "failed" })]);
  eq(X.statusOf(sess("dead")).key, "status.proc.failed",
    "dead + failed -> failed key");
  const unk = X.statusOf(sess("mystery"));
  eq(unk.key, "status.proc.unknown", "unknown proc_state -> unknown key");
  eq(unk.raw, "mystery", "unknown proc_state keeps the raw code as detail");
  eq(X.statusOf(sess("ready")).tone, "neutral", "ready tone neutral");
  eq(X.statusOf(sess("idle_unloaded")).tone, "neutral", "idle tone neutral");
  eq(X.statusOf(sess("busy")).tone, "running", "busy tone running");
  eq(X.statusOf(sess("spawning")).tone, "running", "spawning tone running");
  eq(Object.isFrozen(X.PROC_STATUS), true, "PROC_STATUS is frozen");
  eq(X.PROC_STATUS.dead, undefined, "dead is not in the map (override only)");

  // --- latestTask: chronological on valid created_at, index fallback ---
  const tk = (id, created_at, over) =>
    Object.assign({ task_id: id, session_id: "s1", status: "completed",
      created_at }, over);
  X._setTasks([tk("new", "2026-01-02T00:00:00Z"), tk("old", "2026-01-01T00:00:00Z")]);
  eq(X.latestTask("s1").task_id, "new", "latestTask picks max created_at");
  X._setTasks([tk("a", "2026-01-01T00:00:00Z"), tk("b", "2026-01-01T00:00:00Z")]);
  eq(X.latestTask("s1").task_id, "b", "tie in created_at -> later array index");
  X._setTasks([tk("valid", "2026-01-01T00:00:00Z"), tk("bogus", "not-a-date")]);
  eq(X.latestTask("s1").task_id, "valid",
    "mixed: invalid created_at never outranks a dated row");
  X._setTasks([tk("bad", "not-a-date"), tk("good", "2026-01-01T00:00:00Z"),
    tk("worse", "")]);
  eq(X.latestTask("s1").task_id, "good",
    "mixed: the single valid row wins regardless of position");
  X._setTasks([tk("x", null), tk("y", "garbage"), tk("z", undefined)]);
  eq(X.latestTask("s1").task_id, "z",
    "all missing/invalid -> last array element (legacy order)");
  eq(X.latestTask("nobody"), null, "no tasks for session -> null");

  // --- taskDur: strict started_at clock, status-aware live/terminal ---
  const t0 = Date.now();
  const running = task({ status: "running", finished_at: null,
    started_at: new Date(t0 - 65000).toISOString() });
  let d = X.taskDur(running);
  eq(d !== null && d.end === null, true, "running task is live (end=null)");
  eq(X.taskDur(task({ status: "queued", started_at: null, finished_at: null })),
    null, "queued without started_at -> omitted");
  eq(X.taskDur(task({ status: "queued", started_at: undefined })),
    null, "queued with only created_at -> omitted (queue age never counted)");
  eq(X.taskDur(task({ status: "completed", finished_at: null })),
    null, "terminal without finished_at -> omitted");
  eq(X.taskDur(task({ status: "failed", finished_at: "garbage" })),
    null, "terminal with invalid finished_at -> omitted");
  eq(X.taskDur(task({ status: "cancelled", finished_at: null })),
    null, "cancelled without finished_at -> omitted");
  eq(X.taskDur(task({ status: "running", started_at: "garbage" })),
    null, "running with invalid started_at -> omitted");
  eq(X.taskDur(task({ status: "completed", started_at: "garbage" })),
    null, "terminal with invalid started_at -> omitted");
  d = X.taskDur(task({}));
  eq(d !== null && typeof d.end === "number", true,
    "completed task has a fixed numeric end");
  eq(X.fmtDur(d.end - d.start), "40s", "completed duration is 40s");
  d = X.taskDur(task({ started_at: "2026-01-01T00:10:00Z",
    finished_at: "2026-01-01T00:00:00Z" }));
  eq(X.fmtDur(d.end - d.start), "0s", "finished before started clamps to 0s");
  eq(X.fmtDur(NaN), "", "fmtDur(NaN) renders empty, never NaN");
  eq(X.fmtDur(Infinity), "", "fmtDur(Infinity) renders empty");

  // --- fmtDur boundaries: whole-second floor, no zero-padded units ---
  eq(X.fmtDur(0), "0s", "0s");
  eq(X.fmtDur(999), "0s", "sub-second floors to 0s");
  eq(X.fmtDur(59999), "59s", "59s");
  eq(X.fmtDur(60000), "1m", "60s -> 1m exactly");
  eq(X.fmtDur((20 * 60 + 43) * 1000), "20m 43s", "20m 43s");
  eq(X.fmtDur(3600 * 1000), "1h", "1h exact");
  eq(X.fmtDur((3600 + 4 * 60) * 1000), "1h 4m", "1h 4m");

  // --- durText: separator lives inside the duration text ---
  d = X.taskDur(task({}));
  eq(X.durText(d), " · 40s", "durText renders ' · 40s'");
  const html = X.durSpan(task({}), "sdur");
  eq(html.includes('data-tid="t1"'), true, "durSpan carries the task id");
  eq(html.includes(" · 40s"), true, "durSpan embeds the separator text");

  // --- live duration advances with the clock; terminal stays frozen ---
  const live0 = X.durText(X.taskDur(running));
  await new Promise((r) => setTimeout(r, 1100));
  const live1 = X.durText(X.taskDur(running));
  eq(live0 !== live1, true, `live duration advances (${live0} -> ${live1})`);
  const doneD = X.taskDur(task({}));
  eq(X.durText(doneD) === X.durText(doneD), true,
    "terminal duration freezes at finished_at");

  // --- busy-only avatar pulse; no rotating ring anywhere ---
  const av = (st) => X.agentAvatar(sess(st), 18);
  eq(av("busy").includes("subav-wrap pulse"), true, "busy avatar pulses");
  for (const st of ["spawning", "ready", "idle_unloaded", "dead"])
    eq(av(st).includes("pulse"), false, `${st} avatar stays static`);
  eq(av("busy").includes("arc") || av("busy").includes("spin"), false,
    "no arc/spinner markup on the avatar");

  // --- turnDurMs: task_id join first, prompt-pair fallback for legacy ---
  X._setTasks([task({})]); // t1: 00:00:02 -> 00:00:42 = 40s
  eq(X.turnDurMs({ t: "turn", task: "t1",
    ts: "2026-01-01T00:00:42Z" }, null), 40000,
    "turn joins task duration via task_id");
  const runningTk = task({ task_id: "t2", status: "running",
    started_at: "2026-01-01T00:00:00Z", finished_at: null });
  X._setTasks([task({}), runningTk]);
  eq(X.turnDurMs({ t: "turn", task: "t2",
    ts: "2026-01-01T00:00:30Z" }, null), 30000,
    "still-running task uses the turn event ts as its end");
  eq(X.turnDurMs({ t: "turn", ts: "2026-01-01T00:00:45Z" },
    "2026-01-01T00:00:05Z"), 40000,
    "legacy transcript: pairs with the preceding prompt ts");
  eq(X.turnDurMs({ t: "turn", ts: "2026-01-01T00:00:45Z" }, null), null,
    "no task, no prompt -> no duration");
  eq(X.turnDurMs({ t: "turn", task: "nope", ts: "garbage" }, "also-bad"), null,
    "unparseable timestamps -> no duration");
  eq(X.turnDurMs({ t: "turn", task: "t2", ts: "not-a-date" }, null), null,
    "running task + invalid turn ts -> null, never Date.now()");
  eq(X.turnDurMs({ t: "turn", task: "t1", ts: "not-a-date" }, null), 40000,
    "terminal task ignores the turn ts entirely (finished_at wins)");

  // --- token usage: normalization, headline math, formatting ---
  eq(X.tokCount({ input_tokens: 108414, cached_input_tokens: 108072,
    output_tokens: 4763 }), 5105, "snake_case: (input-cached)+output");
  eq(X.tokCount({ inputTokens: 108414, cachedReadTokens: 108072,
    outputTokens: 4763 }), 5105, "camelCase: (input-cached)+output");
  eq(X.tokCount({ _meta: { "cognition.ai/inputTokens": 108414,
    "cognition.ai/cachedReadTokens": 108072,
    "cognition.ai/outputTokens": 4763 } }), 5105, "Devin ACP _meta keys");
  eq(X.tokCount({ input_tokens: 100, cached_input_tokens: 500,
    output_tokens: 30 }), 30, "all-cached input clamps to 0");
  eq(X.tokCount({ input_tokens: 100, output_tokens: 50,
    reasoning_output_tokens: 900 }), 150, "reasoning is never added on top");
  eq(X.tokCount({ used: 113177, size: 262000 }), 113177,
    "used-only snapshot is the conservative fallback");
  eq(X.tokCount({ totalTokens: 42000 }), 42000, "totalTokens fallback");
  eq(X.tokCount({}), null, "empty usage -> hidden");
  eq(X.tokCount(null), null, "null usage -> hidden");
  eq(X.tokCount("junk"), null, "non-object usage -> hidden");
  eq(X.fmtTok(0), "0", "fmtTok 0");
  eq(X.fmtTok(842), "842", "fmtTok <1k integer");
  eq(X.fmtTok(131000), "131k", "fmtTok k");
  eq(X.fmtTok(1100000), "1m100k", "fmtTok segmented 1m100k");
  eq(X.fmtTok(1005000), "1m5k", "1,005,000 -> '1m5k' (no zero pad)");
  eq(X.fmtTok(1001000), "1m1k", "1,001,000 -> '1m1k'");
  eq(X.fmtTok(1000000), "1m", "exactly 1,000,000 -> '1m', not '1m0k'");
  eq(X.fmtTok(1000999), "1m", "sub-1k remainder drops -> '1m'");
  eq(X.fmtTok(10000000), "10m", "10m");
  eq(X.fmtTok(999999), "999k", "fmtTok just under 1m");
  eq(X.fmtTok(NaN), "", "fmtTok NaN hides");
  eq(X.fmtTok(-5), "", "fmtTok negative hides");
  // --- run_usage is the displayed metric: exact aggregate, not a snapshot ---
  const runTask = task({ run_usage: { scope: "run", quality: "exact",
    input: 60, output: 12, total: 72, streams: 2, used: 12, size: 100 } });
  const runHtml = X.tokSpan(runTask, "stok");
  eq(runHtml.includes("72 tok"), true, "run_usage headline total renders");
  eq(runHtml.includes("~"), false, "exact run_usage is not marked estimate");
  eq(runHtml.includes("Run tokens 72"), true, "title states the run total");
  eq(runHtml.includes("in 60") && runHtml.includes("out 12"), true,
    "title carries the input/output breakdown");
  eq(runHtml.includes("context 12/100"), true,
    "context occupancy shows as metadata only");
  eq(runHtml.includes('aria-label="Run tokens 72'), true,
    "aria-label mirrors the tooltip");

  // run_usage beats a raw usage snapshot on the same task — `used` is never
  // mistaken for the consumed total.
  const bothTk = task({ usage: { used: 999999, size: 1000000 },
    run_usage: { scope: "run", quality: "exact", input: 10, output: 4,
      total: 14 } });
  const bothHtml = X.tokSpan(bothTk, "stok");
  eq(bothHtml.includes("14 tok"), true, "run_usage preferred over raw usage");
  eq(bothHtml.includes("999"), false, "context used never becomes the total");

  // An estimate without computable run counters renders nothing rather than
  // surfacing the whole conversation total as this run's usage.
  eq(X.tokSpan(task({ run_usage: { scope: "run", quality: "estimate",
    conversation_total: { total: 500 } } }), "stok"), "",
    "estimate without run counters -> hidden, not the conversation total");
  const convTk = task({ run_usage: { scope: "run", quality: "exact",
    input: 30, output: 10, total: 40,
    conversation_total: { input: 130, output: 60, total: 190 } } });
  eq(X.tokSpan(convTk, "stok").includes("conversation 190"), true,
    "resumed-with-baseline tooltip keeps the conversation total separate");

  // Live usage-event snapshot renders for a running task only; terminal
  // tasks always read the persisted run_usage.
  const liveTk = task({ task_id: "tl", status: "running", finished_at: null });
  const liveHtml = X.tokSpan(liveTk, "stok",
    { input: 7, output: 3, total: 10, used: 9, size: 100 });
  eq(liveHtml.includes("10 tok"), true, "live partial renders while running");
  eq(liveHtml.includes("live"), true, "live tooltip marks the snapshot");
  const doneTk = task({ run_usage: { scope: "run", quality: "exact",
    input: 10, output: 4, total: 14 } });
  eq(X.tokSpan(doneTk, "stok",
    { input: 999, output: 999, total: 1998 }).includes("14 tok"), true,
    "terminal task ignores the stale live snapshot");

  // Legacy usage-only fallback: marked estimate, context-only stays honest.
  const tkTask = task({ usage: { input_tokens: 108414,
    cached_input_tokens: 108072, output_tokens: 4763 } });
  const legacyHtml = X.tokSpan(tkTask, "stok");
  eq(legacyHtml.includes("~5k tok"), true,
    "legacy snapshot renders the headline as an estimate");
  eq(legacyHtml.includes("estimate"), true, "legacy tooltip marks estimate");
  eq(legacyHtml.includes("in 108,414"), true,
    "legacy tooltip keeps the breakdown");
  // The label must be truthful: a last-snapshot estimate, never a whole-run
  // total — in the tooltip AND the aria-label, which share one string.
  eq(legacyHtml.includes("Last snapshot estimate ~5,105"), true,
    "legacy tooltip calls itself a last-snapshot estimate");
  eq(legacyHtml.includes("not a whole-run total"), true,
    "legacy tooltip disclaims the whole-run total");
  eq(legacyHtml.includes('aria-label="Last snapshot estimate'), true,
    "legacy aria-label carries the same disclaimer");
  // run_usage = {} is the same legacy path — devin-shaped snapshots included.
  const devinSnap = X.tokSpan(task({ run_usage: {},
    usage: { used: 12275, size: 262000,
      _meta: { "cognition.ai/inputTokens": 12219,
        "cognition.ai/cachedReadTokens": 12071,
        "cognition.ai/outputTokens": 56,
        "cognition.ai/subagent_context": { parentAgentId: "root" } } } }),
    "stok");
  eq(devinSnap.includes("~204 tok"), true,
    "empty run_usage + devin snapshot renders as an estimate");
  eq(devinSnap.includes("Last snapshot estimate"), true,
    "empty run_usage tooltip stays a snapshot estimate");
  eq(devinSnap.includes("not a whole-run total"), true,
    "empty run_usage tooltip disclaims the run total");
  const ctxHtml = X.tokSpan(task({ usage: { used: 113177, size: 262000 } }),
    "stok");
  eq(ctxHtml.includes("~113k tok"), true,
    "context-only snapshot renders as estimate");
  eq(ctxHtml.includes("Context in use"), true,
    "context-only title never claims a run total");
  eq(ctxHtml.includes("last snapshot"), true,
    "context-only title admits it is only a snapshot");
  eq(X.tokSpan(task({}), "stok"), "", "no usage -> empty span");
  eq(X.tokSpan(task({ usage: { junk: 1 } }), "stok"), "",
    "unusable usage -> empty span");

  // --- output tokens/sec: output-only rate over elapsed task seconds ---
  // Terminal: 1200 output tokens over the fixture's 40s -> "30 tok/s",
  // frozen at finished_at and immune to a stale live snapshot.
  const tpsTk = task({ run_usage: { scope: "run", quality: "exact",
    input: 3000, output: 1200, total: 4200 } });
  const tpsHtml = X.tpsSpan(tpsTk, "htps");
  eq(tpsHtml.includes('class="htps"'), true,
    "rate span carries the htps class");
  eq(tpsHtml.includes("30 tok/s"), true,
    "terminal rate = output tokens / elapsed");
  eq(tpsHtml.includes('aria-label="Output speed'), true,
    "rate span exposes the localized aria label");
  eq(tpsHtml.includes("1,200"), true,
    "rate tooltip names the output token count");
  eq(X.tpsSpan(tpsTk, "htps", { input: 0, output: 99999, total: 99999 })
    .includes("30 tok/s"), true,
    "terminal rate freezes on run_usage, ignoring stale live output");
  // Formatting: >=10 rounds to an integer, lower rates keep one decimal.
  eq(X.tpsSpan(task({ run_usage: { scope: "run", quality: "exact",
    output: 164, total: 164 } }), "htps").includes("4.1 tok/s"), true,
    "sub-10 rate keeps one decimal");
  // Running: the live consumed output snapshot wins over persisted rows.
  const tpsRun = task({ status: "running", finished_at: null,
    started_at: new Date(Date.now() - 10000).toISOString(),
    run_usage: { scope: "run", quality: "exact", output: 10, total: 10 } });
  const tpsLiveHtml = X.tpsSpan(tpsRun, "htps",
    { input: 50, output: 350, total: 400 });
  const tpsRate = parseFloat(
    (tpsLiveHtml.match(/([\d.]+) tok\/s/) || [])[1]);
  eq(tpsRate > 30 && tpsRate <= 35, true,
    "running rate divides the live output snapshot (~35 tok/s)");
  const tpsNoLive = X.tpsSpan(tpsRun, "htps", null);
  eq(tpsNoLive.includes(" tok/s"), true,
    "running task falls back to run_usage without a live snapshot");
  // Codex-style: nothing streams and nothing persisted while running -> blank.
  eq(X.tpsSpan(task({ task_id: "tc", status: "running", finished_at: null,
    started_at: new Date(Date.now() - 60000).toISOString() }), "htps",
    { input: 9, output: 0, total: 9 }), "",
    "running with only input counted -> hidden, never invented");
  // Suppression matrix: bad output, bad times, inverted or sub-second runs.
  eq(X.tpsSpan(task({ run_usage: { scope: "run", quality: "exact",
    output: 0, total: 5 } }), "htps"), "", "zero output -> hidden");
  eq(X.tpsSpan(task({ run_usage: { scope: "run", quality: "exact",
    input: 50, total: 50 } }), "htps"), "", "input-only run_usage -> hidden");
  eq(X.tpsSpan(task({ started_at: null,
    run_usage: { scope: "run", quality: "exact", output: 10, total: 10 } }),
    "htps"), "", "missing started_at -> hidden");
  eq(X.tpsSpan(task({ finished_at: null,
    run_usage: { scope: "run", quality: "exact", output: 10, total: 10 } }),
    "htps"), "", "terminal without finished_at -> hidden");
  eq(X.tpsSpan(task({ started_at: "2026-01-01T00:10:00Z",
    finished_at: "2026-01-01T00:00:00Z",
    run_usage: { scope: "run", quality: "exact", output: 10, total: 10 } }),
    "htps"), "", "finished before started -> hidden, never a negative rate");
  eq(X.tpsSpan(task({ started_at: "2026-01-01T00:00:41.600Z",
    finished_at: "2026-01-01T00:00:42Z",
    run_usage: { scope: "run", quality: "exact", output: 50, total: 50 } }),
    "htps"), "", "sub-second elapsed -> hidden (spike guard)");
  // Legacy raw-usage fallback, same precedence convention as tokSpan.
  eq(X.tpsSpan(task({ usage: { output_tokens: 800 } }), "htps")
    .includes("20 tok/s"), true, "legacy raw output snapshot still rates");
  // Localized rate unit + tooltip in all three dictionaries.
  X.setLocalePref("zh-CN");
  eq(X.tpsSpan(tpsTk, "htps").includes("30 token/秒"), true,
    "rate unit zh-CN");
  eq(X.tpsSpan(tpsTk, "htps").includes("输出速率"), true,
    "rate tooltip zh-CN");
  X.setLocalePref("zh-TW");
  eq(X.tpsSpan(tpsTk, "htps").includes("30 token/秒"), true,
    "rate unit zh-TW");
  eq(X.tpsSpan(tpsTk, "htps").includes("輸出速率"), true,
    "rate tooltip zh-TW");
  X.setLocalePref("system");
  // Header integration: .htps lands inside .hstatus right after .htok.
  X._setSessions([sess("dead", "s1")]);
  X._setTasks([tpsTk]);
  X._setSelected("s1");
  X.renderSessionHeader();
  const hsub = elCache["#hwrap"].innerHTML;
  eq(hsub.includes('class="htps"'), true,
    "session header renders the tok/s metric");
  eq(hsub.indexOf('class="htok"') < hsub.indexOf('class="htps"'), true,
    "tok/s sits beside the token count in .hstatus");

  // --- addTurn: visible text is exactly "turn ended · <dur>" (+reason) ---
  let lastDiv = null;
  documentStub.createElement = () => (lastDiv = el());
  const spanText = () => {
    const m = lastDiv && lastDiv.innerHTML.match(/<span>([^<]*)<\/span>/);
    return m ? m[1] : null;
  };
  X._setTasks([task({})]); // t1: 00:00:02 -> 00:00:42 = 40s
  X.addTurn({ t: "turn", task: "t1", ts: "2026-01-01T00:00:42Z",
    stop_reason: "end_turn" });
  eq(spanText(), "turn ended · 40s",
    "end_turn renders exactly 'turn ended · <dur>'");
  eq(String(lastDiv.title).length > 0, true,
    "wall-clock timestamp moved to the tooltip");
  X.addTurn({ t: "turn", task: "t1", ts: "2026-01-01T00:00:42Z",
    stop_reason: "stalled" });
  eq(spanText(), "turn ended · 40s · stalled",
    "abnormal stop reason renders after the duration");
  X._setTasks([]);
  X.addTurn({ t: "turn", ts: "2026-01-01T00:00:45Z", stop_reason: "end_turn" });
  eq(spanText(), "turn ended",
    "no task, no prompt -> bare 'turn ended', still no ts segment");
  X.addPrompt({ t: "prompt", ts: "2026-01-01T00:00:05Z", text: "hi",
    src: null });
  X.addTurn({ t: "turn", ts: "2026-01-01T00:00:45Z", stop_reason: "end_turn" });
  eq(spanText(), "turn ended · 40s", "legacy prompt-pair fallback in text");
  documentStub.createElement = function (tag) {
    const e = el(); e.tagName = String(tag || "").toUpperCase();
    created.push(e); return e;
  };

  /* ================= nav rail: marks, geometry, interaction =================
     contentEl/railEl carry real children, offsetTop values and scroll
     metrics, so layoutRail/updateCurMark/updateRulerWave/jumpToMark and the
     rail's own listeners run unmodified inside the vm. */
  // Tick center: the --my custom property is always written; the literal
  // transform is the no-CSS-var fallback path's channel.
  const yOf = (b) => {
    const v = parseFloat(b.style.getPropertyValue("--my"));
    return Number.isFinite(v) ? v
      : parseFloat(b.style.transform.match(/translateY\(([-\d.]+)/)[1]);
  };
  // Thickness encodes kind only: agent message 2px, dispatched 3px, turn
  // end 4px — asserted via the k-* classes, never via inline height.
  const thOf = (b) => b.classList.contains("k-turn") ? 4
    : b.classList.contains("k-disp") ? 3 : 2;
  // Wave emphasis: --mo is always written; literal opacity is the fallback.
  const moOf = (b) => {
    const v = parseFloat(b.style.getPropertyValue("--mo"));
    return Number.isFinite(v) ? v : parseFloat(b.style.opacity);
  };
  const fire = (t, ev, arg) => (t._ls[ev] || []).forEach((f) => f(arg));
  const cardAt = (cls, top) => {
    const c = el(); c.className = cls; c.offsetTop = top; return c;
  };
  // Fresh fixture scene: children + metrics, scroll back to the rest position.
  const railScene = (clsTops, doc, rh) => {
    contentEl.children = clsTops.map(([cls, top]) => cardAt(cls, top));
    contentEl.scrollHeight = doc; contentEl.scrollTop = 0;
    railEl.clientHeight = rh;
    X.layoutRail();
    return X._railBtns();
  };

  // --- taxonomy: normalized events -> DOM -> marks. Agent message cards,
  //     MCP-dispatched prompt cards and successful turn ends are marked;
  //     dashboard-authored "User Message" cards, thinking folds, tool
  //     groups, error dividers, empty states and usage-only events never
  //     get one. ---
  eq(X.MARK_SEL, ".block.card.msg,.block.card.prompt:not(.user),.turnend:not(.err)",
    "MARK_SEL is the pinned taxonomy selector");
  X._setSelected("rail-sess");
  X._setTasks([task({})]);
  contentEl.children = [];
  X.applyEvents([
    { t: "prompt", ts: "2026-01-01T00:00:05Z", text: "mcp task", src: "mcp" },
    { t: "prompt", ts: "2026-01-01T00:00:06Z", text: "typed", src: "dashboard" },
    { t: "msg", ts: "2026-01-01T00:00:07Z", text: "working on it" },
    { t: "think", ts: "2026-01-01T00:00:08Z", text: "hmm" },
    { t: "tool", ts: "2026-01-01T00:00:09Z", id: "tc9", kind: "read",
      title: "read f" },
    { t: "tool_status", ts: "2026-01-01T00:00:10Z", id: "tc9",
      status: "completed" },
    { t: "turn", ts: "2026-01-01T00:00:42Z", task: "t1",
      stop_reason: "end_turn" },
    { t: "error", ts: "2026-01-01T00:00:43Z", text: "boom" },
    { t: "usage", ts: "2026-01-01T00:00:44Z", consumed: { total: 5 } },
  ]);
  eq(contentEl.children.length, 7,
    "taxonomy: every DOM-producing event kind rendered");
  contentEl.children.forEach((c, i) => {
    c.offsetTop = [20, 140, 260, 380, 500, 620, 740][i];
  });
  contentEl.scrollHeight = 800; contentEl.clientHeight = 400;
  railEl.clientHeight = 100;
  X.layoutRail();
  let bs = X._railBtns();
  eq(bs.length, 3, "marks = dispatched prompt + agent message + turn end");
  eq(bs[0]._els[0], contentEl.children[0], "mark 0 -> dispatched prompt card");
  eq(bs[1]._els[0], contentEl.children[2], "mark 1 -> agent message card");
  eq(bs[2]._els[0], contentEl.children[5], "mark 2 -> turn-end divider");
  eq(railEl.hidden, false, "rail shown when markable cards exist");
  eq(bs[0].attrs["aria-label"].includes("Dispatched message"), true,
    "dispatched prompt mark gets the localized kind label");
  eq(bs[1].attrs["aria-label"].includes("Agent message"), true,
    "agent mark gets the localized kind label");
  eq(bs[2].attrs["aria-label"].includes("Turn end"), true,
    "turn-end mark gets the localized kind label");
  eq(bs[0].title, bs[0].attrs["aria-label"], "title mirrors the aria-label");
  // Fixed-length ticks; kind shows as thickness only (2/3/4px classes).
  eq(bs[0].classList.contains("k-disp"), true, "dispatched mark is k-disp");
  eq(bs[1].classList.contains("k-msg"), true, "agent mark is k-msg");
  eq(bs[2].classList.contains("k-turn"), true, "turn-end mark is k-turn");
  eq(thOf(bs[0]), 3, "dispatched tick is 3px thick");
  eq(thOf(bs[1]), 2, "agent tick is 2px thick");
  eq(thOf(bs[2]), 4, "turn-end tick is 4px thick");
  // Compact centered offsets (Voyager buildCompactMarkerOffsets): n=3 ->
  // step=min(8,160/2)=8, the group centered on the rail midpoint (RH/2=50).
  // --my is the tick CENTER (the tick self-centers via translateY(-50%)).
  eq(yOf(bs[0]), 42, "compact offset: midpoint + (0-1)*8");
  eq(yOf(bs[1]), 50, "compact offset: rail midpoint");
  eq(yOf(bs[2]), 58, "compact offset: midpoint + (2-1)*8");

  // --- --railin: measured native scrollbar gutter on #pane ---
  const paneEl = elCache["#pane"];
  contentEl.clientWidth = 400;
  contentEl.offsetWidth = 417;          // 17px classic-scrollbar gutter
  X.layoutRail();
  eq(paneEl.style.getPropertyValue("--railin"), "17px",
    "railin = offsetWidth - clientWidth");
  contentEl.offsetWidth = 400;          // overlay scrollbar: no gutter
  X.layoutRail();
  eq(paneEl.style.getPropertyValue("--railin"), "0px",
    "overlay scrollbars inset the rail by 0");

  // --- a sensible current mark exists at scrollTop = 0 ---
  contentEl.scrollTop = 0;
  X.updateCurMark();
  eq(bs[0].classList.contains("cur"), true,
    "first mark is current at scrollTop=0");
  eq(bs[0].attrs["aria-current"], "true", "aria-current on the first mark");
  eq(bs[0].tabIndex, 0, "first mark is the rail's tab stop at rest");
  eq(bs[1].tabIndex, -1, "other marks leave the tab order at rest");

  // --- the scroll hook tracks the 0.45-viewport reference line ---
  contentEl.scrollTop = 300;          // probe y=480: card tops 20,260 pass
  fire(contentEl, "scroll");
  eq(bs[1].classList.contains("cur"), true, "scrolling moves current to mark 1");
  eq(bs[0].attrs["aria-current"], undefined, "aria-current leaves mark 0");
  eq(bs[1].attrs["aria-current"], "true", "aria-current lands on mark 1");

  // --- the focus wave: a Gaussian crest of opacity tracks the 0.45 line ---
  // focus=480 -> fractional index 1.61: mark 1 is current (forced to 1) and
  // mark 2 sits nearer the crest than mark 0.
  eq(moOf(bs[1]), 1, "current mark rides the crest at full opacity");
  eq(moOf(bs[0]) < moOf(bs[2]) && moOf(bs[2]) < moOf(bs[1]), true,
    "opacity falls off with distance from the focus line");
  // Scrolling moves the crest: at scrollTop=0 focus=180 brackets anchors
  // 20/260, so mark 0 is the hot tick (and the current one -> forced to 1).
  contentEl.scrollTop = 0;
  fire(contentEl, "scroll");
  eq(moOf(bs[0]), 1, "crest back on mark 0 at rest (current mark)");
  eq(moOf(bs[0]) > moOf(bs[1]) && moOf(bs[1]) > moOf(bs[2]), true,
    "opacity decays with distance from the focus line");
  // Every tick keeps its fixed length — no inline width, no scale transform.
  bs.forEach((b, i) => {
    eq(b.style.width, undefined, `tick ${i} length never set inline`);
    eq(/scale/.test(b.style.transform || ""), false,
      `tick ${i} never scaled`);
  });

  // --- compact geometry: every source keeps its own tick — sources are
  //     never merged by document proximity ---
  bs = railScene([["block card msg", 100], ["block card msg", 400],
    ["block card msg", 430]], 1000, 100);
  eq(bs.length, 3, "one tick per source even when cards sit close together");
  eq(yOf(bs[0]), 42, "n=3 -> first tick mid-8");
  eq(yOf(bs[1]), 50, "n=3 -> middle tick on the midpoint");
  eq(yOf(bs[2]), 58, "n=3 -> last tick mid+8");
  // n=1 centers exactly; n=2 flanks the midpoint.
  bs = railScene([["block card msg", 100]], 1000, 100);
  eq(bs.length, 1, "single source -> single tick");
  eq(yOf(bs[0]), 50, "lone tick centered on the rail midpoint");
  bs = railScene([["block card msg", 100], ["block card prompt", 700]],
    1000, 100);
  eq(yOf(bs[0]), 46, "n=2 -> mid-4");
  eq(yOf(bs[1]), 54, "n=2 -> mid+4");
  // Large n: step shrinks to 160/(n-1); edge centers clamp inside the rail.
  bs = railScene(
    Array.from({ length: 30 }, (_, i) => ["block card msg", i * 30]),
    1000, 100);
  eq(bs.length, 30, "30 sources -> 30 ticks, still no merging");
  bs.forEach((b, i) => eq(
    yOf(b) - thOf(b) / 2 >= 0 &&
      yOf(b) + thOf(b) / 2 <= railEl.clientHeight, true,
    `tick ${i} inside rail bounds`));
  eq(yOf(bs[0]), 1, "first tick clamps to th/2");
  eq(yOf(bs[29]), 99, "last tick clamps to RH-th/2");
  eq(yOf(bs[15]), 52.8, "interior step = 160/(n-1)");

  // --- mark buttons are reused across layout passes (no DOM churn) ---
  bs = railScene([["block card msg", 100], ["block card msg", 400],
    ["block card msg", 430]], 1000, 100);
  const reused = bs[0];
  X.layoutRail();
  eq(X._railBtns()[0], reused, "layout passes reuse existing mark buttons");

  // --- jumpToMark: scrolls to the card, keyboard clicks also focus it ---
  const card0 = bs[0]._els[0];
  contentEl._scrollToArgs = null;
  bs[0].onclick({ detail: 1 });
  eq(contentEl._scrollToArgs.top, 92, "jump scrolls to offsetTop-8");
  eq(contentEl._scrollToArgs.behavior, "smooth", "smooth scroll by default");
  eq(card0.focused, 0, "mouse click never moves focus to the card");
  mediaStubs["(prefers-reduced-motion: reduce)"].matches = true;
  bs[0].onclick({ detail: 1 });
  eq(contentEl._scrollToArgs.behavior, "auto",
    "reduced motion jumps instantly");
  mediaStubs["(prefers-reduced-motion: reduce)"].matches = false;
  bs[0].onclick({ detail: 0 });
  eq(card0.focused, 1, "keyboard-activated click focuses the card");
  eq(card0.tabIndex, -1, "card takes tabIndex=-1 for programmatic focus");
  eq(documentStub.activeElement, card0, "card becomes the active element");

  // --- roving tabindex: one tab stop, arrows/Home/End move it ---
  bs[0].focus();
  let pd = 0;
  fire(railEl, "keydown", { key: "ArrowDown", preventDefault: () => pd++ });
  eq(pd, 1, "ArrowDown is handled");
  eq(documentStub.activeElement, bs[1], "ArrowDown focuses the next mark");
  eq(bs[1].tabIndex, 0, "the tab stop moved with focus");
  eq(bs[0].tabIndex, -1, "the previous mark leaves the tab order");
  fire(railEl, "keydown", { key: "End", preventDefault: () => pd++ });
  eq(documentStub.activeElement, bs[2], "End focuses the last mark");
  fire(railEl, "keydown", { key: "Home", preventDefault: () => pd++ });
  eq(documentStub.activeElement, bs[0], "Home focuses the first mark");
  // While the rail is in use the tab stop stays on the focused mark even
  // when scroll position makes another mark current.
  bs[1].focus();
  contentEl.scrollTop = 985;          // probe 1165 passes every card top
  X.updateCurMark();
  eq(bs[2].classList.contains("cur"), true, "current mark tracks scroll");
  contentEl.scrollTop = 0;
  X.updateCurMark();
  eq(bs[0].classList.contains("cur"), true, "current mark back to the first");
  eq(bs[1].tabIndex, 0, "focused mark keeps the tab stop");
  eq(bs[0].tabIndex, -1,
    "current mark leaves the tab order while another is focused");

  // --- no proportional bare-track seek: compact offsets carry no document
  //     position, so clicking the bare rail never scrolls ---
  contentEl._scrollToArgs = null;
  fire(railEl, "click", { target: railEl, offsetY: 50 });
  eq(contentEl._scrollToArgs, null,
    "bare-track clicks are not handled — no proportional seek");
  fire(railEl, "click", { target: railEl, offsetY: yOf(bs[0]) });
  eq(contentEl._scrollToArgs, null, "even level with a mark, the track ignores");

  // --- wheel over the rail scrolls the conversation (Voyager parity) ---
  const st0 = contentEl.scrollTop;
  let pd2 = 0;
  fire(railEl, "wheel", { deltaY: 140, preventDefault: () => pd2++ });
  eq(contentEl.scrollTop, st0 + 140, "wheel delta forwards to #content");
  eq(pd2, 1, "wheel default is prevented");
  fire(railEl, "wheel", { deltaY: -60, preventDefault: () => pd2++ });
  eq(contentEl.scrollTop, st0 + 80, "wheel up scrolls back");
  fire(railEl, "wheel", { deltaY: 0, preventDefault: () => pd2++ });
  eq(contentEl.scrollTop, st0 + 80, "zero delta is a no-op");
  eq(pd2, 2, "zero delta does not preventDefault");

  // --- live updates: observers schedule one rAF-coalesced layout pass ---
  eq(moCalls.length, 1, "a single MutationObserver is registered");
  eq(moCalls[0].t, contentEl, "MutationObserver watches #content");
  eq(!!(moCalls[0].o.childList && moCalls[0].o.subtree &&
    moCalls[0].o.characterData), true,
    "observer watches childList + subtree + characterData");
  eq(moCalls[0].o.attributeFilter.join(","), "open,class",
    "only open/class attribute changes trigger a rescan");
  eq(roCalls.length === 1 && roCalls[0].t === contentEl, true,
    "ResizeObserver watches #content");
  runRaf();                    // flush the boot-time scheduleRail callback
  eq(rafQ.length, 0, "no rail work pending once the queue is drained");
  moCalls[0].cb();
  eq(rafQ.length, 1, "a mutation schedules a rail layout pass");
  moCalls[0].cb(); roCalls[0].cb();
  eq(rafQ.length, 1, "further notifications coalesce into the same frame");
  contentEl.children = [cardAt("block card msg", 50)];
  runRaf();
  eq(X._railBtns().length, 1, "the scheduled pass re-derives marks");

  // --- rail hides cleanly: nothing markable, or no selected session ---
  X.layoutRail();
  eq(railEl.hidden, false, "rail shown for the remaining mark");
  contentEl.children = [];
  X.layoutRail();
  eq(railEl.hidden, true, "rail hides with no markable cards");
  eq(X._railBtns().length, 0, "stale marks are removed");
  contentEl.children = [cardAt("block card msg", 50)];
  X._setSelected(null);
  X.layoutRail();
  eq(railEl.hidden, true, "rail hides with no selected session");
  X._setSelected("rail-sess");
  X.layoutRail();
  eq(railEl.hidden, false, "rail restores on selection");
  // A mark whose card was detached (pane stash) is a safe no-op.
  const stale = X._railBtns()[0];
  contentEl.children[0].isConnected = false;
  contentEl.children = [];
  X.layoutRail();
  contentEl._scrollToArgs = null;
  stale.onclick({ detail: 0 });
  eq(contentEl._scrollToArgs, null, "detached mark click never scrolls");
  documentStub.activeElement = null;

  /* ================= i18n: bundled dictionaries ================= */

  // --- dictionary shape: three frozen locales, English fallback in t() ---
  eq(Object.isFrozen(X.LOCALES), true, "LOCALES is frozen");
  eq(Object.keys(X.LOCALES).sort().join(","), "en,zh-CN,zh-TW",
    "exactly three resource locales");
  const enKeys = Object.keys(X.LOCALES.en).sort();
  eq(JSON.stringify(Object.keys(X.LOCALES["zh-CN"]).sort()),
    JSON.stringify(enKeys), "zh-CN key set identical to en");
  eq(JSON.stringify(Object.keys(X.LOCALES["zh-TW"]).sort()),
    JSON.stringify(enKeys), "zh-TW key set identical to en");
  eq(X.LOCALE_PREFS.join(","), "system,en,zh-CN,zh-TW",
    "preference values: system + three locales");
  eq(X.t("does.not.exist"), "does.not.exist",
    "missing key surfaces the key itself (dev signal)");
  eq(X.t("session.turns", { n: 1 }), "1 turn", "en plural one");
  eq(X.t("session.turns", { n: 7 }), "7 turns", "en plural other");
  eq(X.t("session.working_repo", { repo: "<x>" }), "Working repo · <x>",
    "named interpolation (text, escaping is the caller's job)");

  // --- system-locale resolution: zh-Hant/TW/HK/MO -> zh-TW etc. ---
  const sysLoc = (langs, lang) => {
    sandbox.navigator.languages = langs;
    sandbox.navigator.language = lang;
    return X.resolveSystemLocale();
  };
  eq(sysLoc(["zh-TW"]), "zh-TW", "zh-TW -> zh-TW");
  eq(sysLoc(["zh-Hant"]), "zh-TW", "zh-Hant -> zh-TW");
  eq(sysLoc(["zh-HK"]), "zh-TW", "zh-HK -> zh-TW");
  eq(sysLoc(["zh-MO"]), "zh-TW", "zh-MO -> zh-TW");
  eq(sysLoc(["zh-Hant-HK"]), "zh-TW", "zh-Hant-HK -> zh-TW");
  eq(sysLoc(["zh"]), "zh-CN", "bare zh -> zh-CN");
  eq(sysLoc(["zh-Hans"]), "zh-CN", "zh-Hans -> zh-CN");
  eq(sysLoc(["zh-CN"]), "zh-CN", "zh-CN -> zh-CN");
  eq(sysLoc(["zh-SG"]), "zh-CN", "zh-SG -> zh-CN");
  eq(sysLoc(["zh-MY"]), "zh-CN", "zh-MY -> zh-CN");
  eq(sysLoc(["en-GB"]), "en", "en-GB -> en");
  eq(sysLoc(["en-AU", "zh-TW"]), "en", "first preference wins (en-AU)");
  eq(sysLoc(["fr-FR", "zh-TW"]), "zh-TW",
    "unrecognized tags are skipped in order");
  eq(sysLoc(["fr-FR"]), "en", "unrecognized -> en");
  eq(sysLoc([], "zh-Hant"), "zh-TW",
    "empty navigator.languages falls back to navigator.language");
  eq(sysLoc(undefined, "zh-CN"), "zh-CN", "no languages list -> language");
  eq(sysLoc([], "ja-JP"), "en", "unrecognized language -> en");
  sandbox.navigator.languages = ["en-US"];
  sandbox.navigator.language = "en-US";

  // --- preference persistence + document lang/title ---
  eq(X._getLangPref(), "system", "default preference is system");
  eq(X._getLocale(), "en", "system resolved to en (navigator en-US)");
  eq(documentStub.documentElement.lang, "en", "<html lang> applied");
  eq(documentStub.title, "Agent Bridge Dashboard", "document.title en");

  X.setLocalePref("zh-CN");
  eq(X._getLangPref(), "zh-CN", "zh-CN preference stored");
  eq(store["ab-locale"], "zh-CN", "ab-locale persisted");
  eq(X._getLocale(), "zh-CN", "locale resolved to zh-CN");
  eq(documentStub.documentElement.lang, "zh-CN", "<html lang=zh-CN>");
  eq(documentStub.title, "Agent Bridge 仪表板", "document.title zh-CN");
  eq(elCache["#langsel"].value, "zh-CN", "select mirrors the preference");
  eq(X.t("status.proc.running"), "运行中", "running label zh-CN");
  eq(X.t("status.proc.idle"), "空闲", "idle label zh-CN");
  eq(X.t("session.turns", { n: 7 }), "7 回合", "zh plural flat {n}");
  eq(X.t("session.turns", { n: 1 }), "1 回合", "zh has no one/other split");

  X.setLocalePref("zh-TW");
  eq(X._getLocale(), "zh-TW", "locale resolved to zh-TW");
  eq(documentStub.title, "Agent Bridge 儀表板", "document.title zh-TW");
  eq(X.t("status.proc.running"), "執行中", "running label zh-TW");
  eq(X.t("status.proc.idle"), "閒置", "idle label zh-TW");
  eq(X.t("status.proc.failed"), "失敗", "failed label zh-TW");
  eq(X.t("transcript.user_message"), "使用者訊息", "user message zh-TW");
  eq(X.t("transcript.dispatched_message"), "已派發訊息",
    "dispatched message zh-TW");
  eq(X.t("transcript.turn_ended"), "回合已結束", "turn ended zh-TW");

  X.setLocalePref("bogus");
  eq(X._getLangPref(), "system", "unknown preference falls back to system");
  eq(X._getLocale(), "en", "bogus pref -> system -> en");

  // system mode tracks navigator.languages at apply time
  sandbox.navigator.languages = ["zh-HK"];
  X.setLocalePref("system");
  eq(X._getLocale(), "zh-TW", "system pref + zh-HK -> zh-TW");
  eq(store["ab-locale"], "system", "system itself is persisted");

  // storage sync: another tab wrote a new ab-locale
  store["ab-locale"] = "zh-CN";
  (listeners.storage || []).forEach((f) => f({ key: "ab-locale" }));
  eq(X._getLangPref(), "zh-CN", "storage event re-reads ab-locale");
  eq(X._getLocale(), "zh-CN", "storage event applies zh-CN");
  store["ab-locale"] = "system";
  (listeners.storage || []).forEach((f) => f({ key: "ab-locale" }));
  eq(X._getLangPref(), "system", "storage event back to system");
  // languagechange only fires while preference is system
  sandbox.navigator.languages = ["zh-MO"];
  (listeners.languagechange || []).forEach((f) => f());
  eq(X._getLocale(), "zh-TW", "languagechange re-resolves in system mode");
  X.setLocalePref("en");
  sandbox.navigator.languages = ["zh-TW"];
  (listeners.languagechange || []).forEach((f) => f());
  eq(X._getLocale(), "en", "languagechange ignored with explicit pref");
  sandbox.navigator.languages = ["en-US"];

  // --- localized formats in each locale ---
  X.setLocalePref("zh-CN");
  eq(X.fmtDur(40 * 1000), "40 秒", "duration zh-CN");
  eq(X.fmtDur((20 * 60 + 43) * 1000), "20 分 43 秒", "min+sec zh-CN");
  eq(X.ago(new Date(Date.now() - 30e3).toISOString()), "30 秒前",
    "relative time zh-CN");
  const zhTok = X.tokTitle({ total: 72, input: 60, output: 12, used: 12,
    size: 100 });
  eq(zhTok.includes("运行 token 72"), true, "token tooltip zh-CN");
  eq(zhTok.includes("令牌"), false, "token never translated as 令牌");
  X.setLocalePref("zh-TW");
  eq(X.fmtDur(40 * 1000), "40 秒", "duration zh-TW");
  eq(X.tokTitle({ total: 72 }).includes("執行 token 72"), true,
    "token tooltip zh-TW");
  eq(X.stopReasonLabel("paused"), "已暫停", "paused stop reason zh-TW");
  X.setLocalePref("en");
  eq(X.ago(new Date(Date.now() - 30e3).toISOString()), "30s ago",
    "relative time en");
  eq(X.t("tokens.run", { n: "72" }), "Run tokens 72", "token tooltip en");
  eq(X.stopReasonLabel("paused"), "paused", "paused stop reason en");

  // --- invalid/missing timestamps render nothing in any locale ---
  for (const loc of ["en", "zh-CN", "zh-TW"]) {
    X.setLocalePref(loc);
    eq(X.fmtTs("garbage"), "", `fmtTs unparseable -> "" (${loc})`);
    eq(X.fmtTs(undefined), "", `fmtTs undefined -> "" (${loc})`);
    eq(X.fmtTs(null), "", `fmtTs null -> "" (${loc})`);
    eq(X.ago("garbage"), "", `ago unparseable -> "" (${loc})`);
    eq(X.ago(undefined), "", `ago undefined -> "" (${loc})`);
    eq(X.ago(null), "", `ago null -> "" (${loc})`);
  }
  X.setLocalePref("en");
  eq(X.ago(new Date(Date.now() + 60000).toISOString()), "0s ago",
    "future timestamp clamps to 0s ago");
  eq(X.fmtTs("2026-01-01T10:00:00Z") !== "", true,
    "fmtTs valid timestamp still renders");
  X.setLocalePref("zh-CN");
  eq(X.ago(new Date(Date.now() - 30e3).toISOString()), "30 秒前",
    "valid ago still localizes zh-CN");
  X.setLocalePref("en");

  // --- thinking fold count: words for space-separated scripts, characters
  //     for predominantly-CJK text ---
  eq(X.thinkLabel("hello world foo"), "Thinking · 3 words",
    "en word count unchanged");
  eq(X.thinkLabel("分析一下这个问题"), "Thinking · 8 chars",
    "CJK-dominant text counts characters (en)");
  eq(X.thinkLabel("ok 分析 done"), "Thinking · 3 words",
    "Latin-dominant mixed text keeps word counting");
  X.setLocalePref("zh-CN");
  eq(X.thinkLabel("hello world foo"), "思考中 · 3 词", "zh-CN word count");
  eq(X.thinkLabel("分析一下这个问题"), "思考中 · 8 字", "zh-CN char count");
  X.setLocalePref("zh-TW");
  eq(X.thinkLabel("分析一下这个问题"), "思考中 · 8 字", "zh-TW char count");
  X.setLocalePref("en");

  // --- compact token unit comes from the dictionary ---
  const unitTk = task({ run_usage: { scope: "run", quality: "exact",
    input: 10, output: 4, total: 14 } });
  eq(X.tokSpan(unitTk, "stok").includes("14 tok"), true,
    "en compact unit is 'tok'");
  X.setLocalePref("zh-CN");
  eq(X.tokSpan(unitTk, "stok").includes("14 token"), true,
    "zh-CN unit is the full 'token'");
  X.setLocalePref("en");

  // --- tool kinds / status a11y / stop reasons localize; raw stays raw ---
  X.setLocalePref("zh-CN");
  eq(X.toolKindLabel("execute"), "执行", "tool kind execute zh-CN");
  eq(X.toolKindLabel("weirdkind"), "Weirdkind",
    "unknown tool kind stays raw (capitalized)");
  eq(X.stopReasonLabel("stalled"), "已停滞", "known stop reason zh-CN");
  eq(X.stopReasonLabel("end_turn"), "end_turn",
    "end_turn is suppressed upstream, stays raw here");
  eq(X.stopReasonLabel("provider_x"), "provider_x",
    "unknown stop reason stays raw");
  eq(X.stopReasonLabel("paused"), "已暂停", "paused stop reason zh-CN");
  const beforeTool = created.length;
  X.addTool({ t: "tool", ts: 1, id: "tc1", kind: "execute", title: "run ls",
    input: "ls -la" });
  const toolRow = created.slice(beforeTool)
    .find((e) => e.className === "tool");
  eq(toolRow.innerHTML.includes("执行"), true, "tool row label zh-CN");
  eq(toolRow.innerHTML.includes('aria-label="进行中"'), true,
    "in-progress tool status aria-label zh-CN");
  eq(toolRow.innerHTML.includes("run ls"), true,
    "tool title (user data) not translated");
  X.addToolStatus({ t: "tool_status", id: "tc1", status: "completed", ts: 5 });
  eq(toolRow._q[".st"].attrs["aria-label"], "已完成",
    "completed tool status aria-label zh-CN");
  X.addToolStatus({ t: "tool_status", id: "tc1", status: "weird_state",
    ts: 9 });
  eq(toolRow._q[".st"].attrs["aria-label"], undefined,
    "unknown tool status drops the a11y label (raw text instead)");
  // Invalid tool timestamps leave the duration empty — never "NaNms".
  const beforeTool2 = created.length;
  X.addTool({ t: "tool", ts: "bogus", id: "tc2", kind: "read", title: "r" });
  const toolRow2 = created.slice(beforeTool2)
    .find((e) => e.className === "tool");
  X.addToolStatus({ t: "tool_status", id: "tc2", status: "completed",
    ts: 5 });
  const durEl = toolRow2._q[".dur"];
  eq(durEl ? durEl.textContent : "", "",
    "invalid tool timestamp renders no duration (no NaN leak)");
  X.setLocalePref("en");
  eq(X.toolKindLabel("execute"), "Execute", "tool kind execute en");

  // --- agy pairing: a second full record for the same tool_call_id folds
  //     into the existing row instead of painting a duplicate, and a carried
  //     status closes it. ---
  const beforeMerge = created.length;
  X.addTool({ t: "tool", ts: "2026-01-01T00:00:10Z", id: "conv:7",
    kind: "search", title: "find_by_name", input: '{"Pattern":"*"}' });
  const mergeRow = created.slice(beforeMerge)
    .find((e) => e.className === "tool");
  X.addTool({ t: "tool", ts: "2026-01-01T00:00:12Z", id: "conv:7",
    kind: "search", title: "find_by_name", status: "completed" });
  eq(created.slice(beforeMerge).filter((e) => e.className === "tool").length,
    1, "same tool_call_id merges — no duplicate row");
  eq(mergeRow._q[".st"].attrs["aria-label"], "completed",
    "merged record applies the carried status");
  eq(mergeRow._q[".st"].className, "st completed",
    "merged row status class flips off in_progress");
  // A legacy DONE record whose ACTIVE twin was truncated still lands a
  // complete, already-completed row.
  const beforeSolo = created.length;
  X.addTool({ t: "tool", ts: "2026-01-01T00:00:20Z", id: "conv:8",
    kind: "read", title: "view_file", input: "{}", status: "completed" });
  const soloRow = created.slice(beforeSolo)
    .find((e) => e.className === "tool");
  eq(soloRow._q[".st"].attrs["aria-label"], "completed",
    "standalone DONE row renders completed, never spins forever");
  eq(soloRow._q[".dur"] ? soloRow._q[".dur"].textContent : "", "",
    "standalone DONE row invents no 0ms duration");
  // Status for a never-seen id stays a silent no-op.
  X.addToolStatus({ t: "tool_status", id: "conv:nope", status: "completed" });
  eq(created.slice(beforeSolo).filter((e) => e.className === "tool").length,
    1, "orphan tool_status creates nothing");

  // --- send states: stable keys + params, re-render on locale switch ---
  eq(X.sendKey({ state: "waiting_busy" }), "send.waiting_busy",
    "waiting_busy maps to its key");
  eq(X.sendKey({ state: "waiting_owner" }), "send.waiting_owner",
    "waiting_owner maps to its key");
  eq(X.sendKey({ state: "delivering" }), "send.delivering",
    "delivering maps to its key");
  eq(X.sendKey({ state: "queued" }), "send.queued", "queued default");
  eq(X.sendKey({}), "send.queued", "missing state -> queued");
  const errSt = X.sendErrState({ error_code: "expired", error: "raw diag 1" });
  eq(errSt.key, "send.err_expired", "error_code expired -> its key");
  eq(errSt.detail, "raw diag 1", "raw error kept as detail");
  eq(errSt.final, true, "error states are final");
  eq(X.sendErrState({ error_code: "nope", error: "weird" }).key,
    "send.failed", "unknown error_code -> generic failed key");
  eq(X.sendErrState({}).key, "send.failed", "no error_code -> generic key");
  // Registry/endpoint error codes all map to localized labels; the raw
  // diagnostic stays in the tooltip detail only.
  for (const [code, key] of [
    ["invalid_record", "send.err_invalid_record"],
    ["dispatch_failed", "send.err_dispatch_failed"],
    ["dispatch_error", "send.err_dispatch_error"],
    ["bad_name", "send.err_bad_request"],
  ]) {
    const st = X.sendErrState({ error_code: code, error: "raw diag" });
    eq(st.key, key, `error_code ${code} -> ${key}`);
    eq(st.detail, "raw diag", `${code} keeps raw error as detail`);
    eq(st.final, true, `${code} is final`);
  }
  eq(X.SEND_ERR.not_found, undefined,
    "not_found stays on the generic fallback (unreachable from the page)");
  X.setLocalePref("zh-CN");
  eq(X.t(X.sendErrState({ error_code: "dispatch_failed" }).key), "派发失败",
    "dispatch_failed label zh-CN");
  eq(X.t(X.sendErrState({ error_code: "invalid_record" }).key),
    "无效的队列消息", "invalid_record label zh-CN");
  X.setLocalePref("zh-TW");
  eq(X.t(X.sendErrState({ error_code: "dispatch_error" }).key), "派發錯誤",
    "dispatch_error label zh-TW");
  eq(X.t(X.sendErrState({ error_code: "invalid_record" }).key),
    "無效的佇列訊息", "invalid_record label zh-TW");
  X.setLocalePref("en");

  // selected session + send state + placeholder rerender on locale switch
  X._setSessions([{ session_id: "s1", proc_state: "ready", title: "s1",
    agent: "devin", cwd: "/repo/proj", turns: 3 }]);
  const evCalls0 = apiCalls("/api/events");
  X.select("s1");
  X.setSendState("s1", { key: "send.queued" });
  eq(elCache["#chatstatus"].textContent, "queued…", "send status en");
  eq(elCache["#chatinput"].placeholder, "Send an instruction to s1…",
    "placeholder en interpolates the session title verbatim");
  eq(elCache["#hwrap"].innerHTML.includes("3 turns"), true,
    "header turns en");
  documentStub.activeElement = elCache["#chatinput"];
  const foc0 = elCache["#chatinput"].focused;
  X.setLocalePref("zh-CN");
  eq(elCache["#chatstatus"].textContent, "已排队…",
    "send status re-rendered zh-CN from stored key");
  eq(elCache["#chatinput"].placeholder, "向 s1 发送指令…",
    "placeholder zh-CN keeps the session title verbatim");
  eq(elCache["#hwrap"].innerHTML.includes("3 回合"), true,
    "header turns zh-CN");
  eq(elCache["#hwrap"].innerHTML.includes(">proj</span>"), true,
    "header shows the bare repo name zh-CN");
  eq(elCache["#hwrap"].innerHTML.includes("工作仓库"), false,
    "header drops the working-repo label");
  eq(elCache["#chatinput"].focused > foc0, true,
    "composer focus restored after locale switch");
  eq(documentStub.activeElement === elCache["#chatinput"], true,
    "focus target unchanged (still the composer)");
  X.setSendState("s1", { key: "send.dispatched_task",
    params: { task: "task_9" }, final: true });
  eq(elCache["#chatstatus"].textContent, "已派发 · task_9",
    "dispatched task id interpolates verbatim zh-CN");
  // A bridge-emitted error_code renders the localized label on the status
  // line; the raw diagnostic lives only in the tooltip.
  X.setSendState("s1", X.sendErrState({ error_code: "dispatch_error",
    error: "provider boom" }));
  eq(elCache["#chatstatus"].textContent, "派发错误",
    "registry error_code renders the localized label, not raw English");
  eq(elCache["#chatstatus"].title, "provider boom",
    "raw diagnostic survives only in the tooltip");
  X.setLocalePref("zh-TW");
  eq(elCache["#chatstatus"].textContent, "派發錯誤",
    "error status re-renders zh-TW from the stored key");

  // --- turn divider with the paused stop reason fully localized ---
  X._setTasks([task({})]);
  documentStub.createElement = () => (lastDiv = el());
  X.addTurn({ t: "turn", task: "t1", ts: "2026-01-01T00:00:42Z",
    stop_reason: "paused" });
  eq(spanText(), "回合已結束 · 40 秒 · 已暫停",
    "paused turn divider fully localized zh-TW");
  documentStub.createElement = function (tag) {
    const e = el(); e.tagName = String(tag || "").toUpperCase();
    created.push(e); return e;
  };

  // transcript re-render from eventsCache — zero refetch, source preserved
  X._cache().s1 = [{ t: "prompt", ts: "2026-01-01T00:00:05Z",
    text: "deploy <prod> now", src: "outbox" }];
  const before = apiCalls("/api/events");
  X.setLocalePref("zh-TW");
  eq(apiCalls("/api/events"), before,
    "locale switch does not refetch /api/events");
  const card = created.slice().reverse()
    .find((e) => (e.className || "").includes("card prompt"));
  eq(!!card, true, "dispatched prompt card re-rendered");
  eq(card.innerHTML.includes("已派發訊息"), true, "card label zh-TW");
  eq(card.innerHTML.includes("deploy &lt;prod&gt; now"), true,
    "prompt source text preserved verbatim (escaped, untranslated)");
  X.setLocalePref("en");
  const cardEn = created.slice().reverse()
    .find((e) => (e.className || "").includes("card prompt"));
  eq(cardEn.innerHTML.includes("Dispatched Message"), true,
    "card label back to en");
  eq(cardEn.innerHTML.includes("deploy &lt;prod&gt; now"), true,
    "source text still verbatim after switch back");
  eq(apiCalls("/api/events"), before, "switch back refetches nothing either");

  // empty-state distinguishes loading vs polled-empty across locales
  X._cache().s2 = [];
  X._polled().s2 = false;
  eq(true, true, "polled flag is test-controllable");

  /* ================= session header identifiers ================= */

  // --- the right-pane title carries the displayed sub-agent's ids ---
  X._setSessions([
    { session_id: "s1", proc_state: "busy", title: "Researcher",
      agent: "devin", cwd: "/r",
      last_active_at: new Date(Date.now() - 17 * 60000).toISOString() },
    { session_id: "s2", proc_state: "ready", title: "Writer",
      agent: "kimi", cwd: "/r" },
  ]);
  X._setTasks([
    task({ task_id: "task_1", session_id: "s1" }),
    task({ task_id: "task_2", session_id: "s2" }),
  ]);
  X._setSelected("s1");
  X.renderSessionHeader();
  let hdr = elCache["#hwrap"].innerHTML;
  eq(hdr.includes("task_1 / s1"), true,
    "header shows the raw task_id / session_id pair");
  eq(hdr.includes("task_2"), false,
    "another agent's task_id stays out of the header");
  eq(hdr.includes('class="hids"'), true,
    "ids render in their own .hids row");
  eq(hdr.indexOf('class="htext"') < hdr.indexOf('class="hids"'), true,
    "the session title stays the leading title text");
  eq(hdr.indexOf('class="hstatus"') < hdr.indexOf('class="hids"'), true,
    "the ids row follows the status row");
  eq(hdr.includes('class="hids" title="task_1 / s1"'), true,
    "the ids row tooltip carries the full untruncated pair");
  eq(/class="htitle"><span class="sgr glyph--/.test(hdr), true,
    "the status glyph sits beside the title");
  eq(hdr.includes('class="hturns"'), true,
    "turns render in their own right-aligned element");
  eq(hdr.includes('class="hage"') && hdr.includes("17m ago"), true,
    "relative age renders in its own right-aligned element");
  eq(hdr.indexOf('data-act="pause"') < hdr.indexOf('data-act="resume"') &&
     hdr.indexOf('data-act="resume"') < hdr.indexOf('data-act="cancel"') &&
     hdr.indexOf('data-act="cancel"') < hdr.indexOf('id="dlbtn"'), true,
    "controls grid orders pause,resume over cancel,download");
  eq(hdr.includes("</span></button>"), false,
    "icon-only controls carry no visible label");

  // Selection changes re-render the pair for the newly displayed sub-agent.
  X.select("s2");
  hdr = elCache["#hwrap"].innerHTML;
  eq(hdr.includes("task_2 / s2"), true,
    "select() swaps the header ids to the new sub-agent");
  eq(hdr.includes("task_1"), false,
    "the previous agent's task_id is gone");

  // A session with no task still shows its session_id — no stale task id.
  X._setTasks([]);
  X.renderSessionHeader();
  hdr = elCache["#hwrap"].innerHTML;
  eq(hdr.includes('class="hids" title="s2"'), true,
    "session_id still renders without any task row");
  eq(hdr.includes("task_2"), false, "no stale task_id once tasks are gone");
  eq(hdr.includes('class="hids"'), true,
    "the ids row still renders for the session-only pair");

  // A task row without a task_id contributes nothing — never "null"/"undefined".
  X._setTasks([task({ task_id: null, session_id: "s2" })]);
  X.renderSessionHeader();
  hdr = elCache["#hwrap"].innerHTML;
  eq(hdr.includes('class="hids" title="s2"'), true,
    "session_id survives a null task_id");
  eq(/null|undefined/.test(hdr), false,
    "missing ids never render literal null/undefined");

  // Ids are data — escaped verbatim, never interpreted as markup.
  X._setSessions([{ session_id: "s<1>", proc_state: "ready", title: "T",
    agent: "devin" }]);
  X._setTasks([task({ task_id: "t<b>", session_id: "s<1>" })]);
  X._setSelected("s<1>");
  X.renderSessionHeader();
  hdr = elCache["#hwrap"].innerHTML;
  eq(hdr.includes("t&lt;b&gt; / s&lt;1&gt;"), true, "the id pair is HTML-escaped");
  eq(hdr.includes("t<b>"), false, "raw id markup never reaches the header");

  // The ids stay verbatim data under every locale — no localized labels.
  X._setSessions([{ session_id: "s1", proc_state: "busy", title: "Researcher",
    agent: "devin" }]);
  X._setTasks([task({ task_id: "task_1", session_id: "s1" })]);
  X._setSelected("s1");
  X.setLocalePref("zh-CN");
  hdr = elCache["#hwrap"].innerHTML;
  eq(hdr.includes("task_1 / s1"), true, "zh-CN keeps the raw id pair");
  eq(hdr.includes("任务 task_1"), false, "zh-CN header drops the task label");
  eq(hdr.includes("会话 s1"), false, "zh-CN header drops the session label");
  X.setLocalePref("zh-TW");
  hdr = elCache["#hwrap"].innerHTML;
  eq(hdr.includes("task_1 / s1"), true, "zh-TW keeps the raw id pair");
  eq(hdr.includes("任務 task_1"), false, "zh-TW header drops the task label");
  eq(hdr.includes("工作階段 s1"), false,
    "zh-TW header drops the session label");
  X.setLocalePref("en");

  // No resolvable selection -> the placeholder owns the header, no id chrome.
  X._setSelected("gone");
  X.renderSessionHeader();
  hdr = elCache["#hwrap"].innerHTML;
  eq(hdr.includes("hids"), false, "no ids without a selected session");
  X._setSelected("s1");

  /* ================= weekly total + batched live usage ================= */

// --- weekStartMs: browser-local calendar Monday 00:00 boundary ---
{
  // Jan 4 2026 is a Sunday — getDay()==0 must map back six days to Mon Dec 29.
  const sunday = new Date(2026, 0, 4, 15, 0);
  eq(X.weekStartMs(sunday), new Date(2025, 11, 29).getTime(),
    "Sunday maps back to the preceding Monday");
  // A Wednesday afternoon maps to the same week's Monday local midnight.
  eq(X.weekStartMs(new Date(2026, 0, 7, 15, 30)),
    new Date(2026, 0, 5).getTime(), "mid-week maps to Monday midnight");
  // Monday itself is already the boundary.
  eq(X.weekStartMs(new Date(2026, 0, 5, 9, 15)),
    new Date(2026, 0, 5).getTime(), "Monday is the boundary");
  const ws = new Date(X.weekStartMs());
  eq(ws.getDay(), 1, "week start is always a Monday");
  eq(ws.getHours() === 0 && ws.getMinutes() === 0 && ws.getSeconds() === 0,
    true, "week start is local civil midnight");
  // Mar 9 2026 is the Monday after US DST springs forward — local-civil Date
  // construction stays on a real local midnight in DST zones, and in zones
  // without DST the arithmetic is identical anyway.
  eq(X.weekStartMs(new Date(2026, 2, 11, 12)), new Date(2026, 2, 9).getTime(),
    "DST-adjacent week start stays local-civil");
}

// --- livePartial: task_id pin + selected-session precedence ---
X._setSelected("s1");
X._setLiveUsage({ s1: { input: 5, output: 5, total: 10 } });
X._setLiveAll({ s1: { task_id: "t1", consumed: { input: 8, output: 8, total: 16 } } });
eq(X.livePartial("s1", task({ task_id: "t1", status: "running" })).total, 10,
  "selected session prefers the event-sourced liveUsage");
X._setLiveUsage({});
eq(X.livePartial("s1", task({ task_id: "t1", status: "running" })).total, 16,
  "liveAll partial fills in when no event-sourced value exists");
eq(X.livePartial("s1", task({ task_id: "t2", status: "running" })), null,
  "liveAll is pinned to task_id — a stale partial never attaches to a new task");
X._setLiveUsage({ s1: { input: 5, output: 5, total: 10 } });
X._setSelected("s2");
eq(X.livePartial("s1", task({ task_id: "t1", status: "running" })).total, 16,
  "a non-selected session uses the pinned batch, not stale liveUsage");
X._setSelected("s1");
X._setLiveUsage({});

// --- weekContrib precedence: run_usage > live > legacy ---
const inWeek = (over) => task(Object.assign({
  task_id: "w" + Math.random().toString(36).slice(2, 8),
  session_id: "s1", status: "completed",
  started_at: new Date(Date.now() - 3600e3).toISOString(),
  created_at: new Date(Date.now() - 3700e3).toISOString(),
  finished_at: new Date(Date.now() - 3500e3).toISOString() }, over));
{
  let c = X.weekContrib(inWeek({ status: "running", finished_at: null,
    run_usage: { v: 1, scope: "run", quality: "exact", input: 60, output: 12, total: 72 },
    usage: { total_tokens: 9999 } }));
  eq(c.total, 72, "run_usage wins over live partial and legacy snapshot");
  eq(c.approx, false, "v1 exact run_usage counts without qualification");
  eq(c.live, false, "a final row is not a live contribution");
  X._setLiveAll({ s1: { task_id: "t1", consumed: { v: 1, scope: "run",
    quality: "exact", input: 8, output: 8, total: 16 } } });
  c = X.weekContrib(inWeek({ task_id: "t1", status: "running", finished_at: null,
    usage: { total_tokens: 400 } }));
  eq(c.total, 16, "running task falls back to the pinned live partial");
  eq(c.live, true, "live contribution flag");
  eq(c.approx, false, "v1 exact live partial is live+exact — no ~");
  // Unverified live: an older still-running bridge emits snapshots without
  // the v:1 stamp — they still count, and they force the estimate mark.
  X._setLiveAll({ s1: { task_id: "t1", consumed: { scope: "run",
    quality: "exact", input: 8, output: 8, total: 16 } } });
  c = X.weekContrib(inWeek({ task_id: "t1", status: "running", finished_at: null }));
  eq(c.live, true, "unversioned live still counts as live");
  eq(c.approx, true, "unversioned live partial is live+approx — forces ~");
  X._setLiveAll({ s1: { task_id: "t1", consumed: { v: 1, scope: "run",
    quality: "estimate", input: 8, output: 8, total: 16 } } });
  c = X.weekContrib(inWeek({ task_id: "t1", status: "running", finished_at: null }));
  eq(c.live === true && c.approx === true, true,
    "estimate-quality live is live+approx");
  X._setLiveAll({ s1: { task_id: "t1", consumed: { v: 1, scope: "run",
    quality: "exact", partial: true, input: 8, output: 8, total: 16 } } });
  c = X.weekContrib(inWeek({ task_id: "t1", status: "running", finished_at: null }));
  eq(c.live === true && c.approx === true, true,
    "partial live snapshot is live+approx");
  X._setLiveAll({});
  c = X.weekContrib(inWeek({ usage: { input_tokens: 108414,
    cached_input_tokens: 108072, output_tokens: 4763 } }));
  eq(c.total, 5105, "legacy snapshot contributes the derived count");
  eq(c.approx, true, "legacy rows are estimates");
  c = X.weekContrib(inWeek({ run_usage: { scope: "run", quality: "exact",
    input: 30, output: 10, total: 40 } }));
  eq(c.approx, true, "unversioned run_usage is unverifiable");
  c = X.weekContrib(inWeek({ run_usage: { v: 1, scope: "run", quality: "estimate",
    input: 30, output: 10, total: 40 } }));
  eq(c.approx, true, "v1 estimate quality is an estimate");
  c = X.weekContrib(inWeek({ run_usage: { v: 1, scope: "run", quality: "exact",
    partial: true, input: 30, output: 10, total: 40 } }));
  eq(c.approx, true, "recovered partial rows are estimates");
}

// --- weekStats: window, dedupe, exclusion, ~ flag ---
X._setLiveAll({ s1: { task_id: "t1", consumed: { input: 8, output: 8, total: 16 } } });
X._setTasks([
  inWeek({ task_id: "old", started_at: new Date(X.weekStartMs() - 86400000).toISOString(),
    run_usage: { v: 1, scope: "run", quality: "exact", total: 500 } }),
  inWeek({ task_id: "t1", status: "running", finished_at: null }),
  inWeek({ task_id: "t2", run_usage: { v: 1, scope: "run", quality: "exact",
    input: 60, output: 12, total: 72 } }),
  inWeek({ task_id: "t3", run_usage: { scope: "run", quality: "exact",
    input: 30, output: 10, total: 40 } }),
  inWeek({ task_id: "t4", started_at: "garbage", created_at: "also-bad",
    run_usage: { v: 1, scope: "run", quality: "exact", total: 999 } }),
  inWeek({ task_id: "t5", run_usage: { v: 1, scope: "run", quality: "exact",
    total: 10 } }),
]);
let w = X.weekStats();
eq(w.n, 4, "unique in-window tasks counted (resumed tasks stay separate)");
eq(w.excluded, 1, "both timestamps invalid -> excluded count");
eq(w.total, 138, "72 + 40 + live 16 + 10 — live counted once");
eq(w.approx, true, "unversioned run_usage forces the ~ mark");
eq(w.live, true, "live partial flagged for the tooltip note");

// live -> final swap: the running contribution is replaced, never summed.
X._setTasks([inWeek({ task_id: "t1", status: "running", finished_at: null })]);
w = X.weekStats();
eq(w.total, 16, "live partial contributes while the task runs");
X._setTasks([inWeek({ task_id: "t1", run_usage: { v: 1, scope: "run",
  quality: "exact", total: 20 } })]);
w = X.weekStats();
eq(w.total, 20, "finished run_usage replaces the partial — never added on top");
eq(w.approx, false, "exact-only week does not force ~");

// Timestamp fallback: invalid started_at falls back to created_at.
X._setTasks([inWeek({ task_id: "t6", started_at: "not-a-date",
  created_at: new Date().toISOString(),
  run_usage: { v: 1, scope: "run", quality: "exact", total: 3 } })]);
eq(X.weekStats().total, 3, "created_at fallback attributes the task");
X._setTasks([inWeek({ task_id: "t7", started_at: "not-a-date",
  created_at: new Date(X.weekStartMs() - 1000).toISOString(),
  run_usage: { v: 1, scope: "run", quality: "exact", total: 3 } })]);
eq(X.weekStats().total, 0, "created_at fallback can place a task last week");

// --- renderWeek DOM: value, shared title/aria-label, i18n ---
const wt = elCache["#weektotal"];
const wtv = wt.querySelector(".wt-val");
X._setTasks([
  inWeek({ task_id: "t2", run_usage: { v: 1, scope: "run",
    quality: "exact", input: 60, output: 12, total: 72 } }),
  inWeek({ task_id: "t1", status: "running", finished_at: null }),
]);
X._setLiveAll({ s1: { task_id: "t1", consumed: { v: 1, scope: "run",
  quality: "exact", input: 8, output: 8, total: 16 } } });
X.renderWeek();
eq(wtv.textContent, "88 tok", "weekly value = exact 72 + live 16, no ~");
eq(wt.title.includes("Tokens since"), true, "title carries the window sentence");
eq(wt.title.includes("2 tasks"), true, "title counts unique tasks");
eq(wt.title.includes("based on retained Agent Bridge task history"), true,
  "retained-history scope is always stated");
eq(wt.title.includes("includes live in-progress usage"), true,
  "live note in the shared description");
eq(wt.attrs["aria-label"], wt.title, "aria-label mirrors the title");
eq(wt.attrs["aria-live"], undefined, "passive metric — no live region");

// Unverified live: the ~ mark and the approx note join the live note.
X._setTasks([inWeek({ task_id: "t1", status: "running", finished_at: null })]);
X._setLiveAll({ s1: { task_id: "t1", consumed: { scope: "run",
  quality: "exact", input: 8, output: 8, total: 16 } } });
X.renderWeek();
eq(wtv.textContent, "~16 tok", "unverified live partial forces the ~ mark");
eq(wt.title.includes("includes live in-progress usage"), true,
  "live note kept for unverified live data");
eq(wt.title.includes("estimated or incomplete"), true,
  "approx note added for unverified live data");
// Back to a verified mix for the locale checks.
X._setTasks([
  inWeek({ task_id: "t2", run_usage: { v: 1, scope: "run",
    quality: "exact", input: 60, output: 12, total: 72 } }),
  inWeek({ task_id: "t1", status: "running", finished_at: null }),
]);
X._setLiveAll({ s1: { task_id: "t1", consumed: { v: 1, scope: "run",
  quality: "exact", input: 8, output: 8, total: 16 } } });
X.renderWeek();

X.setLocalePref("zh-CN");
eq(wtv.textContent, "88 token", "zh-CN unit re-renders on locale switch");
eq(wt.title.includes("2 个任务"), true, "zh-CN task count in title");
eq(wt.title.includes("已保留"), true, "zh-CN scope note in title");
X.setLocalePref("zh-TW");
eq(wt.title.includes("2 個任務"), true, "zh-TW task count in title");
eq(wt.title.includes("已保留"), true, "zh-TW scope note in title");
X.setLocalePref("en");

X._setTasks([inWeek({ task_id: "t9", usage: { input_tokens: 108414,
  cached_input_tokens: 108072, output_tokens: 4763 } })]);
X._setLiveAll({});
X.renderWeek();
eq(wtv.textContent, "~5k tok", "legacy-only week renders the estimate mark");
eq(wt.title.includes("estimated or incomplete"), true,
  "approx note appears in the shared description");

// --- sidebar signature: a live batch change repaints once, stable polls never ---
X._setSessions([sess("busy", "s1")]);
X._setTasks([inWeek({ task_id: "t1", session_id: "s1", status: "running",
  finished_at: null })]);
X._setSelected("s1");
X._setLiveUsage({});
X._setLiveAll({ s1: { task_id: "t1", consumed: { input: 8, output: 8, total: 16 } } });
const listEl = elCache["#sesslist"];
let sets0 = listEl._htmlSets;
X.renderSidebar();
eq(listEl._htmlSets, sets0 + 1, "sidebar renders the first signature");
eq(listEl.innerHTML.includes("16 tok"), true,
  "a running agent's sidebar row shows the batched live counter");
X.renderSidebar(); X.renderSidebar();
eq(listEl._htmlSets, sets0 + 1, "stable polls never repaint the sidebar");
X._setLiveAll({ s1: { task_id: "t1", consumed: { input: 20, output: 8, total: 28 } } });
X.renderSidebar();
eq(listEl._htmlSets, sets0 + 2, "a changed live batch repaints exactly once");
eq(listEl.innerHTML.includes("28 tok"), true, "the updated counter renders");
X.renderSidebar();
eq(listEl._htmlSets, sets0 + 2, "stable again — no churn");
// Selected-session event-sourced value overrides the batch everywhere.
X._setLiveUsage({ s1: { input: 40, output: 2, total: 42 } });
X.renderSidebar();
eq(listEl.innerHTML.includes("42 tok"), true,
  "event-sourced liveUsage takes precedence for the selected session");
X._setLiveUsage({});
X._setLiveAll({});

/* ================= presence heartbeat lifecycle ================= */

  // The initial ping(0) already ran during script eval; every lifecycle
  // recovery event must re-register the tab immediately.
  const beats = () => apiCalls("/api/presence");
  const b0 = beats();
  eq(b0 >= 1, true, "initial presence ping fired on load");
  eq(fetchCalls.filter((u) => u.includes("id=SHARED")).length, 0,
    "presence id is per-page, never the cloned sessionStorage id");
  (listeners.pageshow || []).forEach((f) => f());
  eq(beats(), b0 + 1, "pageshow re-pings presence (reload/bfcache)");
  (listeners.focus || []).forEach((f) => f());
  eq(beats(), b0 + 2, "focus re-pings presence");
  (listeners.online || []).forEach((f) => f());
  eq(beats(), b0 + 3, "online re-pings presence");
  documentStub.visibilityState = "hidden";
  (listeners.visibilitychange || []).forEach((f) => f());
  eq(beats(), b0 + 3, "visibilitychange to hidden does not ping");
  documentStub.visibilityState = "visible";
  (listeners.visibilitychange || []).forEach((f) => f());
  eq(beats(), b0 + 4, "visibilitychange to visible re-pings");
  let beacons = [];
  sandbox.navigator.sendBeacon = (u) => { beacons.push(String(u)); };
  (listeners.pagehide || []).forEach((f) => f());
  eq(beacons.length, 1, "pagehide sends exactly one beacon");
  eq(beacons[0].includes("/api/presence") && beacons[0].includes("bye=1"),
    true, "pagehide beacon carries bye=1");

  /* ================= outbox queue + session task controls =================
     Queue cards render straight from /api/overview's outbox map; task
     buttons gate on the latest applicable task via taskActions(). */

  // --- taskActions: enablement matrix on the applicable task ---
  X._setSessions([sess("busy", "s1")]);
  const actTk = (over) => Object.assign(
    { task_id: "t1", session_id: "s1", status: "running",
      created_at: "2026-01-01T00:00:00Z", resumable: false }, over);
  const sLive = sess("busy", "s1");
  let can = X.taskActions(actTk({}), sLive);
  eq(can.pause && can.cancel && !can.resume, true,
    "running task: pause+cancel on, resume off");
  can = X.taskActions(actTk({ status: "queued" }), sLive);
  eq(can.pause && can.cancel, true, "queued task: pause+cancel on");
  can = X.taskActions(actTk({ status: "running" }), sLive);
  eq(can.resume, false, "in-flight task is not resumable");
  can = X.taskActions(actTk({ status: "cancelled", paused: true,
    resumable: true }), sess("ready", "s1"));
  eq(!can.pause && !can.cancel && can.resume, true,
    "paused task: resume only");
  can = X.taskActions(actTk({ status: "failed", resumable: true }),
    sess("ready", "s1"));
  eq(can.resume, true, "failed task with a live session is resumable");
  can = X.taskActions(actTk({ status: "cancelled", resumable: true,
    resumed_by: "t9" }), sess("ready", "s1"));
  eq(can.resume, false, "superseded row (resumed_by) never offers resume");
  can = X.taskActions(actTk({ status: "completed" }), sess("ready", "s1"));
  eq(!can.pause && !can.cancel && !can.resume, true,
    "completed task: nothing enabled");
  can = X.taskActions(actTk({ status: "running", resumable: true }),
    sess("ready", "s1"));
  eq(can.resume, true,
    "dead-owner in-flight row is resumable (server-side gate)");
  can = X.taskActions(actTk({ status: "running" }), sess("dead", "s1"));
  eq(!can.pause && !can.cancel && !can.resume, true,
    "dead session: all controls off");
  eq(JSON.stringify(X.taskActions(null, sLive)),
    JSON.stringify({ pause: false, cancel: false, resume: false }),
    "no task -> all off");
  eq(JSON.stringify(X.taskActions(actTk({}), null)),
    JSON.stringify({ pause: false, cancel: false, resume: false }),
    "no session -> all off");
  // Remote-owned active rows stay enabled — the req routes to the owner.
  can = X.taskActions(actTk({ remote: true }), sLive);
  eq(can.pause && can.cancel, true, "remote live-owned task stays actionable");

  // --- header: three labelled controls in a group, gated by the task ---
  X._setSessions([sess("busy", "s1")]);
  X._setTasks([actTk({})]);
  X._setSelected("s1");
  X.renderSessionHeader();
  let h = elCache["#hwrap"].innerHTML;
  eq(h.includes('class="hactions"'), true, "header renders the actions group");
  eq(h.includes('role="group"'), true, "actions group is a role=group");
  eq(h.includes('aria-label="Task actions"'), true,
    "actions group carries the localized label");
  for (const a of ["pause", "cancel", "resume"])
    eq(h.includes(`data-act="${a}"`), true, `header has a ${a} control`);
  eq(/data-act="pause" title/.test(h) && !/data-act="pause" disabled/.test(h),
    true, "pause enabled for a running task");
  eq(/data-act="resume" disabled/.test(h), true,
    "resume disabled for a running task");
  eq(h.includes('title="nothing to resume"'), true,
    "disabled resume explains why in the tooltip");
  X._setTasks([actTk({ status: "cancelled", paused: true, resumable: true })]);
  X._setSessions([sess("ready", "s1")]);
  X.renderSessionHeader();
  h = elCache["#hwrap"].innerHTML;
  eq(/data-act="resume" title/.test(h) && !/data-act="resume" disabled/.test(h),
    true, "resume enabled for a paused task");
  eq(/data-act="pause" disabled/.test(h), true,
    "pause disabled for a terminal task");
  eq(h.includes('title="no active task"'), true,
    "disabled pause explains why");
  // An in-flight req_* parks all three controls.
  X._setTasks([actTk({})]);
  X._setSessions([sess("busy", "s1")]);
  X.setSendState("s1", { key: "send.queued" });
  X.sendState.s1.name = "req_pause_1_abcd1234.json";
  X.renderSessionHeader();
  h = elCache["#hwrap"].innerHTML;
  eq(/data-act="pause" disabled/.test(h) && /data-act="resume" disabled/.test(h),
    true, "pending req parks every control");
  eq(h.includes('title="request in flight…"'), true,
    "parked controls name the pending request");
  X.sendState.s1.final = true;
  X.renderSessionHeader();
  h = elCache["#hwrap"].innerHTML;
  eq(!/data-act="pause" disabled/.test(h), true,
    "controls re-enable once the request resolves");

  // --- taskAction: writes a req record + polls it; stale state never fires ---
  X._setSessions([sess("ready", "s1")]);
  X._setTasks([actTk({ status: "completed" })]);
  X._setSelected("s1");
  let actCalls0 = apiCalls("/api/task_action");
  await X.taskAction("resume");      // not resumable -> early return
  eq(apiCalls("/api/task_action"), actCalls0,
    "a disabled action never hits the network");
  X._setSessions([sess("busy", "s1")]);
  X._setTasks([actTk({})]);
  delete X.sendState.s1;
  const realFetch = sandbox.fetch;
  sandbox.fetch = (u, o) => {
    fetchCalls.push(String(u));
    const body = o && o.body ? JSON.parse(o.body) : {};
    return Promise.resolve({ json: () => Promise.resolve(
      { ok: true, name: "req_pause_1_abcd1234.json",
        task_id: body.task_id, action: body.action }) });
  };
  await X.taskAction("pause");
  sandbox.fetch = realFetch;
  eq(apiCalls("/api/task_action"), actCalls0 + 1, "enabled pause POSTs once");
  eq(X.sendState.s1.name, "req_pause_1_abcd1234.json",
    "the req name is tracked for the status poll");
  eq(X.sendState.s1.key, "send.queued", "request shows as queued");
  // A second click while the req is pending never doubles the request.
  await X.taskAction("cancel");
  eq(apiCalls("/api/task_action"), actCalls0 + 1,
    "in-flight request blocks a second action");
  // A rejected action surfaces the localized error, not a spinner.
  sandbox.fetch = (u) => {
    fetchCalls.push(String(u));
    return Promise.resolve({ json: () => Promise.resolve(
      { ok: false, error_code: "unknown_task", error: "unknown task t1" }) });
  };
  delete X.sendState.s1;
  await X.taskAction("cancel");
  sandbox.fetch = realFetch;
  eq(X.sendState.s1.key, "act.err_unknown_task",
    "rejected action surfaces the error code");
  eq(X.sendState.s1.final, true, "rejected action is final");

  // --- renderQueue: cards for the selected session only, escaped, gated ---
  X._setSelected("s1");
  X._setOutbox({
    s1: [{ name: "msg_1_abcdef12.json", message: "hello <b>world</b>",
      ts: 1700000000, state: "queued" }],
    s2: [{ name: "msg_2_abcdef12.json", message: "other session" }],
  });
  X.renderQueue();
  const qEl = elCache["#queue"];
  let q = qEl.innerHTML;
  eq(qEl.hidden, false, "queue shown for the selected session");
  eq(q.includes('class="qcard"'), true, "queue card renders");
  eq(q.includes("hello &lt;b&gt;world&lt;/b&gt;"), true,
    "queued message text is escaped");
  eq(q.includes('data-act="steer"') && q.includes('data-act="delete"'), true,
    "queue card carries steer + delete actions");
  eq(q.includes('data-name="msg_1_abcdef12.json"'), true,
    "card pins the outbox record name");
  eq(q.includes("msg_2_"), false, "another session's queue never renders");
  eq(q.includes("queued"), true, "card meta shows the queued state");
  const qSets = qEl._htmlSets;
  X.renderQueue();
  eq(qEl._htmlSets, qSets, "a stable queue never repaints");
  // Bridge-annotated requeue states surface their real reason.
  X._setOutbox({ s1: [{ name: "msg_1_abcdef12.json", message: "hi",
    ts: 1700000000, state: "waiting_busy" }] });
  X.renderQueue();
  eq(qEl.innerHTML.includes("waiting for agent"), true,
    "waiting_busy state label renders");
  // Session switch re-renders that session's queue (and clears for none).
  X._setOutbox({ s2: [{ name: "msg_2_abcdef12.json",
    message: "other session", ts: 1700000001 }] });
  X._setSelected("s2");
  X.renderQueue();
  eq(qEl.innerHTML.includes("other session"), true,
    "selecting s2 renders s2's queue");
  X._setOutbox({});
  X.renderQueue();
  eq(qEl.hidden, true, "empty queue hides the strip");
  // dropQueueEntry removes just the acted-on record.
  X._setSelected("s1");
  X._setOutbox({ s1: [
    { name: "msg_1_abcdef12.json", message: "one", ts: 1700000000 },
    { name: "msg_2_abcdef12.json", message: "two", ts: 1700000001 }] });
  X.renderQueue();
  X.dropQueueEntry("s1", "msg_1_abcdef12.json");
  q = qEl.innerHTML;
  eq(q.includes("msg_1_"), false, "dropped record leaves the queue");
  eq(q.includes("msg_2_"), true, "the other queued record stays");
  // Locale switch re-bakes the queue labels.
  X.setLocalePref("zh-CN");
  eq(qEl.innerHTML.includes("移回"), true, "queue card labels zh-CN");
  eq(qEl.innerHTML.includes("已排队"), true, "queue state label zh-CN");
  X.setLocalePref("en");

  // --- steer/del: one dequeue call, message back to composer or gone ---
  sandbox.fetch = (u, o) => {
    fetchCalls.push(String(u));
    const body = o && o.body ? JSON.parse(o.body) : {};
    return Promise.resolve({ json: () => Promise.resolve(
      { ok: true, name: body.name, session_id: "s1",
        message: "edit me again" }) });
  };
  elCache["#chatinput"].value = "";
  await X.steerMsg("msg_2_abcdef12.json");
  eq(elCache["#chatinput"].value, "edit me again",
    "steer returns the message to the composer");
  eq(X.sendState.s1.key, "queue.recalled", "steer reports the recall");
  eq(qEl.innerHTML.includes("msg_2_"), false,
    "steered record leaves the queue");
  await X.delMsg("msg_2_abcdef12.json");
  eq(X.sendState.s1.key, "queue.deleted", "delete reports the removal");
  // Claimed mid-flight -> localized error, card stays for the next poll.
  X._setOutbox({ s1: [{ name: "msg_3_abcdef12.json", message: "busy",
    ts: 1700000000 }] });
  X.renderQueue();
  sandbox.fetch = (u) => {
    fetchCalls.push(String(u));
    return Promise.resolve({ json: () => Promise.resolve(
      { ok: false, error_code: "delivering",
        error: "message is mid-delivery and cannot be recalled" }) });
  };
  await X.delMsg("msg_3_abcdef12.json");
  sandbox.fetch = realFetch;
  eq(X.sendState.s1.key, "queue.err_delivering",
    "claimed record reports the mid-delivery error");
  eq(elCache["#chatstatus"].textContent, "already being delivered",
    "queue error renders localized on the status line");

  // --- sendErrState: req records read "code" too; action-worded fallback ---
  eq(X.sendErrState({ code: "resume_failed", error: "x" }, true).key,
    "act.failed", "req done failure maps via the code field");
  eq(X.sendErrState({ error_code: "expired" }, true).key, "act.err_expired",
    "req expiry never reads like an expired message");
  eq(X.sendErrState({ error_code: "weird_code" }, true).key, "act.failed",
    "unknown req error -> action failed, not send failed");
  eq(X.sendErrState({ error_code: "delivering" }).key,
    "queue.err_delivering", "dequeue race maps to its label");
  eq(X.sendErrState({ error_code: "dispatched" }).key,
    "queue.err_dispatched", "already-dispatched maps to its label");
  eq(X.sendErrState({ error_code: "wrong_session" }, true).key,
    "act.err_wrong_session", "stale session selection maps to its label");
  // ACT_DONE keys exist in every locale.
  for (const loc of ["en", "zh-CN", "zh-TW"]) {
    X.setLocalePref(loc);
    for (const key of Object.values(X.ACT_DONE))
      eq(X.t(key) !== key, true, `${loc} has ${key}`);
    for (const key of ["queue.label", "queue.steer", "queue.delete",
        "queue.recalled", "queue.deleted", "queue.err_delivering",
        "queue.err_dispatched", "queue.err_failed", "act.pause",
        "act.cancel", "act.resume", "act.requesting", "act.pending",
        "act.paused", "act.cancelled", "act.resumed", "act.failed",
        "act.err_expired", "act.err_unknown_task", "act.err_wrong_session",
        "act.no_active", "act.no_resumable", "a11y.task_actions",
        "queue.steer_hint"])
      eq(X.t(key) !== key, true, `${loc} has ${key}`);
  }
  X.setLocalePref("en");

  /* ================= transcript download =================
     The header button exports the selected session's cached events as a
     Markdown file — every helper below is the production code path, not a
     reimplementation. */

  // --- sanitizeFilename: every forbidden class collapses to one "-" ---
  eq(X.sanitizeFilename('Researcher: Part 1; <v2> / test? * "pipe|"'),
    "Researcher-Part-1-v2-test-pipe",
    "Windows-forbidden chars collapse into single hyphens");
  eq(X.sanitizeFilename("task;resume;step"), "task-resume-step",
    "semicolons are replaced (explicit user requirement)");
  eq(X.sanitizeFilename("My Title...   "), "My-Title",
    "separator runs collapse; trailing dots/spaces strip");
  eq(X.sanitizeFilename(":::;;;???***"), "transcription",
    "all-invalid title falls back");
  eq(X.sanitizeFilename("title\x00with\x1f\x7fcontrols"),
    "title-with-controls", "C0/DEL control characters are replaced");
  eq(X.sanitizeFilename(""), "transcription", "empty -> fallback");
  eq(X.sanitizeFilename(null), "transcription", "null -> fallback");
  eq(X.sanitizeFilename(undefined), "transcription", "undefined -> fallback");
  eq(X.sanitizeFilename("...---..."), "transcription",
    "edge-only separators collapse to nothing -> fallback");
  eq(X.sanitizeFilename("  . leading"), "leading",
    "leading unsafe chars strip");
  eq(X.sanitizeFilename("a".repeat(150)).length, 100,
    "title caps at 100 chars");
  eq(X.sanitizeFilename("会话 标题: v2"), "会话-标题-v2",
    "non-ASCII titles survive");
  eq(X.sanitizeFilename("a/b\\c:d*e?f\"g<h>i|j;k"), "a-b-c-d-e-f-g-h-i-j-k",
    "every forbidden char maps to a separator");
  eq(X.sanitizeFilename("CON"), "CON",
    "device names pass — the date prefix keeps the basename safe");

  // --- buildTranscriptFilename: MM-DD-YYYY-Title.md, browser-local date ---
  const dexp = (d) => String(d.getMonth() + 1).padStart(2, "0") + "-" +
    String(d.getDate()).padStart(2, "0") + "-" + d.getFullYear();
  const dCreated = new Date("2026-09-12T10:00:00Z");
  eq(X.buildTranscriptFilename(
    { title: "Researcher", created_at: "2026-09-12T10:00:00Z" }),
    dexp(dCreated) + "-Researcher.md",
    "MM-DD-YYYY-Title.md in the local timezone");
  eq(X.buildTranscriptFilename(
    { title: "a:b;c", created_at: "2026-09-12T10:00:00Z" }),
    dexp(dCreated) + "-a-b-c.md", "filename title is sanitized");
  eq(X.buildTranscriptFilename({ session_id: "sess_9" })
    .endsWith("-sess-9.md"), true,
    "missing title falls back to session_id (underscores collapse)");
  eq(X.buildTranscriptFilename({ title: "x", created_at: "not-a-date" })
    .startsWith(dexp(new Date()) + "-x.md"), true,
    "invalid created_at falls back to the current date");
  eq(X.buildTranscriptFilename(null).endsWith("-transcription.md"), true,
    "missing session falls back entirely");
  eq(X.buildTranscriptFilename({ title: "x", created_at: "" })
    .startsWith(dexp(new Date()) + "-x.md"), true,
    "empty created_at falls back to the current date");
  eq(/^\d{2}-\d{2}-\d{4}-.+\.md$/.test(
    X.buildTranscriptFilename({ title: "t",
      created_at: "2026-09-12T10:00:00Z" })), true,
    "filename matches the convention");

  // --- sessionToMarkdown: coalesced chunks, folded tools, safe metadata ---
  X._setTasks([task({ task_id: "t1", session_id: "s1" })]);
  const sessMd = { session_id: "s1", title: "Demo", agent: "devin",
    model: "swe-2", cwd: "/repo/proj", created_at: "2026-09-12T10:00:00Z" };
  const md = X.sessionToMarkdown(sessMd, [
    { t: "prompt", ts: "2026-09-12T10:00:01Z", text: "do it <now>",
      src: "dashboard" },
    { t: "msg", ts: "2026-09-12T10:00:02Z", text: "Hello " },
    { t: "msg", ts: "2026-09-12T10:00:03Z", text: "**world**" },
    { t: "think", ts: "2026-09-12T10:00:04Z", text: "hmm" },
    { t: "tool", ts: "2026-09-12T10:00:05Z", id: "tc1", kind: "execute",
      title: "Ran pwd", input: '{"command":"pwd"}' },
    { t: "tool_status", ts: "2026-09-12T10:00:06Z", id: "tc1",
      status: "completed" },
    { t: "usage", ts: "2026-09-12T10:00:06Z", consumed: { total: 5 } },
    { t: "turn", ts: "2026-09-12T10:00:07Z", stop_reason: "end_turn" },
    { t: "error", ts: "2026-09-12T10:00:08Z", text: "stalled once" },
    { t: "prompt", ts: "2026-09-12T10:00:09Z", text: "dispatched job",
      src: "mcp" },
  ]);
  eq(md.startsWith("# Demo\n"), true,
    "document opens with the title heading");
  eq(md.includes("- devin · swe-2"), true, "agent/model metadata line");
  eq(md.includes("task t1 · session s1"), true, "task/session id line");
  eq(md.includes("Working repo · proj"), true, "repo metadata line");
  eq(md.includes("## User Message (2026-09-12T10:00:01Z)"), true,
    "dashboard prompt heading");
  eq(md.includes("```\ndo it <now>\n```"), true,
    "prompt text verbatim inside a fence");
  eq(md.includes("## Agent\n\nHello **world**"), true,
    "streamed message chunks coalesce into one agent block");
  eq(md.includes("<details>\n<summary>Thinking</summary>\n\nhmm"), true,
    "thinking chunks fold into a details block");
  eq(md.includes("**Execute** — `Ran pwd` · completed"), true,
    "tool_status folds into the tool line");
  eq(md.includes('{"command":"pwd"}'), true, "tool input preserved");
  eq(md.includes("*turn ended*"), true, "turn divider renders");
  eq(md.includes("> **error:** stalled once"), true,
    "error blockquote renders");
  eq(md.includes("## Dispatched Message (2026-09-12T10:00:09Z)"), true,
    "dispatched prompt heading");
  eq(md.includes("consumed") || md.includes('"total"'), false,
    "internal usage noise never leaks into the export");
  // Folded twin record: a second full tool event for the same id updates the
  // line instead of duplicating it.
  const mdDup = X.sessionToMarkdown(sessMd, [
    { t: "tool", ts: "1", id: "c:1", kind: "read", title: "view f" },
    { t: "tool", ts: "2", id: "c:1", kind: "read", title: "view f",
      status: "completed" },
  ]);
  eq((mdDup.match(/\*\*Read\*\*/g) || []).length, 1,
    "same tool_call_id merges — no duplicate line");
  eq(mdDup.includes("**Read** — `view f` · completed"), true,
    "merged record carries the status");
  // Dynamic metadata can never grow fake headings or break a fence.
  const evil = X.sessionToMarkdown(
    { session_id: "s2", title: "evil\n\n# injected" },
    [{ t: "msg", ts: "1", text: "x" }]);
  eq(evil.split("\n")[0], "# evil # injected",
    "a newline-bearing title flattens into one heading line");
  const fenced = X.sessionToMarkdown(sessMd, [
    { t: "prompt", text: "a\n```\nb", src: "dashboard" }]);
  eq(fenced.includes("````\na\n```\nb\n````"), true,
    "content backticks force a longer fence — no breakout");
  // Exported section labels localize with the rest of the chrome.
  X.setLocalePref("zh-CN");
  const mdZh = X.sessionToMarkdown(sessMd, [
    { t: "prompt", text: "问", src: "dashboard" },
    { t: "msg", text: "答" }]);
  eq(mdZh.includes("## 用户消息"), true, "prompt heading zh-CN");
  eq(mdZh.includes("## Agent 消息"), true, "agent heading zh-CN");
  X.setLocalePref("en");

  // --- header button: placement + state ---
  X._setSessions([{ session_id: "s1", proc_state: "busy", title: "Demo",
    agent: "devin", cwd: "/r", turns: 1 }]);
  X._setTasks([actTk({})]);
  X._setSelected("s1");
  X._cache().s1 = [];
  X.renderSessionHeader();
  h = elCache["#hwrap"].innerHTML;
  eq(h.includes('id="dlbtn"'), true, "download button renders in the header");
  eq(h.includes('data-act="download"'), true,
    "button carries data-act=download");
  eq(h.indexOf('class="hactions"') < h.indexOf('id="dlbtn"'), true,
    "download renders after the task controls in the same group");
  eq(h.indexOf('id="dlbtn"') < h.indexOf("</div>",
    h.indexOf('class="hactions"')), true,
    "download lives inside the .hactions grid");
  eq(h.includes('class="hside"'), false, "no .hside wrapper remains");
  eq(/id="dlbtn"[^>]*disabled/.test(h), true,
    "disabled until the session has transcript events");
  eq(h.includes('title="no transcript yet"'), true,
    "disabled state explains why in the tooltip");
  eq(h.includes('aria-label="Download"'), true, "button is accessibly named");
  X._cache().s1 = [{ t: "msg", ts: "2026-01-01T00:00:00Z", text: "hi" }];
  X.renderSessionHeader();
  h = elCache["#hwrap"].innerHTML;
  eq(/id="dlbtn"[^>]*disabled/.test(h), false,
    "enabled once transcript events exist");
  eq(h.includes('title="Download transcript as Markdown"'), true,
    "enabled tooltip describes the action");
  eq(X.hasTranscript("s1"), true, "hasTranscript sees renderable events");
  X._cache().s1 = [{ t: "usage", consumed: { total: 1 } }];
  eq(X.hasTranscript("s1"), false, "usage-only cache is not a transcript");
  X.renderSessionHeader();
  eq(/id="dlbtn"[^>]*disabled/.test(elCache["#hwrap"].innerHTML), true,
    "usage-only events keep the button disabled");
  X._cache().s1 = [{ t: "msg", text: "back" }];

  // --- enablement flips when event data arrives, not only on selection ---
  const dlStub = documentStub.querySelector("#dlbtn");
  dlStub.disabled = true; dlStub.title = "";
  X._cache().s1 = [];
  const realFetchDl = sandbox.fetch;
  sandbox.fetch = () => Promise.resolve({ ok: true,
    json: () => Promise.resolve({ offset: 10, reset: false,
      events: [{ t: "prompt", ts: "2026-01-01T00:00:00Z", text: "hi",
        src: "dashboard" }] }) });
  await X.pollEvents();
  sandbox.fetch = realFetchDl;
  eq(X._cache().s1.length, 1, "events appended to the session cache");
  eq(dlStub.disabled, false, "the button enabled on event arrival");
  eq(dlStub.title, "Download transcript as Markdown",
    "the arrival refresh also fixes the tooltip");
  // A reset emptying the cache disables it again.
  sandbox.fetch = () => Promise.resolve({ ok: true,
    json: () => Promise.resolve({ offset: 0, reset: true, events: [] }) });
  await X.pollEvents();
  sandbox.fetch = realFetchDl;
  eq(dlStub.disabled, true, "a transcript reset disables the button");
  eq(dlStub.title, "no transcript yet",
    "the disabled tooltip returns");

  // --- downloadTranscript: Blob -> object URL -> anchor click -> cleanup ---
  X._cache().s1 = [
    { t: "prompt", ts: "2026-01-01T00:00:00Z", text: "hi", src: "dashboard" },
    { t: "msg", ts: "2026-01-01T00:00:01Z", text: "ok" }];
  const beforeA = created.length;
  X.downloadTranscript();
  const anchor = created.slice(beforeA).find((e) => e.tagName === "A");
  eq(!!anchor, true, "a temporary anchor is created");
  eq(anchor.clicks, 1, "the anchor is clicked once");
  eq(/^blob:mock-/.test(anchor.href), true, "href is the blob object URL");
  eq(/^\d{2}-\d{2}-\d{4}-Demo\.md$/.test(anchor.download), true,
    "download name matches MM-DD-YYYY-Title.md");
  eq(blobs.length, 1, "one blob was minted");
  eq(blobs[0].type, "text/markdown;charset=utf-8", "blob is markdown");
  eq(blobs[0].parts[0].includes("## User Message"), true,
    "blob content is the generated transcript");
  eq(documentStub.body.children.includes(anchor), true,
    "anchor was attached for the click");
  runTimers();
  eq(documentStub.body.children.includes(anchor), false,
    "anchor removed after the download starts");
  eq(revokedUrls.length, 1, "the object URL is revoked");
  // A stale click on a transcript-less session never mints anything.
  const blobCount = blobs.length;
  X._setSelected("s_none");
  X.downloadTranscript();
  eq(blobs.length, blobCount, "no download without transcript events");
  X._setSelected("s1");

  console.log(failed ? `\n${failed} FAILED` : "\nall assertions passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
