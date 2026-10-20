"""The MCP surface: seven tools, short descriptions, compact payloads.

Registry's own dicts stay rich (the dashboard and Registry tests read them);
these tests pin what a coordinator actually receives through the tools.
"""

import asyncio
import json
import types
import uuid

import pytest

from agent_bridge.quota import QuotaStatus, QuotaWindow
from agent_bridge.registry import Registry
from agent_bridge.server import (
    FIRST_PAGE_CHARS,
    INSTRUCTIONS,
    _registry,
    cancel_task,
    dispatch_task,
    get_result,
    list_agents,
    list_tasks,
    mcp,
    set_preferences,
    wait_task,
)

TOOLS = {
    "cancel_task",
    "dispatch_task",
    "get_result",
    "list_agents",
    "list_tasks",
    "set_preferences",
    "wait_task",
}


def _ctx(registry: Registry):
    return types.SimpleNamespace(request_context=types.SimpleNamespace(lifespan_context=registry))


def _no_empty_values(payload):
    """No key carries null / false / empty — except answers that are booleans by nature."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key not in {"available", "dispatch_enabled", "result"}:
                empty = value is None or value is False or (isinstance(value, (list, dict, str)) and not value)
                assert not empty, f"{key} is {value!r}"
            _no_empty_values(value)
    elif isinstance(payload, list):
        for item in payload:
            _no_empty_values(item)


@pytest.fixture
async def bridge(bridge_home, tmp_path):
    registry = Registry.create(bridge_home)
    registry.config.dashboard.enabled = False
    await registry.start()
    work = tmp_path / "work"
    work.mkdir()
    try:
        yield registry, _ctx(registry), str(work.resolve())
    finally:
        await registry.stop()


def test_seven_tools_registered():
    assert set(mcp._tool_manager._tools) == TOOLS


@pytest.mark.asyncio
async def test_public_catalog_and_text_budget():
    tools = await mcp.list_tools()
    by_name = {tool.name: tool for tool in tools}
    assert set(by_name) == TOOLS
    assert by_name["wait_task"].input_schema["required"] == ["task_id"]
    assert by_name["cancel_task"].input_schema["required"] == ["task_id"]
    dispatch = by_name["dispatch_task"].input_schema
    assert "continue_task" in dispatch["properties"]
    assert not dispatch.get("required")
    # The handshake plus every description is what each coordinator loads.
    assert len(INSTRUCTIONS) <= 600
    for tool in tools:
        assert len(tool.description or "") <= 300, tool.name
    assert len(INSTRUCTIONS) + sum(len(tool.description or "") for tool in tools) <= 2600


def test_handshake_instructions_carry_hard_rules():
    assert mcp.instructions == INSTRUCTIONS
    for phrase in ("list_agents", "dispatch_enabled", "dispatch_task.cwd", "wait_task", "git diff", "sibling"):
        assert phrase in INSTRUCTIONS
    for gone in ("check_task", "pause_task", "resume_task", "end_session", "get_transcript", "list_sessions"):
        assert gone not in INSTRUCTIONS


@pytest.mark.asyncio
async def test_list_agents_is_compact(bridge_home, monkeypatch):
    registry = Registry.create(bridge_home)
    registry.config.dashboard.enabled = False
    rows = [
        {
            "agent": "kimi",
            "available": True,
            "version": "1.2.3",
            "detail": "ok",
            "quota": {
                "status": "ok",
                "windows": [
                    {
                        "name": "weekly",
                        "remaining_percent": 40.0,
                        "resets_at": "2026-10-20T00:00:00Z",
                        "resets_in_sec": 99,
                    }
                ],
                "balance": None,
                "plan": "pro",
                "source": "kimi",
                "fetched_at": "x",
                "cached": True,
                "stale": False,
                "detail": "read fine",
            },
        },
        {
            "agent": "grok",
            "available": False,
            "version": None,
            "detail": "grok not found",
            "quota": {"status": "unknown"},
        },
    ]
    monkeypatch.setattr(Registry, "list_agents", lambda self: asyncio.sleep(0, result=rows))
    env = {"proxy": None, "proxy_source": None, "present": ["A"], "missing": ["B"], "warnings": ["2 other instances"]}
    monkeypatch.setattr(Registry, "env_status", lambda self: asyncio.sleep(0, result=env))
    payload = await list_agents(_ctx(registry))
    assert payload == {
        "mode": registry.config.coordinator.mode,
        "dispatch_enabled": True,
        "warnings": ["2 other instances"],
        "agents": [
            {
                "agent": "kimi",
                "available": True,
                "note": "an empty result with warnings is a failed turn, not a no-op",
                "quota": {
                    "status": "ok",
                    "windows": [{"name": "weekly", "remaining_percent": 40.0, "resets_at": "2026-10-20T00:00:00Z"}],
                },
            },
            {"agent": "grok", "available": False, "detail": "grok not found", "note": payload["agents"][1]["note"]},
        ],
    }
    assert "server" not in payload and "env" not in payload and "coordinator" not in payload


@pytest.mark.asyncio
async def test_dispatch_wait_returns_result_inline(bridge):
    _reg, ctx, cwd = bridge
    sent = await dispatch_task(ctx, message="build it", agent="fake", cwd=cwd)
    assert set(sent) == {"task_id", "session_id"}
    done = await wait_task(ctx, sent["task_id"], timeout_sec=30)
    assert done["status"] == "completed"
    assert done["result"] == "[fake:fake] build it"
    assert "next_cursor" not in done
    _no_empty_values(done)
    for gone in (
        "ok",
        "remote",
        "owner",
        "retryable",
        "usage",
        "created_at",
        "hint",
        "resume_hint",
        "paused",
        "timed_out",
        "stall_timeout_sec",
    ):
        assert gone not in done
    # The terminal payload without its text stays small.
    assert len(json.dumps({k: v for k, v in done.items() if k != "result"})) <= 1500


@pytest.mark.asyncio
async def test_wait_zero_is_a_check_and_running_payload_is_progress(bridge, monkeypatch):
    _reg, ctx, cwd = bridge
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    sent = await dispatch_task(ctx, message="slow", agent="fake", cwd=cwd)
    await asyncio.sleep(0.2)
    running = await wait_task(ctx, sent["task_id"], timeout_sec=0)
    assert running["status"] == "running"
    assert "result" not in running and "files_changed" not in running
    assert set(running) <= {
        "task_id",
        "session_id",
        "status",
        "owner",
        "elapsed_sec",
        "silent_for_sec",
        "activity",
        "run_usage",
        "resumable",
    }
    timed_out = await wait_task(ctx, sent["task_id"], timeout_sec=0.2)
    assert timed_out["status"] == "running"
    await cancel_task(ctx, sent["task_id"])


@pytest.mark.asyncio
async def test_cancel_is_graceful_and_continue_task_resumes(bridge, monkeypatch):
    _reg, ctx, cwd = bridge
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_PARTIAL", "1")
    sent = await dispatch_task(ctx, message="long job", agent="fake", cwd=cwd)
    await asyncio.sleep(0.2)
    stopped = await cancel_task(ctx, sent["task_id"])
    assert stopped["status"] == "cancelled"
    assert stopped["stop_reason"] == "cancelled"
    assert stopped["resumable"] is True
    assert "partial progress" in stopped["result"]
    # Idempotent on a finished task.
    again = await cancel_task(ctx, sent["task_id"])
    assert again["status"] == "cancelled"

    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "0.05")
    monkeypatch.delenv("AGENT_BRIDGE_FAKE_PARTIAL")
    resumed = await dispatch_task(ctx, continue_task=sent["task_id"])
    assert resumed["continues"] == sent["task_id"]
    assert resumed["session_id"] == sent["session_id"]
    done = await wait_task(ctx, resumed["task_id"], timeout_sec=30)
    assert done["status"] == "completed"
    # A second resume of the same row returns the continuation, not a new turn.
    replay = await dispatch_task(ctx, continue_task=sent["task_id"])
    assert replay["task_id"] == resumed["task_id"] and replay["reused"] is True

    rows = {row["task_id"]: row for row in (await list_tasks(ctx))["tasks"]}
    assert rows[sent["task_id"]]["resumed_by"] == resumed["task_id"]
    assert "resumable" not in rows[sent["task_id"]]
    assert rows[resumed["task_id"]]["resume_of"] == sent["task_id"]
    _no_empty_values(rows)


@pytest.mark.asyncio
async def test_dispatch_argument_errors(bridge):
    _reg, ctx, _cwd = bridge
    with pytest.raises(RuntimeError, match="required unless continue_task"):
        await dispatch_task(ctx, message="x", agent="fake")
    with pytest.raises(RuntimeError, match="continue_task takes only"):
        await dispatch_task(ctx, continue_task="t-1", agent="fake")
    with pytest.raises(RuntimeError, match=r"^unknown task t-missing$"):
        await wait_task(ctx, "t-missing", timeout_sec=0)


@pytest.mark.asyncio
async def test_request_id_reused_only_when_true(bridge):
    _reg, ctx, cwd = bridge
    rid = str(uuid.uuid4())
    first = await dispatch_task(ctx, message="once", agent="fake", cwd=cwd, request_id=rid)
    assert "reused" not in first
    second = await dispatch_task(ctx, message="once", agent="fake", cwd=cwd, request_id=rid)
    assert second["task_id"] == first["task_id"] and second["reused"] is True


@pytest.mark.asyncio
async def test_long_result_pages_through_get_result(bridge):
    _reg, ctx, cwd = bridge
    long_message = "y" * (FIRST_PAGE_CHARS + 5000)
    sent = await dispatch_task(ctx, message=long_message, agent="fake", cwd=cwd)
    done = await wait_task(ctx, sent["task_id"], timeout_sec=30)
    assert len(done["result"]) == FIRST_PAGE_CHARS
    rest = await get_result(ctx, sent["task_id"], cursor=done["next_cursor"])
    assert set(rest) == {"result"}
    assert done["result"] + rest["result"] == f"[fake:fake] {long_message}"


@pytest.mark.asyncio
async def test_dispatch_warns_on_exhausted_model_window(bridge):
    registry, ctx, cwd = bridge
    registry._quota_cache.put(
        "fake",
        QuotaStatus(
            status="ok",
            windows=[
                QuotaWindow(name="weekly", remaining_percent=50.0),
                QuotaWindow(name="weekly:opus", remaining_percent=0.0, resets_at="2099-01-01T00:00:00Z"),
            ],
        ),
        ttl_sec=600,
    )
    sent = await dispatch_task(ctx, message="hi", agent="fake", cwd=cwd)
    assert "warnings" not in sent
    assert registry.quota_warnings("fake", "claude-opus-5") == [
        "fake weekly:opus quota is at 0% for claude-opus-5; resets 2099-01-01T00:00:00Z"
    ]
    assert registry.quota_warnings("fake", "claude-sonnet-5") == []


@pytest.mark.asyncio
async def test_set_preferences_returns_policy_only(bridge):
    _reg, ctx, _cwd = bridge
    payload = await set_preferences(ctx, mode="eager", instructions="research goes to antigravity")
    assert payload == {"mode": "eager", "instructions": "research goes to antigravity"}


@pytest.mark.asyncio
async def test_nested_bridge_refuses_cancel(bridge):
    registry, ctx, cwd = bridge
    sent = await dispatch_task(ctx, message="x", agent="fake", cwd=cwd)
    await wait_task(ctx, sent["task_id"], timeout_sec=30)
    registry.dispatch_enabled = False
    with pytest.raises(RuntimeError):
        await cancel_task(ctx, sent["task_id"])


def test_registry_from_lifespan_context_touches_activity(bridge_home):
    registry = Registry.create(bridge_home)
    registry._last_activity = registry._last_activity - 1
    before = registry._last_activity
    got = _registry(_ctx(registry))
    assert got is registry
    assert registry._last_activity > before


def test_registry_rejects_dict_lifespan(bridge_home):
    registry = Registry.create(bridge_home)
    ctx = types.SimpleNamespace(request_context=types.SimpleNamespace(lifespan_context={"registry": registry}))
    with pytest.raises(RuntimeError, match="not available"):
        _registry(ctx)
