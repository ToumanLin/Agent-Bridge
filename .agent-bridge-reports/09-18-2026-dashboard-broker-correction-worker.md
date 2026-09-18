# Dashboard Broker Failure-Signaling Correction — Worker Report

**Document Version:** 1.0.0
**Date:** September 18, 2026
**Role:** Worker-Dashboard-Broker-Failure (correction pass)
**Report File:** `C:\Users\Touma\Documents\Agent-Bridge\.agent-bridge-reports\09-18-2026-dashboard-broker-correction-worker.md`
**Repository Worktree:** `C:\Users\Touma\Documents\Agent-Bridge` (branch: `main`, uncommitted)
**Primary Defect Report:** `.agent-bridge-reports/09-18-2026-dashboard-acceptance-review.md` (verdict REJECT, findings 3.1–3.3)
**Scope:** Correct the Windows WMI broker so CIM/PowerShell failures and missing/invalid PIDs can never be reported as launch success, and so WMI always receives an absolute `CurrentDirectory`.

---

## 1. Objective & Outcome

The candidate's broker had three defects, all corrected:

1. **`_BROKER_PS` silent false-success (HIGH):** default `ErrorActionPreference` let a terminating `Invoke-CimMethod` CIM failure leave `$r = $null`, after which `exit [int]$null` exited **0** with empty stdout — Python logged `pid unknown` and `_launch` returned `True`.
2. **`_popen_brokered` accepted exit-0 with no PID (HIGH):** stdout was never validated and stderr was discarded on the success path.
3. **Relative `CurrentDirectory` (MEDIUM):** `_BROKER_CWD_ENV` received unresolved `log_path.parent`; `Win32_Process.Create` rejects a relative `CurrentDirectory` with `ReturnValue = 8` — and the brokered session-0 child would also have resolved a relative log path / `--dir` against its own working directory, not ours.

The fixed broker only reports success when a real positive PID reaches stdout; every other outcome (CIM exception, null result, nonzero `ReturnValue`, missing `ProcessId`, garbage/empty stdout) logs diagnostics and raises, so `_launch` returns `False`. Live-verified on this host's real Windows PowerShell 5.1 (Section 4.2).

---

## 2. Exact Diff Summary

Only the two permitted code/test files were modified (plus this report).

### `src/agent_bridge/dashboard.py`

**`_BROKER_PS`** — rewritten from:
```powershell
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $env:AGENT_BRIDGE_DASHBOARD_CMDLINE; CurrentDirectory = $env:AGENT_BRIDGE_DASHBOARD_CWD }; if ($r.ReturnValue -ne 0) { exit [int]$r.ReturnValue }; Write-Output $r.ProcessId
```
to:
```powershell
$ErrorActionPreference = 'Stop'; try { $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $env:AGENT_BRIDGE_DASHBOARD_CMDLINE; CurrentDirectory = $env:AGENT_BRIDGE_DASHBOARD_CWD } } catch { Write-Error $_ -ErrorAction Continue; exit 1 }; $rc = 1; if ($null -ne $r -and $null -ne $r.ReturnValue) { $rc = [int]$r.ReturnValue }; if ($rc -ne 0) { Write-Error "Win32_Process.Create failed with ReturnValue $rc" -ErrorAction Continue; exit $rc }; if (-not $r.ProcessId) { Write-Error 'Win32_Process.Create returned no ProcessId' -ErrorAction Continue; exit 1 }; Write-Output $r.ProcessId
```
- `$ErrorActionPreference = 'Stop'` + `try/catch`: any CIM/PowerShell error writes to stderr and exits 1 — the `exit [int]$null → 0` hole is closed.
- `$rc` defaults to 1 and is only overwritten by a real `ReturnValue`: a `$null` result or `$null` `ReturnValue` can no longer be cast to a 0 exit.
- Missing/zero `ProcessId` exits 1. Diagnostics reach stderr in every failure branch; stdout carries only the PID.
- Remains a static `-Command` literal; transport stays env-block-only. Verified parseable (0 parse errors) and behaviorally correct on Windows PowerShell 5.1.

**`_popen_brokered`**:
- `log_path = log_path.resolve()` before use → `_BROKER_CWD_ENV` (`str(log_path.parent)`) and the stub's log-path argument are always absolute.
- New PID gate after the returncode check: `pid = stdout.strip()` must satisfy `pid.isdecimal() and int(pid) > 0` (exactly one positive decimal token; surrounding whitespace tolerated). Otherwise stderr/stdout detail is logged and `OSError` raised → `_launch` returns `False`.
- Docstring updated: "Raises unless WMI confirms the process was created **and reports its PID**."

**`_launch`**:
- `home = home.resolve()` at entry so `--dir`, `logs/dashboard.log`, and the `home/"dashboard.py"` fallback are absolute for the session-0 brokered child (which resolves relative paths against its own CWD). Harmless no-op for the absolute homes production uses.

### `tests/test_dashboard_open.py`

