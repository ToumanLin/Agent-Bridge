# Dashboard Durable Launch — Worker Report

**Document Version:** 1.0.0
**Date:** September 18, 2026
**Role:** Worker-Dashboard-Durable-Launch
**Report File:** `C:\Users\Touma\Documents\Agent-Bridge\.agent-bridge-reports\09-18-2026-dashboard-durable-launch-worker.md`
**Repository Worktree:** `C:\Users\Touma\Documents\Agent-Bridge` (branch: `main`)
**Scope:** Make Windows dashboard launch durable when `CREATE_BREAKAWAY_FROM_JOB` is refused, so the dashboard is created outside the coordinator host's `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` Job Object.

---

## 1. Objective & Outcome

`src/agent_bridge/dashboard.py` previously retried a refused job breakaway with an ordinary `DETACHED_PROCESS` `Popen`, which stays inside the host Job Object and dies with Bridge. The fallback is now a **brokered launch through WMI `Win32_Process.Create`**: the WMI service (`WmiPrvSE`, outside whatever job an agent host put Bridge into) creates the process as the calling user, non-elevated, non-interactive, with no window (session 0). No persistent scheduled task, no shell parsing of the command line, no in-job `Popen` anywhere on the refused path.

The direct breakaway attempt remains the fast path; POSIX behavior is unchanged.

---

## 2. Changed Files

| File | Change |
| :--- | :--- |
| `src/agent_bridge/dashboard.py` | Added `_powershell_path()`, `_popen_brokered()`, `_BROKER_*` constants; `_popen_detached(argv, out, log_path)` brokers via WMI on refused breakaway instead of in-job `Popen`; `_launch` passes the log path. |
| `tests/test_dashboard_open.py` | Rewrote the refused-breakaway test to assert the brokered route (single `Popen` with breakaway flags only); added broker stub `_stub_broker`; added tests for safe quoting, hidden broker, broker nonzero/exception failure; updated the flag-constants pin and section comment. |

No other files were modified. `src/agent_bridge/adapters/antigravity.py`, `tests/fake_agy.py`, `tests/test_agy_dashboard.py`, `tests/test_agy_parse.py` and existing reports retain their pre-existing uncommitted/user state — not touched, staged, or committed.

---

## 3. Design

### 3.1 Launch flow (Windows)

```
_launch(home, host, port)
  argv = [sys.executable, script, --port, port, --dir, home]
  _popen_detached(argv, out, log_file)
    fast path: Popen(creationflags = NEW_PROCESS_GROUP|DETACHED|BREAKAWAY_FROM_JOB)
    on OSError (breakaway refused):
      _popen_brokered(argv, log_file)
        cmdline = list2cmdline([sys.executable, -c, _BROKER_STUB, log_file, *argv[1:]])
        env[AGENT_BRIDGE_DASHBOARD_CMDLINE] = cmdline
        env[AGENT_BRIDGE_DASHBOARD_CWD]     = <logs dir>
        subprocess.run(powershell -NoProfile -NonInteractive -WindowStyle Hidden
                       -Command <fixed _BROKER_PS>, env=env, CREATE_NO_WINDOW, timeout=30)
          → Invoke-CimMethod Win32_Process.Create(CommandLine=cmdline, CurrentDirectory=cwd)
          → WMI service spawns the dashboard OUTSIDE our Job Object
          → stub rebinds std streams to dashboard.log, runs script via runpy
```

### 3.2 Mechanism choice

| Requirement | How it is met |
| :--- | :--- |
| Creates process outside caller's Job Object | `Win32_Process.Create` executes in the WMI provider service, not in Bridge's process tree — job membership cannot be inherited. |
| Non-elevated, standard user | `Win32_Process.Create` on the local machine requires no admin for the calling user; verified on this host (`ReturnValue=0`, real PID). |
| Non-interactive, hidden, no visible window | Child lands in session 0 / non-interactive window station; the PowerShell broker itself runs `CREATE_NO_WINDOW -NonInteractive -WindowStyle Hidden` and exits in ~1s. |
| No persistent scheduled task | One transient WMI method call; zero artifacts. |
| No shell-command injection | The `-Command` text is a fixed literal (`_BROKER_PS`); the command line travels inside the PowerShell child's **environment block** (`$env:...`), never through string interpolation. No `cmd /c` anywhere. |
| Safe quoting (spaces/metacharacters) | `subprocess.list2cmdline` produces the single Win32 command line; `CommandLineToArgvW` round-trips it exactly. Verified end-to-end with `a&b 'c' &"d"` and spaced/`&` paths. |
| Confirmed launch or reported failure | `_BROKER_PS` exits with `Win32_Process.Create`'s `ReturnValue`; nonzero → logged warning + `OSError` → `_launch` returns `False`. `TimeoutExpired`/`OSError` from `subprocess.run` → logged + propagates → `False`. Success logs the created PID. |

### 3.3 `_BROKER_STUB` — why it exists and why runpy

`Win32_Process.Create` supports no std-handle redirection, so a bare `python dashboard.py` would drop the `dashboard.log` capture the direct path has. The `-c` stub rebinds `sys.stdin/stdout/stderr` (nul + append-mode log) and then runs the script via `runpy.run_path(..., run_name="__main__")` with the script's directory inserted into `sys.path` — needed because `share/dashboard.py` imports `dashboard_events`, `dashboard_outbox`, `dashboard_page` as siblings when run as a script.

Two pitfalls were found and fixed during real-host verification:

