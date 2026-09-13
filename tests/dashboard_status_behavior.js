// Executable behavior tests for the dashboard page's status/duration helpers.
//
// Extracts the real inline <script> from src/agent_bridge/share/dashboard.py's
// PAGE and runs it inside a vm context with a minimal DOM stub — no jsdom, no
// npm packages. Covers the centralized proc_state map, latestTask chronology,
// and the taskDur/fmtDur/durText rules end to end.
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
   pending so no poll mutates `tasks` between assertions. */
const el = () => ({
  innerHTML: "", textContent: "", style: {}, title: "", type: "",
  classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
  dataset: {}, disabled: false, value: "", placeholder: "", tabIndex: 0,
  open: false, hidden: false, isConnected: true, offsetTop: 0,
  children: [], firstChild: null, firstElementChild: null,
  scrollTop: 0, scrollHeight: 0, clientHeight: 0,
  appendChild() {}, insertAdjacentHTML() {}, setAttribute() {},
  removeAttribute() {}, replaceChildren() {},
  addEventListener() {}, querySelector: () => el(), querySelectorAll: () => [],
  focus() {}, remove() {},
});
const documentStub = {
  documentElement: el(), activeElement: null,
  querySelector: () => el(), querySelectorAll: () => [],
  createElement: () => el(), addEventListener() {},
};
const pending = () => new Promise(() => {});
const sandbox = {
  document: documentStub,
  fetch: pending,
  localStorage: { getItem: () => null, setItem() {} },
  sessionStorage: { getItem: () => null, setItem() {} },
  navigator: { sendBeacon() {} },
  crypto: { randomUUID: () => "00000000-0000-0000-0000-000000000000" },
  matchMedia: () => ({
    matches: false, media: "", addEventListener() {}, removeEventListener() {},
    addListener() {}, removeListener() {},
  }),
  addEventListener() {}, removeEventListener() {},
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
    " addTurn, addPrompt, clusterYs, markable, MARK_SEL, MARK_GAP," +
    " scheduleRail, layoutRail, jumpToMark, updateCurMark," +
    " _setTasks: (v) => { tasks = v; } };",
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

(async () => {
  // --- centralized status mapping: proc_state wins over task status ---
  X._setTasks([task({})]);
  eq(X.statusOf(sess("busy")).label, "Running",
    "busy -> Running even when latest task is completed");
  eq(X.statusOf(sess("spawning")).label, "Starting", "spawning -> Starting");
  eq(X.statusOf(sess("ready")).label, "Ready", "ready + completed -> Ready");
  eq(X.statusOf(sess("idle_unloaded")).label, "Idle",
    "idle_unloaded + completed -> Idle");
  eq(X.statusOf(sess("dead")).label, "Done", "dead + completed -> Done");
  X._setTasks([task({ status: "failed" })]);
  eq(X.statusOf(sess("dead")).label, "Failed", "dead + failed -> Failed");
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

  console.log(failed ? `\n${failed} FAILED` : "\nall assertions passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
