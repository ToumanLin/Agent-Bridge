// Deterministic benchmark for the dashboard's session-switch render path.
//
// Loads the real inline PAGE from src/agent_bridge/share/dashboard_page.py into
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
// DASH_PAGE may point at a standalone .html (raw page) or a .py source
// embedding PAGE as a raw string.
const html = (() => {
  const custom = process.env.DASH_PAGE;
  if (custom && custom.endsWith(".html")) return fs.readFileSync(custom, "utf8");
  const src = fs.readFileSync(
    custom || path.join(ROOT, "src", "agent_bridge", "share", "dashboard_page.py"), "utf8");
  const m = src.match(/PAGE = r"""([\s\S]*?)"""/);
  if (!m) throw new Error("PAGE not found in dashboard_page.py");
  return m[1];
})();

// The rail's mark taxonomy lives in exactly one place — the page's MARK_SEL
// constant — so the rail invariant below reads it back out of the loaded
// page rather than keeping a second copy that could silently drift.
const MARK_SEL = (html.match(/const MARK_SEL="([^"]+)"/) || [])[1] || null;

/* ---------- synthetic streams (deterministic, no real transcript data) ---------- */
function synthPathological() {
  // One giant single-block chunk stream + tail — the O(chunks x block) killer.
  // 1200 chunks is enough to expose per-chunk md()+innerHTML (msgBodySets >> slices)
  // while keeping even the unoptimized path to tens of seconds, not minutes.
  const evs = [{ t: "prompt", ts: 1, text: "synthetic prompt" }];
  for (let i = 0; i < 1200; i++)
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
  // ~3.1K events over 10 turns: under an ~8ms-capped rAF replay this spans
  // several slices (~2x margin over single-slice capacity) yet completes in
  // well under a second under jsdom. The old 40x600 fixture (24K events)
  // took minutes.
  const evs = [];
  for (let turn = 0; turn < 10; turn++) {
    evs.push({ t: "prompt", ts: turn * 1000, text: `task ${turn}` });
    for (let i = 0; i < 150; i++)
      evs.push({ t: "think", ts: turn * 1000 + i + 1, text: `thought ${turn}.${i} ` });
    for (let i = 0; i < 150; i++)
      evs.push({ t: "msg", ts: turn * 1000 + i + 200, text: `msg ${turn}.${i} ` });
    for (let i = 0; i < 4; i++) {
      const id = `rt${turn}_${i}`;
      evs.push({ t: "tool", ts: turn * 1000 + 400 + i, id, kind: "execute",
        title: `tool ${i}`, input: `{"cmd":"x ${i}"}` });
      evs.push({ t: "tool_status", ts: turn * 1000 + 401 + i, id, status: "completed" });
    }
    evs.push({ t: "turn", ts: turn * 1000 + 500, stop_reason: "end_turn" });
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
// Regression-scenario actors: a small "stale" session whose reset lands
// mid-switch, and two identical "race" sessions. sess_race_a provides the
// baseline element count; sess_race_b is never rendered before its replay is
// raced against the reset, so its select() is a guaranteed cold chunked
// replay — no dependence on the pane-LRU capacity to force a second replay.
streams.push({ id: "sess_race_a", stream: synthRace() });
streams.push({ id: "sess_race_b", stream: synthRace() });
streams.push({ id: "sess_stale",
  stream: { events: [{ t: "prompt", ts: 1, text: "stale" }], offset: 1 } });

const overview = {
  sessions: streams.map((s, i) => ({
    session_id: s.id, title: s.id, agent: "devin", model: "bench",
    proc_state: i ? "busy" : "dead", cwd: "C:/bench",
    turns: 3, created_at: "2026-01-01T00:00:00Z",
    last_active_at: "2026-01-01T00:0" + i + ":00Z",
  })),
  // Fixed timestamps make durations deterministic: the dead session's task is
  // frozen at 40s (finished-started); the busy session's task started ~65s ago
  // so its .sdur keeps ticking under the 1s tickDurations interval.
  tasks: [
    { task_id: "task_done", session_id: streams[0].id, agent: "devin",
      status: "completed", message: "finished work",
      created_at: "2026-01-01T00:00:00Z", started_at: "2026-01-01T00:00:02Z",
      finished_at: "2026-01-01T00:00:42Z",
      usage: { input_tokens: 108414, cached_input_tokens: 108072,
        output_tokens: 4763 } },
    { task_id: "task_run", session_id: streams[1].id, agent: "devin",
      status: "running", message: "still working",
      created_at: new Date(Date.now() - 70000).toISOString(),
      started_at: new Date(Date.now() - 65000).toISOString() },
  ],
  // Batched live-usage map, shaped like /api/overview emits it: pinned to
  // the running task's id so it can only land on that row.
  live: {
    [streams[1].id]: {
      task_id: "task_run",
      consumed: { scope: "run", quality: "exact", v: 1,
        input: 10, output: 5, total: 15 },
    },
  },
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
  // "Done" = element count AND rAF counter both unchanged for 3 consecutive
  // 25ms samples. rAF must be in the signature: inside a long chunk run the
  // element count can sit flat while replay slices keep firing, so count-only
  // sampling can declare stability mid-replay. Idle panes settle in ~75ms;
  // the loop hard-caps at ~15s so a stuck replay fails instead of hanging.
  let last = -1, lastRaf = -1, stable = 0;
  for (let i = 0; i < 600 && stable < 3; i++) {
    await sleep(25);
    const n = w.document.querySelector("#content").querySelectorAll("*").length;
    stable = n === last && stats.raf === lastRaf ? stable + 1 : 0;
    last = n; lastRaf = stats.raf;
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
  // Nav rail: the compact ruler keeps one .mark button per element matching
  // MARK_SEL — this cross-checks the live rail against the selector the
  // page actually applies.
  const markCount = () => w.document.querySelectorAll("#rail .mark").length;
  const cardCount = () => MARK_SEL
    ? w.document.querySelector("#content").querySelectorAll(MARK_SEL).length
    : markCount();
  const railShown = () => !w.document.querySelector("#rail").hidden;
  results.push({
    session: coldId, events: expected, elements: nodes,
    coldSelectWallMs: +coldMs.toFixed(0), syncSelectMs: +selectSyncMs.toFixed(0),
    rafSlices: stats.raf, msgBodySets: stats.msgBodySets,
    maxTimerGapMs: +stats.maxTaskGap.toFixed(1),
    maxInnerHTMLParseMs: +(stats.maxParseMs || 0).toFixed(1),
    railMarks: markCount(), markableCards: cardCount(),
    railOk: railShown() && markCount() === cardCount() && cardCount() > 0,
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
    // Warm stash/restore re-derives the same marks from the moved DOM nodes.
    railMarks: markCount(), markableCards: cardCount(),
    railOk: railShown() && markCount() === cardCount() && cardCount() > 0,
  });

  // ---- regression: a stale session's reset must not cancel a live replay ----
  // sess_race_a's cold replay establishes the baseline element count and
  // proves multi-slice replay. sess_race_b is an identical stream that has
  // never been rendered, so its select() below is deterministically a cold
  // chunked replay — no pane-LRU eviction needed. While sess_stale is
  // selected we arm a delayed reset and start its fetch; switching to
  // sess_race_b starts its replay, and the reset resolves a few dozen
  // microtasks later — deterministically mid-replay for any multi-slice
  // stream, since rAF slices are macrotasks and the reset cannot outrun the
  // first frame. A global (non-per-session) abort token would let the stale
  // reset kill sess_race_b's replay, freezing the element count mid-stream.
  const elCount = () =>
    w.document.querySelector("#content").querySelectorAll("*").length;
  stats.raf = 0;
  w.select("sess_race_a");
  const raceElements = await waitStable();
  const raceASlices = stats.raf;
  w.select("sess_stale");
  await waitStable();
  resetArm["sess_stale"] = true;
  delayArm["sess_stale"] = 25;         // lands after race_b's replay starts, before frame 2
  const staleFetch = w.pollEvents();   // in-flight fetch for the stale session
  stats.raf = 0;
  w.select("sess_race_b");             // cold chunked replay begins
  await staleFetch;                    // stale reset lands mid-replay — must be inert
  const c0 = elCount(), r0 = stats.raf;
  await sleep(160); const c1 = elCount(), r1 = stats.raf;
  await sleep(160); const c2 = elCount(), r2 = stats.raf;
  const raceProgressed = c1 > c0 || c2 > c1 || r1 > r0 || r2 > r1;
  const raceFinal = await waitStable();
  const raceBSlices = stats.raf;
  results.push({
    regression: "stale-reset-mid-replay",
    raceASlices, raceBSlices,
    raceProgressed,
    raceCompletedOnce: raceFinal === raceElements,
    raceFinalElements: raceFinal, raceExpectedElements: raceElements,
    // Marks reflect the completed replay of race_b — no stale marks survive
    // the stale session's mid-flight reset.
    railMarks: markCount(), markableCards: cardCount(),
    railOk: railShown() && markCount() === cardCount() && cardCount() > 0,
  });

  // ---- sidebar signature: identical polls must not rewrite #sesslist ----
  stats.sesslistSets = 0;
  await w.pollOverview();
  await w.pollOverview();
  const sesslistSetsOnStablePolls = stats.sesslistSets;

  // ---- live batch: exactly one repaint for a changed partial, then stable ----
  overview.live[streams[1].id].consumed =
    { scope: "run", quality: "exact", v: 1, input: 20, output: 8, total: 28 };
  await w.pollOverview();
  const sesslistSetsOnLiveChange = stats.sesslistSets;
  await w.pollOverview();
  const sesslistSetsAfterLive = stats.sesslistSets;

  // ---- weekly metric: batched live contributes; the node stays passive ----
  const weekEl = w.document.querySelector("#weektotal");
  const weekVal = weekEl && weekEl.querySelector(".wt-val").textContent;
  const weekTitle = weekEl ? weekEl.title : "";
  const weekAriaLive = weekEl ? weekEl.getAttribute("aria-live") : "missing";

  // ---- task working durations: a completed task freezes at
  // finished_at - started_at ("40s" per the fixture); a running task ticks
  // live via the 1s tickDurations interval — textContent only, so the
  // sidebar signature gate above is unaffected.
  const sessRow = (sid) =>
    [...w.document.querySelectorAll(".sess")].find((r) => r.dataset.id === sid);
  const durOf = (sid) => {
    const d = sessRow(sid) && sessRow(sid).querySelector(".sdur");
    return d ? d.textContent : null;
  };
  const statusText = (sid) => {
    const el = sessRow(sid) && sessRow(sid).querySelector(".sstatus");
    return el ? el.textContent : "";
  };
  const doneDur0 = durOf(streams[0].id);
  const runDur0 = durOf(streams[1].id);
  // The dashboard refreshes durations on a one-second interval whose phase
  // is independent of this sample. Wait through two ticks so the assertion
  // checks the live clock rather than depending on test timing.
  await sleep(2200);
  const doneDur1 = durOf(streams[0].id);
  const runDur1 = durOf(streams[1].id);
  results.push({
    durations: { doneDur0, doneDur1, runDur0, runDur1 },
    doneFrozen: doneDur0 === " · 40s" && doneDur1 === " · 40s",
    runAdvanced: runDur0 !== null && runDur1 !== null && runDur1 !== runDur0,
    statusLabels: statusText(streams[0].id).includes("Done")
      && statusText(streams[1].id).includes("Running"),
    // task_done usage: (108414-108072)+4763 = 5105 -> " · 5k tok"
    tokensShown: statusText(streams[0].id).includes("5k tok"),
    // running agent's sidebar counter shows the batched live partial
    liveTokensShown: statusText(streams[1].id).includes("28 tok"),
  });

  results.push({
    liveBatch: { sesslistSetsOnLiveChange, sesslistSetsAfterLive,
      weekVal, weekTitle, weekAriaLive },
    liveRepaintOnce: sesslistSetsOnLiveChange === 1,
    liveStableAfter: sesslistSetsAfterLive === 1,
    weekValOk: weekVal === "28 tok",
    weekTitleOk: weekTitle.includes("1 task")
      && weekTitle.includes("live in-progress")
      && weekTitle.includes("retained Agent Bridge task history"),
    weekPassive: weekAriaLive === null,
  });

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
    (r.raceASlices === undefined || r.raceASlices > 1) &&
    (r.raceBSlices === undefined || r.raceBSlices > 1) &&
    (r.raceProgressed === undefined || r.raceProgressed) &&
    (r.raceCompletedOnce === undefined || r.raceCompletedOnce) &&
    // One rail mark per markable card, rail visible, after every render path.
    (r.railOk === undefined || r.railOk) &&
    (r.doneFrozen === undefined || r.doneFrozen) &&
    (r.runAdvanced === undefined || r.runAdvanced) &&
    (r.statusLabels === undefined || r.statusLabels) &&
    (r.tokensShown === undefined || r.tokensShown) &&
    (r.liveTokensShown === undefined || r.liveTokensShown) &&
    (r.liveRepaintOnce === undefined || r.liveRepaintOnce) &&
    (r.liveStableAfter === undefined || r.liveStableAfter) &&
    (r.weekValOk === undefined || r.weekValOk) &&
    (r.weekTitleOk === undefined || r.weekTitleOk) &&
    (r.weekPassive === undefined || r.weekPassive)) &&
    sesslistSetsOnStablePolls === 0 && !jsError;
  process.exit(ok ? 0 : 1);
})().catch((e) => { console.error(e); probing = false; process.exit(1); });
