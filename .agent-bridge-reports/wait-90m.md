# `wait_task` and 90-minute waits

## Scope and verdict

This report traces Agent Bridge's `wait_task` path, the MCP/host boundary, Codex yield behavior, task-completion wakes, and the feasible ways to let a worker run for up to 90 minutes without repeatedly waking the coordinator model. No production code was changed.

### Bottom line

- **Agent Bridge itself has no 90-minute `wait_task` cap.** `wait_task(timeout_sec=5400)` is accepted by the Python signature and the registry simply passes it to `asyncio.wait_for` for local tasks.
- **A pending Bridge `wait_task` does not poll the coordinator model.** For a locally owned task it parks one server coroutine on an `asyncio.Event`; for a sibling-owned task it polls `state.json` every 1.5 seconds inside the Bridge process. Neither is a coordinator/model turn.
- **A worker can already keep running for 90 minutes without a pending `wait_task`.** `dispatch_task` starts an independent `_run_task` background task and returns immediately. The worker keeps running while the owning Bridge process survives and the task is not cancelled or marked stalled.
- **A single 90-minute MCP request is host-limited.** Bridge can hold it, but the host must permit a tool call longer than 5400 seconds and must keep the stdio server/process alive. The repository's current Codex setup guidance uses `tool_timeout_sec = 600`, so `timeout_sec=5400` will normally be cut off by the host long before Bridge's timer expires.
- **A `wait_task` timeout is not a worker timeout.** `timed_out=True` means this observation request ended; the task continues. A host-side MCP cancellation likewise cuts the waiter, not the `_run_task` background task, unless the host kills the Bridge process tree.
- **The prior “outer execution yields every ~60 seconds while the model need not wake” claim is not supported as a general rule.** In Agent Bridge, the obvious 60-second loop is the idle-exit watchdog. In external Codex source, the Code Mode default exec/wait yield is 10,000 ms; ordinary unified `exec_command` has a 30,000 ms initial-yield cap in the inspected source. A yield is a model-visible tool boundary in those paths, not an invisible 60-second tick.

## End-to-end wait path

### 1. Coordinator invokes the MCP tool

The server entry point runs the MCP server over stdio:

- `src/agent_bridge/cli.py:209-217` starts `mcp.run(transport="stdio")`.
- `src/agent_bridge/server.py:24-33` creates the `Registry` in the MCP lifespan and calls `registry.stop()` when the transport/lifespan ends.
- `src/agent_bridge/server.py:80-85` resolves the lifespan registry and calls `touch_activity()` for each MCP request.

`wait_task` is only a forwarding wrapper:

- `src/agent_bridge/server.py:155-162`
- Signature: `wait_task(ctx, task_id, timeout_sec=DEFAULT_WAIT_SEC)`.
- It calls `Registry.wait_task(...)`, wraps the returned snapshot as `{ok: true, ...}`, and returns exceptions as `{ok: false, ...}`.
- Its docstring already says that timeout is not failure and that the caller should stay below the host MCP tool timeout.

The default is:

- `src/agent_bridge/models.py:34-35`: `DEFAULT_WAIT_SEC = 180.0`.

There is no explicit `timeout_sec` upper bound in the wrapper or registry.

### 2. Dispatch starts independent work

`dispatch_task` is explicitly documented as returning immediately:

- `src/agent_bridge/server.py:124-152`, especially the docstring at `server.py:137`.

The registry launches the turn as an independent asyncio background task:

- `src/agent_bridge/registry.py:1302`: `self._bg[task.task_id] = asyncio.create_task(self._run_task(...))`.
- `src/agent_bridge/registry.py:1312-1335`: `_run_task` marks the task running, snapshots the workspace, creates the stall watcher when enabled, then awaits `adapter.run_turn(session, task)`.

That `_bg` task is not owned by `wait_task`. A caller may dispatch and never call `wait_task`; completion still writes state and the result artifact.

### 3. Local `wait_task` parks on an event

For a task owned by this Bridge instance:

- `src/agent_bridge/registry.py:1983-1995`
  - finds the task;
  - obtains/creates `self._done[task_id]`;
  - while nonterminal, awaits `asyncio.wait_for(event.wait(), timeout=timeout_sec)`;
  - returns `{timed_out: true, ...snapshot}` on timeout;
  - returns `{timed_out: false, ...snapshot with result}` after the event or if the task was already terminal.

