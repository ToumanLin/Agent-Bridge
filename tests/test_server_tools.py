import asyncio
import types

import pytest

from agent_bridge.registry import Registry
from agent_bridge.server import (
    INSTRUCTIONS,
    _error,
    _registry,
    list_agents,
    list_tasks,
    mcp,
)


def test_thirteen_tools_registered():
    names = sorted(mcp._tool_manager._tools)
    assert names == [
        "cancel_task",
        "check_task",
        "dispatch_task",
        "end_session",
        "get_result",
        "get_transcript",
        "list_agents",
        "list_sessions",
        "list_tasks",
        "pause_task",
        "resume_task",
        "set_preferences",
        "wait_task",
    ]


@pytest.mark.asyncio
async def test_public_mcp_catalog_exposes_all_tools_and_pause_schemas():
    """The public MCP catalog is the contract coordinators actually consume.

    Keep this separate from the private tool-manager test above: a Registry
    method can exist, and a decorator can be present, while the public
    ``tools/list`` representation still has a schema/serialization problem.
    """
    tools = await mcp.list_tools()
    by_name = {tool.name: tool for tool in tools}

    assert set(by_name) == {
        "cancel_task",
        "check_task",
        "dispatch_task",
        "end_session",
        "get_result",
        "get_transcript",
        "list_agents",
        "list_sessions",
        "list_tasks",
        "pause_task",
        "resume_task",
        "set_preferences",
        "wait_task",
    }

    pause_schema = by_name["pause_task"].input_schema
    assert pause_schema["properties"]["task_id"]["type"] == "string"
    assert pause_schema["required"] == ["task_id"]

    resume_schema = by_name["resume_task"].input_schema
    assert resume_schema["properties"]["task_id"]["type"] == "string"
    assert resume_schema["required"] == ["task_id"]


def test_handshake_instructions_carry_hard_rules():
    assert mcp.instructions == INSTRUCTIONS
    for phrase in (
        "dispatch_task.cwd",
        "wait_task",
        "coordinator.mode",
        "dispatch_enabled",
        "runtime_context",
        "cancel_task",
        "end_session",
        "pause_task",
        "resume_task",
        "remote",
        "list_tasks",
    ):
        assert phrase in INSTRUCTIONS


def test_list_tasks_tool_contract(bridge_home):
    """list_tasks is the read-only rediscovery path after a coordinator
    restart: remote rows are marked, never executed or cancelled here."""
    doc = list_tasks.__doc__ or ""
    assert "remote" in doc
    assert "read-only" in doc.lower() or "read only" in doc.lower()


@pytest.mark.asyncio
async def test_list_agents_reports_server_policy(bridge_home, monkeypatch):
    registry = Registry.create(bridge_home)
    registry.config.dashboard.enabled = False
    registry.config.server.shutdown_policy = "linger"
    monkeypatch.setattr(Registry, "list_agents", lambda self: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(Registry, "env_status", lambda self: asyncio.sleep(0, result={}))
    ctx = types.SimpleNamespace(
        request_context=types.SimpleNamespace(lifespan_context=registry)
    )
    payload = await list_agents(ctx)
    assert payload["server"] == {
        "idle_exit_sec": 7200,
        "shutdown_policy": "linger",
        "linger_max_sec": 86400,
        "remote_tasks": True,
    }


def test_error_exposes_exception_type():
    assert _error(ValueError("bad cwd")) == {
        "ok": False,
        "error": "ValueError: bad cwd",
        "error_type": "ValueError",
    }


def test_registry_from_lifespan_context_touches_activity(bridge_home):
    registry = Registry.create(bridge_home)
    registry._last_activity = registry._last_activity - 1
    before = registry._last_activity
    ctx = types.SimpleNamespace(
        request_context=types.SimpleNamespace(lifespan_context=registry)
    )
    got = _registry(ctx)
    assert got is registry
    assert registry._last_activity > before


def test_registry_rejects_dict_lifespan(bridge_home):
    registry = Registry.create(bridge_home)
    ctx = types.SimpleNamespace(
        request_context=types.SimpleNamespace(lifespan_context={"registry": registry})
    )
    with pytest.raises(RuntimeError, match="not available"):
        _registry(ctx)
