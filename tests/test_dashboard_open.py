"""Auto-open decision tests for ``agent_bridge.dashboard``.

The duplicate-popup fix has three moving parts, all exercised here with the
network and wall clock stubbed out: the confirm recheck before a browser
open (covering reload gaps and post-launch client registration), the
cross-process ``.dashboard-open`` cooldown marker, and the existing
in-process debounces plus the enabled/worker-context suppression gates.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_bridge import dashboard as dl

URL = "http://127.0.0.1:8787/"


@pytest.fixture
def auto(tmp_path, monkeypatch):
    """Run ``_auto_open`` fully stubbed: no sockets, no sleep, no browser."""
    opened = []
    monkeypatch.setattr(dl.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(dl.time, "sleep", lambda _sec: None)
    monkeypatch.setattr(dl, "_LAST_OPEN", 0.0)
    monkeypatch.setattr(dl, "_launch", lambda *a: True)
    monkeypatch.setattr(dl, "_wait_for_dashboard", lambda url: True)
    return tmp_path, opened


def _states(monkeypatch, seq):
    it = iter(seq)
    monkeypatch.setattr(dl, "_client_open", lambda url: next(it, seq[-1]))


def test_client_present_never_opens(auto, monkeypatch):
    home, opened = auto
    _states(monkeypatch, [True])
    dl._auto_open(home, "127.0.0.1", 8787)
    assert opened == []


def test_first_use_opens_and_marks(auto, monkeypatch):
    """Genuine first use: server reachable, zero clients, no marker → open."""
    home, opened = auto
    _states(monkeypatch, [False, False])
    dl._auto_open(home, "127.0.0.1", 8787)
    assert opened == [URL]
    assert (home / dl._OPEN_MARKER).is_file()


def test_no_open_when_client_reappears(auto, monkeypatch):
    """clients==0 -> a tab registers during the confirm wait -> no open."""
    home, opened = auto
    _states(monkeypatch, [False, True])
    dl._auto_open(home, "127.0.0.1", 8787)
    assert opened == []


def test_no_open_after_launch_when_clients_present(auto, monkeypatch):
    """Transient client_state failure launched a server; by the time it
    answers, the original tab has re-registered -> no unconditional open."""
    home, opened = auto
    _states(monkeypatch, [None, True])
    launched = []
    monkeypatch.setattr(dl, "_launch", lambda *a: launched.append(a) or True)
    dl._auto_open(home, "127.0.0.1", 8787)
    assert launched and opened == []


def test_launch_or_wait_failure_never_opens(auto, monkeypatch):
    home, opened = auto
    _states(monkeypatch, [None])
    monkeypatch.setattr(dl, "_launch", lambda *a: False)
    dl._auto_open(home, "127.0.0.1", 8787)
    monkeypatch.setattr(dl, "_launch", lambda *a: True)
    monkeypatch.setattr(dl, "_wait_for_dashboard", lambda url: False)
    dl._auto_open(home, "127.0.0.1", 8787)
    assert opened == []


def test_recheck_unreachable_never_opens(auto, monkeypatch):
    """If the server dies between the probe and the confirm, a browser tab
    aimed at a dead endpoint is worse than waiting for the next attempt."""
    home, opened = auto
    _states(monkeypatch, [False, None])
    dl._auto_open(home, "127.0.0.1", 8787)
    assert opened == []


def test_fresh_marker_suppresses_open(auto, monkeypatch):
    """A sibling Bridge instance opened a tab moments ago -> stay quiet."""
    home, opened = auto
    _states(monkeypatch, [False, False])
    (home / dl._OPEN_MARKER).write_text("", encoding="utf-8")
    dl._auto_open(home, "127.0.0.1", 8787)
    assert opened == []


def test_stale_marker_allows_open(auto, monkeypatch):
    home, opened = auto
    _states(monkeypatch, [False, False])
    marker = home / dl._OPEN_MARKER
    marker.write_text("", encoding="utf-8")
    old = time.time() - dl._OPEN_COOLDOWN_SEC - 5
    os.utime(marker, (old, old))
    dl._auto_open(home, "127.0.0.1", 8787)
    assert opened == [URL]


def test_open_debounce_suppresses_open(auto, monkeypatch):
    home, opened = auto
    _states(monkeypatch, [False, False])
    monkeypatch.setattr(dl, "_LAST_OPEN", time.monotonic())
    dl._auto_open(home, "127.0.0.1", 8787)
    assert opened == []


def test_claim_open_dedupes_siblings(tmp_path):
    """First claim wins; every later claim inside the cooldown loses; an
    expired marker is pruned and re-claimed. Filesystem-atomic via O_EXCL."""
    assert dl._claim_open(tmp_path) is True
    assert dl._claim_open(tmp_path) is False
    marker = tmp_path / dl._OPEN_MARKER
    stale = time.time() - dl._OPEN_COOLDOWN_SEC - 1
    os.utime(marker, (stale, stale))
    assert dl._claim_open(tmp_path) is True
    assert dl._claim_open(tmp_path) is False


def test_claim_open_readonly_home_still_allows(tmp_path, monkeypatch):
    """A marker that can never be written must not disable auto-open."""
    monkeypatch.setattr(dl.os, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    assert dl._claim_open(tmp_path) is True


class _FakeThread:
    def __init__(self, target=None, args=(), daemon=None, **_kw):
        self.target, self.args, self.daemon = target, args, daemon
        self.started = False
        spawned.append(self)

    def start(self):
        self.started = True


spawned: list[_FakeThread] = []


@pytest.fixture
def threads(monkeypatch):
    spawned.clear()
    monkeypatch.setattr(dl.threading, "Thread", _FakeThread)
    monkeypatch.setattr(dl, "_LAST_ATTEMPT", 0.0)
    return spawned


def _cfg(enabled=True):
    return SimpleNamespace(enabled=enabled, host="127.0.0.1", port=8787)


def test_maybe_open_dashboard_spawns_daemon(tmp_path, threads):
    dl.maybe_open_dashboard(tmp_path, _cfg())
    assert len(threads) == 1
    t = threads[0]
    assert t.daemon is True and t.started is True
    assert t.target is dl._auto_open
    assert t.args == (tmp_path, "127.0.0.1", 8787)


def test_maybe_open_dashboard_disabled_never_spawns(tmp_path, threads):
    dl.maybe_open_dashboard(tmp_path, _cfg(enabled=False))
    dl.maybe_open_dashboard(tmp_path, None)
    assert threads == []


def test_maybe_open_dashboard_worker_context_never_spawns(tmp_path, threads, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_PARENT_CONTEXT", "worker")
    dl.maybe_open_dashboard(tmp_path, _cfg())
    assert threads == []


def test_maybe_open_dashboard_attempt_debounce(tmp_path, threads):
    dl.maybe_open_dashboard(tmp_path, _cfg())
    dl.maybe_open_dashboard(tmp_path, _cfg())
    assert len(threads) == 1


# --- _launch spawn strategy -------------------------------------------------
#
# The singleton dashboard must survive the orderly idle exit of whichever
# Bridge instance launched it. On Windows that requires escaping the Job
# Object the agent host put this Bridge into — DETACHED_PROCESS and
# CREATE_NEW_PROCESS_GROUP cannot do that — so CREATE_BREAKAWAY_FROM_JOB is
# attempted first. A job without JOB_OBJECT_LIMIT_BREAKAWAY_OK refuses it
# with ERROR_ACCESS_DENIED, and an ordinary in-job Popen would still die
# with Bridge, so the refused path is brokered through WMI
# (Win32_Process.Create runs the new process under the WMI service, outside
# the job). Popen and subprocess.run are stubbed; nothing is ever spawned.

_WIN32_BASE_FLAGS = 0x200 | 0x8  # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
_WIN32_BREAKAWAY = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
_WIN32_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def _as_platform(monkeypatch, platform):
    monkeypatch.setattr(dl.sys, "platform", platform)
    if platform == "win32":
        # POSIX test hosts lack these constants; pinning them keeps the flag
        # assertions identical wherever the suite runs.
        monkeypatch.setattr(dl.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
        monkeypatch.setattr(dl.subprocess, "DETACHED_PROCESS", 0x8, raising=False)
        monkeypatch.setattr(dl.subprocess, "CREATE_BREAKAWAY_FROM_JOB", _WIN32_BREAKAWAY, raising=False)
        monkeypatch.setattr(dl.subprocess, "CREATE_NO_WINDOW", _WIN32_NO_WINDOW, raising=False)


def _stub_spawn(tmp_path, monkeypatch, fail_times=0):
    """Point ``_launch`` at a real script file and record Popen calls.

    ``fail_times`` leading calls raise PermissionError, matching the
    ERROR_ACCESS_DENIED a Job Object without breakaway permission gives.
    """
    script = tmp_path / "dashboard.py"
    script.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(dl, "bundled_dashboard", lambda: script)
    calls: list[dict] = []
    state = {"failures": fail_times}

    def fake_popen(argv, **kw):
        calls.append({"argv": list(argv), **kw})
        if state["failures"]:
            state["failures"] -= 1
            raise PermissionError(5, "Access is denied")
        return object()

    monkeypatch.setattr(dl.subprocess, "Popen", fake_popen)
    return calls


def _stub_broker(monkeypatch, returncode=0, stdout="4321\r\n", stderr="", raises=None):
    """Record ``subprocess.run`` calls — the WMI broker invocation."""
    calls: list[dict] = []

    def fake_run(cmd, **kw):
        calls.append({"argv": list(cmd), **kw})
        if raises is not None:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(dl.subprocess, "run", fake_run)
    return calls


def test_launch_windows_breaks_out_of_host_job(tmp_path, monkeypatch):
    """Detached flags alone keep the dashboard inside the host's Job Object,
    so it dies when the launching Bridge exits; breakaway leaves the job."""
    _as_platform(monkeypatch, "win32")
    calls = _stub_spawn(tmp_path, monkeypatch)
    assert dl._launch(tmp_path, "127.0.0.1", 8787) is True
    assert len(calls) == 1
    call = calls[0]
    assert call["creationflags"] == _WIN32_BASE_FLAGS | _WIN32_BREAKAWAY
    assert "start_new_session" not in call
    assert call["stdin"] is dl.subprocess.DEVNULL
    assert call["argv"] == [
        dl.sys.executable,
        str(tmp_path / "dashboard.py"),
        "--port",
        "8787",
        "--dir",
        str(tmp_path.resolve()),
    ]


def test_launch_windows_brokers_when_breakaway_refused(tmp_path, monkeypatch):
    """A job without JOB_OBJECT_LIMIT_BREAKAWAY_OK fails CreateProcess with
    ERROR_ACCESS_DENIED. An in-job retry would still die with Bridge, so the
    launch is brokered through WMI instead — never a second plain Popen."""
    _as_platform(monkeypatch, "win32")
    calls = _stub_spawn(tmp_path, monkeypatch, fail_times=1)
    broker = _stub_broker(monkeypatch)
    assert dl._launch(tmp_path, "127.0.0.1", 8787) is True
    assert [c["creationflags"] for c in calls] == [_WIN32_BASE_FLAGS | _WIN32_BREAKAWAY]
    assert len(broker) == 1
    ps = broker[0]
    assert ps["argv"][0].lower().endswith("powershell.exe")
    assert ps["argv"][-2] == "-Command"
    assert ps["argv"][-1] == dl._BROKER_PS


def test_launch_windows_broker_quotes_paths_safely(tmp_path, monkeypatch):
    """The WMI command line is a single Win32 string: list2cmdline quoting
    must carry spaces and shell metacharacters verbatim, and it reaches
    PowerShell through the environment block — never interpolated into the
    -Command script — so no path can inject into the broker."""
    _as_platform(monkeypatch, "win32")
    home = tmp_path / "home dir & 'quotes'"
    home.mkdir()
    _stub_spawn(home, monkeypatch, fail_times=1)
    broker = _stub_broker(monkeypatch)
    assert dl._launch(home, "127.0.0.1", 8787) is True
    ps = broker[0]
    log_path = home.resolve() / "logs" / "dashboard.log"
    argv = [dl.sys.executable, str(home / "dashboard.py"), "--port", "8787", "--dir", str(home.resolve())]
    expected = dl.subprocess.list2cmdline([dl.sys.executable, "-c", dl._BROKER_STUB, str(log_path), *argv[1:]])
    assert ps["env"][dl._BROKER_CMD_ENV] == expected
    assert ps["env"][dl._BROKER_CWD_ENV] == str(log_path.parent)
    assert os.path.isabs(ps["env"][dl._BROKER_CWD_ENV])
    assert f'"{home.resolve()}"' in expected


def test_launch_windows_broker_runs_hidden(tmp_path, monkeypatch):
    """The PowerShell broker must not flash a window or prompt: it runs with
    CREATE_NO_WINDOW, -NonInteractive, and -WindowStyle Hidden."""
    _as_platform(monkeypatch, "win32")
    _stub_spawn(tmp_path, monkeypatch, fail_times=1)
    broker = _stub_broker(monkeypatch)
    assert dl._launch(tmp_path, "127.0.0.1", 8787) is True
    ps = broker[0]
    assert ps["creationflags"] == _WIN32_NO_WINDOW
    assert "-NonInteractive" in ps["argv"]
    assert "-NoProfile" in ps["argv"]
    assert ps["argv"][ps["argv"].index("-WindowStyle") + 1] == "Hidden"
    assert ps["stdin"] is dl.subprocess.DEVNULL


def test_launch_windows_broker_failure_returns_false(tmp_path, monkeypatch):
    """A brokered launch WMI refused (nonzero exit) is a launch failure:
    report it, never claim success — and never fall back to an in-job Popen."""
    _as_platform(monkeypatch, "win32")
    calls = _stub_spawn(tmp_path, monkeypatch, fail_times=1)
    _stub_broker(monkeypatch, returncode=8, stderr="Unknown failure")
    assert dl._launch(tmp_path, "127.0.0.1", 8787) is False
    assert [c["creationflags"] for c in calls] == [_WIN32_BASE_FLAGS | _WIN32_BREAKAWAY]


def test_launch_windows_broker_exception_returns_false(tmp_path, monkeypatch):
    """A broker that cannot even answer (missing powershell.exe, WMI down,
    timeout) is likewise a reported launch failure, not a silent retry."""
    _as_platform(monkeypatch, "win32")
    calls = _stub_spawn(tmp_path, monkeypatch, fail_times=1)
    _stub_broker(monkeypatch, raises=dl.subprocess.TimeoutExpired("powershell.exe", 30))
    assert dl._launch(tmp_path, "127.0.0.1", 8787) is False
    assert [c["creationflags"] for c in calls] == [_WIN32_BASE_FLAGS | _WIN32_BREAKAWAY]


def test_launch_windows_broker_zero_exit_no_pid_returns_false(tmp_path, monkeypatch):
    """A broker that exits 0 without printing a PID — e.g. a CIM exception
    reported only on stderr — is a reported launch failure, never a claimed
    success (and never an in-job Popen retry)."""
    _as_platform(monkeypatch, "win32")
    calls = _stub_spawn(tmp_path, monkeypatch, fail_times=1)
    _stub_broker(monkeypatch, returncode=0, stdout="", stderr="Invoke-CimMethod : CimException: winmgmt stopped")
    assert dl._launch(tmp_path, "127.0.0.1", 8787) is False
    assert [c["creationflags"] for c in calls] == [_WIN32_BASE_FLAGS | _WIN32_BREAKAWAY]


@pytest.mark.parametrize("stdout", ["garbage", "1234 5678", "ProcessId=4321", "0", " \r\n "])
def test_launch_windows_broker_invalid_pid_returns_false(tmp_path, monkeypatch, stdout):
    """Exit-0 output that is not exactly one positive decimal PID —
    nonnumeric text, multiple tokens, a nonpositive number, or whitespace —
    cannot confirm the WMI create and must fail the launch."""
    _as_platform(monkeypatch, "win32")
    calls = _stub_spawn(tmp_path, monkeypatch, fail_times=1)
    _stub_broker(monkeypatch, returncode=0, stdout=stdout, stderr="broker diagnostics")
    assert dl._launch(tmp_path, "127.0.0.1", 8787) is False
    assert [c["creationflags"] for c in calls] == [_WIN32_BASE_FLAGS | _WIN32_BREAKAWAY]


@pytest.mark.parametrize("stdout", ["4321\r\n", "  9876 \n", "42"])
def test_launch_windows_broker_valid_pid_succeeds(tmp_path, monkeypatch, stdout):
    """A single positive decimal PID on stdout confirms the WMI-created
    process; surrounding whitespace is harmless."""
    _as_platform(monkeypatch, "win32")
    _stub_spawn(tmp_path, monkeypatch, fail_times=1)
    _stub_broker(monkeypatch, returncode=0, stdout=stdout)
    assert dl._launch(tmp_path, "127.0.0.1", 8787) is True


def test_launch_windows_broker_relative_home_gets_absolute_cwd(tmp_path, monkeypatch):
    """Win32_Process.Create rejects a relative CurrentDirectory (WMI
    ReturnValue 8). A relative ``home`` must still broker with an absolute
    CWD, and the log/--dir paths inside the brokered command line must be
    absolute too — the session-0 child resolves relative paths against its
    own working directory, not this Bridge's."""
    _as_platform(monkeypatch, "win32")
    monkeypatch.chdir(tmp_path)
    home = Path("rel home & stuff")
    calls = _stub_spawn(tmp_path, monkeypatch, fail_times=1)
    broker = _stub_broker(monkeypatch)
    assert dl._launch(home, "127.0.0.1", 8787) is True
    assert [c["creationflags"] for c in calls] == [_WIN32_BASE_FLAGS | _WIN32_BREAKAWAY]
    ps = broker[0]
    cwd = ps["env"][dl._BROKER_CWD_ENV]
    assert os.path.isabs(cwd)
    log_path = (home / "logs" / "dashboard.log").resolve()
    assert cwd == str(log_path.parent)
    argv = [str(tmp_path / "dashboard.py"), "--port", "8787", "--dir", str(home.resolve())]
    expected = dl.subprocess.list2cmdline([dl.sys.executable, "-c", dl._BROKER_STUB, str(log_path), *argv])
    assert ps["env"][dl._BROKER_CMD_ENV] == expected


