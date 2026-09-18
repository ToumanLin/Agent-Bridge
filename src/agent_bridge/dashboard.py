"""Open the local Agent Bridge dashboard in a browser when MCP tools are used.

Fire-and-forget: every check runs on a daemon thread so tool calls are never
blocked. A tab is considered open while it heartbeats ``/api/presence``; the
bridge only pops the page up when no tab has been seen recently.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from agent_bridge.paths import bundled_dashboard, parent_context_is_worker

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_LAST_ATTEMPT = 0.0
_ATTEMPT_DEBOUNCE_SEC = 10.0
_LAST_OPEN = 0.0
_OPEN_DEBOUNCE_SEC = 30.0
_CLIENT_STATE_TIMEOUT = 0.8
_STARTUP_WAIT_SEC = 4.0
_CONFIRM_SEC = 2.0
_OPEN_MARKER = ".dashboard-open"
_OPEN_COOLDOWN_SEC = 60.0

_BROKER_TIMEOUT_SEC = 30.0
_BROKER_CMD_ENV = "AGENT_BRIDGE_DASHBOARD_CMDLINE"
_BROKER_CWD_ENV = "AGENT_BRIDGE_DASHBOARD_CWD"
# Runs inside the WMI-created process, whose std handles are invalid (no
# console, nothing inherited) so dup2-level redirects fail — rebind the
# Python streams to the launcher's log instead, then run the dashboard
# script in-process.  run_path (not exec/spawn) keeps this a single process
# and avoids a second command-line quoting round-trip; the script's dir is
# pushed onto sys.path so its sibling imports resolve as usual.
_BROKER_STUB = (
    "import os,runpy,sys;"
    "sys.stdin=open(os.devnull);"
    "sys.stdout=sys.stderr=open(sys.argv[1],'a',buffering=1,errors='replace');"
    "sys.argv=sys.argv[2:];"
    "sys.path.insert(0,os.path.dirname(sys.argv[0]));"
    "runpy.run_path(sys.argv[0],run_name='__main__')"
)
# Fixed -Command text: the command line travels in the environment block,
# never inside the script, so no path can inject into PowerShell.
# ErrorActionPreference=Stop plus the explicit result checks keep a CIM
# exception, a null result, or a missing ProcessId from ever exiting 0 —
# a brokered launch is only confirmed once a real PID reaches stdout.
_BROKER_PS = (
    "$ErrorActionPreference = 'Stop';"
    " try {"
    f" $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create"
    f" -Arguments @{{ CommandLine = $env:{_BROKER_CMD_ENV}; CurrentDirectory = $env:{_BROKER_CWD_ENV} }}"
    " } catch { Write-Error $_ -ErrorAction Continue; exit 1 };"
    " $rc = 1;"
    " if ($null -ne $r -and $null -ne $r.ReturnValue) { $rc = [int]$r.ReturnValue };"
    ' if ($rc -ne 0) { Write-Error "Win32_Process.Create failed with ReturnValue $rc" -ErrorAction Continue; exit $rc };'
    " if (-not $r.ProcessId) { Write-Error 'Win32_Process.Create returned no ProcessId' -ErrorAction Continue; exit 1 };"
    " Write-Output $r.ProcessId"
)


def _client_open(url: str) -> bool | None:
    """True when a dashboard tab is open, False when none, None when unreachable."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/client_state", timeout=_CLIENT_STATE_TIMEOUT) as resp:
            data = json.loads(resp.read())
        return bool(data.get("clients"))
    except Exception:
        return None


def _wait_for_dashboard(url: str) -> bool:
    deadline = time.monotonic() + _STARTUP_WAIT_SEC
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/client_state", timeout=0.5):
                return True
        except Exception:
            time.sleep(0.2)
    return False


def _powershell_path() -> str:
    """Locate Windows PowerShell without trusting PATH order."""
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate) if candidate.is_file() else "powershell.exe"


