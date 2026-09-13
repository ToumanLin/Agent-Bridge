// Deterministic benchmark for the dashboard's session-switch render path.
//
// Loads the real inline PAGE from src/agent_bridge/share/dashboard.py into
// jsdom, stubs fetch/matchMedia, replays event streams through the actual
// page script, and reports the numbers the performance work targets:
//
//   * cold select: rAF slice count (chunked replay engaged), .msg-body
//     innerHTML writes (batch flush — should be ~slices, not ~chunks),
//     wall time, and worst timer starvation (proxy for long tasks).
//   * warm re-select: synchronous select() cost, DOM-node identity
//     (pane stash — no rebuild), details.open preservation.
//   * sidebar: #sesslist innerHTML writes must not grow across identical
//     overview polls (render signature gate).
//
// Usage:
//   node scripts/bench_dashboard_replay.js [stream.json ...]
//
// With no arguments it benchmarks two built-in synthetic streams (no real
// transcript content). Extra arguments are JSON files shaped like the
// /api/events response: {"events": [...]} (e.g. captured live snapshots).
// Requires jsdom (npm i jsdom, or NODE_PATH=<dir containing jsdom>).
"use strict";
const fs = require("fs");
const path = require("path");
const { performance } = require("perf_hooks");

let JSDOM, VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = require("jsdom"));
} catch (e) {
  console.error("jsdom not found. Run: npm i jsdom  (or set NODE_PATH)");
  process.exit(2);
}

const ROOT = path.resolve(__dirname, "..");
// DASH_PAGE may point at a standalone .html (raw page) or a dashboard.py source.
const html = (() => {
  const custom = process.env.DASH_PAGE;
  if (custom && custom.endsWith(".html")) return fs.readFileSync(custom, "utf8");
  const src = fs.readFileSync(
    custom || path.join(ROOT, "src", "agent_bridge", "share", "dashboard.py"), "utf8");
  const m = src.match(/PAGE = r"""([\s\S]*?)"""/);
  if (!m) throw new Error("PAGE not found in dashboard.py");
  return m[1];
})();

/* ---------- synthetic streams (deterministic, no real transcript data) ---------- */
function synthPathological() {
  // One giant single-block chunk stream + tail — the O(chunks x block) killer.
  const evs = [{ t: "prompt", ts: 1, text: "synthetic prompt" }];
  for (let i = 0; i < 4000; i++)
    evs.push({ t: "msg", ts: 2 + i, text: `chunk-${i} **bold** \`code\` ` });
  evs.push({ t: "turn", ts: 9999, stop_reason: "end_turn" });
  return { events: evs, offset: evs.length };
}
function synthWide() {
  const evs = [];
  for (let turn = 0; turn < 12; turn++) {
    evs.push({ t: "prompt", ts: turn * 1000, text: `task ${turn}` });
    for (let i = 0; i < 120; i++)
      evs.push({ t: "think", ts: turn * 1000 + i + 1, text: `t${i} ` });
    for (let i = 0; i < 250; i++)
      evs.push({ t: "msg", ts: turn * 1000 + i + 130, text: `m${i} ` });
    for (let i = 0; i < 20; i++) {
      const id = `t${turn}_${i}`;
      evs.push({ t: "tool", ts: turn * 1000 + 400 + i, id, kind: "execute",
        title: `tool ${i}`, input: `{"cmd":"x ${i}"}` });
      evs.push({ t: "tool_status", ts: turn * 1000 + 401 + i, id, status: "completed" });
    }
    evs.push({ t: "turn", ts: turn * 1000 + 500, stop_reason: "end_turn" });
  }
  return { events: evs, offset: evs.length };
}

function synthRace() {
  // Big multi-block stream so a cold chunked replay spans many rAF slices —
  // enough to observe an abort as a mid-replay freeze.
  const evs = [];
  for (let turn = 0; turn < 40; turn++) {
    for (let i = 0; i < 300; i++)
      evs.push({ t: "think", ts: turn * 1000 + i, text: `thought ${turn}.${i} ` });
    for (let i = 0; i < 300; i++)
      evs.push({ t: "msg", ts: turn * 1000 + i + 300, text: `msg ${turn}.${i} ` });
    evs.push({ t: "turn", ts: turn * 1000 + 700, stop_reason: "end_turn" });
  }
  return { events: evs, offset: evs.length };
}

const argStreams = process.argv.slice(2).map((f) => {
  const j = JSON.parse(fs.readFileSync(f, "utf8"));
  return { id: path.basename(f, ".json"), stream: { events: j.events, offset: j.offset || j.events.length } };
});
const streams = argStreams.length
  ? argStreams
  : [{ id: "sess_patho", stream: synthPathological() },
     { id: "sess_wide", stream: synthWide() }];
if (streams.length < 2)
  streams.push({ id: "sess_extra", stream: synthWide() });
