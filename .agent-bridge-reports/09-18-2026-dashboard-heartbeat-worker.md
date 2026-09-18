# Dashboard Presence Heartbeat Frequency — Worker Report

**Document Version:** 1.0.0
**Date:** September 18, 2026
**Role:** Worker-Dashboard-Heartbeat
**Report File:** `C:\Users\Touma\Documents\Agent-Bridge\.agent-bridge-reports\09-18-2026-dashboard-heartbeat-worker.md`
**Repository Worktree:** `C:\Users\Touma\Documents\Agent-Bridge` (branch: `main`)
**Scope:** Reduce open-dashboard presence heartbeat request frequency while preserving reliable presence detection under throttled background-browser timers.
**Handoff:** `.agent-bridge-reports/09-18-2026-dashboard-lifecycle-report.md` (Slice 1, §8).

---

## 1. Objective & Outcome

The embedded dashboard page heartbeated `GET /api/presence` every 4,000 ms per open tab (15 req/min/tab). The cadence is now **15,000 ms** (4 req/min/tab — a 73.3% reduction) and the server-side lease `PRESENCE_TTL` is **180.0 s**, so a hidden tab under Chromium intensive timer throttling (~one wake-up per 60 s, with the ability to miss a tick entirely) still lands ~3 beats per lease window and survives two fully missed 60 s wake-ups at the boundary (`now - seen > PRESENCE_TTL`, strict).

All other presence semantics are unchanged: immediate `ping(0)` on load and on `pageshow`/`focus`/`online`/`visibilitychange→visible`, `pagehide` `sendBeacon` `bye=1` removal, and per-page `crypto.randomUUID()` client identity (never persisted, so a duplicated tab cannot evict a sibling). The `/api/overview` (3 s) and `/api/events` (1.5 s) pollers are untouched.

---

## 2. Changed Files

| File | Change |
| :--- | :--- |
| `src/agent_bridge/share/dashboard_page.py` | `setInterval(()=>ping(0),4000)` → `15000` (L465). |
| `src/agent_bridge/share/dashboard.py` | `PRESENCE_TTL = 150.0` → `180.0`; adjacent comment updated to state the new lease rationale. |
| `tests/test_dashboard_page.py` | `test_presence_survives_throttled_heartbeat`: pinned `PRESENCE_TTL == 180.0`, added a third 60 s clock step asserting a second missed wake-up still stays inside the lease (diff = 180, strict `>` keeps it). `test_presence_heartbeat_lifecycle`: pinned `PRESENCE_TTL == 180.0`, extracted interval pinned `ms == 15000`, kept the `ms * 10 <= PRESENCE_TTL * 1000` safety invariant (150,000 ≤ 180,000 → 12 opportunities ≥ 10). |

No other files were modified. `src/agent_bridge/dashboard.py` (`_CONFIRM_SEC`, launcher, WMI broker), `src/agent_bridge/registry.py`, `src/agent_bridge/config.py`, adapters, and docs retain their pre-existing uncommitted/user state — not touched, staged, or committed.

---

## 3. Design

### 3.1 Timing contract

```
presence_update / presence_count prune:  now - seen > PRESENCE_TTL (180.0s)
foreground tab:      beat every 15s   → 180/15 = 12 opportunities per lease
hidden tab (≤5min):  clamped to ≥1s   → same as foreground
hidden tab (>5min):  ~60s tick        → 3 beats per lease; one missed tick
                                       leaves a ~120s gap — well inside 180s
sleeping/discarded:  timers suspended → recovered by pageshow/focus/
                                       visibilitychange/online immediate ping
closed tab:          pagehide sendBeacon(bye=1) → removed at once (no TTL wait)
```

### 3.2 Why 15 s / 180 s

- **Request volume**: 4 req/min/tab vs. 15 — the requested reduction.
- **Missed-tick tolerance**: at the 60 s intensive-throttle bucket, the lease tolerates one fully missed wake-up (~120 s gap) with 60 s of jitter margin, and exactly two missed wake-ups at the 180 s boundary under the strict `>` comparison — pinned by the new third clock step in `test_presence_survives_throttled_heartbeat`.
- **Closed-tab latency**: `bye=1` removal is immediate, so the longer TTL does not delay reopen decisions on a genuinely closed tab; only crashed/killed tabs linger up to 180 s (was 150 s).
- **Invariant**: `ms * 10 <= PRESENCE_TTL * 1000` keeps ≥10 heartbeat opportunities per lease; actual ratio is 12.

