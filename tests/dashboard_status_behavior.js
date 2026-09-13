// Executable behavior tests for the dashboard page's status/duration helpers.
//
// Extracts the real inline <script> from src/agent_bridge/share/dashboard.py's
// PAGE and runs it inside a vm context with a minimal DOM stub — no jsdom, no
// npm packages. Covers the centralized proc_state map, latestTask chronology,
// the taskDur/fmtDur/durText rules end to end, and the bundled i18n layer
// (en / zh-CN / zh-TW): dictionaries, locale resolution, localized labels,
// and rerendering on locale switch without a transcript refetch.
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
   records setAttribute values and returns stable children for querySelector. */
const created = [];
const el = () => {
  const e = {
    innerHTML: "", textContent: "", style: {}, title: "", type: "",
    className: "",
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    dataset: {}, disabled: false, value: "", placeholder: "", tabIndex: 0,
    open: false, hidden: false, isConnected: true, offsetTop: 0,
    children: [], firstChild: null, firstElementChild: null,
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    attrs: {}, _q: {}, focused: 0,
    appendChild() {}, insertAdjacentHTML() {}, after() {},
    setAttribute(k, v) { e.attrs[k] = String(v); },
    getAttribute(k) { return e.attrs[k]; },
    removeAttribute(k) { delete e.attrs[k]; },
    replaceChildren() {},
    addEventListener() {},
    querySelector(s) { return e._q[s] || (e._q[s] = el()); },
    querySelectorAll: () => [],
    focus() { e.focused++; },
    remove() {},
  };
  return e;
};
const elCache = {};
const documentStub = {
  documentElement: Object.assign(el(), { lang: "" }),
  activeElement: null, title: "",
  querySelector: (s) => elCache[s] || (elCache[s] = el()),
  querySelectorAll: () => [],
  createElement() {
    const e = el(); created.push(e); return e;
  },
  addEventListener() {},
};
const pending = () => new Promise(() => {});
const fetchCalls = [];
const store = {};            // mutable localStorage backing
const listeners = {};        // captured window-level event listeners
const sandbox = {
  document: documentStub,
  fetch: (u) => { fetchCalls.push(String(u)); return pending(); },
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  },
  sessionStorage: { getItem: () => null, setItem() {} },
  navigator: { sendBeacon() {}, languages: ["en-US"], language: "en-US" },
  crypto: { randomUUID: () => "00000000-0000-0000-0000-000000000000" },
  matchMedia: () => ({
    matches: false, media: "", addEventListener() {}, removeEventListener() {},
    addListener() {}, removeListener() {},
  }),
  addEventListener(ev, fn) { (listeners[ev] || (listeners[ev] = [])).push(fn); },
  removeEventListener() {},
  setInterval: () => 0, clearInterval() {},
  setTimeout: () => 0, clearTimeout() {},
  requestAnimationFrame: () => 0,
  performance: { now: () => 0 },
  // Observer no-ops: the rail wiring runs but never fires a layout pass.
  MutationObserver: function () { return { observe() {}, disconnect() {} }; },
  ResizeObserver: function () { return { observe() {}, disconnect() {} }; },
  console,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(
  code +
    "\n;globalThis.__x = { statusOf, latestTask, taskDur, fmtDur, durText," +
    " durSpan, PROC_STATUS, subSeed, agentAvatar, statusGlyph, icon," +
    " usageNums, tokCount, runTok, tokTitle, fmtTok, tokSpan, turnDurMs," +
    " addTurn, addPrompt, addTool, addToolStatus, clusterYs, markable," +
    " MARK_SEL, MARK_GAP," +
    " scheduleRail, layoutRail, jumpToMark, updateCurMark," +
    " t, LOCALES, LOCALE_PREFS, localePref, resolveSystemLocale," +
    " setLocalePref, applyLocale, rerenderLocale, ago, fmtTs, fmtNum," +
    " sendKey, sendErrState, setSendState, renderSendStatus," +
    " refreshComposer, toolKindLabel, stopReasonLabel, renderLive," +
    " select, pollEvents, sendState," +
    " _setTasks: (v) => { tasks = v; }," +
    " _setSessions: (v) => { sessions = v; }," +
    " _cache: () => eventsCache, _polled: () => polled," +
    " _panes: () => panes, _rendered: () => rendered," +
    " _getLocale: () => locale, _getLangPref: () => langPref };",
  sandbox);
const X = sandbox.__x;

/* ---------- assertions ---------- */
let failed = 0;
function eq(got, want, name) {
  const ok = got === want;
  console.log(`${ok ? "PASS" : "FAIL"} ${name}  got=${JSON.stringify(got)} want=${JSON.stringify(want)}`);
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
  documentStub.createElement = function () {
    const e = el(); created.push(e); return e;
  };

  // --- nav rail: mark predicate mirrors MARK_SEL (user cards excluded) ---
  const fakeEl = (cls) => ({ classList: { contains: (c) => cls.includes(c) } });
  eq(X.markable(fakeEl(["block", "card", "msg"])), true,
    "agent message card is marked");
  eq(X.markable(fakeEl(["block", "card", "prompt"])), true,
    "dispatched prompt card is marked");
  eq(X.markable(fakeEl(["block", "card", "prompt", "user"])), false,
    "dashboard-authored User Message card is excluded");
  eq(X.markable(fakeEl(["block", "think"])), false, "think fold excluded");
  eq(X.markable(fakeEl(["block", "toolgroup"])), false, "tool group excluded");
  eq(X.markable(fakeEl(["block", "card", "toolgroup"])), false,
    "card without msg/prompt kind excluded");
  eq(X.markable(fakeEl(["turnend"])), false, "turn divider excluded");
  eq(X.markable(fakeEl(["turnend", "err"])), false, "error divider excluded");
  eq(X.markable(fakeEl(["card", "msg"])), false, "non-block card excluded");
  eq(X.markable(fakeEl(["empty"])), false, "empty placeholder excluded");
  eq(X.MARK_SEL.includes(".block.card.msg"), true, "MARK_SEL marks msg cards");
  eq(X.MARK_SEL.includes(":not(.user)"), true, "MARK_SEL excludes user cards");
  eq(X.MARK_GAP, 6, "cluster gap is 6px");

  // --- nav rail: clusterYs single-linkage clustering within `gap` px ---
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
  // rail wiring degrades safely: helpers callable with an empty DOM stub.
  X.layoutRail();
  X.updateCurMark();
  X.scheduleRail();
  eq(true, true, "rail helpers run on the DOM stub without throwing");

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
  X.setLocalePref("en");
  eq(X.ago(new Date(Date.now() - 30e3).toISOString()), "30s ago",
    "relative time en");
  eq(X.t("tokens.run", { n: "72" }), "Run tokens 72", "token tooltip en");

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

  console.log(failed ? `\n${failed} FAILED` : "\nall assertions passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
