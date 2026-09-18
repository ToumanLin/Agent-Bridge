# Acceptance Review: Antigravity Thinking/Reasoning Pipeline

**Date:** 2026-09-17  
**Reviewer:** Independent Acceptance Reviewer  
**Worktree:** `C:\Users\Touma\Documents\Agent-Bridge` on branch `main`  
**Candidate Commits / Uncommitted Files Under Audit:**
- [`src/agent_bridge/adapters/antigravity.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py)
- [`tests/fake_agy.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/tests/fake_agy.py)
- [`tests/test_agy_dashboard.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/tests/test_agy_dashboard.py)
- [`tests/test_agy_parse.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/tests/test_agy_parse.py)

---

## Final Recommendation

### **REJECT**

While the core mechanics of tailing `transcript_full.jsonl` (polling, partial line buffering, read-only safety under `~/.gemini`, and graceful post-turn flushing) are sound in isolation, the current candidate exhibits four distinct defects — two of High severity and two of Medium severity. In particular:
1. **Flawed Forward-Compatibility Dispatch Order:** In [`classify_event`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py#L369-L382), checking `if step_type == "agent_response" or chunk:` before the thought branch causes any future or alternative thinking event carrying a text chunk (`text_delta`) to misclassify as `message_chunk`, corrupting the agent's response text and violating the forward-compatibility requirement.
2. **Zombie Subprocess / Resource Leak on Early Process Spawn Cancellation:** In [`AgyAdapter.run_turn`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py#L638-L648), the call to `await asyncio.create_subprocess_exec` occurs outside the `try ... finally` block. If `run_turn` is cancelled while awaiting process creation (or if cancellation is delivered immediately upon spawn completion), the subprocess is orphaned without being registered in `self._procs` or reaped via `reap_subprocess`. Furthermore, the test [`test_run_turn_cancel_cleans_up_brain_watcher`](file:///C:/Users/Touma/Documents/Agent-Bridge/tests/test_agy_dashboard.py#L441-L457) relies on a racy fixed `asyncio.sleep(0.3)` instead of deterministic synchronization.
3. **Resumed Session Boundary Leak on Missing/Late File:** In [`_BrainThoughtTail.bind`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py#L441-L445), when a resumed turn has `native_session_id` set but the brain file is not yet created or inaccessible at pre-launch bind time, `_offset` defaults to `0`. If the file later appears populated with past turns, the tail replays historical thoughts into the current turn.
4. **Exact-Text Deduplication Semantic Defect:** In [`_BrainThoughtTail._record`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py#L497-L501), dedup is implemented strictly by checking `thinking in self._emitted`. If a model legitimately outputs the identical thought text in two separate reasoning phases or separate planner responses across a turn, the second legitimate thought is suppressed.

---

## Severity-Ranked Findings

### 1. [HIGH] Forward-Compatibility Shadowing in `classify_event`
- **File & Lines:** [`src/agent_bridge/adapters/antigravity.py#L369-L383`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py#L369-L383)
- **Symbol:** [`classify_event`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py#L353)
- **Description:**  
  `classify_event` evaluates:
  ```python
  if step_type == "agent_response" or chunk:
      if chunk:
          return "message_chunk", {"text": chunk}
      return "raw", {}
  ...
  if step_type in {"checkpoint", "thought", "reasoning"} or event in {"thought", "reasoning"}:
      text = _first_text(step, obj, keys=("text", "text_delta", "thought", "thinking", "reasoning", "summary"))
      return ("thought_chunk", {"text": text}) if text else ("raw", {})
  ```
  `chunk` is extracted via `chunk = text_delta_of(obj)` at [L715](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py#L715). If upstream `agy` emits a thought event containing a delta (e.g. `{"event": "step_update", "step_update": {"step_type": "thought", "text_delta": "thinking..."}}` or top-level `{"event": "thought", "text_delta": "..."}`), `chunk` is non-empty (`"thinking..."`). Because `or chunk` is evaluated in the first branch, the event is immediately classified as `("message_chunk", {"text": chunk})`, completely bypassing the thought branch.
- **Impact:**  
  Model reasoning text would be appended to `text_parts` and emitted as user-visible assistant response text instead of collapsible thought folds in the dashboard. This directly contradicts the forward-compatibility goal.

---

### 2. [HIGH] Process Lifecycle Vulnerability: Unhandled Cancellation During Process Spawn
- **File & Lines:** [`src/agent_bridge/adapters/antigravity.py#L638-L648`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py#L638-L648), [`tests/test_agy_dashboard.py#L441-L458`](file:///C:/Users/Touma/Documents/Agent-Bridge/tests/test_agy_dashboard.py#L441-L458)
- **Symbols:** [`AgyAdapter.run_turn`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py#L616), [`test_run_turn_cancel_cleans_up_brain_watcher`](file:///C:/Users/Touma/Documents/Agent-Bridge/tests/test_agy_dashboard.py#L441)
- **Description:**  
  In `AgyAdapter.run_turn`:
  ```python
  proc = await asyncio.create_subprocess_exec(
      *cmd,
      ...
  )
  self._procs[session.session_id] = proc
  ...
  try:
      ...
  except asyncio.CancelledError:
      await reap_subprocess(proc)
      ...
  finally:
      ...
  ```
  `create_subprocess_exec` is an asynchronous coroutine that creates OS process handles and sets up pipe transports. If `run_turn` is cancelled while awaiting `create_subprocess_exec`, the task raises `asyncio.CancelledError` before reaching `self._procs[session.session_id] = proc` and before entering the `try ... finally` block.
  Because there is no outer guard, the spawned child process remains unmanaged, leaking OS processes and file descriptors.
  Additionally, in `tests/test_agy_dashboard.py`, `test_run_turn_cancel_cleans_up_brain_watcher` uses:
  ```python
  run = asyncio.create_task(adapter.run_turn(session, task))
  await asyncio.sleep(0.3)  # let init arrive and the tail bind
  run.cancel()
  ```
  A fixed sleep timer (0.3s) is inherently flaky across environments under varying CPU/IO load. If process launch takes longer than 300ms, the cancellation fires during `create_subprocess_exec` and fails the test.
- **Impact:**  
  Flaky test runs under system load, orphaned child processes, and failure to honor the invariant: "clean up all async/process resources on every exit path."

---

### 3. [MEDIUM] Resumed Session Boundary Replay on Missing/Inaccessible Brain Log
- **File & Lines:** [`src/agent_bridge/adapters/antigravity.py#L437-L446`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py#L437-L446)
- **Symbol:** [`_BrainThoughtTail.bind`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py#L437)
- **Description:**  
  In `_BrainThoughtTail.bind`:
  ```python
  self._path = agy_brain_log(conversation_id)
  if resumed:
      try:
          self._offset = self._path.stat().st_size
      except OSError:
          self._offset = 0
  ```
  If a turn resumes an existing session (`session.native_session_id` is set), but the brain file does not exist on disk at the exact moment before process spawn (for instance, if the session was archived, running across a shared volume, or delayed in directory creation), `stat()` raises `FileNotFoundError` (an `OSError`).
  `_offset` is reset to `0`. If the engine creates or populates the file mid-turn with past turns included, or if the file becomes accessible, `drain()` starts reading from byte `0`, replaying historical thoughts from all previous turns.
- **Impact:**  
  Replay of prior turn reasoning into the current turn transcript upon transient filesystem delay or delayed file availability during resume.

---

### 4. [MEDIUM] Loss of Legitimate Repeated Thoughts via Global Text Dedup Set
- **File & Lines:** [`src/agent_bridge/adapters/antigravity.py#L431`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py#L431), [`#L497-L501`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py#L497-L501), [`#L717-L722`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py#L717-L722)
- **Symbols:** [`_BrainThoughtTail._emitted`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py#L431), [`_BrainThoughtTail._record`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py#L487)
- **Description:**  
  `_BrainThoughtTail` uses `self._emitted: set[str]` where the key is solely `thinking: str`.
  The candidate uses this single set for two conflicting purposes:
  a) Cross-source deduplication between stdout events and brain log records.
  b) Deduplicating records within the brain log itself.
  However, file offset progression (`self._offset += len(blob)`) already guarantees that records within the brain log are read exactly once!
  By suppressing any record whose text matches `thinking in self._emitted`, if a model legitimately produces identical reasoning strings at different points in a turn (e.g. `thinking: "Let me check the test failure"` before a tool call, and `thinking: "Let me check the test failure"` again after another tool call), the second occurrence is silently dropped.
- **Impact:**  
  Missing thoughts in multi-step turns where repeated thoughts occur. Cross-source dedup should only suppress an event if it was received from stdout for that turn, or cross-referenced, rather than collapsing all identical strings in the brain log.

---

## Detailed Audit Against Candidate Contract

| Contract Requirement | Status | Evidence / Analysis |
| :--- | :--- | :--- |
| **Read-only / Non-fatal** | **PASS** | `_BrainThoughtTail` opens files with `open("rb")` in read mode. No files under `~/.gemini` are modified. All IO/parse errors are caught in `drain()`, `_record()`, and `_watch_brain()`. |
| **Ordering & Interleaving** | **PASS** | Polling via `_watch_brain` at `BRAIN_POLL_SEC = 0.4s` emits `thought_chunk` events during turn execution ahead of later messages/tools. Verified in `test_run_turn_brain_thoughts_stream_ahead_of_answers`. |
| **Partial-line Buffering** | **PASS** | Uses `lines = (self._partial + blob).split(b"\n")` and `self._partial = lines.pop()`. Partial writes at EOF are retained until completed with newline. |
| **Truncation / Rotation** | **PASS** | If `size < self._offset`, `self._offset = size` and `self._partial = b""`, resetting safely without crashing. |
| **Clean Exit on All Paths** | **FAIL** | Process spawn is outside `try ... finally`. Cancellation during `create_subprocess_exec` leaks processes. |
| **Resumed Session Boundary** | **FAIL** | Missing file on resumed session defaults offset to 0 instead of handling deferred EOF binding. |
| **Stdout Classification & Forward Compatibility** | **FAIL** | `classify_event` prioritizes `or chunk` over `thought`/`reasoning` step types. |
| **Exact-Once / Dedup Semantics** | **FAIL** | Collapses distinct identical brain records due to set-of-strings dedup. |

---

## Smallest Correction Specification

To bring this implementation to an **ACCEPT** standard, apply the following targeted fixes:

### 1. Fix `classify_event` Evaluation Order
In [`src/agent_bridge/adapters/antigravity.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/adapters/antigravity.py):
Move the thought/reasoning check **before** checking `if step_type == "agent_response" or chunk:`.
```python
    if step_type in {"checkpoint", "thought", "reasoning"} or event in {"thought", "reasoning"}:
        text = _first_text(
            step, obj, keys=("text", "text_delta", "thought", "thinking", "reasoning", "summary")
        )
        if text:
            return "thought_chunk", {"text": text}
        if step_type == "checkpoint":
            return "raw", {}

    if step_type == "agent_response" or chunk:
        if chunk:
            return "message_chunk", {"text": chunk}
        return "raw", {}
```

### 2. Protect Process Spawn Under Cancellation and Use Deterministic Test Sync
Wrap process creation and task scheduling in an outer `try ... except asyncio.CancelledError ... finally` block, or track `proc` in a scope that cleans up if interrupted:
```python
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(...)
        self._procs[session.session_id] = proc
        ...
    except BaseException:
        if proc is not None:
            await reap_subprocess(proc)
        raise
```
In `tests/test_agy_dashboard.py::test_run_turn_cancel_cleans_up_brain_watcher`:
Instead of `await asyncio.sleep(0.3)`, wait for the PID to be recorded in `adapter._procs` or wait for an `asyncio.Event` triggered when the process begins executing.

### 3. Handle Missing Resumed Brain Log Safely
In `_BrainThoughtTail.bind`:
If `resumed=True` and the file does not currently exist, record `self._resumed_pending = True`.
In `drain()`:
If `self._resumed_pending` is True:
- If file exists, set `self._offset = path.stat().st_size` and `self._resumed_pending = False`.
- Do not read from byte 0.

### 4. Refine Deduplication Semantics
- Let the file offset handle ordering and deduplication for brain log records.
- Use `self._emitted_from_stdout: set[str]` specifically for cross-source deduplication with stdout `step_update` events, rather than a global suppression set that drops repeated brain thoughts.
