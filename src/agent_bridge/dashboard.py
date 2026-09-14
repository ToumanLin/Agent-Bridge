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


def _popen_detached(argv: list[str], out) -> None:
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
    # (ERROR_ACCESS_DENIED), so the plain process-group flags stay a retry.
    base = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    try:
        subprocess.Popen(argv, creationflags=base | breakaway, **kw)
    except OSError:
        if not breakaway:
            raise
        log.warning("dashboard auto-open: job breakaway refused; relaunching within job")
        subprocess.Popen(argv, creationflags=base, **kw)


def _launch(home: Path, host: str, port: int) -> bool:
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
    try:
        with open(log_dir / "dashboard.log", "ab") as out:
            _popen_detached(argv, out)
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