// Regression-scenario actors: filler sessions to force pane-LRU eviction, a
// small "stale" session whose reset lands mid-switch, and a big "race"
// session whose cold replay must not be cancelled by that reset.
for (let i = 0; i < 5; i++)
  streams.push({ id: `sess_fill${i}`,
    stream: { events: [{ t: "prompt", ts: 1, text: `f${i}` }], offset: 1 } });
streams.push({ id: "sess_stale",
  stream: { events: [{ t: "prompt", ts: 1, text: "stale" }], offset: 1 } });
streams.push({ id: "sess_race", stream: synthRace() });

const overview = {
  sessions: streams.map((s, i) => ({
    session_id: s.id, title: s.id, agent: "devin", model: "bench",
    proc_state: i ? "busy" : "dead", cwd: "C:/bench",
    turns: 3, created_at: "2026-01-01T00:00:00Z",
    last_active_at: "2026-01-01T00:0" + i + ":00Z",
  })),
  tasks: [],
};

/* ---------- jsdom harness ---------- */
const stats = { raf: 0, msgBodySets: 0, sesslistSets: 0, maxTaskGap: 0 };
const resetArm = {}; // session id -> next /api/events response is a reset
const delayArm = {}; // session id -> delay next response by N microtask hops
const virtualConsole = new VirtualConsole();
let jsError = null;
virtualConsole.on("jsdomError", (e) => { jsError = jsError || e; });

const dom = new JSDOM(html, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  url: "http://127.0.0.1:8787/",
  virtualConsole,
  beforeParse(window) {
    window.matchMedia = window.matchMedia || (() => ({
      matches: false, media: "", addEventListener() {}, removeEventListener() {},
      addListener() {}, removeListener() {},
    }));
    window.fetch = async (url) => {
      const u = new URL(url, "http://x");
      const body = (o) => ({ ok: true, status: 200, json: async () => o });
      if (u.pathname === "/api/overview") return body(overview);
      if (u.pathname === "/api/events") {
        const id = u.searchParams.get("session");
        const off = +u.searchParams.get("offset") || 0;
        if (delayArm[id]) {
          let n = delayArm[id];
          delayArm[id] = 0;
          while (n--) await Promise.resolve();
        }
        if (resetArm[id]) {
          resetArm[id] = false;
          return body({ events: [], offset: off, reset: true });
        }
        const s = streams.find((x) => x.id === id);
        const evs = s ? s.stream.events : [];
        return body(off === 0
          ? { events: evs, offset: s ? s.stream.offset : evs.length, reset: false }
          : { events: [], offset: off, reset: false });
      }
      if (u.pathname === "/api/presence") return body({ ok: true, clients: 1 });
      if (u.pathname === "/api/client_state") return body({ clients: 1, open: true });
      return body({ ok: true });
    };
    window.navigator.sendBeacon = () => {};
    if (!window.crypto || !window.crypto.randomUUID)
      window.crypto = Object.assign(window.crypto || {}, { randomUUID: () => "bench" });
    const raf = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = (cb) => { stats.raf++; return raf(cb); };
    const desc = Object.getOwnPropertyDescriptor(window.Element.prototype, "innerHTML");
    Object.defineProperty(window.Element.prototype, "innerHTML", {
      configurable: true,
      get() { return desc.get.call(this); },
      set(v) {
        if (this.id === "sesslist") stats.sesslistSets++;
        const t0 = performance.now();
        const r = desc.set.call(this, v);
        const dt = performance.now() - t0;
        if (this.classList && this.classList.contains("msg-body")) {
          stats.msgBodySets++;
          stats.maxMsgParseMs = Math.max(stats.maxMsgParseMs || 0, dt);
        }
        stats.maxParseMs = Math.max(stats.maxParseMs || 0, dt);
        return r;
      },
    });
  },
});
const w = dom.window;

// Timer-starvation probe: a >50ms synchronous task delays 0ms timeouts.
let probing = true, lastTick = performance.now();
(function probe() {
  const now = performance.now();
  stats.maxTaskGap = Math.max(stats.maxTaskGap, now - lastTick);
  lastTick = now;
  if (probing) setTimeout(probe, 0);
})();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitStable() {
  let last = -1, stable = 0;
  for (let i = 0; i < 400 && stable < 4; i++) {
    await sleep(50);
    const n = w.document.querySelector("#content").querySelectorAll("*").length;
    stable = n === last ? stable + 1 : 0;
    last = n;
  }
  return last;
}