def test_launch_windows_failure_returns_false(tmp_path, monkeypatch):
    _as_platform(monkeypatch, "win32")
    calls = _stub_spawn(tmp_path, monkeypatch, fail_times=99)
    _stub_broker(monkeypatch, raises=OSError("powershell.exe missing"))
    assert dl._launch(tmp_path, "127.0.0.1", 8787) is False
    assert len(calls) == 1  # the refused breakaway attempt; never an in-job retry


def test_launch_windows_without_breakaway_constant(tmp_path, monkeypatch):
    """Python builds lacking CREATE_BREAKAWAY_FROM_JOB keep the old flags and
    do not retry the identical spawn."""
    _as_platform(monkeypatch, "win32")
    monkeypatch.delattr(dl.subprocess, "CREATE_BREAKAWAY_FROM_JOB", raising=False)
    calls = _stub_spawn(tmp_path, monkeypatch, fail_times=99)
    assert dl._launch(tmp_path, "127.0.0.1", 8787) is False
    assert [c["creationflags"] for c in calls] == [_WIN32_BASE_FLAGS]


def test_launch_posix_uses_new_session(tmp_path, monkeypatch):
    _as_platform(monkeypatch, "linux")
    calls = _stub_spawn(tmp_path, monkeypatch)
    assert dl._launch(tmp_path, "127.0.0.1", 8787) is True
    assert len(calls) == 1
    assert calls[0]["start_new_session"] is True
    assert "creationflags" not in calls[0]


def test_launch_posix_failure_returns_false(tmp_path, monkeypatch):
    _as_platform(monkeypatch, "linux")
    calls = _stub_spawn(tmp_path, monkeypatch, fail_times=99)
    assert dl._launch(tmp_path, "127.0.0.1", 8787) is False
    assert len(calls) == 1  # POSIX has no second flag set to retry with


def test_launch_missing_script_never_spawns(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "bundled_dashboard", lambda: tmp_path / "nope.py")
    calls = []
    monkeypatch.setattr(dl.subprocess, "Popen", lambda argv, **kw: calls.append(kw))
    assert dl._launch(tmp_path, "127.0.0.1", 8787) is False
    assert calls == []
