"""Open the local Agent Bridge dashboard in a browser when MCP tools are used.

Fire-and-forget: every check runs on a daemon thread so tool calls are never
blocked. A tab is considered open while it heartbeats ``/api/presence``; the
bridge only pops the page up when no tab has been seen recently.
"""

from __future__ import annotations

import json
import logging
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


def _client_open(url: str) -> bool | None:
    """True when a dashboard tab is open, False when none, None when unreachable."""
    try:
        with urllib.request.urlopen(
            url.rstrip("/") + "/api/client_state", timeout=_CLIENT_STATE_TIMEOUT
        ) as resp:
            data = json.loads(resp.read())
        return bool(data.get("clients"))
    except Exception:
        return None


def _wait_for_dashboard(url: str) -> bool:
    deadline = time.monotonic() + _STARTUP_WAIT_SEC
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                url.rstrip("/") + "/api/client_state", timeout=0.5
            ):
                return True
        except Exception:
            time.sleep(0.2)
    return False


def _launch(home: Path, host: str, port: int) -> bool:
    script = home / "dashboard.py"
    if not script.is_file():
        script = bundled_dashboard()
    if not script.is_file():
        log.warning("dashboard auto-open: no dashboard.py in %s or bundled", home)
        return False
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out = open(log_dir / "dashboard.log", "ab")
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [sys.executable, str(script), "--port", str(port), "--dir", str(home)],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
    except Exception:
        out.close()
        log.exception("dashboard auto-open: failed to launch %s", script)
        return False
    log.info("dashboard auto-open: launched %s on port %s", script, port)
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
        now = time.monotonic()
        if now - _LAST_OPEN < _OPEN_DEBOUNCE_SEC:
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
