"""Quota wiring in Registry.list_agents, the MCP surface, and the CLI."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from agent_bridge.adapters.fake import FakeAdapter
from agent_bridge.cli import main
from agent_bridge.models import TurnResult
from agent_bridge.quota import QuotaProvider, QuotaStatus, QuotaWindow
from agent_bridge.registry import Registry
from agent_bridge.server import list_agents


def _only_fake(registry: Registry) -> None:
    registry.config.agents = {"fake": registry.config.agents["fake"]}


@pytest.mark.asyncio
async def test_list_agents_rows_carry_quota_without_touching_available(bridge_home, monkeypatch):
    registry = Registry.create(bridge_home)
    monkeypatch.setattr("agent_bridge.registry.count_sibling_servers", lambda: 0)
    rows = await registry.list_agents()
    assert rows, "bundled agents.toml should list workers"
    for row in rows:
        assert "quota" in row
        assert row["quota"]["status"] in {"ok", "exhausted", "unknown"}
        assert isinstance(row["quota"]["detail"], str) or row["quota"]["status"] != "unknown"
    fake = next(row for row in rows if row["agent"] == "fake")
    assert fake["available"] is True
    assert fake["quota"]["status"] == "unknown"
    assert "not supported" in fake["quota"]["detail"]
    missing = [row for row in rows if not row["available"]]
    for row in missing:
        assert row["quota"]["detail"] == "worker command not found"


@pytest.mark.asyncio
async def test_list_agents_uses_provider_and_cache(bridge_home, monkeypatch):
    registry = Registry.create(bridge_home)
    _only_fake(registry)
    calls = 0

    async def provider(cfg, env):
        nonlocal calls
        calls += 1
        assert isinstance(env, dict)
        return QuotaStatus(status="ok", windows=[QuotaWindow(name="5h", remaining_percent=42.0)])

    monkeypatch.setattr("agent_bridge.registry.provider_table", lambda config: {"fake": QuotaProvider(None, provider)})
    first = (await registry.list_agents())[0]["quota"]
    second = (await registry.list_agents())[0]["quota"]
    assert calls == 1
    assert first["status"] == "ok" and first["cached"] is False
    assert second["cached"] is True
    assert second["windows"][0]["remaining_percent"] == 42.0


@pytest.mark.asyncio
async def test_list_agents_bounded_by_quota_timeout(bridge_home, monkeypatch):
    registry = Registry.create(bridge_home)
    _only_fake(registry)
    registry.config.quota.timeout_sec = 0.3

    async def hang(cfg, env):
        await asyncio.sleep(10)
        return QuotaStatus(status="ok")

    monkeypatch.setattr("agent_bridge.registry.provider_table", lambda config: {"fake": QuotaProvider(None, hang)})
    started = time.monotonic()
    row = (await registry.list_agents())[0]
    assert time.monotonic() - started < 3
    assert row["available"] is True
    assert row["quota"]["status"] == "unknown"
    assert "timed out" in row["quota"]["detail"]


@pytest.mark.asyncio
async def test_list_agents_quota_disabled(bridge_home, monkeypatch):
    registry = Registry.create(bridge_home)
    _only_fake(registry)
    registry.config.quota.enabled = False

    async def never(cfg, env):
        raise AssertionError("provider must not run when quota is disabled")

    monkeypatch.setattr("agent_bridge.registry.provider_table", lambda config: {"fake": QuotaProvider(None, never)})
    row = (await registry.list_agents())[0]
    assert row["quota"]["status"] == "unknown"
    assert "disabled" in row["quota"]["detail"]


@pytest.mark.asyncio
async def test_list_agents_survives_provider_raising(bridge_home, monkeypatch):
    registry = Registry.create(bridge_home)
    _only_fake(registry)

    async def boom(cfg, env):
        raise RuntimeError("HTTP 500: upstream")

    monkeypatch.setattr("agent_bridge.registry.provider_table", lambda config: {"fake": QuotaProvider(None, boom)})
    row = (await registry.list_agents())[0]
    assert row["available"] is True
    assert row["quota"]["status"] == "unknown"
    assert "HTTP 500" in row["quota"]["detail"]


@pytest.mark.asyncio
async def test_quota_error_on_a_turn_invalidates_the_cache(bridge_home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    _only_fake(registry)
    calls = 0

    async def provider(cfg, env):
        nonlocal calls
        calls += 1
        return QuotaStatus(status="ok", windows=[QuotaWindow(name="5h", remaining_percent=5.0)])

    monkeypatch.setattr("agent_bridge.registry.provider_table", lambda config: {"fake": QuotaProvider(None, provider)})

    async def dry_turn(self, session, task):
        return TurnResult(text="", stop_reason="error", error="quota exceeded for this plan")

    monkeypatch.setattr(FakeAdapter, "run_turn", dry_turn)
    await registry.start()
    try:
        await registry.list_agents()
        assert calls == 1
        dispatched = await registry.dispatch_task("fake", "go", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "failed"
        await registry.list_agents()
        assert calls == 2, "a quota failure must drop the cached reading"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_kimi_style_warning_on_completed_turn_invalidates_the_cache(bridge_home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    _only_fake(registry)
    calls = 0

    async def provider(cfg, env):
        nonlocal calls
        calls += 1
        return QuotaStatus(status="ok")

    monkeypatch.setattr("agent_bridge.registry.provider_table", lambda config: {"fake": QuotaProvider(None, provider)})

    async def warned_turn(self, session, task):
        return TurnResult(text="", stop_reason="end_turn", warnings=["turn failed: provider.quota_exceeded"])

    monkeypatch.setattr(FakeAdapter, "run_turn", warned_turn)
    await registry.start()
    try:
        await registry.list_agents()
        dispatched = await registry.dispatch_task("fake", "go", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        await registry.list_agents()
        assert calls == 2
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_unrelated_failure_keeps_the_cache(bridge_home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    _only_fake(registry)
    calls = 0

    async def provider(cfg, env):
        nonlocal calls
        calls += 1
        return QuotaStatus(status="ok")

    monkeypatch.setattr("agent_bridge.registry.provider_table", lambda config: {"fake": QuotaProvider(None, provider)})

    async def broken_turn(self, session, task):
        return TurnResult(text="", stop_reason="error", error="syntax error in tool call")

    monkeypatch.setattr(FakeAdapter, "run_turn", broken_turn)
    await registry.start()
    try:
        await registry.list_agents()
        dispatched = await registry.dispatch_task("fake", "go", cwd=str(work.resolve()))
        await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        await registry.list_agents()
        assert calls == 1
    finally:
        await registry.stop()


def test_mcp_surface_documents_quota():
    doc = list_agents.__doc__ or ""
    # Quota semantics live in list_agents' own description now; the
    # handshake carries only the hard rules.
    for token in ("quota", "ok/exhausted/unknown", "unknown means unreadable, not empty"):
        assert token in doc


def test_cli_quota_prints_json_for_every_agent(bridge_home, capsys):
    main(["--quota"])
    payload = json.loads(capsys.readouterr().out)
    assert "fake" in payload
    assert payload["fake"]["status"] == "unknown"
    for row in payload.values():
        assert row["status"] in {"ok", "exhausted", "unknown"}
    main(["help"])
    assert "--quota" in capsys.readouterr().out