- Added `from pathlib import Path`.
- `test_launch_windows_breaks_out_of_host_job`: `--dir` expectation → `str(tmp_path.resolve())` (matches the new resolve; also correct on hosts where `tmp_path` contains a symlink).
- `test_launch_windows_broker_quotes_paths_safely`: expectations recomputed against `home.resolve()`; added `os.path.isabs` assertion on the broker CWD env.
- **New tests:**
  - `test_launch_windows_broker_zero_exit_no_pid_returns_false` — returncode 0, empty stdout, CimException-style stderr → `_launch` False; sole Popen carried breakaway flags.
  - `test_launch_windows_broker_invalid_pid_returns_false` (parametrized: `"garbage"`, `"1234 5678"`, `"ProcessId=4321"`, `"0"`, `" \r\n "`) → `_launch` False.
  - `test_launch_windows_broker_valid_pid_succeeds` (parametrized: `"4321\r\n"`, `"  9876 \n"`, `"42"`) → `_launch` True.
  - `test_launch_windows_broker_relative_home_gets_absolute_cwd` — relative `home` (`"rel home & stuff"` under a monkeypatched cwd) still brokers: env CWD is absolute and equals the resolved logs dir; the env command line equals `list2cmdline` of fully absolute stub/log/script/`--dir` arguments.

---

## 3. Preserved Semantics

- Direct `CREATE_BREAKAWAY_FROM_JOB` fast path unchanged; fallback routes exclusively to `_popen_brokered` — no in-job `Popen` anywhere.
- Broker stays bounded (`_BROKER_TIMEOUT_SEC = 30`), hidden (`CREATE_NO_WINDOW`, `-NoProfile -NonInteractive -WindowStyle Hidden`, `stdin=DEVNULL`), noninteractive.
- `_BROKER_PS` remains a static constant; command line still travels via `AGENT_BRIDGE_DASHBOARD_CMDLINE` through `subprocess.list2cmdline` — no interpolation, no injection surface added.
- `_BROKER_STUB` (runpy in-process, stream rebinding) untouched; POSIX branch untouched; accepted heartbeat files and all unrelated pre-existing dirty files untouched.

---

## 4. Verification (Tier 1)

### 4.1 Required commands

```
uv run pytest tests/test_dashboard_open.py
36 passed in 0.32s

uv run ruff check src/agent_bridge/dashboard.py tests/test_dashboard_open.py
All checks passed!
```

| Acceptance criterion | Test |
| :--- | :--- |
| rc 0 + empty stdout + CimException stderr → False | `test_launch_windows_broker_zero_exit_no_pid_returns_false` |
| rc 0 + nonnumeric/multi-token/nonpositive PID → False | `test_launch_windows_broker_invalid_pid_returns_false` (5 cases) |
| Valid positive PID → success | `test_launch_windows_broker_valid_pid_succeeds` (3 cases) + `test_launch_windows_brokers_when_breakaway_refused` |
| Relative home → absolute CWD + safely encoded cmdline | `test_launch_windows_broker_relative_home_gets_absolute_cwd` |
| Broker nonzero/exception failure | `test_launch_windows_broker_failure_returns_false`, `test_launch_windows_broker_exception_returns_false` |
| Quoting/hidden/fast-path/POSIX | existing suite — all pass |

### 4.2 Live probes on this host (Windows PowerShell 5.1, real WMI)

| Scenario | Exit | stdout | stderr |
| :--- | :--- | :--- | :--- |
| Parse check (`Parser::ParseFile`) | — | 0 parse errors | — |
| Valid cmdline + absolute CWD (real `Win32_Process.Create` of a no-op `python -c pass`) | 0 | `9636` (real PID only) | empty |
| Relative CWD → WMI `ReturnValue = 8` | **8** | empty | `Win32_Process.Create failed with ReturnValue 8` |
| Bogus CIM class → CimException → catch | **1** | empty | error record text |
| `$r` forced `$null` | **1** | empty | diagnostic |

Every failure mode now yields nonzero exit + stderr diagnostics and no PID on stdout — the exact inverse of the rejected behavior, where CIM failures exited 0 silently. Probe artifacts were deleted; no stray files remain.

---

## 5. Defect-to-Fix Traceability

- **Review 3.1 (silent false success)** → `_BROKER_PS` EAP/try/catch/`$rc` guards + Python-side PID gate. Double-layered: even if PowerShell ever exits 0 without a PID, `_popen_brokered` still raises.
- **Review 3.2 (relative CWD, WMI error 8)** → `log_path.resolve()` in `_popen_brokered` + `home.resolve()` in `_launch`, so `CurrentDirectory`, the stub log path, and `--dir` are all absolute.
- **Review 3.3 (missing PID-validation tests)** → the four new tests in Section 2.

---

## 6. Do-Not-Touch Compliance

- No commits, no staging, no reverts, no formatting passes.
- Accepted heartbeat files (`src/agent_bridge/share/dashboard.py`, `src/agent_bridge/share/dashboard_page.py`, `tests/test_dashboard_page.py`) untouched.
- Pre-existing dirty files (`src/agent_bridge/adapters/antigravity.py`, `tests/fake_agy.py`, `tests/test_agy_dashboard.py`, `tests/test_agy_parse.py`) untouched.
- Temporary live-probe files created under the worktree were deleted after verification.

---

*Report compiled and verified by Worker-Dashboard-Broker-Failure on 2026-09-18.*
