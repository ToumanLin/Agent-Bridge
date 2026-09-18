REJECT

# Acceptance Review: Dashboard Durable Lifecycle & Presence Heartbeat Frequency

**Date:** 2026-09-18  
**Reviewer:** Reviewer-Dashboard-Lifecycle (Independent, Adversarial Read-Only Acceptance Review)  
**Worktree:** `C:\Users\Touma\Documents\Agent-Bridge` on branch `main` (uncommitted candidate state)  
**Candidate Files Under Review:**
- `src/agent_bridge/dashboard.py`
- `src/agent_bridge/share/dashboard.py`
- `src/agent_bridge/share/dashboard_page.py`
- `tests/test_dashboard_open.py`
- `tests/test_dashboard_page.py`

**Handoff References:**
- `.agent-bridge-reports/09-18-2026-dashboard-lifecycle-report.md`
- `.agent-bridge-reports/09-18-2026-dashboard-durable-launch-worker.md`
- `.agent-bridge-reports/09-18-2026-dashboard-heartbeat-worker.md`

---

## 1. Executive Summary & Verdict

### **Verdict: REJECT**

The candidate makes substantial, architecturally sound progress toward escaping Windows Job Object fate-sharing:
1. It replaces the previous fatal behavior (relaunching inside the caller's `KILL_ON_JOB_CLOSE` Job Object) with an out-of-process WMI `Win32_Process.Create` broker.
2. It uses `subprocess.list2cmdline` passed via environment variables, avoiding PowerShell string interpolation and shell command injection.
3. It successfully updates presence heartbeats to exactly 15,000 ms with a 180.0-second server lease, passing all unit and browser status lifecycle suites while leaving unrelated dirty files completely untouched.

However, the candidate **must be rejected** due to a critical defect in Windows broker failure signaling (**Invariant 2** & **Invariant 7**):
- **Silent False Success on WMI/CIM Failure**: In `_BROKER_PS`, when `Invoke-CimMethod` encounters an unhandled PowerShell/CIM error (e.g. WMI service `winmgmt` stopped/disabled, RPC unavailable, invalid namespace/class), `$r` is `$null`. PowerShell evaluates `if ($r.ReturnValue -ne 0)` to `$True` (because `$null -ne 0` is `$True` in PowerShell), and executes `exit [int]$null`. In PowerShell, `[int]$null` evaluates to `0`. Consequently, PowerShell exits with **exit code 0** and empty stdout. In `_popen_brokered`, the returncode check passes, `pid` defaults to `""`, the function logs `created pid unknown` at INFO level, raises no exception, and `_launch` returns `True`.
- This violates the explicit contract stated in the worker report ("WMI service unavailable/disabled -> broker fails -> _launch logs + returns False; never claims success") and masks underlying WMI provider errors.

A second secondary defect (**Medium/Low**) exists where relative data directories (`home`) passed to `_launch` cause WMI to reject `CurrentDirectory` with error code 8 because `log_path.parent` is not resolved to an absolute path.

Both defects have minimal, concrete corrections detailed in Section 5 below.

---

## 2. Invariant Challenge & Verification Matrix

| # | Invariant | Status | Findings / Evidence |
| :- | :--- | :--- | :--- |
| **1** | **Direct breakaway fast path & no in-job retry** | **PASS** | `_popen_detached` attempts `CREATE_BREAKAWAY_FROM_JOB` (`0x01000000`) first. On `OSError`, the fallback routes exclusively to `_popen_brokered`; no second `Popen` inside the job is ever performed. Proved empirically with a Windows Job Object configured with `KILL_ON_JOB_CLOSE` where direct breakaway failed with `WinError 5 Access is denied` and the WMI brokered process survived job closure. |
| **2** | **WMI broker: outside job, standard user, hidden, bounded, signals failure** | **FAIL (Defect)** | The process spawns under `WmiPrvSE.exe` outside the caller's Job Object, runs non-elevated as the calling user, hidden via `CREATE_NO_WINDOW` / `-WindowStyle Hidden`, and is bounded by `_BROKER_TIMEOUT_SEC = 30.0`. However, **failure signaling is flawed**: when `Invoke-CimMethod` throws a CimException, PowerShell exits with code 0 (`exit [int]$null`), causing `_popen_brokered` to silently report success (`_launch == True`). |
| **3** | **Quoting & injection safety** | **PASS** | `_BROKER_PS` is a static constant. The command line is constructed with `subprocess.list2cmdline` and passed via `AGENT_BRIDGE_DASHBOARD_CMDLINE` environment variable. `_BROKER_STUB` executes in-process via `runpy.run_path`. Verified with exotic paths containing spaces, single quotes, double quotes, ampersands, semicolons, dollar signs, percent signs, backticks, and Chinese Unicode characters (`test dir & 'single' ; $doll% `back` 测试`). |
| **4** | **Sibling imports, sys.argv, streams, process lifecycle** | **PASS** | `_BROKER_STUB` rebinds `sys.stdin` to `os.devnull`, `sys.stdout`/`sys.stderr` to line-buffered log file (`buffering=1, errors='replace'`), sets `sys.argv` correctly (`sys.argv[2:]`), and pushes `os.path.dirname(sys.argv[0])` to `sys.path[0]`. Sibling imports (`dashboard_events`, `dashboard_outbox`, `dashboard_page`) resolve cleanly. `SO_EXCLUSIVEADDRUSE` on NT terminates duplicate servers immediately with exit code 1; `.dashboard-open` file lock prevents duplicate browser openings. |
| **5** | **POSIX & normal Windows compatibility** | **PASS** | POSIX branch in `_popen_detached` remains isolated (`start_new_session=True`). Windows fast path attempts direct breakaway first. |
| **6** | **Presence timing (15000 ms / 180 s) & lifecycle** | **PASS** | Frontend interval is exactly `15000` ms (`src/agent_bridge/share/dashboard_page.py#L465`). Server TTL is `180.0` s (`src/agent_bridge/share/dashboard.py#L56`). Immediate startup ping, lifecycle listeners (`pageshow`, `focus`, `online`, `visibilitychange`), `pagehide` `sendBeacon` (`bye=1`), per-page `crypto.randomUUID()`, and polling timers (`pollOverview` 3000ms, `pollEvents` 1500ms, `tickDurations` 1000ms) remain intact. |
| **7** | **Meaningful test contract verification** | **PARTIALLY FLAWED** | Existing tests verify breakaway flags, quoting, hidden flags, and mocked nonzero returncode. However, tests fail to assert broker behavior when WMI returns exit code 0 with an empty PID or stderr exception output. |
| **8** | **Minimal scope & preservation of unrelated dirty files** | **PASS** | Only the 5 candidate files were modified. The 4 unrelated modified files (`src/agent_bridge/adapters/antigravity.py`, `tests/fake_agy.py`, `tests/test_agy_dashboard.py`, `tests/test_agy_parse.py`) remain completely untouched. |

---

## 3. Severity-Ranked Findings

### 3.1 [HIGH] Silent False Success on WMI/PowerShell CIM Failures
- **File & Lines:** `src/agent_bridge/dashboard.py#L56-L60`, `src/agent_bridge/dashboard.py#L132-L137`
- **Symbols:** `_BROKER_PS`, `_popen_brokered`
- **Evidence & Mechanism:**
  In `_BROKER_PS`:
  ```powershell
  $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $env:AGENT_BRIDGE_DASHBOARD_CMDLINE; CurrentDirectory = $env:AGENT_BRIDGE_DASHBOARD_CWD };
  if ($r.ReturnValue -ne 0) { exit [int]$r.ReturnValue };
  Write-Output $r.ProcessId
  ```
  If `Invoke-CimMethod` encounters a PowerShell or CIM exception (such as when the WMI service `winmgmt` is stopped or disabled, RPC server is unavailable, or class/namespace metadata fails):
  1. `Invoke-CimMethod` writes an error to the error stream (`proc.stderr`).
  2. Because default `$ErrorActionPreference` is `'Continue'`, script execution continues to the next statement.
  3. `$r` is `$null`.
  4. In PowerShell, `$null -ne 0` evaluates to `$True`.
  5. The script executes `exit [int]$r.ReturnValue` -> `exit [int]$null`.
  6. In PowerShell, `[int]$null` evaluates to `0`.
  7. PowerShell terminates with **exit code 0**.
  8. In `_popen_brokered`:
     ```python
     if proc.returncode != 0:
         ...
         raise OSError(...)
     pid = (proc.stdout or "").strip()
     log.info("dashboard auto-open: brokered launch created pid %s", pid or "unknown")
     ```
     `proc.returncode != 0` is `False`. `pid` is `""`. No exception is raised.
  9. `_launch` logs `dashboard auto-open: launched ...` and returns `True`.
- **Impact:**
  When WMI fails catastrophically, Bridge falsely claims the dashboard was launched successfully, suppressing error logs and causing downstream components to wait 4 seconds in `_wait_for_dashboard` before logging a generic `no response` warning. The actual diagnostic reason (the CIM exception in `proc.stderr`) is swallowed and discarded.

---

### 3.2 [MEDIUM] WMI Process Creation Rejection on Relative `home` Paths
- **File & Lines:** `src/agent_bridge/dashboard.py#L108`, `src/agent_bridge/dashboard.py#L175-L181`
- **Symbols:** `_popen_brokered`, `_launch`
- **Evidence & Mechanism:**
  In `_launch`:
  ```python
  log_dir = home / "logs"
  log_file = log_dir / "dashboard.log"
  ...
  _popen_detached(argv, out, log_file)
  ```
  In `_popen_brokered`:
  ```python
  env[_BROKER_CWD_ENV] = str(log_path.parent)
  ```
  If `home` is supplied as a relative path (e.g. `Path(".agent-bridge")` or `Path("data")`), `log_path.parent` is a relative path.
  In Windows WMI, `Win32_Process.Create` requires `CurrentDirectory` to be an absolute path. Passing a relative path causes `Win32_Process.Create` to fail immediately with WMI `ReturnValue = 8` (Unknown failure / invalid parameter).
  While production Bridge setups via `ensure_home()` produce absolute paths, `dashboard.py` functions accept any `Path`.
- **Impact:**
  Any caller or test providing a relative `home` directory fails to broker via WMI with return code 8.
- **Remediation:**
  Ensure `log_path.resolve().parent` is passed to `_BROKER_CWD_ENV`.

---

### 3.3 [LOW] Missing Broker PID Validation in `test_dashboard_open.py`
- **File & Lines:** `tests/test_dashboard_open.py#L324-L340`
- **Symbols:** `test_launch_windows_broker_failure_returns_false`
- **Evidence & Mechanism:**
  The test suite mocks `subprocess.run` with `returncode=8` or `raises=TimeoutExpired`. No test exercises the scenario where the broker process exits with code 0 but outputs no PID or outputs error diagnostics on stderr, allowing Finding 3.1 to pass unnoticed.

---

## 4. Architectural & Security Evaluation

### 4.1 Windows Broker Security & Process Lifetime Design
1. **Job Object Escape**:
   `Win32_Process.Create` executes inside the Windows Management Instrumentation service host (`WmiPrvSE.exe`). As confirmed by live job-containment probes, this completely severs parent-child Job Object inheritance. When the calling MCP host terminates, Windows kernel closes the host's Job Object handle, killing in-job processes but leaving the WMI-created dashboard intact.
2. **Privilege & Session Isolation**:
   The call is made without credentials, impersonating the calling local user. The created process runs non-elevated in Session 0 / non-interactive desktop, which is appropriate for a headless HTTP service.
3. **Command Quoting & Injection Immunity**:
   The implementation is clean:
   - The PowerShell `-Command` argument is a static literal.
   - The command line travels inside an environment variable (`AGENT_BRIDGE_DASHBOARD_CMDLINE`).
   - The command line is constructed by Python's `subprocess.list2cmdline`, ensuring strict adherence to Win32 `CommandLineToArgvW` rules.
   - `_BROKER_STUB` uses `runpy.run_path` in-process rather than spawning another shell or running `os.execv`, avoiding second-round command line parsing issues.

### 4.2 Heartbeat Timing & Browser Throttling Mathematics
1. **Frequency & Cadence**:
   - Client interval: `15000` ms (15.0 s).
   - Server lease: `180.0` s (3.0 min).
2. **Request Volume**:
   Reduces heartbeat requests from 15 req/min per tab down to 4 req/min per tab (a 73.3% reduction).
3. **Chromium Background Timer Throttling**:
   Chromium throttles background tabs older than 5 minutes to a 1-minute tick boundary (~60s). Under this mode:
   - Expected heartbeats per lease: $\lfloor 180 / 60 \rfloor = 3$ heartbeats.
   - Missed-tick tolerance: If a hidden tab misses one 60s wake-up, the next beat occurs at ~120s, which is safely within the 180s lease.
   - Strict `>` comparison in `presence_update` / `presence_count` ensures boundary survival.
4. **Lifecycle Recovery**:
   Event listeners for `pageshow`, `focus`, `online`, and `visibilitychange` fire `ping(0)` immediately upon tab resurfacing, recovering presence after deep sleep or frozen states.

---

## 5. Required Corrections & Verification Specification

To achieve acceptance, apply the following minimal targeted changes (do NOT touch unrelated files):

### 5.1 Production Code Corrections in `src/agent_bridge/dashboard.py`

1. **Fix `_BROKER_PS`**:
   Add `$ErrorActionPreference = 'Stop';` and ensure failure to create a process or return a valid PID exits with a nonzero code:
   ```python
   _BROKER_PS = (
       "$ErrorActionPreference = 'Stop';"
       "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create"
       f" -Arguments @{{ CommandLine = $env:{_BROKER_CMD_ENV}; CurrentDirectory = $env:{_BROKER_CWD_ENV} }};"
       " if ($null -eq $r -or $r.ReturnValue -ne 0) { exit [int]$(if ($r -and $r.ReturnValue) { $r.ReturnValue } else { 1 }) };"
       " Write-Output $r.ProcessId"
   )
   ```

2. **Fix `_popen_brokered`**:
   - Resolve `log_path` parent to an absolute path for `_BROKER_CWD_ENV`:
     ```python
     env[_BROKER_CWD_ENV] = str(log_path.resolve().parent)
     ```
   - Validate that `pid` is non-empty and numeric, raising `OSError` with stderr diagnostics otherwise:
     ```python
     pid = (proc.stdout or "").strip()
     if not pid.isdigit():
         detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
         log.warning("dashboard auto-open: brokered launch failed to return valid pid: %s", detail or f"exit {proc.returncode}")
         raise OSError(f"brokered launch failed to return valid pid (exit {proc.returncode}): {detail}")
     log.info("dashboard auto-open: brokered launch created pid %s", pid)
     ```

### 5.2 Test Additions in `tests/test_dashboard_open.py`

Add a test asserting that exit code 0 with an empty PID or stderr output is reported as a failure:
```python
def test_launch_windows_broker_empty_pid_returns_false(tmp_path, monkeypatch):
    """If WMI or PowerShell encounters a CIM exception or fails to emit a valid PID,
    it must be reported as a launch failure (return False), not claimed as success."""
    _as_platform(monkeypatch, "win32")
    calls = _stub_spawn(tmp_path, monkeypatch, fail_times=1)
    _stub_broker(monkeypatch, returncode=0, stdout="", stderr="CimException: Service winmgmt stopped")
    assert dl._launch(tmp_path, "127.0.0.1", 8787) is False
    assert [c["creationflags"] for c in calls] == [_WIN32_BASE_FLAGS | _WIN32_BREAKAWAY]
```

---

## 6. Probes & Empirical Tests Run During Review

1. **Job Object Containment Probe** (`probe_job_breakaway.py`):
   - Created Win32 Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` without breakaway flag.
   - Assigned worker child to job; verified `CREATE_BREAKAWAY_FROM_JOB` failed with `[WinError 5] Access is denied`.
   - Invoked `_popen_brokered`; verified target process PID 11644 was spawned.
   - Closed Job Object handle; verified target process survived with exit code 259 (`STILL_ACTIVE`).
2. **WMI Quoting & Argument Round-Trip Probe** (`probe_wmi_broker.py` & `probe_quoting.py`):
   - Created target directory containing spaces, quotes, ampersands, semicolons, dollar signs, percent signs, backticks, and Chinese Unicode characters: `test dir & 'single' ; $doll% `back` 测试`.
   - Executed `_popen_brokered` with target script importing sibling module.
   - Verified log file captured startup prints, `sys.argv` matched verbatim, and sibling import succeeded (`SIBLING_MAGIC: 42`).
3. **PowerShell Error Exit Behavior Probe**:
   - Evaluated `Invoke-CimMethod` with missing/invalid class and `$null` ReturnValue.
   - Confirmed `exit [int]$null` exits with returncode 0 in Windows PowerShell, proving Finding 3.1.
4. **Focused Test Suites**:
   - `uv run pytest tests/test_dashboard_open.py tests/test_dashboard_page.py` (99 passed in 21.19s).
   - `node tests/dashboard_status_behavior.js` (all 741 checks passed).
   - `uv run ruff check src/agent_bridge/dashboard.py src/agent_bridge/share/dashboard.py src/agent_bridge/share/dashboard_page.py tests/test_dashboard_open.py tests/test_dashboard_page.py` (All checks passed).

---

## 7. Residual Risks & Notes (Post-Correction)

1. **Confirm Window vs 15s Heartbeat**:
   `_CONFIRM_SEC = 2.0s` is shorter than the 15-second heartbeat interval. If the dashboard server restarts while a client tab is open in the background, `_auto_open` may check client state before the tab's next scheduled ping. Mitigations (immediate ping on `focus`, `pageshow`, `online`, `visibilitychange`, and `_OPEN_COOLDOWN_SEC` throttle) bound duplicate popups, and the scope explicitly preserved `_CONFIRM_SEC`.
2. **Session 0 Environment**:
   Processes spawned via WMI run in Session 0 without an interactive console or window. Headless HTTP servers operate correctly in this environment, but scripts must not assume GUI window access.
