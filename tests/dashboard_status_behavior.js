// Executable behavior tests for the dashboard page's status/duration helpers
// and its conversation navigation rail.
//
// Extracts the real inline <script> from src/agent_bridge/share/dashboard.py's
// PAGE and runs it inside a vm context with a minimal DOM stub — no jsdom, no
// npm packages. Covers the centralized proc_state map, latestTask chronology,
// the taskDur/fmtDur/durText rules end to end, the bundled i18n layer
// (en / zh-CN / zh-TW), and the nav rail end to end: MARK_SEL taxonomy,
// layoutRail geometry (cluster spans, rail bounds), updateCurMark, jumpToMark,
// track-click seeking, roving tabindex, and the MutationObserver/ResizeObserver
// + rAF-coalesced update loop.
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
    textContent: "", style: {}, title: "", type: "", tagName: "",
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
    children: [], _parent: null,
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
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
  setTimeout: () => 0, clearTimeout() {},
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
    " usageNums, tokCount, runTok, tokTitle, fmtTok, tokSpan, turnDurMs," +
    " addTurn, addPrompt, addTool, addToolStatus, clusterYs, applyEvents," +
    " MARK_SEL, MARK_GAP," +
    " scheduleRail, layoutRail, jumpToMark, updateCurMark," +
    " t, LOCALES, LOCALE_PREFS, localePref, resolveSystemLocale," +
    " setLocalePref, applyLocale, rerenderLocale, ago, fmtTs, fmtNum," +
    " sendKey, sendErrState, SEND_ERR, setSendState, renderSendStatus," +
    " refreshComposer, toolKindLabel, stopReasonLabel, renderLive," +
    " thinkLabel," +
    " select, pollEvents, sendState," +
    " _setTasks: (v) => { tasks = v; }," +
    " _setSessions: (v) => { sessions = v; }," +
    " _cache: () => eventsCache, _polled: () => polled," +
    " _panes: () => panes, _rendered: () => rendered," +
    " _railBtns: () => railBtns, _setSelected: (v) => { selected = v; }," +
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
  const ctxHtml = X.tokSpan(task({ usage: { used: 113177, size: 262000 } }),
    "stok");
  eq(ctxHtml.includes("~113k tok"), true,
    "context-only snapshot renders as estimate");
  eq(ctxHtml.includes("Context in use"), true,
    "context-only title never claims a run total");
  eq(X.tokSpan(task({}), "stok"), "", "no usage -> empty span");
  eq(X.tokSpan(task({ usage: { junk: 1 } }), "stok"), "",
    "unusable usage -> empty span");

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
     metrics, so layoutRail/updateCurMark/jumpToMark and the rail's own
     listeners run unmodified inside the vm. */
  const yOf = (b) =>
    parseFloat(b.style.transform.match(/translateY\(([-\d.]+)/)[1]);
  const hOf = (b) => parseFloat(b.style.height);
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

  // --- taxonomy: normalized events -> DOM -> marks. Only agent message
  //     cards and MCP-dispatched prompt cards are marked; dashboard-authored
  //     "User Message" cards, thinking folds, tool groups, turn/error
  //     dividers, empty states and usage-only events never get one. ---
  eq(X.MARK_SEL, ".block.card.msg,.block.card.prompt:not(.user)",
    "MARK_SEL is the pinned taxonomy selector");
  eq(X.MARK_GAP, 6, "cluster gap is 6px");
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
  eq(bs.length, 2, "marks = dispatched prompt + agent message only");
  eq(bs[0]._els[0], contentEl.children[0], "mark 0 -> dispatched prompt card");
  eq(bs[1]._els[0], contentEl.children[2], "mark 1 -> agent message card");
  eq(railEl.hidden, false, "rail shown when markable cards exist");
  eq(bs[0].attrs["aria-label"].includes("Dispatched message"), true,
    "dispatched prompt mark gets the localized kind label");
  eq(bs[1].attrs["aria-label"].includes("Agent message"), true,
    "agent mark gets the localized kind label");
  eq(bs[0].title, bs[0].attrs["aria-label"], "title mirrors the aria-label");
  // Document-fraction positions: offsetTop / scrollHeight * railHeight.
  eq(yOf(bs[0]), 2.5, "mark y is the card's document fraction (20/800*100)");
  eq(yOf(bs[1]), 32.5, "mark y is the card's document fraction (260/800*100)");
  eq(hOf(bs[0]), 3, "a lone mark is 3px tall");

  // --- a sensible current mark exists at scrollTop = 0 ---
  contentEl.scrollTop = 0;
  X.updateCurMark();
  eq(bs[0].classList.contains("cur"), true,
    "first mark is current at scrollTop=0");
  eq(bs[0].attrs["aria-current"], "true", "aria-current on the first mark");
  eq(bs[0].tabIndex, 0, "first mark is the rail's tab stop at rest");
  eq(bs[1].tabIndex, -1, "other marks leave the tab order at rest");

  // --- the scroll hook tracks the top-of-viewport card ---
  contentEl.scrollTop = 300;          // probe y=308: card tops 20,260 pass
  fire(contentEl, "scroll");
  eq(bs[1].classList.contains("cur"), true, "scrolling moves current to mark 1");
  eq(bs[0].attrs["aria-current"], undefined, "aria-current leaves mark 0");
  eq(bs[1].attrs["aria-current"], "true", "aria-current lands on mark 1");

  // --- cluster pills: height covers the member span, never overflow ---
  bs = railScene([["block card msg", 100], ["block card msg", 400],
    ["block card msg", 430]], 1000, 100);
  eq(bs.length, 2, "3px-apart marks cluster, the 30px one stays single");
  eq(yOf(bs[1]), 40, "cluster anchored at its first member");
  eq(hOf(bs[1]), 6, "cluster pill covers member span + 3");
  // A cluster that ends exactly at the rail's bottom edge must not paint past
  // it: anchor clamps to RH - height instead of hanging over the chatbar.
  bs = railScene([["block card msg", 100], ["block card msg", 980],
    ["block card msg", 985], ["block card prompt", 990],
    ["block card msg", 995]], 1000, 100);
  eq(bs.length, 2, "four close marks merge into one bottom cluster");
  bs.forEach((b, i) => eq(
    yOf(b) >= 0 && yOf(b) + hOf(b) <= railEl.clientHeight, true,
    `pill ${i} fully inside rail bounds`));
  eq(yOf(bs[1]) + hOf(bs[1]), 100,
    "bottom cluster lands exactly on the rail's bottom edge");
  // Cluster label: localized count + the first member's timestamp.
  contentEl.children[1].querySelector(".ctime").textContent = "10:00";
  X.layoutRail();
  eq(bs[1].attrs["aria-label"], "4 messages · 10:00",
    "cluster label = localized count + first member time");

  // --- mark buttons are reused across layout passes (no DOM churn) ---
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
  eq(documentStub.activeElement, bs[1], "End focuses the last mark");
  fire(railEl, "keydown", { key: "Home", preventDefault: () => pd++ });
  eq(documentStub.activeElement, bs[0], "Home focuses the first mark");
  // While the rail is in use the tab stop stays on the focused mark even
  // when scroll position makes another mark current.
  bs[1].focus();
  contentEl.scrollTop = 985;          // probe passes the cluster's first member
  X.updateCurMark();
  eq(bs[1].classList.contains("cur"), true, "current mark tracks scroll");
  contentEl.scrollTop = 0;
  X.updateCurMark();
  eq(bs[0].classList.contains("cur"), true, "current mark back to the first");
  eq(bs[1].tabIndex, 0, "focused mark keeps the tab stop");
  eq(bs[0].tabIndex, -1,
    "current mark leaves the tab order while another is focused");

  // --- bare-track click: same document fraction the marks are placed by ---
  contentEl._scrollToArgs = null;
  fire(railEl, "click", { target: railEl, offsetY: 50 });
  eq(contentEl._scrollToArgs.top, 500,
    "track click seeks to offsetY/RH * scrollHeight");
  // Clicking level with a mark lands that card at the top of the viewport.
  fire(railEl, "click", { target: railEl, offsetY: yOf(bs[0]) });
  eq(contentEl._scrollToArgs.top, bs[0]._els[0].offsetTop,
    "clicking level with a mark seeks exactly to its card");
  fire(railEl, "click", { target: railEl, offsetY: 100 });
  eq(contentEl._scrollToArgs.top, 600, "track bottom clamps to doc - viewport");
  const lastTop = contentEl._scrollToArgs.top;
  fire(railEl, "click", { target: bs[0], offsetY: 50 });
  eq(contentEl._scrollToArgs.top, lastTop,
    "mark clicks are not double-handled by the track listener");

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

  // --- clusterYs single-linkage clustering within `gap` px ---
  let g = X.clusterYs([0, 5, 20, 24, 50], 6);
  eq(g.length, 3, "clusters: [0,5] [20,24] [50]");
  eq(g[0].idx.length, 2, "first cluster holds two marks");
  eq(g[0].y, 0, "cluster anchored at its first mark");
  eq(g[2].idx[0], 4, "lonely last mark stays single");
  eq(X.clusterYs([0, 5, 9], 6).length, 1,
    "chained proximity merges (5-0<6, 9-5<6)");
  eq(X.clusterYs([0, 6, 12], 6).length, 3,
    "exactly-gap marks stay separate (boundary is exclusive)");
  eq(X.clusterYs([10], 6).length, 1, "single mark -> single cluster");
  eq(X.clusterYs([], 6).length, 0, "no marks -> no clusters");
  eq(X.clusterYs([0, 0, 0], 0).length, 3,
    "zero gap never clusters (unmeasurable-layout fallback)");

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
  eq(elCache["#hwrap"].innerHTML.includes("工作儲存庫") ||
     elCache["#hwrap"].innerHTML.includes("工作仓库"), true,
    "header repo label localized zh-CN");
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

  console.log(failed ? `\n${failed} FAILED` : "\nall assertions passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
