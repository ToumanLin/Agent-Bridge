# Dashboard Refactor Acceptance Review Report

**Date**: September 16, 2026  
**Role**: Independent Read-Only Reviewer  
**Worktree**: `C:\Users\Touma\Documents\Agent-Bridge` (branch `main`, HEAD `95e9158`)  
**Primary Architecture Reference**: `.agent-bridge-reports/09-16-2026-dashboard-refactor-report.md`  
**Review Target**: Uncommitted refactor candidate decomposing `src/agent_bridge/share/dashboard.py` into `dashboard_events.py`, `dashboard_outbox.py`, and `dashboard_page.py`.

---

## 1. Executive Summary & Verdict

### Verdict: **ACCEPT**

The uncommitted dashboard refactor successfully decomposes [`src/agent_bridge/share/dashboard.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard.py) from 3,537 lines to 630 lines by isolating domain responsibilities into three cohesive sibling modules:
1. [`src/agent_bridge/share/dashboard_events.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_events.py) (380 lines): Transcript parsing, event normalization, and incremental live usage tail caching.
2. [`src/agent_bridge/share/dashboard_outbox.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_outbox.py) (186 lines): Outbox queue inspection, state loading, and process-ownership liveness checks.
3. [`src/agent_bridge/share/dashboard_page.py`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_page.py) (2,515 lines): Raw multiline string `PAGE` containing HTML, CSS design system, and client-side JavaScript SPA.

All 8 review invariants were challenged with independent static analysis and dynamic execution. Zero regressions were discovered. The refactor maintains 100% backwards compatibility for runtime CLI execution, HTTP endpoints, CSP headers, test rebinding, packaging, and Node testing harnesses, while preserving the user's uncommitted title-bar avatar removal and benchmark timing adjustments.

---

## 2. Verification Execution Matrix

| Test Suite / Verification Step | Scope | Command | Result | Notes |
| :--- | :--- | :--- | :--- | :--- |
| **Focused Python Test Suite** | 127 tests across dashboard page, Antigravity adapter, launcher/open, outbox/events characterization, packaging | `uv run pytest tests/test_dashboard_page.py tests/test_agy_dashboard.py tests/test_dashboard_open.py tests/test_dashboard_events.py tests/test_dashboard_outbox.py tests/test_packaging.py` | **127 passed in 22.91s** | Covers HTTP routing, live-tail resets, outbox atomic claims, and adapter event parsing. |
| **Node Status Behavior Harness** | ~80 assertions on proc states, durations, i18n dictionaries, export sanitization | `node tests/dashboard_status_behavior.js` | **100% Passed (code 0)** | Verified extracted script from `dashboard_page.py` in VM context with DOM stub. |
| **Replay Benchmark** | DOM batching, rAF slicing, live tokens, live week tokens under jsdom | `node scripts/bench_dashboard_replay.js` | **Healthy (code 0)** | `jsError: false`, `doneFrozen: true`, `runAdvanced: true`. Preserves user's 2200ms sleep. |
| **Linter / Formatting** | Ruff syntax and formatting rules | `uv run ruff check` | **All checks passed** | `pyproject.toml` correctly configured with `RUF001/002/003` ignore for localized CJK prose in `dashboard_page.py`. |
| **Direct Script Execution** | Standalone launch with unset `__package__` | `python src/agent_bridge/share/dashboard.py --port <port>` | **Passed** | Tested from repo root and external temporary working directory. Serves `/` and `/api/overview` with HTTP 200. |
| **Wheel Packaging & Smoke** | Hatchling wheel packaging and isolated unpacked execution | `uv build --wheel` + unzip + launch `--help` | **Passed** | Wheel contains all 4 `agent_bridge/share/` Python modules. Unpacked CLI runs cleanly. |
| **PAGE Byte Equivalence** | Compare served `PAGE` string against git baseline `95e9158` | Python AST & difflib comparison | **Passed** | 21 diff lines total, matching exactly the user's uncommitted title-bar avatar removal. |

---

## 3. Invariant-by-Invariant Evaluation

### Invariant 1: Direct Script Execution with `__package__` Unset
- **Status**: **SATISFIED**
- **Evidence**:
  - In [`src/agent_bridge/share/dashboard.py#L32-L40`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard.py#L32-L40):
    ```python
    if __package__:
        from . import dashboard_events as _events
        from . import dashboard_outbox as _outbox
        from . import dashboard_page as _page
    else:
        import dashboard_events as _events
        import dashboard_outbox as _outbox
        import dashboard_page as _page
    ```
  - In [`src/agent_bridge/share/dashboard_outbox.py#L27-L30`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard_outbox.py#L27-L30):
    ```python
    if __package__:
        from .dashboard_events import SAFE_ID
    else:
        from dashboard_events import SAFE_ID
    ```
  - Direct execution was tested by spawning a subprocess running `python src/agent_bridge/share/dashboard.py --port <dynamic_port>` with current working directory set to `tempfile.gettempdir()`. The server successfully bound to the port, returned `200 OK` for `GET /` with `<!DOCTYPE html>`, and returned `200 OK` with session data for `GET /api/overview`.

### Invariant 2: Canonical `PAGE` Identity, Served Bytes, and Headers
- **Status**: **SATISFIED**
- **Evidence**:
  - Identity: [`src/agent_bridge/share/dashboard.py#L176`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard.py#L176) defines `PAGE = _page.PAGE`. In [`tests/test_dashboard_page.py#L170-L174`](file:///C:/Users/Touma/Documents/Agent-Bridge/tests/test_dashboard_page.py#L170-L174), `assert dashboard.PAGE is dashboard_page.PAGE` verifies object identity.
  - Byte Equivalence: Unified diff of `dashboard.PAGE` against `git show 95e9158:src/agent_bridge/share/dashboard.py` reveals exactly 21 diff lines. The only changes are:
    1. Removal of `.avatar{flex:none;width:46px;height:46px;...}` in embedded CSS.
    2. Removal of `<div class="avatar">${agentAvatar(s,26)}</div>` in `renderSessionHeader()`.
    No other markup, logic, script, or localization strings were altered.
  - Headers & CSP: [`src/agent_bridge/share/dashboard.py#L204-L210`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard.py#L204-L210) retains the verbatim CSP string (`default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'`), `Content-Type: text/html; charset=utf-8`, and `X-Content-Type-Options: nosniff`.

### Invariant 3: Runtime and Test Dynamic Rebinding
- **Status**: **SATISFIED**
- **Evidence**:
  - `STATE_FILE`, `TRANSCRIPT_DIR`, `OUTBOX_DIR`: In `dashboard.py`, these module-level variables are reassigned when `main()` parses `--dir` (lines 610–615). The wrapper functions in `dashboard.py` (`load_state`, `read_events`, `_live_consumed`, `live_usage_map`, `outbox_queue`) pass `STATE_FILE`, `TRANSCRIPT_DIR`, or `OUTBOX_DIR` explicitly down to `_events` and `_outbox` on each call.
  - `open` injection: `load_state()` in `dashboard.py` (line 122) passes `open` explicitly: `return _outbox.load_state(STATE_FILE, open)`. Monkeypatching `dashboard.open` in tests intercepts file reading as proven by [`tests/test_dashboard_outbox.py#L112-L128`](file:///C:/Users/Touma/Documents/Agent-Bridge/tests/test_dashboard_outbox.py#L112-L128).
  - `psutil` injection: `_my_create_time()`, `_owner_alive()`, `_dash_claim_name()`, and `_task_action_fields()` dynamically pass `psutil` down to `_outbox`. Monkeypatching `dashboard.psutil` in tests takes effect immediately, validated by [`tests/test_dashboard_outbox.py#L140-L183`](file:///C:/Users/Touma/Documents/Agent-Bridge/tests/test_dashboard_outbox.py#L140-L183).

### Invariant 4: Identity, Caching, Normalization, and API Semantics
- **Status**: **SATISFIED**
- **Evidence**:
  - `_LIVE_TAIL` and `_LIVE_LOCK`: Re-exported directly by reference in [`src/agent_bridge/share/dashboard.py#L146-L147`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard.py#L146-L147). Tested via `assert dashboard._LIVE_TAIL is dashboard_events._LIVE_TAIL`.
  - Process create-time caching: Stored in `dashboard_outbox._MY_CREATE_TIME` and `_MY_CREATE_TIME_SET`. `dashboard.py` defines `__getattr__` (lines 125–131) returning these attributes from `_outbox`, maintaining a single live cache.
  - `SAFE_ID`: Compiled once in `dashboard_events.py` (line 20) and shared across `dashboard_outbox.py` and `dashboard.py`.
  - Endpoint JSON and Status Codes: `Handler` remains in `dashboard.py` (lines 179–582). All status codes (200, 400, 404, 409, 500) and JSON payloads (`error_code` strings, `events`, `offset`, `reset`, `clients`, `outbox`, `live`) are structurally identical to the baseline.

### Invariant 5: Wheel Packaging and Distribution Layout
- **Status**: **SATISFIED**
- **Evidence**:
  - In `pyproject.toml`, Hatchling includes `packages = ["src/agent_bridge"]`. Because all three extracted files are Python source modules within `src/agent_bridge/share/`, they are automatically bundled without requiring `force-include` modifications.
  - Inspection of `uv build --wheel` archive verified:
    - `agent_bridge/share/dashboard.py`
    - `agent_bridge/share/dashboard_events.py`
    - `agent_bridge/share/dashboard_outbox.py`
    - `agent_bridge/share/dashboard_page.py`
  - Unpacked wheel test confirmed `agent_bridge/paths.py::bundled_dashboard()` can point to `dashboard.py` and execute cleanly in an installed layout.

### Invariant 6: Node Test Harness & Benchmark Preserved
- **Status**: **SATISFIED**
- **Evidence**:
  - `tests/dashboard_status_behavior.js` (lines 21–25): path updated to read `dashboard_page.py`. Regex `/PAGE = r"""([\s\S]*?)"""/` extracts `PAGE` and executes VM assertions without failure.
  - `scripts/bench_dashboard_replay.js` (lines 40–45): fallback path updated to read `dashboard_page.py`. Line 391 retains `await sleep(2200);`, ensuring 2 clock ticks elapse so live clock assertions remain stable.

### Invariant 7: Worktree Integrity
- **Status**: **SATISFIED**
- **Evidence**:
  - `git status` confirms no unrelated files were touched.
  - User's uncommitted title-bar avatar removal is preserved in `dashboard_page.py`.
  - User's uncommitted 2200ms benchmark sleep is preserved in `scripts/bench_dashboard_replay.js`.
  - Only expected report and new module files were introduced.

### Invariant 8: Code Quality and Architectural Hygiene
- **Status**: **SATISFIED**
- **Evidence**:
  - **No Circular Imports**: The dependency graph is a strict DAG:
    - `dashboard_page`: 0 internal imports (pure leaf).
    - `dashboard_events`: 0 internal imports (pure leaf).
    - `dashboard_outbox`: imports only `SAFE_ID` from `dashboard_events`.
    - `dashboard`: imports `dashboard_events`, `dashboard_outbox`, `dashboard_page`.
  - **No Duplicate State**: Constants and locks (`_LIVE_LOCK`, `_LIVE_TAIL`, `SAFE_ID`) are defined in one place and referenced.
  - **Clean Parameter Decoupling**: Extracted functions take directory paths and callers (`open`, `psutil`) explicitly rather than binding to parent module globals.

---

## 4. Severity-Ranked Findings & Observations

### Low Severity / Informational Findings

1. **`dashboard.__getattr__` Rebinding Asymmetry**
   - **Location**: [`src/agent_bridge/share/dashboard.py#L125-L131`](file:///C:/Users/Touma/Documents/Agent-Bridge/src/agent_bridge/share/dashboard.py#L125-L131)
   - **Observation**: `__getattr__` provides read transparency for `_MY_CREATE_TIME` and `_MY_CREATE_TIME_SET` from `dashboard_outbox`. However, module assignment in Python (e.g. `dashboard._MY_CREATE_TIME = val`) writes directly to `dashboard.__dict__` rather than delegating to `_outbox`.
   - **Impact**: Zero runtime impact in production since `_my_create_time()` modifies `_outbox._MY_CREATE_TIME` directly. Tests that mock this variable should patch `dashboard_outbox._MY_CREATE_TIME` (as `test_dashboard_outbox.py` already does).
   - **Action**: No change required; documented as residual risk.

2. **Error Code Reflection via Source Parsing**
   - **Location**: [`tests/test_dashboard_page.py#L1250`](file:///C:/Users/Touma/Documents/Agent-Bridge/tests/test_dashboard_page.py#L1250)
   - **Observation**: `test_i18n_send_err_covers_emitted_codes()` discovers emitted HTTP error codes by scanning `Path(dashboard.__file__).read_text()`. Because `Handler` and its endpoint error codes were retained in `dashboard.py`, the test continues to pass completely.
   - **Impact**: Future refactors that move HTTP routing handlers out of `dashboard.py` must remember to update this test's search path.
   - **Action**: None required for this slice.

---

## 5. Residual Risks

1. **Direct Script Execution Relies on `sys.path[0]`**:
   When launching via `python share/dashboard.py`, Python sets `sys.path[0]` to the directory containing `dashboard.py`. If a caller uses an esoteric launcher that sets `__package__` to a non-existent package or clears `sys.path[0]`, the sibling imports could fail. This is standard Python script behavior and fully covered by both `agent_bridge/dashboard.py` subprocess launch and standalone CLI invocation.

2. **Node Regex Dependency on Formatting of `PAGE = r"""..."""`**:
   `tests/dashboard_status_behavior.js` and `scripts/bench_dashboard_replay.js` depend on `PAGE` being defined as a raw triple-quoted string with exact naming `PAGE = r"""..."""`. Any future change to dynamically construct `PAGE` via string concatenation or template rendering will require updating those Node regexes.

---

## 6. Final Recommendation

**ACCEPT Candidate Uncommitted Refactor as-is.**  
The refactor adheres strictly to the architectural constraints, exhibits zero functional regressions, preserves all user edits, cleans up 2,900 lines from `dashboard.py`, and maintains rigorous test coverage.