The event itself is initialized in:

- `src/agent_bridge/registry.py:268`: `self._done: dict[str, asyncio.Event] = {}`.

This is event-driven, not a 60-second polling loop. The Bridge event loop may perform unrelated work, but the `wait_task` coroutine has no periodic wake until the event fires or the timeout expires.

### 4. Worker completion signals the waiter

At the end of `_run_task`, regardless of success/failure/cancellation:

- `src/agent_bridge/registry.py:1335-1389`: the adapter result is persisted into result text/artifact, file changes, usage, observed model/effort, and terminal status.
- `src/agent_bridge/registry.py:1414-1435`: the finally block cancels the stall watcher, stamps `finished_at`, flushes the session transcript, calls `self._done[task_id].set()`, removes the background task, and saves state.
- `src/agent_bridge/registry.py:1437-1450`: logs final status and schedules idle unload.

So task completion wakes the server-side `wait_task` coroutine immediately. It does **not** create a coordinator model turn by itself. It only lets the pending MCP response return to the host.

### 5. Sibling-owned tasks use disk polling, not model polling

If the task is not in this instance's `self.tasks`, `wait_task` delegates to `_wait_remote_task`:

- `src/agent_bridge/registry.py:1983-1986`.
- `src/agent_bridge/registry.py:1683-1723` implements the remote wait.

Remote behavior:

- `REMOTE_TASK_POLL_SEC = 1.5` at `src/agent_bridge/registry.py:118-119`.
- `_remote_task` reads task rows from shared `state.json` and validates ownership/safe IDs at `registry.py:1603-1635`.
- `_wait_remote_task` returns early when:
  - the remote row reaches a terminal status,
  - the recorded owner process is no longer alive,
  - the result artifact exists after the row vanished,
  - or the caller timeout expires.
- It deliberately requires the row's own terminal state rather than treating a just-written artifact as complete; see `registry.py:1699-1704`.

Those 1.5-second wakes are inside the Bridge server process. They do not send MCP messages and do not wake the coordinator model.

Tests covering this behavior include:

- `tests/test_lifecycle.py:21-108`: sibling-owned task remains read-only, can be followed to completion, and is never executed by the observer.
- `tests/test_lifecycle.py:111-160`: dead owner's queued/running row reports `owner_lost` and resolves immediately.
- `tests/test_lifecycle.py:163-194`: `remote_tasks=false` restores local-only behavior.

### 6. Worker adapters remain active while the waiter sleeps

The worker is driven by the adapter inside `_run_task`, independently of whether anyone is waiting.

Representative paths:

- ACP workers: `src/agent_bridge/adapters/acp.py:1419-1540`
  - sends `live.conn.prompt(...)` at `acp.py:1451-1455`;
  - awaits `live.prompt_task` with no Bridge-side prompt timeout at `acp.py:1456-1457`;
  - streams session updates through `_BridgeClient.session_update` at `acp.py:515-580`.
- Codex worker: `src/agent_bridge/adapters/codex.py:99-242`
  - starts `codex exec --json ...` at `codex.py:119-128`;
  - reads JSONL stdout until EOF at `codex.py:148-181`;
  - waits briefly for process exit after stdout closes at `codex.py:182-187`.
- Antigravity worker: `src/agent_bridge/adapters/antigravity.py:278-400`
  - starts the one-shot process at `antigravity.py:295-304`;
  - reads stream-json stdout at `antigravity.py:330-381`;
  - default `--print-timeout` comes from `AgentConfig.print_timeout = "120m"` at `src/agent_bridge/config.py:98-99` and is passed at `antigravity.py:221-230`.

These details matter because “the worker may run 90 minutes” is separate from “the coordinator may hold one 90-minute MCP request.” Bridge can support the former already; the latter is controlled by the host.

## What wakes, and what consumes tokens

These are different layers:

