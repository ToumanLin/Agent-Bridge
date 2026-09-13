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
  innerHTML: "", textContent: "", style: {},
  classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
  dataset: {}, disabled: false, value: "", placeholder: "", tabIndex: 0,
  open: false, children: [], firstChild: null, firstElementChild: null,
  scrollTop: 0, scrollHeight: 0, clientHeight: 0,
  appendChild() {}, insertAdjacentHTML() {}, setAttribute() {},
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
  console,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(
  code +
    "\n;globalThis.__x = { statusOf, latestTask, taskDur, fmtDur, durText," +
    " durSpan, PROC_STATUS, subSeed, agentAvatar, statusGlyph, icon," +
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

  console.log(failed ? `\n${failed} FAILED` : "\nall assertions passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
