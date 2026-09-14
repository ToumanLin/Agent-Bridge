# Antigravity `gemini-3.8-flash-medium` 503 dispatch failures — Explorer report

Date: 2026-09-14. Scope: investigation only; no production changes made.

## TL;DR

The `UNAVAILABLE (code 503): No capacity available for model gemini-3.8-flash-medium`
failures are **provider-side transient capacity exhaustion** on Google's
`cloudcode-pa` backend (reason `MODEL_CAPACITY_EXHAUSTED`), surfaced verbatim
through the `agy` stream-json result event. It is **not** a Bridge bug, not a
quota problem, and not an invalid model name — the same model initialized and
streamed 100+ steps for 13–20 minutes before the error arrived mid-response.

Bridge-side, the real gap is **classification and guidance**: the failure lands
as a generic `status=failed / stop_reason=error` with no "transient / retryable /
resumable" signal, even though the agy conversation is preserved and
`resume_task` can continue it cheaply. A separate, fully local bug also bit the
same dispatch wave: `--model gemini-3.8-flash-medium --effort high` is rejected
by agy because the slug already pins effort, and Bridge passes both flags
blindly.

## 1. Observed evidence

### 1.1 Two distinct failure waves in `C:\Users\Touma\.agent-bridge\logs\bridge-20260914.log`

Wave A — instant config rejection (lines 143–145), ~2–3 s after dispatch
(lines 137–141, 10:56:46–47):

```
task_finished task_id=task_cdb22ac838 ... agent=antigravity status=failed
  duration_ms=2196 stop_reason=error
  error='invalid model selection (--model "gemini-3.8-flash-medium" --effort "high"):
         --model gemini-3.8-flash-medium conflicts with --effort=high'
```
(same for `task_76b0f3d012`, `task_5a3147992b`; state.json confirms
`model="gemini-3.8-flash-medium", effort="high"` on all three).

Wave B — the 503s (lines 146–150 dispatched 10:57:14–15 with `effort=null`;
lines 163–164, 168 finished):

```
11:10:19 task_a9cc36c3a7 failed duration_ms=784968  error='API error (attempt 2):
  UNAVAILABLE (code 503): No capacity available for model gemini-3.8-flash-medium on the server'
11:10:20 task_b857025371 failed duration_ms=785776  error='API error (attempt 1): ...'
11:17:41 task_c438adabf7 failed duration_ms=1226837 error='API error (attempt 1): ...'
```

Context: the same model + `effort="medium"` **succeeded** at 10:30
(`task_e9d0e77e70`, log line 110–112, 5.6 s), so the model name and matching
effort are valid. A fourth task on the same model (`task_cfdaf615be`,
`sess_de02f4320a`, dispatched 11:06) hit recoverable `error_message` steps at
16:24 UTC (transcript steps 85–86) and **kept running** — i.e. capacity errors
in this window were intermittent, not hard-down.

### 1.2 Transcripts show mid-stream, mid-turn failure — not a startup failure

`C:\Users\Touma\.agent-bridge\transcripts\sess_7503cc067c.jsonl`:

- init event carries `"model": "gemini-3.8-flash-medium"` (line 2) — the model
  was accepted and served.
- Line 148: `step_type:"error_message"` at step_index 97 (16:06:21 UTC) — a
  first provider error that agy absorbed; the turn continued.
- Lines 827–831: `agent_response` `text_delta` chunks at step 106 still
  `state:"ACTIVE"` at 16:10:20 — the model was mid-generation.
- Line 834: final `error` event `"API error (attempt 1): UNAVAILABLE (code 503):
  No capacity available for model gemini-3.8-flash-medium on the server",
  "code": 0` — **process exit code 0**.

Same pattern in `sess_9b7c88b464.jsonl` (error_message steps 111–112 at lines
169–170, fatal "attempt 2" at line 816) and `sess_068d7d4758.jsonl`
(error_message steps 101 and 138 at lines 154, 209; fatal "attempt 1" at line
1042). Note `error_message` step payloads carry **no error text** — they are
opaque markers, so the failure detail only exists in the final result event.

### 1.3 Partial work survived

`C:\Users\Touma\.agent-bridge\results\task_{b857025371,a9cc36c3a7,c438adabf7}.txt`
hold 26–29 KB of nearly-complete reports — `result.text` is persisted even on
error (`registry.py:1338-1342` runs before status finalization, and
`antigravity.py:408` falls back to accumulated `text_parts`). All three failed
sessions kept `native_session_id` in `state.json`
(`sess_7503cc067c`→`740dde30-…`, `sess_9b7c88b464`→`6946e487-…`,
`sess_068d7d4758`→`a3e46ba0-…`), so `resume_task` can continue the exact
conversation via `--conversation` (`antigravity.py:217-218`).

## 2. Current code path (how the error flows)

1. `dispatch_task` stores `model`/`effort` verbatim on the Task
   (`registry.py:1196`, `1277-1287`; tool surface `server.py:125-152`).
   `normalize_effort` only checks the five Bridge tokens (`models.py:39-47`);
   nothing validates the model slug or the slug↔effort combination.