| Event | Bridge process | MCP transport | Coordinator/model |
| --- | --- | --- | --- |
| Local `wait_task` pending | One coroutine waits on `asyncio.Event` | One JSON-RPC tool request remains pending | No periodic wake |
| Remote `wait_task` pending | Coroutine polls shared disk every 1.5 s | One request remains pending | No periodic wake |
| Worker emits output | Adapter appends transcript activity | No MCP response is sent | No coordinator wake |
| Task reaches terminal state | `_done.set()` wakes local waiters | Pending `wait_task` response returns | Host/model resumes to process the tool result |
| `timeout_sec` expires | `wait_task` returns `timed_out=True` | Tool response returns | Host/model gets a new decision point |
| Host MCP timeout/cancel fires | Waiter request is cancelled; worker `_bg` is independent | Tool call fails/cancelled | Host/model sees an error/cancellation |
| Bridge idle watchdog | Sleeps/checks every 60 s | No request | No model involvement |
| Bridge stall watchdog | Checks worker silence, cancels on limit | Completion/failure response only if a waiter is pending | No wake until response |

### Token implications

- Bridge does not measure or generate coordinator-model tokens. `Task.usage` and `Task.run_usage` are worker/run accounting fields (`src/agent_bridge/models.py:140-143`), not coordinator tokens.
- A pending direct MCP call does not require the model to sample anything while it is pending. The provider/host may keep process/transport resources open, but there is no per-second model inference inherent in `wait_task`.
- Each returned `timed_out=True` is model-visible. The coordinator then has to decide whether to call `wait_task` again. Those decisions can consume coordinator turns/context even if each tool payload is small.
- If the coordinator instead dispatches and stops touching the task, Bridge and the worker can continue with no extra coordinator `wait_task` calls. The limitation is that Agent Bridge currently has no host-side completion push that is guaranteed to wake the model later.

## Where the 60-second claim went wrong

The claim conflates at least three different intervals and layers.

### Agent Bridge's actual 60-second intervals

The clear 60-second loop in this repository is the abandoned-server watchdog:

- `src/agent_bridge/registry.py:435-452`: `await asyncio.sleep(60)`, then check `idle_exit_due()`.
- `idle_exit_due()` at `registry.py:425-433` exits only when there has been no MCP activity for `idle_exit_sec` **and** no queued/running tasks.
- Default `idle_exit_sec = 7200` at `src/agent_bridge/config.py:132` and `agents.toml:92-105`.

There are also unrelated 60-second constants for outbox stale-claim/sweep behavior at `registry.py:112-117`. None is a `wait_task` model-yield interval.

### Codex Code Mode evidence

External Codex source inspected during this investigation shows a different value:

- `codex-rs/code-mode/src/runtime/mod.rs` at commit `53b50197`: `DEFAULT_EXEC_YIELD_TIME_MS = 10_000` and `DEFAULT_WAIT_YIELD_TIME_MS = 10_000` (`https://github.com/openai/codex/blob/53b50197/codex-rs/code-mode/src/runtime/mod.rs`).
- `codex-rs/code-mode/src/service.rs` at commit `31519549`: `execute()` uses `request.yield_time_ms.unwrap_or(DEFAULT_EXEC_YIELD_TIME_MS)` and `wait()` passes the caller-provided `yield_time_ms` into `ObserveMode::YieldAfter` (`https://github.com/openai/codex/blob/31519549/codex-rs/code-mode/src/service.rs`).
- `codex-rs/core/src/tools/code_mode/wait_handler.rs` at commit `914c8eeb`: `wait` deserializes `yield_time_ms`, defaulting to `DEFAULT_WAIT_YIELD_TIME_MS`, and sends it to the Code Mode service (`https://github.com/openai/codex/blob/914c8eeb/codex-rs/core/src/tools/code_mode/wait_handler.rs`).
- `wait_spec.rs` describes `yield_time_ms` as the time before yielding again and exposes it as an ordinary model-controlled number (`https://github.com/openai/codex/blob/914c8eeb/codex-rs/core/src/tools/code_mode/wait_spec.rs`).

Separately, Codex unified shell execution has its own yield behavior:

- `codex-rs/core/src/unified_exec/mod.rs` exposes `MIN_YIELD_TIME_MS = 250` and `MAX_YIELD_TIME_MS = 30_000` in the inspected revisions (`https://github.com/openai/codex/blob/0a0caa9d/codex-rs/core/src/unified_exec/mod.rs`).
- Open issue `openai/codex#22541` reports an initial `exec_command` yield capped around 30 seconds even when the model requested 60 seconds (`https://github.com/openai/codex/issues/22541`).

Therefore:

