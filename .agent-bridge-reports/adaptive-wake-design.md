# Adaptive wake design: Astra dispatches, sleeps, and is woken to check Agent Bridge

## Scope

Design only — no production changes. The desired coordinator flow is:

1. Astra (a Codex app thread) dispatches a worker through Agent Bridge.
2. Astra estimates a next wake time from task complexity, ends its turn, and sleeps with no polling and no model activity.
3. At that time the Codex app wakes the **same thread** automatically; Astra calls `check_task`.
4. If the task is still running, Astra computes a new interval and reschedules the wake.
5. When the task is terminal, Astra retrieves the result and verifies it.

## Feasibility verdict

**The whole loop is achievable today on this machine, without any Agent Bridge or Codex code change**, using the Codex app's native thread-heartbeat automation plus the existing Bridge read tools. The pieces that exist:

- `codex_app.automation_update` is a per-thread dynamic tool whose schema already exposes `kind = "heartbeat"`, `destination = "thread"`, `targetThreadId`, minute-grain `rrule` (`FREQ=MINUTELY;INTERVAL=n`), `mode` (`view|create|update|delete|suggested_create|suggested_update`), and `status` (`ACTIVE|PAUSED`). Observed verbatim in `C:\Users\Touma\.codex\state_5.sqlite`, table `thread_dynamic_tools` (dozens of threads carry it; `defer_loading = 0` in every sampled row).
- Official docs confirm the semantics: "Thread automations are heartbeat-style recurring wake-up calls attached to the current thread … minute-based intervals for active follow-up loops" — each run is a new turn inside the existing thread, so context (the task_id, the plan) is preserved across sleeps (<https://developers.openai.com/codex/app/automations>, section "Thread automations").
- Bridge side needs nothing new: `dispatch_task` returns immediately while `_run_task` runs in the background (`src/agent_bridge/registry.py:1319`, `registry.py:1329-1471`); `check_task` is a non-blocking snapshot (`registry.py:2045-2052`, `server.py:165-171`); `get_result` pages the result artifact (`registry.py:2054-2115`).

**What cannot be done today** (would need Codex host changes, not Bridge changes):

- **Event-driven wake at completion.** No mechanism exists that fires exactly when the task finishes. Bridge explicitly documents "Bridge cannot wake a dormant coordinator at completion — that needs a host scheduler or notification mechanism" (`SETUP.md:375`, `ORCHESTRATION.md:61`). A heartbeat approximates this by waking near the estimated finish, not at it.
- **A true one-shot `wake_at`.** The heartbeat contract is a recurring RRULE. One-shot behavior must be emulated by deleting or updating the automation after it fires. Whether `RRULE:...;COUNT=1` or `UNTIL=` is honored is unverified — do not rely on it (upstream issue openai/codex#32609 reports a one-time request was persisted as a daily schedule).
- **Sub-minute wake precision** — RRULE granularity is minutes.
- **Guaranteed fire while the app is closed / machine asleep.** Docs: "Keep the computer on and the app running." Issue #32609 shows a missed occurrence is skipped, not caught up — the scheduler advances to the next occurrence.
- **A guaranteed-present `automation_update` tool.** Issue openai/codex#29128 reports the tool is inconsistently exposed across threads and may need lazy-loading via `tool_search`; issue #32609 reports it absent from the manifest entirely on some builds. The current build's stored schema does include heartbeat fields (the cron-only regression of issue #35601 is not present here), but per-thread availability must be verified at runtime before relying on it.

So: adaptive-wake **is** feasible as a schedule-driven approximation; an exact completion wake is not.

## What exists today (evidence)

### Agent Bridge

- **Dispatch is fire-and-forget.** `dispatch_task` creates `Task(status=queued)`, persists it, and spawns `self._bg[task_id] = asyncio.create_task(self._run_task(...))` (`registry.py:1294-1319`). `_run_task` awaits `adapter.run_turn` and writes the result artifact, usage, and terminal status in its `finally` (`registry.py:1352-1457`). A caller may never call `wait_task` and the task still completes.
- **`wait_task` is a passive observation call.** Local tasks park one coroutine on `asyncio.Event` until `_done.set()` or the caller's `timeout_sec` (`registry.py:2031-2043`); `DEFAULT_WAIT_SEC = 180` (`models.py:35`). Sibling-owned tasks poll `state.json` every `REMOTE_TASK_POLL_SEC = 1.5` inside the Bridge process (`registry.py:128`, `registry.py:1704-1744`). Neither costs a coordinator turn while pending; only the returned timeout does. Host ceiling here: `[mcp_servers.agent_bridge] tool_timeout_sec = 1260` in `C:\Users\Touma\.codex\config.toml:202-205`.
- **`check_task` already returns everything the adaptive algorithm needs** (`_task_snapshot`, `registry.py:1951-2029`): `status`, `stop_reason`, `error`, `error_kind`, `retryable`, `elapsed_sec` (2005-2010), `recent_activity` (1986, last ~5 transcript summaries via `transcript.py:258-273`), `silent_for_sec` (2022-2026, `worker_silence_sec` at `transcript.py:95-101`), `stall_timeout_sec` (2028), `paused`/`resumable`/`resume_hint`, `remote`/`owner`/`owner_lost` (remote rows: `registry.py:1658-1702`).
- **Durable state.** `~/.agent-bridge/state.json` holds `sessions[]` + `tasks[]` (`paths.py:46-47`; `Task` fields at `models.py:110-151`; `Session` at `models.py:86-107`). Writes are atomic with a merge that preserves live siblings' rows (`registry.py:340-399`, `persist.py:33-47`). Results live in `results/<task_id>.txt` (`paths.py:69-70`), transcripts in `transcripts/<session_id>.jsonl` (`paths.py:65-66`). Retention: `TASK_KEEP_PER_SESSION = 20`, `TASK_KEEP_TOTAL = 200`, sessions retained `SESSION_RETAIN_SEC = 14d` (`registry.py:102-108`) — enough history for duration statistics.
- **Restart / remote owner.** On `start()`, rows owned by dead instances are adopted; still-queued/running ones finalize `failed`/`bridge_restarted` (paused ones end `cancelled`/`paused`) (`registry.py:904-947`). Live siblings' rows surface as `remote: true` with `owner{pid,create_time,alive}` and `owner_lost` when the owner died mid-run (`registry.py:1631-1702`, `1746-1825`). `cancel_task`/`pause_task` are rejected on live siblings; `resume_task` adopts dead-owner rows or routes to a live sibling through the outbox (`server.py:229-237`, `registry.py:2254-2288`).
- **Lifecycle.** `idle_exit_sec = 7200` self-exit is suppressed while any task is queued/running (`config.py:132`, `registry.py:434-463`); `shutdown_policy = "cancel"|"linger"` bounds orderly-close behavior (`config.py:119-135`, `registry.py:985-1057`). Stall watchdog: `stall_timeout_sec` default 1800 per worker (`config.py:98`), checked every `STALL_POLL_SEC = 30` (`registry.py:110`, `1473-1515`).
- **Dedup.** `dispatch_task`/`resume_task` accept a UUID `request_id`; identical replays return `reused=true` within the same Bridge instance (`registry.py:1193-1197`, `1230-1243`). Lost on restart — not exactly-once.

### Codex app (this machine)

- `codex_app.automation_update` full schema (stored per thread in `state_5.sqlite` `thread_dynamic_tools`): `mode`, `kind` (`cron`|`heartbeat`), `name`, `prompt` ("self-sufficient… do not include schedule, workspace, or thread details"), `rrule` ("Heartbeat automations attached to a thread can use minute-based intervals such as `FREQ=MINUTELY;INTERVAL=30` or daily/weekly wall-clock schedules"), `destination` ("Use `thread` for heartbeat automations attached to the current local thread"), `targetThreadId`, `status`; cron-only fields `cwds`, `executionEnvironment`, `localEnvironmentConfigPath`, `model`, `reasoningEffort`. Description guidance: "Prefer heartbeats for requests to continue this thread later, especially below one hour." "Prefer updating an existing automation over creating a duplicate."
- Automation definitions persist under `$CODEX_HOME/automations/*/automation.toml` (per the tool description; directory absent here — none created yet). A persisted heartbeat row looks like `kind="heartbeat"`, `status="ACTIVE"`, `rrule="FREQ=…"`, `target_thread_id="…"` (issue #23370 example).
- Other relevant `codex_app` tools in the same store: `send_message_to_thread(threadId, prompt, model?, thinking?)`, `create_thread`, `read_thread`, `list_threads`, `set_thread_archived`, `set_thread_pinned`, `set_thread_title`.
- Follow-ups into a busy thread queue (`queue_1.sqlite` `queued_items`; `[desktop] followUpQueueMode = "queue"` at `config.toml:159`). Turn-end OS notification hook exists (`notify = [… "turn-ended"]` at `config.toml:28`). A built-in `sleep` tool call exists but caps at ~60 s per call and keeps the turn alive (20 `sleep` items with `durationMs: 60000` in `thread_history_1.sqlite`) — it is not a substitute for a heartbeat.
- Known upstream caveats to design around: #23370 (a daily RRULE with multiple `BYHOUR`/`BYMINUTE` entries fires only the first same-day occurrence — use `FREQ=MINUTELY;INTERVAL=n`, not multi-occurrence daily rules); #32609 (file-created heartbeats display but may not run — always create via the tool, never by writing `automation.toml` by hand; the tool description also forbids that); #29128/#35601 (tool exposure/schema drift across builds — verify presence before use).

## Design

### State machine

Coordinator-side watch states (the Bridge `Task.status` machine — `queued|running|completed|failed|cancelled`, `models.py:18-34` — stays untouched; this machine wraps it):

```text
DISPATCHED ──create heartbeat──▶ WATCHING
                                   │  heartbeat fires (new turn in thread)
                                   ▼
                                CHECKING ──check_task──┐
                                   │                   │
        ┌──────────────────────────┼───────────────────┤
        │ running/queued           │ terminal          │ watch budget exhausted
        ▼                          ▼                   ▼
   RESCHEDULE                 COMPLETING            ESCALATE
   (automation_update          (get_result + verify;  (delete automation,
    mode=update, new rrule)     delete automation)     tell user)
        │
        └──▶ WATCHING
```

`COMPLETING` is the only state that does verification work. Every other transition is one `check_task` plus at most one `automation_update`.

### Durable data fields

Two stores, by ownership:

**Codex side (already durable — the automation itself):** the `automation.toml` holds `id`, `name`, `prompt`, `rrule` (the effective `wake_at` is the scheduler's next occurrence of that rule), `status`, `target_thread_id`, and the app tracks `next_run_at` internally (visible in the Scheduled UI per #23370). The **prompt is the durable watch record**: it must be self-sufficient per the tool contract, so embed the watch spec in it:

```text
Bridge watch: task_id=<task_xxx>, session_id=<sess_xxx>, agent=<worker>,
dispatched_at=<iso>, attempt=<k>, policy_rev=<r>,
min_min=<m>, max_min=<M>, deadline=<iso>, max_attempts=<n>.
On each wake: call check_task(task_id). If terminal → get_result, verify,
then automation_update(mode=delete, id=<auto_id>) and report. If still
running → compute next interval per policy, automation_update(mode=update,
id=<auto_id>, rrule=…, prompt=<this text with attempt=k+1>), end turn.
If owner_lost or failed/retryable → resume_task or escalate to the user.
If task unknown/pruned or now > deadline or attempt ≥ max_attempts →
delete the automation and report the unfinished state.
```

That gives `task_id`, thread identity (implicit — the heartbeat is bound to the thread; `targetThreadId` only if watching from a different thread), automation id, attempt counter, policy revision, and the terminal/deadline conditions — all surviving app restarts because they live in the automation file, not in conversation memory.

**Bridge side (already durable, no changes):** `task_id`, `session_id`, `status`, `stop_reason`, `error`/`error_kind`, `started_at`/`finished_at`/`elapsed_sec`, `silent_for_sec`, `recent_activity`, `owner`/`remote`/`owner_lost`, `paused`/`resumable`, `resumed_by`/`resume_of` for continuation chains, `usage`/`run_usage`.

Optionally a Bridge-persisted watch row (`watch_id → {task_id, policy, attempt, next_wake_at}`) could replace prompt-carried state — see "implementation slices"; it is convenient but not required, and Bridge cannot act on `next_wake_at` itself.

### Wake-interval algorithm

Let `elapsed` = `check_task.elapsed_sec`, `silent` = `silent_for_sec` (null for remote rows), `stall` = `stall_timeout_sec`.

**Initial estimate `E0`:** classify the dispatched work into a coarse bucket and pick a base expectation, optionally adjusted by worker/model:

| complexity | `E0`    | typical use                        |
| ---------- | ------- | ---------------------------------- |
| trivial    | 3 min   | single-file read/small edit        |
| small      | 8 min   | one-file change + quick test       |
| medium     | 20 min  | multi-file feature, test loop      |
| large      | 60 min  | port, breadth research, big builds |

Refinements: multiply by ~1.5 for heavyweight models/effort (`xhigh`, `max`) and ~0.7 for light ones; when Bridge-side history is available (slice B below), replace `E0` with the P60 of `finished_at − started_at` for the same `(agent, complexity-bucket)` and cap it at the P90 — `state.json` retains enough terminal rows for that.

**At wake `k` (task still running):**

```text
overruns      = number of wakes where elapsed > current estimate
if silent is not null and stall > 0 and silent > 0.7 * stall:
    next = MIN_I                      # worker near its silence budget; watch closely
elif elapsed < E_k:
    next = (E_k - elapsed) * 1.15     # aim just past expected finish; early wakes waste turns
else:                                 # estimate missed — geometric backoff on the interval
    next = I_{k-1} * 1.6              # e.g. 10 → 16 → 26 → 41 → 60(cap)
    E_{k+1} = elapsed * 1.5           # re-anchor the estimate
next = clamp(next, MIN_I, MAX_I) + jitter
```

- `MIN_I = 5 min` — every wake costs a full coordinator turn; below this a `wait_task` loop is comparable in cost and simpler. `MAX_I = 60 min` — bounds completion-detection lag and stays inside the "especially below one hour" heartbeats the tool description steers toward.
- `jitter` = uniform `±15%` rounded to whole minutes — prevents the watch cadence from phase-locking with other automations.
- `deadline` = e.g. `dispatched_at + 4 h` and `max_attempts ≈ 20`: hard stop that deletes the automation and escalates, so a wedged worker can never wake the thread forever. Also schedule `MIN_I` when `recent_activity` shows the worker reporting "finalizing"-style output — cheap optional refinement.
- A simpler v0: fixed `FREQ=MINUTELY;INTERVAL=15` heartbeat with no per-wake `automation_update`. Fewer moving parts, more turns. The adaptive version above only adds one `automation_update` call per wake.

The RRULE is rewritten each wake (`mode=update`, `rrule="FREQ=MINUTELY;INTERVAL=<next_min>"`), so "next wake at T" is expressed as "next occurrence in ~`next` minutes." That is the only mechanism — do not try to encode an absolute `wake_at` via `BYHOUR`/`BYMINUTE` lists (#23370 bug).

### Race and idempotency handling

| Race | Handling |
| --- | --- |
| Task completes before the first wake | First `check_task` sees `completed` → `get_result`, verify, delete automation. Normal path, no special case. |
| Wake fires while a user/assistant turn is running | The heartbeat lands as a queued follow-up (`followUpQueueMode="queue"`, `config.toml:159`; `queued_items` table). The wake prompt must be idempotent and cheap — it only reads status and reschedules. |
| Duplicate/overlapping heartbeat runs | Each run is a fresh turn that re-reads `check_task` and acts on *current* state only; `attempt`/`policy_rev` in the prompt makes stale-prompt updates visible. If two updates race, both write the same automation id — last write wins, both are semantically equivalent (recurring minute rule). |
| App/scheduler misses an occurrence (app closed, machine asleep) | Missed beats are skipped, not caught up (#32609). Because the rule is recurring, the next occurrence still fires — worst case adds `interval` minutes of lag. Deadline/`max_attempts` bound total watch time. |
| Bridge (coordinator's MCP server) restarted | Task row persists in `state.json`. If the *owning* Bridge instance died, `check_task` reports `remote:true` + `owner_lost:true` (`registry.py:1675-1680`); the wake should `resume_task` (adopts the session, reaps the orphan, continues the conversation as a new task — update `task_id` in the prompt) or escalate. If a *sibling* owns it, keep watching read-only — never cancel/re-dispatch (`ORCHESTRATION.md:76`). |
| Retryable failure | `status=failed` + `error_kind="transient_provider"` + `retryable=true` → `resume_task` (same conversation), update `task_id`/`attempt` in the prompt, keep watching. `quota`/`config` kinds → delete automation, report. |
| Cancellation / pause | `cancelled` or `paused`/`resumable` → delete automation, report the partial state (`stop_reason` distinguishes `paused` from `cancelled`). |
| Task pruned / `unknown task` | Terminal rows are pruned past `TASK_KEEP_*` (`registry.py:104-108`); an `unknown task` answer → delete automation, check `results/<task_id>.txt` via `get_result` fallback, report. |
| Dispatch replay | Only the initial `dispatch_task` needs `request_id` dedup (`registry.py:1230-1243`); the watch loop itself never re-dispatches except deliberate `resume_task`, which has its own `resumed_by` dedup (`server.py:230`). |
| User says "stop watching" mid-sleep | `automation_update(mode=delete)` or `status=PAUSED`; the next beat is either gone or a no-op that re-deletes. |

### Token behavior

| Moment | Astra (coordinator) invoked? | Cost |
| --- | --- | --- |
| `dispatch_task` + create heartbeat | Yes — one turn | dispatch call + one `automation_update` |
| Sleeping between wakes | **No** — thread is idle; no pending MCP call, no sampling | zero coordinator tokens; worker tokens accrue independently in `run_usage` (`models.py:140-143`, `usage.py:1-43`) |
| Each heartbeat fire | Yes — one turn in the same thread | small: injected prompt + `check_task` (+ `automation_update` when rescheduling) |
| Completion wake | Yes | `get_result` pages + verification tool calls — same as today |
| Bridge internals during the wait | No Astra involvement | `_run_task`, stall watch, state flushes are all in-process (`registry.py:1329-1515`) |

Versus the `wait_task` poll loop: a 30-minute task at `timeout_sec=180` costs ~10 returned-timeout turns; the adaptive watch costs ~1-3 wake turns. Note each wake still resubmits the thread's context as input tokens — fewer, longer-spaced wakes are cheaper, and very long watches benefit from keeping the thread small or letting compaction run.

## Minimal implementation slices

**Slice A — coordinator-side only (docs/skill, zero Bridge code).** Add a "background watch" pattern to `ORCHESTRATION.md` and `skills/agent-bridge/SKILL.md`: the prompt template above, the interval table, and the delete-on-terminal rule. This alone delivers the flow on the current Codex build. Test: a `test_docs.py`-style check that the documented fields (`attempt`, `deadline`, `task_id`) appear in the template, plus a manual E2E note (creating a real heartbeat requires the desktop host — call it human-verified, not automated, per `skills/add-coordinator/SKILL.md:28`).

**Slice B — optional Bridge read tool `plan_check` (or a `suggested_next_check` block inside `check_task`).** A pure function `next_check_interval(elapsed, estimate, silent, stall, overruns) -> {interval_sec, rrule, reasons}` plus historical percentiles computed from terminal rows in `state.json`. Value: centralizes the policy so every coordinator computes the same interval, and only Bridge can see cross-session duration history. It must be documented as *advisory computation* — it schedules nothing by itself. Tests (pure, no host): bounds clamp, jitter range, `elapsed > E` backoff growth, near-stall shortcut, deadline/max-attempts escalation, percentile computation over fixture rows.

**Slice C — Codex host (upstream, out of this repo's control).** Verified one-shot RRULE semantics, a wake-on-completion event, and consistent `automation_update` exposure. Nice-to-have; not required for the design to work.

**Recommendation on `schedule_check`:** do **not** build a Bridge tool that schedules a wake — Bridge cannot reach into the host to fire one, and a tool named `schedule_check` that only stores state would imply a capability that does not exist (the same reasoning as the rejected `subscribe_task` in `.agent-bridge-reports/wait-90m.md`). If anything, ship Slice B's read-only `plan_check`/`suggested_next_check_sec` — the durable record belongs in the heartbeat automation (or stays computable statelessly from `check_task`), and the timer belongs to Codex.

## Security, lifecycle, notification

- **Prompt contents:** the automation prompt is durable and visible in the Scheduled UI — embed ids/policy only, never secrets, paths with sensitive names, or instructions that let a hijacked run do harm ("only read status, reschedule, or report").
- **Permissions:** heartbeat runs execute in the thread unattended; this machine runs `approval_policy = "never"` and `sandbox_mode = "danger-full-access"` (`config.toml:4-6`), so a wake turn can act without asking — keep the wake prompt read-only-by-construction (`check_task`, `automation_update`, `get_result` only).
- **Lifecycle:** always delete the automation on terminal task states, deadline, or user stop; `PAUSED` for user-initiated suspend. Orphan risk: if the thread is archived/deleted, the automation may keep firing into a dead target — the `deadline` + `max_attempts` bound is the backstop. Heartbeat files survive app restart; missed beats are skipped (self-healing via recurrence).
- **Notification:** the completion wake ends with a normal agent message in the thread; the configured `notify` turn-ended hook (`config.toml:28`) can surface it at OS level. Standalone cron runs report into the Scheduled inbox; thread heartbeats report inside the thread instead.
- **Cross-thread targeting:** prefer `destination="thread"` (current thread). `targetThreadId` can point a heartbeat at another local thread — legitimate for a dedicated "watcher" thread, but it widens the surface; don't use it by default.

## Verification needed before implementation (unverified claims to test)

1. `automation_update` is present in the coordinator thread's tool manifest (lazy-load via `tool_search` if not; #29128).
2. Minimum accepted heartbeat `INTERVAL` (docs example is 30; no floor found in the schema).
3. `RRULE` prefix requirement and whether `COUNT`/`UNTIL` are honored — assume not; use update/delete.
4. Exact wake-prompt delivery shape in `thread_items` (no local sample exists; expected: the automation prompt as a new turn input, per docs and the `TurnInput` submission pattern in `logs_2.sqlite`).
5. Behavior of a heartbeat that fires while the target thread is mid-turn under `followUpQueueMode="queue"` (expected: queued; verify).