2. `AgyAdapter._build_cmd` appends `--model X` and `--effort Y` when set
   (`antigravity.py:206-212`; effort mapped by `agy_effort`,
   `models.py:50-57`). Both Wave A and B flags went straight through.
3. `run_turn` reads stream-json lines (`antigravity.py:331-381`). The agy
   result event `{"status":"ERROR","error":"API error (attempt N): …"}`
   matches `is_result_event` (`antigravity.py:59-64` via `_RESULT_STATUSES`
   at line 30) and `result_error_of` (`107-115`).
4. Error path returns `TurnResult(stop_reason="error", error=…)`
   (`antigravity.py:437-446`); `session.native_session_id` is set at line 413
   **before** the error return — resume stays possible.
5. Registry finalizes `status=failed` (`registry.py:1382-1384`) and logs
   `task_finished` (`1438-1449`). Quota-cache invalidation consults
   `looks_like_quota_error` (`registry.py:1422-1426`), whose markers
   (`quota.py:46-57`: "quota", "rate limit", "billing", "exceeded", …) do
   **not** match "No capacity available" — correctly, since this is not quota.
6. Coordinator-facing payload (`_task_snapshot`, `registry.py:1910-1981`)
   carries `error`, `resumable`, `resume_hint` (`_resume_fields`,
   `1809-1860` — failed tasks return `resumable=True` with a resume hint).
   `_result_hint` (`1862-1908`) has per-agent notes for grok/kimi/opencode/
   claude/cursor/devin but **nothing for antigravity** and nothing marking a
   failure transient. Dashboard shows `failed` + stop_reason label
   (`share/dashboard.py:1255`, `1685`, `1722`).

Existing knobs: `stall_timeout_sec` (default 1800) and `print_timeout`
(default "120m") (`config.py:98-99`, `agents.toml:218`). Neither fired — the
turn was actively streaming when the 503 arrived.

## 3. Root cause vs. provider-side uncertainty

**Certain — provider-side:**

- The error text is generated inside `agy`/Google's backend, not Bridge:
  Bridge only relays it. Upstream reports show the identical payload —
  `{"code":503, "status":"UNAVAILABLE", "reason":"MODEL_CAPACITY_EXHAUSTED",
  "domain":"cloudcode-pa.googleapis.com", "message":"No capacity available for
  model <m> on the server"}` — across Antigravity models and accounts,
  including paid tiers. Google's own guidance: 503 means "services are
  temporarily overloaded… unrelated to your quota"; recommended remedy is
  backoff + retry.
- Three concurrent tasks on the *same* model failed inside one ~12-minute
  window, each preceded by recoverable `error_message` steps; a fourth task
  on the same model hit the same errors and recovered. That is a capacity
  crunch profile, amplified by three parallel heavyweight dispatches.
- The WSL/remote variant of this error (language server launched against
  `daily-cloudcode-pa.googleapis.com`, the zero-capacity staging endpoint)
  does **not** apply: `agy.exe` runs locally on Windows
  (`C:\Users\Touma\.agent-bridge\agents.toml:7`) and the model demonstrably
  served 100+ steps before failing.

**Certain — Bridge-side:**

- No transient/retryable classification exists anywhere; 503 capacity and
  "authentication required" produce the same `stop_reason="error"`.
- Mid-turn `error_message` steps are logged as opaque `raw` transcript events
  (unmapped `step_type` → `event_type="raw"`, `antigravity.py:350-372`) and
  never surface in `warnings`.
- No Bridge-level retry and no model-fallback config; `AgentConfig`
  (`config.py:88-106`) has no such fields.
- Effort-suffixed slugs are unguarded: `gemini-3.8-flash-medium` + `effort`
  `"high"` → agy rejects instantly (Wave A). A **matching** effort was
  accepted (`task_e9d0e77e70` used `effort="medium"`), so the conflict is
  specifically *mismatched* `--effort` vs. the slug's level. Neither
  `ORCHESTRATION.md:52` nor the `list_agents` detail (`probes.py:160-162`)
  warns about this.

**Uncertain — provider-side:**

- Exact meaning of `(attempt N)` in agy's error wrapper and agy's internal
  retry budget/backoff — not observable from outside. The recoverable-then-
  fatal sequence is consistent with a known upstream defect class (gemini-cli
  issue google-gemini/gemini-cli#24815: after one transient error the model
  is marked `sticky_retry` with `maxAttempts=1` and `RetryInfo` is not
  honored for the rest of the session), but that is a different codebase; it
  is a plausible explanation for why agy gave up after 1–2 attempts, not a
  verified mechanism for `agy`.
- Whether capacity freed at a predictable time; whether a `--conversation`
  resume may switch `--model` (a fallback-model-on-resume design depends on
  this — do not assume it).

## 4. Recommended changes (robust, minimal)

### 4.1 Classification + user/coordinator-visible behavior