- “~60 seconds” is not a universal Codex yield.
- It may have come from host-specific historical behavior, a requested `yield_time_ms`, or the fact that a nested wait/tool happened to return after roughly a minute.
- In Code Mode, each yield produces model-visible output and gives the model another decision point. It is not an invisible runtime heartbeat.
- In a direct MCP call, there is no Code Mode cell/yield at all; the tool request is simply pending until response, cancellation, transport failure, or host timeout.

### Codex Code Mode can turn a long MCP call into a model loop

Codex source and issue evidence show that when MCP tools are routed through Code Mode:

- The model calls `exec`, not the MCP tool directly.
- The nested MCP call is invoked through the normal tool runtime from Code Mode (`call_nested_tool` in `codex-rs/core/src/tools/code_mode/mod.rs`; search result/source `https://github.com/openai/codex/blob/914c8eeb/codex-rs/core/src/tools/code_mode/mod.rs`).
- The outer `exec` can yield while the nested MCP call is still running.
- The model then has to call `wait` for the yielded cell.
- Every `wait` is another model-visible tool boundary.

Open issue `openai/codex#29122` reports exactly this failure mode for long-running MCP calls and discusses `features.code_mode.direct_only_tool_namespaces` as a mitigation (`https://github.com/openai/codex/issues/29122`). Open issue `openai/codex#35108` describes a nested `wait_agent` inside Code Mode producing repeated parent/model boundaries (`https://github.com/openai/codex/issues/35108`). Those are external reports, but the inspected Codex source supports the mechanism.

The relevant config exists upstream:

- `CodeModeConfigToml.default_exec_yield_time_ms` and `direct_only_tool_namespaces` in `codex-rs/features/src/feature_configs.rs` at commit `2230d644` (`https://github.com/openai/codex/blob/2230d644/codex-rs/features/src/feature_configs.rs`).
- `apply_mcp_tool_exposure_policy` removes deferred/Code Mode exposure for namespaces in `direct_only_tool_namespaces` (`https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/spec_plan.rs`).

That mitigation is client-side, exact-name dependent, and version-sensitive. It is not an MCP server-controlled contract.

## Can we truly wait 90 minutes without coordinator wakes?

There are four different questions.

### 1. Can the worker run for 90 minutes?

**Yes, in the successful path, if all of these hold:**

- the worker CLI/provider does not impose its own shorter turn limit;
- the worker produces output often enough to stay under `stall_timeout_sec`, or that limit is raised/disabled;
- the owning Bridge process and worker process tree survive;
- no `cancel_task`, host force-kill, or Bridge shutdown policy cancels it.

The stall boundary is independent of `wait_task`:

- `AgentConfig.stall_timeout_sec` defaults to 1800 seconds and can be set to 0 to disable (`src/agent_bridge/config.py:88-105`).
- `_stall_watch` polls at most every `STALL_POLL_SEC = 30`, fails the task as `failed`/`stalled` after the configured silence, cancels the adapter, then bounds cleanup (`src/agent_bridge/registry.py:100-102`, `registry.py:1452-1499`).
- Transcript writes update worker activity via `append_event` (`src/agent_bridge/transcript.py:59-86`), and `mark_worker_activity` / `worker_silence_sec` are at `transcript.py:89-101`.
- Tests: `tests/test_registry.py:1499-1580` covers silent failure, activity reset, and `stall_timeout_sec=0`.

A worker that is legitimately silent for 90 minutes needs `stall_timeout_sec > 5400` or `0`. Setting `0` removes a useful hang detector.

### 2. Can `wait_task` remain pending for 90 minutes inside Bridge?

**Yes.** There is no server-side cap and local waiting is event-driven. But Bridge does not control whether the MCP host allows the request to stay alive that long.

The documented host limits are much shorter:

- `ORCHESTRATION.md:61-68`: Codex `tool_timeout_sec` 600; Cursor roughly 45-60 s; Kimi/ZCode examples use 600000 ms; Grok recommended explicit 600; Claude Code uses a 600000 ms per-server timeout with historical desktop issues around 60 s.
- `SETUP.md:33-51`: the Codex example sets `tool_timeout_sec = 600`.
- `README.md:64-75`: the Kimi example sets `toolTimeoutMs: 600000`.