1. **`os.execv` re-quote bug**: the original stub used `os.execv`; on Windows that re-spawns via the CRT's naive quoting, which mangles arguments containing embedded `"` — the payload arrived truncated. `runpy` runs the script **in-process**, eliminating the second quoting round-trip entirely (one process, and WMI's `ProcessId` is the actual dashboard).
2. **Invalid std handles**: WMI-created processes have *invalid* (not absent) std handles, so `os.dup2(f, 1)` fails with `ERROR_INVALID_HANDLE`. Python-level stream rebinding sidesteps CRT handle state.

### 3.4 Preserved behavior

- Singleton/port logic (`SO_EXCLUSIVEADDRUSE`), `.dashboard-open` cooldown, debounces, `_CONFIRM_SEC` recheck, `webbrowser.open` — untouched.
- `dashboard.log` receives the same startup prints as the direct path (verified: `"Agent Bridge dashboard → http://127.0.0.1:8897"` / `"data dir: ..."` captured through the stub).
- Error/log surface: refused breakaway logs a warning naming the WMI route; broker failure logs exit code + stderr or the exception; `_launch` still returns `False` via `log.exception` on any failure — never claims success.
- No `CREATE_BREAKAWAY_FROM_JOB` constant (ancient Python): unchanged — single base-flags attempt, no broker, no retry (`test_launch_windows_without_breakaway_constant`).

---

## 4. Verification (Tier 1)

### 4.1 Required command

```
uv run pytest tests/test_dashboard_open.py
26 passed in 0.20s
```

All 26 tests pass, covering every acceptance criterion:

| Criterion | Test |
| :--- | :--- |
| Direct breakaway used when allowed | `test_launch_windows_breaks_out_of_host_job` |
| Refused breakaway → brokered route, never in-job Popen | `test_launch_windows_brokers_when_breakaway_refused` (asserts sole Popen call carries `BREAKAWAY`), `test_launch_windows_failure_returns_false` |
| Broker failure returns False | `test_launch_windows_broker_failure_returns_false` (WMI ReturnValue≠0), `test_launch_windows_broker_exception_returns_false` (timeout/missing exe) |
| Spaces/metacharacters safely conveyed | `test_launch_windows_broker_quotes_paths_safely` (env cmdline == `list2cmdline(...)`; `-Command` arg is the fixed literal) |
| No visible helper window | `test_launch_windows_broker_runs_hidden` (`CREATE_NO_WINDOW`, `-NonInteractive`, `-WindowStyle Hidden`, `stdin=DEVNULL`) |
| POSIX unchanged | `test_launch_posix_uses_new_session`, `test_launch_posix_failure_returns_false` |
| Missing script never spawns | `test_launch_missing_script_never_spawns` |

`uv run ruff check src/agent_bridge/dashboard.py tests/test_dashboard_open.py` → all checks passed.

### 4.2 Real-host spot checks (this machine is Windows; beyond the stubbed suite)

1. `Invoke-CimMethod -ClassName Win32_Process -MethodName Create` directly → `ReturnValue=0`, real `ProcessId`.
2. Real `_popen_brokered(argv, log)` call → WMI-created process wrote the marker containing `a&b 'c' &"d"` verbatim (paths under `sub dir & stuff\`), and its stdout landed in `dash board.log`.
3. `_BROKER_STUB` running the real `share/dashboard.py` → server answered `GET /api/client_state`; `dashboard.log` captured the startup prints; sibling imports resolved.

---

## 5. Residual Risks & Notes

- **WMI service unavailable/disabled** (`winmgmt` stopped, heavily stripped images) → broker fails → `_launch` logs + returns `False`; the next MCP call retries after `_ATTEMPT_DEBOUNCE_SEC`. Deliberate: better a logged miss than an in-job child that dies with Bridge.
- **`powershell.exe` missing** (Server Core minimal, Nano) → `OSError` → logged `False`. `_powershell_path()` prefers `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`, falling back to PATH.
- **Confirmation depth**: WMI `ReturnValue=0` means the process was created — same guarantee level as `Popen` success; it does not prove the port bound. `_wait_for_dashboard` still polls `/api/client_state` and warns on no response, unchanged.
- **Environment/session**: the brokered dashboard gets the WMI provider's environment and runs in session 0 — it only needs absolute paths (`sys.executable`, script, `--dir`); `psutil` import in the dashboard is already optional.
- **Timing**: a cold WMI call can take a few seconds; bounded by `_BROKER_TIMEOUT_SEC = 30` on the daemon launcher thread, so tool calls never block.
- **`_popen_detached` signature** gained a required `log_path` parameter — internal helper, sole caller is `_launch`.
- **Log stream nuance**: brokered stdout is a text stream (`errors="replace"`) vs. the binary `ab` file on the direct path — the dashboard emits only `print()` lines, so observable behavior is equivalent.

---

## 6. Do-Not-Touch Compliance

- No commits made.
- `Registry`/`idle_exit_due`, `stall_timeout_sec`, heartbeat interval/TTL, adapters, task ownership — unchanged.
- Pre-existing uncommitted changes in `antigravity.py`, `tests/fake_agy.py`, `tests/test_agy_dashboard.py`, `tests/test_agy_parse.py` — untouched.
- Report's out-of-scope `Registry.idle_exit_due` presence-probing suggestion was not implemented, per instructions.

---

*Report compiled and verified by Worker-Dashboard-Durable-Launch on 2026-09-18.*