def _popen_brokered(argv: list[str], log_path: Path) -> None:
    """Create the dashboard outside this Bridge's Job Object via WMI.

    Win32_Process.Create asks the WMI service — already running outside
    whatever job an agent host put Bridge into — to spawn the process.
    That is the standard non-elevated escape from
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE when CREATE_BREAKAWAY_FROM_JOB is
    refused: no admin rights, no scheduled task, no interactive session
    (the child lands in session 0 with no window), and no shell parses the
    command line — list2cmdline quoting reaches CreateProcess verbatim via
    the environment block.  ``_BROKER_STUB`` rebinds the std streams to the
    launcher's log file and runs the script in-process.  Raises unless WMI
    confirms the process was created and reports its PID.
    """
    # Win32_Process.Create rejects a relative CurrentDirectory, and the
    # session-0 child would resolve a relative log path against its own
    # working directory — resolve once so both stay absolute.
    log_path = log_path.resolve()
    brokered = [sys.executable, "-c", _BROKER_STUB, str(log_path), *argv[1:]]
    env = dict(os.environ)
    env[_BROKER_CMD_ENV] = subprocess.list2cmdline(brokered)
    env[_BROKER_CWD_ENV] = str(log_path.parent)
    ps_argv = [
        _powershell_path(),
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-Command",
        _BROKER_PS,
    ]
    try:
        proc = subprocess.run(
            ps_argv,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_BROKER_TIMEOUT_SEC,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        log.warning("dashboard auto-open: brokered launch failed: %s", exc)
        raise
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        log.warning("dashboard auto-open: brokered launch exit %s: %s", proc.returncode, detail)
        raise OSError(f"brokered launch exited {proc.returncode}: {detail}")
    # Exit 0 alone is not success: the only confirmed launch is a stdout
    # carrying exactly one positive decimal PID. A CIM failure reported
    # solely on stderr, or any other output, is a launch failure.
    pid = (proc.stdout or "").strip()
    if not pid.isdecimal() or int(pid) <= 0:
        detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        log.warning("dashboard auto-open: brokered launch returned no valid pid: %s", detail or "<no output>")
        raise OSError(f"brokered launch returned no valid pid: {detail}")
    log.info("dashboard auto-open: brokered launch created pid %s", pid)


def _popen_detached(argv: list[str], out, log_path: Path) -> None:
    """Popen ``argv`` detached so the dashboard outlives the launching Bridge."""
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT}
    if sys.platform != "win32":
        subprocess.Popen(argv, start_new_session=True, **kw)
        return
    # DETACHED_PROCESS and CREATE_NEW_PROCESS_GROUP isolate only the console
    # and Ctrl+C group — a child still joins any Job Object an agent host
    # placed this Bridge process into, and JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    # then kills the dashboard when the launching Bridge exits.
    # CREATE_BREAKAWAY_FROM_JOB removes the child from that job at creation;
    # a job without JOB_OBJECT_LIMIT_BREAKAWAY_OK refuses it
    # (ERROR_ACCESS_DENIED). An ordinary in-job Popen would die with this
    # Bridge, so the refused path is brokered through WMI instead.
    base = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    try:
        subprocess.Popen(argv, creationflags=base | breakaway, **kw)
    except OSError:
        if not breakaway:
            raise
        log.warning("dashboard auto-open: job breakaway refused; brokering launch via WMI")
        _popen_brokered(argv, log_path)


def _launch(home: Path, host: str, port: int) -> bool:
    # Resolve home up front: the brokered child lands in session 0 with its
    # own working directory, so --dir, the log path, and the WMI
    # CurrentDirectory derived from it must all be absolute.
    home = home.resolve()
    # Prefer the packaged page so upgrades cannot be shadowed indefinitely by
    # an old dashboard.py copied into the data directory.  The home copy is a
    # compatibility fallback for source layouts that have no bundled asset.
    script = bundled_dashboard()
    if not script.is_file():
        script = home / "dashboard.py"
    if not script.is_file():
        log.warning("dashboard auto-open: no dashboard.py in %s or bundled", home)
        return False
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, str(script), "--port", str(port), "--dir", str(home)]
    log_file = log_dir / "dashboard.log"
    try:
        with open(log_file, "ab") as out:
            _popen_detached(argv, out, log_file)
    except Exception:
        log.exception("dashboard auto-open: failed to launch %s", script)
        return False
    log.info("dashboard auto-open: launched %s on port %s", script, port)
    return True


def _claim_open(home: Path) -> bool:
    """True when this process should perform the browser open.

    ``home/.dashboard-open`` is a cooldown marker shared by every Bridge
    instance using this home; its mtime is the last open time. Claiming is
    atomic-enough on Windows and POSIX — an O_EXCL create after pruning a
    stale marker — and a leftover marker simply expires instead of wedging
    auto-open. Filesystem trouble degrades to "allowed" rather than
    disabling the dashboard popup.
    """
    marker = home / _OPEN_MARKER
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        pass
    except OSError:
        return True
    else:
        os.close(fd)
        return True
    try:
        fresh = time.time() - marker.stat().st_mtime < _OPEN_COOLDOWN_SEC
    except FileNotFoundError:
        return False  # vanished mid-check: a sibling is claiming the slot
    except OSError:
        return True
    if fresh:
        return False
    try:
        marker.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False  # a sibling won the re-claim
    except OSError:
        return True
    os.close(fd)
    return True


def _auto_open(home: Path, host: str, port: int) -> None:
    global _LAST_OPEN
    url = f"http://{host}:{port}/"
    try:
        state = _client_open(url)
        if state is True:
            return
        if state is None:
            if not _launch(home, host, port):
                return
            if not _wait_for_dashboard(url):
                log.warning("dashboard auto-open: no response from %s", url)
                return
        # A tab can (re)appear while we wait: reload/bfcache gaps, a
        # throttled heartbeat landing late, or a client that registered
        # during launch. Open only when a reachable server still reports
        # zero clients a moment later.
        time.sleep(_CONFIRM_SEC)
        if _client_open(url) is not False:
            return
        now = time.monotonic()
        if now - _LAST_OPEN < _OPEN_DEBOUNCE_SEC:
            return
        if not _claim_open(home):
            return
        _LAST_OPEN = now
        webbrowser.open(url)
        log.info("dashboard auto-open: opened %s", url)
    except Exception:
        log.exception("dashboard auto-open failed")


def maybe_open_dashboard(home: Path, cfg) -> None:
    """Spawn a background check that opens the dashboard if no tab is open."""
    global _LAST_ATTEMPT
    if cfg is None or not getattr(cfg, "enabled", True):
        return
    if parent_context_is_worker():
        return
    now = time.monotonic()
    if now - _LAST_ATTEMPT < _ATTEMPT_DEBOUNCE_SEC:
        return
    with _LOCK:
        if time.monotonic() - _LAST_ATTEMPT < _ATTEMPT_DEBOUNCE_SEC:
            return
        _LAST_ATTEMPT = time.monotonic()
    threading.Thread(
        target=_auto_open,
        args=(home, getattr(cfg, "host", "127.0.0.1"), getattr(cfg, "port", 8787)),
        daemon=True,
    ).start()
