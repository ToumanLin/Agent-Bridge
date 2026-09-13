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