Upstream Codex has changed its default MCP timeout across versions. The current upstream docs/search result says `tool_timeout_sec` overrides the per-tool timeout (`https://developers.openai.com/codex/config-reference`), while current source at commit `fbe65995` shows `DEFAULT_TOOL_TIMEOUT = 300` seconds (`https://github.com/openai/codex/blob/fbe65995/codex-rs/codex-mcp/src/rmcp_client.rs`). Either way, the repository's explicit 600-second recommendation is below 5400 seconds.

For a genuine single 90-minute direct wait, the host timeout must be set above the requested value with margin, for example:

```toml
[mcp_servers.agent_bridge]
tool_timeout_sec = 5700
```

Then call `wait_task(task_id=..., timeout_sec=5400)`. Do not set both to exactly 5400; scheduling and serialization overhead need headroom. Whether Codex accepts and honors that value for this version/host must be verified; it is not guaranteed by Agent Bridge.

### 3. Can that pending request avoid waking the coordinator model?

**Only if the host exposes it as a direct pending tool call.**

For a direct MCP tool call, the normal model-facing semantics are: model emits a tool call, runtime waits for the tool result, then the model is resumed with that result. There is no Bridge-created wake cadence.

For Codex Code Mode, the answer is different: the outer `exec` yield interval is model-visible. With the inspected default, a 90-minute wait can become roughly 540 model-visible yield/wait boundaries at 10 seconds each, unless the effective yield is raised or the namespace is forced direct. Each boundary can consume coordinator tokens and context.

`default_exec_yield_time_ms` can raise the initial Code Mode exec yield, but it does not by itself create a stable long-running MCP contract:

- the model can still choose an earlier `yield_time_ms`;
- subsequent `wait` calls are model-controlled;
- the nested MCP call still has the host `tool_timeout_sec` ceiling;
- the option is part of an evolving Codex feature surface.

The stronger Codex workaround is direct-only exposure for the Agent Bridge namespace, but the exact generated namespace must be verified. For the documented server key `agent_bridge`, the likely namespace is `mcp__agent_bridge`, but Codex derives/normalizes it client-side and the server cannot discover or declare it:

```toml
[features.code_mode]
direct_only_tool_namespaces = ["mcp__agent_bridge"]
```

Treat that as version-sensitive, not as a portable solution.

### 4. Can the coordinator avoid repeated wait calls entirely?

**Yes for execution, no for automatic result delivery under the current host contract.**

Best current pattern:

1. `dispatch_task` returns `task_id` immediately.
2. The coordinator does other useful work, or ends its turn and tells the user the task is running.
3. Later, on a user request, scheduled host event, or another model turn, call `list_tasks`, `check_task`, `wait_task`, or `get_result`.
4. If the coordinator restarted and another live Bridge instance owns the task, follow it as `remote: true`; do not re-dispatch.

This avoids holding an MCP request open and avoids polling turns. It does not magically wake the coordinator at completion: Agent Bridge has no guaranteed model-push channel today.

## Server lifetime and restart behavior

A running task prevents abandoned-server self-exit:

- `idle_exit_due()` requires both no recent MCP activity and no queued/running tasks (`registry.py:425-433`).
- The watchdog checks every 60 seconds (`registry.py:435-452`).
- Default `idle_exit_sec` is 7200 (`config.py:132`, `agents.toml:92-105`).

Orderly shutdown is different from force-kill:

- `Registry.stop()` and `_shutdown()` are at `registry.py:948-1048`.
- With default `shutdown_policy="cancel"`, in-flight background tasks are cancelled during shutdown.
- With `shutdown_policy="linger"`, `_shutdown` waits for in-flight tasks up to `linger_max_sec` (`registry.py:989-1023`), default 86400 seconds (`config.py:132-135`).
- This is documented at `SETUP.md:344-352` and `agents.toml:92-105`.
- Linger cannot survive a force-kill of the Bridge process tree; `SETUP.md:348-350` calls out Codex terminating the MCP Job Object as an example.
- To carry work across a host restart, use `pause_task` before shutdown and `resume_task` afterward.

On startup, dead-owner queued/running tasks are not silently continued:

- `src/agent_bridge/registry.py:912-931`: adopted tasks that were queued/running become `failed`/`bridge_restarted`, unless they were paused, in which case they end `cancelled`/`paused`.

Tests for linger and shutdown:

