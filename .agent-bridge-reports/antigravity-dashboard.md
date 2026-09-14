# Antigravity dashboard activity renders blank — root cause and fixes

**Date:** 2026-09-14
**Scope:** why Antigravity (`agy`, Gemini 3.8 Flash) subagent activity shows as an empty
"Agent" card followed by "Tool" rows with `input = null` on the Agent Bridge dashboard,
while Devin (ACP, SWE-2) renders message text, "Read file" titles, JSON input, and Thinking.

## Verdict

The Antigravity adapter serializes every stream-json record opaquely as
`{"type": <classified>, "data": {"payload": <raw agy object>}}`. Every consumer of the
transcript — the dashboard's `normalize_event`, the dashboard feed JS, and the bridge's own
`recent_activity`/`_partial_run_usage` — expects the **flat normalized schema** that only the
ACP adapter emits (`data.text`, `data.title`, `data.input`, `data.tool_call_id`, `data.kind`,
`data.status`, `data.consumed`). No code path ever unwraps `data.payload`. The Codex adapter
has the identical defect.

## The pipeline

```
agy --output-format stream-json  (stdout JSONL)
  → AgyAdapter.run_turn           src/agent_bridge/adapters/antigravity.py:331-381
      classifies event → append_event(type, {"payload": obj})   antigravity.py:355-372
  → ~/.agent-bridge/transcripts/sess_*.jsonl                    transcript.py:59-86
  → dashboard /api/events → read_events → normalize_event       share/dashboard.py:139-169, 2386-2401
  → browser handlers addMsg/addThink/addTool                    share/dashboard.py:1637-1692
```

`get_transcript` (MCP) bypasses normalization and returns raw records —
`registry.py:2069-2075` via `transcript.page_events` — so the failure is confined to
dashboard rendering and `recent_activity`/live-usage summaries, which is why dispatch still
worked and only the dashboard looked empty.

## Evidence

### Write side — Antigravity emits `data.payload`, not normalized fields

`src/agent_bridge/adapters/antigravity.py:355-372`:

```python
event_type = "raw"
if event == "init":
    event_type = "raw"
elif step_type == "agent_response" or chunk:
    event_type = "message_chunk"
    ...
elif step_type in {"tool"} or event in {"tool", "tool_call"}:
    event_type = "tool_call"
    collect_tool_paths(obj, files)
elif step_type == "checkpoint" or event in {"thought", "reasoning"}:
    event_type = "thought_chunk"
append_event(
    session.session_id,
    event_type,
    {"payload": obj} if len(raw) < 4000 else {"truncated": True},
    self.home,
)
```

The event `type` is normalized but `data` is not — it is either `{"payload": <raw>}` or
`{"truncated": true}` for lines ≥ 4000 bytes.

### Read side — dashboard only understands the flat schema

`src/agent_bridge/share/dashboard.py:79-136` (`normalize_event`):

- `message_chunk` → `{"t":"msg","text": d.get("text","")}` — agy data has no `text` → `""`.
- `tool_call` → reads `d.input`, `d.tool_call_id`, `d.kind`, `d.title` — all absent under
  `payload`, so `inp=None` → `s = json.dumps(None)` → the literal string `"null"`, and the
  event becomes `{"t":"tool","id":null,"kind":"tool","title":"","input":"null"}`.
- `tool_call_update`, `usage`, `thought_chunk` — agy never emits these types at all.
- Unknown/`raw`/`truncated` records → `None` (dropped).

Frontend `share/dashboard.py:1637-1671`: `addMsg` creates an Agent card even for `text:""`
(empty body → the "Agent" header with nothing under it). `addTool` renders `e.title||e.id`
(empty) and, because `e.input === "null"` is a truthy string, opens a "Tool input" fold
containing `null`. That is the exact reported symptom: "Agent then Tool/input/null".

### Transcript comparison (real data)

`C:\Users\Touma\.agent-bridge\transcripts\sess_0b04e1dd40.jsonl` — agy, 629 lines:
`{prompt_sent:1, raw:5, message_chunk:484, tool_call:138, turn_end:1}` — no `usage`,
no `tool_call_update`, no `thought_chunk`.

Message chunk (text lives at `payload.step_update.text_delta`, invisible to the dashboard):