### 3.3 Explicitly unchanged (per contract)

- `_CONFIRM_SEC = 2.0` in `src/agent_bridge/dashboard.py` — the lifecycle report's optional bump was **not** applied (contract: do not change). See §5.
- `setInterval(pollOverview,3000)`, `setInterval(pollEvents,1500)`, `setInterval(tickDurations,1000)` — unchanged.
- `Registry.idle_exit_due`, `stall_timeout_sec`, worker stall watchdog, task ownership, dashboard launcher/WMI broker, adapters, docs — unchanged.

---

## 4. Verification (Tier 1 only)

```
uv run pytest tests/test_dashboard_page.py
→ 73 passed in 20.90s

node tests/dashboard_status_behavior.js
→ all assertions passed (741 checks; presence lifecycle block:
  initial ping, pageshow/focus/online/visibilitychange re-pings,
  hidden-state no-ping, single pagehide bye=1 beacon, per-page id)

uv run ruff check src/agent_bridge/share/dashboard.py \
                  src/agent_bridge/share/dashboard_page.py \
                  tests/test_dashboard_page.py
→ All checks passed!
```

| Acceptance criterion | Where verified |
| :--- | :--- |
| 15,000 ms interval asserted exactly | `test_presence_heartbeat_lifecycle` (`assert ms == 15000`) |
| 180 s TTL asserted exactly | `assert dashboard.PRESENCE_TTL == 180.0` in both presence tests |
| ≥10 heartbeat opportunities invariant | `assert ms * 10 <= dashboard.PRESENCE_TTL * 1000` (12 actual) |
| Throttled hidden-tab presence survives | `test_presence_survives_throttled_heartbeat` (+ new second-missed-tick step) |
| Immediate ping + lifecycle re-pings | `test_presence_heartbeat_lifecycle`; JS harness lines 510–517 |
| pagehide `bye=1` beacon + per-page id | `test_presence_heartbeat_lifecycle`; JS harness; `test_presence_and_client_state` |
| Stale lease expires | `test_presence_survives_throttled_heartbeat` final step |
| Narrow diff | `git diff` — 3 files, +13/−6 lines |

---

## 5. Residual Risks & Notes

- **Fresh-server confirm window**: `_CONFIRM_SEC = 2.0` is shorter than the new 15 s cadence. When the dashboard server restarts while an old tab is still open, `_auto_open` may check `_client_open` before that tab's next scheduled beat — the lifecycle report flagged this and suggested raising `_CONFIRM_SEC`, but the contract excluded it. Mitigations already in place: tabs re-ping immediately on `focus`/`visibilitychange`/`online`/`pageshow`, and a fetched `/api/overview` or user action also lands within the window only opportunistically — so a rare duplicate `webbrowser.open` remains possible (bounded by `_OPEN_COOLDOWN_SEC`). This is the same trade-off the explorer accepted for Slice 1.
- **Crash/kill lingering**: a tab killed without `pagehide` (browser crash, `SIGKILL`, OS sleep) keeps its lease up to 180 s (was 150 s), delaying a reopen decision by ≤30 s extra. Bounded and one-directional.
- **Boundary precision**: the "two missed wake-ups" guarantee is exact at 180.0 s of measured monotonic gap; real timers fire late, not early, so the practical safe margin is one missed wake-up plus ~60 s jitter — which is the stated design target.
- **Test scope**: Tier 1 only per contract; `tests/test_dashboard_open.py` was not re-run (untouched by this slice — it belongs to the accepted durable-launch changes).

---

## 6. Do-Not-Touch Compliance

- No commits made; nothing staged.
- `_CONFIRM_SEC`, `idle_exit_due`, `stall_timeout_sec`, overview/events poll cadences, task ownership, launcher/WMI broker, adapters, docs — unchanged.
- Pre-existing uncommitted changes in `src/agent_bridge/dashboard.py`, `src/agent_bridge/adapters/antigravity.py`, `tests/fake_agy.py`, `tests/test_agy_dashboard.py`, `tests/test_agy_parse.py`, `tests/test_dashboard_open.py`, and existing reports — untouched.
- `tests/dashboard_status_behavior.js` required no edits (it stubs `setInterval` and asserts lifecycle semantics only); its assertions all pass against the new cadence.

---

*Report compiled and verified by Worker-Dashboard-Heartbeat on 2026-09-18.*