- `tests/test_lifecycle.py:229-252`: linger lets an in-flight task finish.
- `tests/test_lifecycle.py:255-275`: `linger_max_sec` bounds the wait.
- `tests/test_lifecycle.py:278-308`: interrupted linger still tears down and flushes state.
- `tests/test_registry.py:1466-1485`: ordinary `stop()` cancels a running task quickly.
- `tests/test_registry.py:956-985`: queued/running tasks suppress idle-exit.

## Best current usage

For the current codebase, the safest recommendations are:

1. **Dispatch without immediately waiting when no coordinator work is needed.** `dispatch_task` returns a `task_id`; the task is already independent.
2. **If the coordinator needs to block in-turn, use `wait_task` below the host's configured tool timeout.** For the documented Codex setup, `timeout_sec=180` is safe and `timeout_sec` up to somewhat below 600 is reasonable. On uncertain hosts, use 30-45 second polls.
3. **Treat `timed_out=True` as nonterminal.** Inspect `status`, `silent_for_sec`, `stall_timeout_sec`, `remote`, `owner_lost`, `paused`, `resumable`, and `recent_activity` before deciding.
4. **Do not confuse worker stall timeout with host tool timeout.** Raise the worker's `stall_timeout_sec` only when long silence is expected and legitimate.
5. **Keep `server.idle_exit_sec` above expected abandoned gaps, or disable it if the deployment requires.** Running tasks already suppress idle exit, but a completed task followed by a silent coordinator can still let the server exit later.
6. **Use `linger` only for orderly close.** Use `pause_task`/`resume_task` when the intent is to survive a restart or force-kill.
7. **For Codex Code Mode, prefer direct exposure for Agent Bridge if the host/model routes MCP tools through `exec`.** Verify the exact generated namespace rather than assuming `mcp__agent_bridge` forever.
8. **Do not interpret a host-side timeout as worker failure.** After a cut-off wait, call `check_task` or `wait_task` again; the task may still be running.

## Architectural options

### A. Single long MCP call

Change needed outside Bridge: set host `tool_timeout_sec` above 5400 seconds with margin and keep the MCP server process alive.

Advantages:

- One coordinator-visible wait.
- Bridge already supports it.
- Local task completion wakes immediately through `_done.set()`.

Risks:

- host hard caps;
- Code Mode/deferred-tool routing can introduce model-visible yields;
- UI cancellation, client shutdown, transport failure, or Job Object cleanup can still kill it;
- provider/session limits may bound a single assistant turn.

### B. Short-poll `wait_task` loop

Already supported.

Advantages:

- works under conservative host timeouts;
- timeout returns are safe and nonterminal;
- remote tasks can be followed read-only.

Costs:

- every timeout returns control to the coordinator/model;
- the model may spend turns and context deciding to wait again;
- under Codex Code Mode, each outer yield/wait is another model-visible boundary.

This is the safest interoperable mode, but it does **not** satisfy “no coordinator wake for 90 minutes.”

### C. Dispatch-and-later-check

Already supported.

Advantages:

- strongest current way to avoid holding a request open;
- worker and Bridge continue independently;
- no coordinator polling cost while the coordinator is idle;
- restart rediscovery exists through shared `state.json`/remote rows.

Limitations:

- no guaranteed automatic model wake at completion;
- result retrieval needs a later user turn, host scheduler, dashboard/manual trigger, or some other host mechanism.

This is the best current answer if “without consuming extra main-agent tokens” means no polling loop, rather than requiring an unattended completion wake.

### D. Completion notification channel

Possible design, not currently sufficient as a Bridge-only change:

- expose `subscribe_task(task_id)` or stream progress/completion through MCP notifications;
- or write a completion event into a host-watched queue.

Limits:

- MCP progress/logging notifications do not automatically guarantee a model inference or a future tool call;
- each host needs support for turning a server notification into a model-visible event;
- without that host hook, Bridge can record completion but cannot wake a dormant coordinator.

This would be useful for dashboards and future host integrations, but it is not a universal replacement for `wait_task`.

### E. Durable external worker supervisor

Move execution ownership out of the per-coordinator MCP child process:

- a persistent daemon owns worker processes/pipes;
- MCP Bridge instances submit and observe durable task records;
- completion artifacts and native session IDs are stored durably;
- cancellation, orphan reaping, adoption, and result paging go through the daemon/shared store.