```json
{"type":"message_chunk","data":{"payload":{"event":"step_update","step_update":
 {"step_index":141,"state":"ACTIVE","step_type":"agent_response",
  "text_delta":"### Root-Cause Diagnosis\n\nThere is *"}}}}
```

Tool call (name/params/output live at `payload.step_update.tool_info`):

```json
{"type":"tool_call","data":{"payload":{"event":"step_update","step_update":
 {"step_index":2,"state":"ACTIVE","step_type":"tool","tool_name":"find_by_name",
  "tool_info":{"name":"find_by_name","parameters":{"Pattern":"*","SearchDirectory":"..."}}}}}
```

`C:\Users\Touma\.agent-bridge\transcripts\sess_22e5c23a8c.jsonl` — devin/ACP, 616 lines.
Equivalent events carry the flat fields:

```json
{"type":"message_chunk","data":{"update_type":"AgentMessageChunk","text":"I'll audit the token"}}
{"type":"tool_call","data":{"update_type":"ToolCallStart","title":"Read file",
 "tool_call_id":"read:0","kind":"read","locations":["...usage.py"],
 "input":"{\"file_path\": \"...usage.py\"}"}}
{"type":"tool_call_update","data":{"update_type":"ToolCallProgress","tool_call_id":"read:0",
 "status":"in_progress"}}
{"type":"thought_chunk","data":{"update_type":"AgentThoughtChunk","text":"I'm going to start..."}}
{"type":"usage","data":{"consumed":{"scope":"run","input":12101,"output":508,"total":12609,...}}}
```

The flat shape is produced by `AcpClient.session_update`,
`src/agent_bridge/adapters/acp.py:515-580` (text at :522/:527, tool fields at :563-579,
`usage` events at :536-551).

### Not a dashboard regression

- `git log -S '"payload"' src/agent_bridge/adapters/antigravity.py` → only `c6fc0a7`
  (initial public commit): the adapter has *always* written `{"payload": obj}`.
- `~/.agent-bridge/dashboard.py.pre-ui-20260912.bak` (pre-UI backup) contains the same
  `normalize_event` reading `d.get("text")`/`d.get("tool_call_id")` — no version ever
  unwrapped `payload`. agy activity was never rendered; nobody noticed because result text
  and `turn_end` still worked.
- `tests/test_dashboard_page.py:64-78` fixtures only exercise the flat ACP shape — no test
  ever fed an agy-shaped record through `normalize_event`.

### Secondary symptoms of the same cause

- **Duplicate, permanently-spinning tool rows.** agy emits two `step_update` records per
  tool call (`state: "ACTIVE"` then `"DONE"`); both become `tool_call` events, so
  `addTool` paints two rows per call. No `tool_call_update` is ever written, so
  `addToolStatus` never fires and the spinner stays `in_progress` forever.
  (In the sample transcript: 69 ACTIVE + 57 DONE tool steps → 126 rows for ~63 calls.)
- **No live token counters.** The dashboard's live usage (`_usage_consumed_last` /
  `live_usage_map`, `share/dashboard.py:202-295`) and the bridge's
  `_partial_run_usage` (`registry.py:197-211`, surfaced in `wait_task`/`check_task` at
  `registry.py:1953-1956`) both scan for `type=="usage"` records with `data.consumed`.
  `AgyAdapter` tracks usage in `DeltaUsage` for `TurnResult` but never appends a `usage`
  transcript event — so running agy tasks show no tokens anywhere.
- **Bare `recent_activity`.** `transcript.recent_activity` (`transcript.py:258-273`) pulls
  `data.text/title/path/summary`; all absent under `payload`, so `wait_task` activity for
  agy tasks is just `["tool_call","message_chunk","raw",...]`.
- **Extra feed latency.** `usage` events are an immediate-flush trigger in
  `append_event` (`transcript.py:80-83`). Devin's frequent usage events flush the buffered
  transcript constantly; agy emits none, so its feed waits for the 64 KB / 30 s buffer
  flush (`transcript.py:17-19`) on top of being blank.
- **`{"truncated": true}` holes.** agy lines ≥ 4000 bytes (large tool outputs, the final
  `result` record) are persisted as `{"type":"raw","data":{"truncated":true}}` — 13 records
  in `sess_0b04e1dd40` alone. Even a read-side fix cannot recover those; the normalized
  fields are gone.
