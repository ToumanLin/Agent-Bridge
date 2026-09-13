"""ACP run-usage coverage: UsageUpdate stream aggregation over the wire,
PromptResponse conversation-snapshot fallback, and the live transcript feed."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from acp.schema import UsageUpdate

from agent_bridge.adapters.acp import AcpAdapter, _BridgeClient
from agent_bridge.config import AgentConfig
from agent_bridge.models import Session, Task
from agent_bridge.transcript import read_events

ECHO = Path(__file__).resolve().parent / "echo_agent.py"

_COG = "cognition.ai/"


def _upd(used: int, meta: dict) -> UsageUpdate:
    return UsageUpdate(
        used=used, size=100, sessionUpdate="usage_update", field_meta=meta
    )


def _adapter(tmp_path: Path, env: dict[str, str] | None = None) -> AcpAdapter:
    return AcpAdapter(
        AgentConfig(
            name="echo",
            protocol="acp",
            command=[sys.executable, str(ECHO)],
            env=env or {},
        ),
        tmp_path,
    )


def _task(session: Session, task_id: str, message: str) -> Task:
    return Task(
        task_id=task_id,
        session_id=session.session_id,
        agent="echo",
        message=message,
        cwd=session.cwd,
    )


@pytest.mark.asyncio
async def test_client_usage_updates_aggregate_streams(tmp_path):
    """Every UsageUpdate payload is folded in per stream; the transcript gets
    deduped normalized 'usage' events with no subagent ids."""
    client = _BridgeClient("sess_u", tmp_path, "echo")
    client.reset_turn()
    sub = {_COG + "subagent_context": {"runId": "sub-1", "parentAgentId": "root"}}
    updates = [
        _upd(5, {_COG + "inputTokens": 10, _COG + "outputTokens": 4}),
        _upd(8, {_COG + "inputTokens": 20, **sub}),
        _upd(9, {_COG + "inputTokens": 25, _COG + "outputTokens": 9}),
        _upd(9, {_COG + "inputTokens": 25, _COG + "outputTokens": 9}),  # paired dup
        _upd(12, {_COG + "inputTokens": 35, _COG + "outputTokens": 3, **sub}),
    ]
    for update in updates:
        await client.session_update("sess_u", update)

    # raw compat: last-wins snapshot is still the final update payload
    assert client.usage.get("used") == 12
    assert client.usage.get("sessionUpdate") == "usage_update"

    final = client.run.finish()
    assert final["input"] == 60
    assert final["output"] == 12
    assert final["total"] == 72
    assert final["streams"] == 2
    assert final["quality"] == "exact"
    assert final["used"] == 12 and final["size"] == 100

    events = [e for e in read_events("sess_u", tmp_path) if e["type"] == "usage"]
    assert len(events) == 4  # the identical re-emission is suppressed
    assert events[-1]["data"]["consumed"]["total"] == 72
    # no stream routing key / subagent id anywhere in the emitted events
    assert "sub-1" not in json.dumps(events)
    assert '"stream":' not in json.dumps(events)


@pytest.mark.asyncio
async def test_client_usage_updates_context_only_still_emit_once(tmp_path):
    client = _BridgeClient("sess_ctx", tmp_path, "echo")
    client.reset_turn()
    await client.session_update("sess_ctx", _upd(5, {}))
    await client.session_update("sess_ctx", _upd(5, {}))  # identical -> dropped
    await client.session_update("sess_ctx", _upd(7, {}))
    events = [e for e in read_events("sess_ctx", tmp_path) if e["type"] == "usage"]
    assert len(events) == 2
    consumed = events[-1]["data"]["consumed"]
    assert consumed["used"] == 7 and "total" not in consumed


@pytest.mark.asyncio
async def test_run_turn_prompt_response_is_conversation_delta(tmp_path):
    """With no UsageUpdate counters, PromptResponse.usage (cumulative across
    turns) falls back to a per-run delta — the second turn counts zero."""
    adapter = _adapter(tmp_path)
    session = Session(session_id="sess_pr", agent="echo", cwd=str(tmp_path))
    try:
        first = await adapter.run_turn(session, _task(session, "t_pr1", "one"))
        assert first.run_usage["input"] == 1
        assert first.run_usage["output"] == 2
        assert first.run_usage["total"] == 3
        assert first.run_usage["quality"] == "exact"
        # The conversation baseline persists on the session for restart safety.
        assert session.usage_baseline["counters"]["total"] == 3

        second = await adapter.run_turn(session, _task(session, "t_pr2", "two"))
        assert second.run_usage == {"scope": "run", "quality": "exact"}
    finally:
        await adapter.shutdown(session)


@pytest.mark.asyncio
async def test_run_turn_usage_stream_aggregates_root_and_subagent(tmp_path):
    """ECHO_USAGE makes the echo agent emit a devin-shaped stream; the wire
    path (client -> accumulator -> TurnResult) must match the unit math."""
    adapter = _adapter(tmp_path, env={"ECHO_USAGE": "1"})
    session = Session(session_id="sess_st", agent="echo", cwd=str(tmp_path))
    try:
        result = await adapter.run_turn(session, _task(session, "t_st1", "hi"))
        assert result.run_usage["input"] == 60
        assert result.run_usage["output"] == 12
        assert result.run_usage["total"] == 72
        assert result.run_usage["streams"] == 2
        assert result.run_usage["used"] == 12 and result.run_usage["size"] == 100
        # raw compat keeps the last snapshot (which is a UsageUpdate dump here)
        assert result.usage.get("used") == 12
        # PromptResponse counters were reconciled into the baseline, not summed.
        assert session.usage_baseline["counters"]["total"] == 3

        events = [e for e in read_events("sess_st", tmp_path) if e["type"] == "usage"]
        assert events and events[-1]["data"]["consumed"]["total"] == 72

        # Turn 2 on the same conversation: the echo agent continues its
        # cumulative counters (root +5/+4, sub +5/+3) — only the delta counts.
        second = await adapter.run_turn(session, _task(session, "t_st2", "again"))
        assert second.run_usage["streams"] == 2
        assert second.run_usage["input"] == 10
        assert second.run_usage["output"] == 7
        assert second.run_usage["total"] == 17
    finally:
        await adapter.shutdown(session)