This is the only design that can survive arbitrary coordinator/Bridge process death while keeping one worker turn alive. Current Bridge deliberately avoids being that daemon: worker pipes belong to the owning process, and `linger` only handles orderly shutdown. `pause_task`/`resume_task` is the current safer alternative, but it ends and restarts a turn rather than freezing it.

### F. Codex-specific configuration

Possible host-side mitigations:

- raise `[mcp_servers.agent_bridge].tool_timeout_sec` above 5400;
- verify whether the active model/tool catalog routes MCP calls directly or through Code Mode;
- use `features.code_mode.direct_only_tool_namespaces` for the exact Agent Bridge namespace;
- optionally raise `features.code_mode.default_exec_yield_time_ms` as a fallback, understanding that it does not remove model-controlled subsequent waits.

This is not portable to other hosts and can change with Codex model metadata/tool-mode behavior.

## Concrete changes worth making later

No implementation was performed here. If production changes are approved, the highest-value changes are:

### Documentation/config

- Update `ORCHESTRATION.md` Step 3 to state explicitly:
  - `wait_task` timeout is an observation timeout, not worker lifetime;
  - dispatch alone lets the worker continue;
  - a single 5400-second wait requires host `tool_timeout_sec > 5400` and direct tool execution;
  - repeated waits are model-visible and can cost coordinator turns.
- Update `SETUP.md` Codex guidance with an optional long-wait example (`tool_timeout_sec = 5700`) and a warning that Codex Code Mode can wrap MCP calls.
- If Codex direct-only namespace configuration is recommended, document how to discover/verify the actual model-visible namespace and label it version-sensitive.

### Server/tool contract

- Add a task snapshot field such as `recommended_wait_timeout_sec` only if Bridge gains a configured expected-host limit; otherwise do not pretend Bridge knows the host deadline.
- Consider a `wait_task` response field like `wait_again: true` plus `next_poll_after_sec` to make model retries deterministic, while documenting that retries still wake the coordinator.
- Consider a `subscribe_task`/`task_events` tool only if a target host can surface notifications to the model. Do not add it merely as another polling API.
- Keep `wait_task` itself event-driven; no server-side change is needed for long local waits.

### Persistence/supervision

- If restart-independent continuous execution is required, design a daemon-owned worker supervisor rather than expanding `linger`.
- Preserve the current ownership rules: one live owner per task/session, orphan reaping before adoption, result artifacts independent of in-memory rows.

### Tests

Useful tests if changes are approved:

- `wait_task(timeout_sec≈0)` returns `timed_out=True` while the task continues, then a later wait returns completion.
- Dispatch followed by no waiter still produces terminal state and a result artifact.
- A host-cancelled wait leaves `_bg`/worker running; this may need an adapter-level cancellation test because MCP client cancellation is outside the current unit surface.
- A long-wait regression test using a fake adapter gate: completion calls `_done.set()` and wakes without relying on a poll interval.
- Existing coverage already proves remote read-only waits, `owner_lost`, stall behavior, idle suppression for running tasks, and linger semantics.

## Final answer

A 90-minute worker run is already feasible when the worker remains productive/alive, `stall_timeout_sec` permits it, and the owning Bridge process survives. A 90-minute `wait_task` is also feasible inside Bridge because local waits are event-driven and uncapped. The real boundary is the coordinator host: its tool timeout, Code Mode/deferred execution behavior, transport lifetime, and process management decide whether that wait stays one pending request or becomes repeated model-visible wakes.

The corrected model is therefore:

- **Bridge waiting:** free of coordinator-model wakes; local event wait, remote 1.5-second disk poll.
- **Direct MCP pending call:** normally no model inference until response, subject to host semantics.
- **Internal `timeout_sec` response:** wakes the coordinator/model once per timeout.
- **Codex Code Mode yield:** wakes the coordinator/model at every yield/wait boundary; inspected default is 10 seconds, not 60.
- **Worker tokens:** accrue independently in `usage`/`run_usage`; waiting does not add worker tokens.
- **Coordinator tokens:** not consumed merely by Bridge sleeping, but every returned wait/yield can create another model turn and resubmit context.
- **No-wake completion:** currently requires a host-provided background/notification mechanism; Bridge cannot guarantee it with `wait_task` alone.