(async () => {
  await waitStable(); // auto-select of first busy session + its cold replay
  if (jsError) { console.error("PAGE script error:", jsError); process.exit(1); }

  // ids[0] is dead (auto-select skips it), ids[1] is busy and gets auto-selected.
  const [coldId, warmId] = [streams[0].id, streams[1].id];
  const results = [];

  // ---- warm pane of the auto-selected session: fold a details open ----
  const det = w.document.querySelector("#content details");
  if (det) det.open = true;
  const warmNode = w.document.querySelector("#content").firstElementChild;
  const openCount = w.document.querySelectorAll("#content details[open]").length;

  // ---- cold select of a session never rendered: fetch + chunked replay ----
  stats.raf = 0; stats.msgBodySets = 0; stats.maxTaskGap = 0; stats.maxParseMs = 0;
  let t = performance.now();
  w.select(coldId);
  const selectSyncMs = performance.now() - t;
  const nodes = await waitStable();
  const coldMs = performance.now() - t;
  const expected = streams.find((s) => s.id === coldId).stream.events.length;
  results.push({
    session: coldId, events: expected, elements: nodes,
    coldSelectWallMs: +coldMs.toFixed(0), syncSelectMs: +selectSyncMs.toFixed(0),
    rafSlices: stats.raf, msgBodySets: stats.msgBodySets,
    maxTimerGapMs: +stats.maxTaskGap.toFixed(1),
    maxInnerHTMLParseMs: +(stats.maxParseMs || 0).toFixed(1),
  });

  // ---- warm re-select of the first session: stash restore, no rebuild ----
  stats.raf = 0; stats.msgBodySets = 0; stats.maxTaskGap = 0;
  t = performance.now();
  w.select(warmId);
  const warmSyncMs = performance.now() - t;
  await sleep(80);
  const after = w.document.querySelector("#content").firstElementChild;
  results.push({
    session: warmId, warmSelectSyncMs: +warmSyncMs.toFixed(1),
    sameDomNode: warmNode === after, rafDuringWarm: stats.raf,
    openFoldsKept:
      w.document.querySelectorAll("#content details[open]").length >= openCount,
  });

  // ---- regression: a stale session's reset must not cancel the live replay ----
  // Fill sess_race's cache, evict its pane via the stash LRU (5 filler
  // selects + the stale select). Then, while sess_stale is selected, arm a
  // delayed reset response and start its fetch; switch to sess_race so its
  // cold chunked replay is running (and its own poll has early-returned on
  // replaying===id) when the stale reset lands. Under a global abort token
  // the reset kills the replay, which then stays frozen until the next poll.
  const elCount = () =>
    w.document.querySelector("#content").querySelectorAll("*").length;
  w.select("sess_race");
  const raceElements = await waitStable();
  for (let i = 0; i < 5; i++) { w.select(`sess_fill${i}`); await waitStable(); }
  w.select("sess_stale");
  await waitStable();
  resetArm["sess_stale"] = true;
  delayArm["sess_stale"] = 10;         // let sess_race's own poll settle first
  const staleFetch = w.pollEvents();   // in-flight fetch for the stale session
  w.select("sess_race");               // cold chunked replay begins (slice 1 sync)
  await staleFetch;                    // stale reset lands mid-replay — must be inert
  const c0 = elCount(), r0 = stats.raf;
  await sleep(160); const c1 = elCount(), r1 = stats.raf;
  await sleep(160); const c2 = elCount(), r2 = stats.raf;
  const raceProgressed = c1 > c0 || c2 > c1 || r1 > r0 || r2 > r1;
  const raceFinal = await waitStable();
  results.push({
    regression: "stale-reset-mid-replay",
    raceProgressed,
    raceCompletedOnce: raceFinal === raceElements,
    raceFinalElements: raceFinal, raceExpectedElements: raceElements,
  });

  // ---- sidebar signature: identical polls must not rewrite #sesslist ----
  stats.sesslistSets = 0;
  await w.pollOverview();
  await w.pollOverview();
  const sesslistSetsOnStablePolls = stats.sesslistSets;

  probing = false;
  for (const r of results) console.log(JSON.stringify(r));
  console.log(JSON.stringify({ sesslistSetsOnStablePolls, jsError: !!jsError }));
  w.close();
  const ok = results.every((r) =>
    // msg-body rewrites bounded by ~blocks-per-slice, never by chunk count
    // (the pre-fix code did one md()+innerHTML per message_chunk).
    (r.msgBodySets === undefined || r.msgBodySets <= r.rafSlices * 8 + 40) &&
    (r.sameDomNode === undefined || r.sameDomNode) &&
    (r.openFoldsKept === undefined || r.openFoldsKept) &&
    // Worst event-loop gap: replay slices are 8ms-capped; a single big block's
    // md()+innerHTML parse is once-per-replay inherent work (jsdom parses far
    // slower than a browser — the pre-fix code produced multi-second gaps).
    (r.maxTimerGapMs === undefined || r.maxTimerGapMs < 400) &&
    (r.raceProgressed === undefined || r.raceProgressed) &&
    (r.raceCompletedOnce === undefined || r.raceCompletedOnce)) &&
    sesslistSetsOnStablePolls === 0 && !jsError;
  process.exit(ok ? 0 : 1);
})().catch((e) => { console.error(e); probing = false; process.exit(1); });