- **`step_type: "error_message"` is unhandled** → classified `raw` → invisible
  (25 occurrences across the 9 agy transcripts; observed records carry no text, only
  `duration_seconds`).
- **Codex has the same bug.** `adapters/codex.py:176-181` also writes
  `{"payload": obj}` / `{"truncated": true}`; codex sessions will render identically blank.
- **Thinking cannot be fabricated.** The agy stream contains no thinking content
  (`thinking_tokens` is a counter inside `usage` only). `thought_chunk` will stay absent
  unless a future agy stream version adds it — acceptable, but the classifier's
  `checkpoint`/`thought`/`reasoning` branch should still emit `text` if such content ever
  arrives.

## Expected normalized event schema (what the dashboard contract actually is)

Transcript record: `{"ts": iso8601, "type": str, "data": {...}}` (`models.py:153-156`).
Per-type `data` fields consumed by `normalize_event` + frontend + `recent_activity`:

| type                | required `data` fields                                   | dashboard output                        |
|---------------------|----------------------------------------------------------|-----------------------------------------|
| `prompt_sent`       | `text`, `task_id`, `source`                              | prompt card                             |
| `message_chunk`     | `text` (non-empty; chunks append)                        | "Agent" card body                       |
| `thought_chunk`     | `text`                                                   | "Thinking" fold                         |
| `tool_call`         | `tool_call_id`, `title`, `kind`, `input` (str or JSON-able), optional `path`, `locations` | tool row + "Tool input" fold |
| `tool_call_update`  | `tool_call_id`, `status` ∈ in_progress/completed/failed/error | status icon + duration            |
| `usage`             | `consumed` (dict of counters)                            | live token badges; also immediate flush |
| `turn_end`          | `stop_reason`, `task_id`, optional `error`               | "turn ended" divider                    |
| `error`             | `error` or `text`                                        | error divider                           |

`kind` maps to icons via `TOOL_KINDS`/`TOOL_ICON` (`dashboard.py:1647-1648`): `execute`,
`read`, `search`, `edit`, `fetch`, `tool`.

### Proposed agy → normalized mapping

From `payload.event == "step_update"`, `su = payload.step_update`:

- `su.step_type == "agent_response"` and `su.text_delta` → `message_chunk {text: delta}`.
  Skip usage-only DONE steps (no `text_delta`) — see edge cases.
- `su.step_type == "tool"`, `su.state == "ACTIVE"` → `tool_call` with
  `tool_call_id = f"{su.conversation_id}:{su.step_index}"`,
  `title = su.tool_name or su.tool_info.name`,
  `input = json.dumps(su.tool_info.parameters)`,
  `path`/`locations` from the path parameters `collect_tool_paths` already inspects
  (`antigravity.py:184`), and `kind` mapped: `view_file|find_by_name→read/search`,
  `grep_search|search_web→search`, `run_command|send_command_input|command_status→execute`,
  `write_to_file|replace_file_content|multi_replace_file_content|sed_file|notebook_edit→edit`,
  `read_url_content→fetch`, else `tool`.
- `su.step_type == "tool"`, `su.state == "DONE"` → `tool_call_update
  {tool_call_id, status:"completed"}`; a FAILED/error state (unobserved in the 9 local
  transcripts but plausible) → `status:"failed"` and surface `tool_info.output`/`error`
  as `output`.
- `su.usage` (dict on any step) or `result.usage` → `usage {consumed: <normalized>}`
  reusing the `DeltaUsage` snapshot shape ACP already emits.
- `step_type == "checkpoint"` / `event in {"thought","reasoning"}` → `thought_chunk {text}`
  if a text field is present.
- `step_type == "error_message"` → `error` if it carries text; otherwise `raw` (current).

## Fixes (investigation only — not implemented)

**Fix A (write-side, required): normalize in `AgyAdapter.run_turn`.** Replace the blanket
`{"payload": obj}` append at `antigravity.py:367-372` with per-type `data` built per the
mapping above — mirroring `AcpClient.session_update` (`acp.py:554-580`), including
`_tool_io_summary`-style truncation (`acp.py:75-79`). Keep `payload` alongside the flat
fields when `len(raw) < 4000` for `get_transcript` fidelity; when truncating, still emit the
normalized fields (`{"title":..., "input":..., "truncated": true}`) so the dashboard never
goes blank on big tool outputs. Also emit `usage` events and `tool_call_update` on DONE —
this alone fixes live tokens, the forever-spinner, and the 30 s feed lag.