- Add a transient-error classifier next to `looks_like_quota_error`
  (`quota.py:397-401`): e.g. `TRANSIENT_ERROR_MARKERS = ("no capacity
  available", "model_capacity_exhausted", "unavailable (code 503)",
  "code 503", "overloaded", "deadline exceeded")`. Keep it **separate** from
  `QUOTA_ERROR_MARKERS` — capacity is not quota, and antigravity quota is
  `unknown` anyway.
- Surface it on the task snapshot: add e.g. `retryable`/`error_kind`
  ("transient_provider" | "config" | "unknown") in `_task_snapshot`
  (`registry.py:1910-1981`), and a hint branch — either an antigravity arm in
  `_result_hint` (`registry.py:1862-1908`) or a generic transient arm — telling
  the coordinator: "transient provider capacity error; `resume_task` continues
  the same conversation with the partial result, or retry later / switch
  model."
- Dashboard: the failure already renders `failed` + stop_reason
  (`share/dashboard.py:1255`, `1722`); with a `retryable`/`error_kind` field
  the UI can render "transient — resumable" (warning tone) instead of a hard
  error, plus a Resume affordance.
- Surface mid-turn `error_message` steps: in `AgyAdapter.run_turn`, map
  `step_type == "error_message"` to a `warning` transcript event and append a
  count to `TurnResult.warnings` (e.g. "agy reported 2 mid-turn API errors
  before this turn ended"). Today a turn that *recovers* leaves no trace of
  the instability.

### 4.2 Retry / fallback

- **Prefer explicit resume over silent in-adapter retry.** `resume_task`
  already accepts failed tasks (`registry.py:2257-2266` rejects only
  queued/running/completed) and dispatches a continuation on the same
  `--conversation`, where the model sees its partial work — cheap because
  `cache_read_tokens` dominated usage (52–65k cached). Document this recovery
  path in `ORCHESTRATION.md` and in the failure hint text.
- Optional opt-in auto-resume: `[agents.antigravity] transient_retries = 2`
  with bounded backoff, triggered only when the error matches transient
  markers **and** `native_session_id` was captured (a pre-init failure has
  nothing to resume — a fresh turn is correct there). Mark the continuation
  with `source="resume"`-style provenance like the existing resume flow.
- Model fallback: do **not** silently change `--model` on a resumed
  conversation (unverified whether agy honors it). Safer: coordinator-level
  fallback — on persistent capacity errors, re-dispatch fresh on
  `gemini-3.7-flash` or another worker per `ORCHESTRATION.md` Step 2, and
  stagger parallel same-model dispatches. If a config shortcut is wanted,
  `[agents.antigravity] fallback_model = "gemini-3.7-flash"` applied to a
  *fresh* session only.
- Wave-A guard: when an agy model slug ends in `-low`/`-medium`/`-high`,
  treat it as effort-pinning: matching `effort` → drop the flag (or keep —
  it worked); mismatched `effort` → fail `dispatch_task` early with a clear
  error ("slug already pins effort=medium; use the -high variant or omit
  effort"). Update `ORCHESTRATION.md:52` and `probes.py:161` accordingly.

### 4.3 Config

- `~/.agent-bridge/agents.toml` coordinator instructions pin
  `gemini-3.8-flash-medium` unconditionally — suggest adding "on repeated
  capacity 503s, fall back to `gemini-3.7-flash` or Grok" so the coordinator
  degrades gracefully instead of re-pinning a saturated model.

## 5. Concrete tests

1. **`tests/fake_agy.py`**: add `FAKE_AGY_MODE=capacity_503` — emits init
   (with `model`), several `agent_response` deltas, an `error_message`
   `step_update`, more deltas, then a result
   `{"status":"ERROR","response":"<partial>","error":"API error (attempt 1):
   UNAVAILABLE (code 503): No capacity available for model
   gemini-3.8-flash-medium on the server"}`, exit 0 (mirrors the real
   transcript shape, incl. `code:0`).
2. **`tests/test_agy_parse.py`**: with that fake, assert `run_turn` returns
   `stop_reason=="error"`, `error` contains "No capacity available",
   `text=="<partial>"`, `session.native_session_id` is set (resume-safe),
   and — after the warning change — `warnings` records the mid-turn
   `error_message`.
3. **Classifier unit tests**: transient markers match the real error string;
   auth / "invalid model selection" / "unknown model" do not; and
   `looks_like_quota_error` stays `False` for the 503 text (guards against
   misclassifying capacity as quota).
4. **Registry test**: FakeAdapter returning
   `TurnResult(stop_reason="error", error="<503 text>")` → task snapshot has
   `resumable=True`, the new `retryable`/`error_kind` field, and a hint
   mentioning `resume_task`; `resume_task` then dispatches a continuation on
   the same session.
5. **`_build_cmd` test**: `model="gemini-3.8-flash-medium"` + `effort="high"`
   exercises the chosen guard (early `ValueError`, or `--effort` dropped with
   a recorded warning); `effort="medium"` still passes.
6. **If auto-resume is added**: fake counts spawns — first run emits the 503
   result, second succeeds → task completes with the retry recorded in
   `warnings`; a run that keeps failing stops at `transient_retries` and ends
   `failed`/`transient`.
