from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_worker_context(monkeypatch):
    """A suite launched inside a dispatched worker inherits the worker-context
    mark and host mode pin; without clearing them every Registry comes up in
    "nested" mode. Tests may still set both explicitly after fixtures run."""
    monkeypatch.delenv("AGENT_BRIDGE_PARENT_CONTEXT", raising=False)
    monkeypatch.delenv("AGENT_BRIDGE_MODE", raising=False)


@pytest.fixture
def bridge_home(tmp_path, monkeypatch):
    home = tmp_path / "bridge-home"
    monkeypatch.setenv("AGENT_BRIDGE_HOME", str(home))
    monkeypatch.setenv("AGENT_BRIDGE_ENABLE_FAKE", "1")
    return home