**Fix B (read-side, recommended as well): unwrap `data.payload` in
`normalize_event`.** A read-side branch for `data.payload.step_update` retroactively heals
the 9 agy transcripts already on disk and future-proofs against other adapters writing
opaque payloads. It is stateless per record, so map `state=="DONE"` tool steps to
`tool_status` events carrying the same `id` (`{conversation_id}:{step_index}`); the
frontend merges by `tools[e.id]`. Caveat: a DONE record without a preceding ACTIVE in the
read window yields a status for an unknown id — `addToolStatus` silently no-ops
(`dashboard.py:1673`), so also let a DONE tool step emit a complete `tool` row carrying
`input`+`output` when its ACTIVE twin was `truncated`/missed.

**Same change needed in `adapters/codex.py:176-181`** (`item.completed` →
`agent_message`/`file_change`) or codex stays blank.

## Edge cases to handle

1. `agent_response` DONE steps carrying `usage` but no `text_delta` — 69 in one transcript.
   Emitting `msg` for them would still paint empty Agent cards; skip or convert to `usage`.
2. Whitespace-only `text_delta` (`" "`, `"\n"`) — append faithfully; merging is the
   frontend's job, but don't manufacture an event for absent text.
3. Tool ACTIVE without `tool_info` (observed `tool_name` present, `tool_info` occasionally
   missing on early records) → title from `tool_name`, `input` empty, no detail fold.
4. `tool_info.output` on DONE can be large (the `find_by_name` output above) — summarize
   via the `_TOOL_IO_LIMIT` convention, never inline raw.
5. `{"truncated": true}` legacy records — must stay skippable (normalize → `None`), not crash.
6. Multiple step streams on one session (resume) reuse `step_index` per conversation — key
   `tool_call_id` on `conversation_id:step_index`, not `step_index` alone.
7. `raw` records for non-JSON stdout lines (`{"text": raw[:2000]}`) — keep dropping them
   from the feed; they are diagnostics, not activity.
8. `warning` (recovered agy tool-schema error, `antigravity.py:416-421`), `permission`,
   `plan` types — `normalize_event` drops them today; decide deliberately whether
   `warning` should render (it is user-meaningful).
9. Codex `item.completed` payloads use different keys (`item.type`, `item.text`) — a shared
   payload-unwrapper needs per-adapter detection, or normalize at write time only.

## Concrete regression tests

1. **`tests/test_agy_parse.py` — emit flat fields.** Extend `fake_agy.py` to print a tool
   `step_update` pair (ACTIVE then DONE with `tool_info.output`) plus a usage-bearing
   `agent_response` DONE with no `text_delta`. Assert `read_events()` on the transcript
   contains: one `tool_call` with `tool_call_id`/`title`/`input`, one `tool_call_update`
   `{"status":"completed"}`, one `usage` event, and no empty-text `message_chunk`.
2. **`normalize_event` unit tests** (new `tests/test_dashboard_normalize.py`, or extend
   `test_dashboard_page.py`): feed the verbatim agy records quoted above → expect
   `{"t":"msg","text":"### Root-Cause Diagnosis..."}` and
   `{"t":"tool","title":"find_by_name","input":"{\"Pattern\":\"*\",...}"}` with a non-null
   `id`; feed a `{"truncated":true}` record → `None`; feed the flat ACP records → unchanged
   output (guard against double-unwrapping).
3. **Round-trip test:** run `AgyAdapter.run_turn` against `fake_agy.py`, then pipe the
   produced transcript file through dashboard `read_events()` and assert every emitted
   `msg` has non-empty `text`, every `tool` has non-empty `title` and `input != "null"`,
   and every `tool` id is followed by a `tool_status`. This is the test that would have
   caught the bug — write-side and read-side schema drift in one assertion.
4. **Codex parity test:** same round-trip through `CodexAdapter`/`apply_codex_event`
   asserting `msg`/`tool` events are normalized.
5. **`recent_activity` test:** agy transcript → summaries contain text/title, not bare
   `"message_chunk"`/`"tool_call"`.
6. **Live-usage test:** agy run → `_usage_consumed_last`/`live_usage_map` returns a
   `consumed` snapshot (currently always `None` for agy).
