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


def _upd(used: int, meta: dict, size: int = 100) -> UsageUpdate:
    return UsageUpdate(
        used=used, size=size, sessionUpdate="usage_update", field_meta=meta
    )


def _adapter(
    tmp_path: Path,
    env: dict[str, str] | None = None,
    *,
    revivable: bool = False,
    prompt_usage_scope: str = "conversation",
) -> AcpAdapter:
    return AcpAdapter(
        AgentConfig(
            name="echo",
            protocol="acp",
            command=[sys.executable, str(ECHO)],
            env=env or {},
            revivable=revivable,
            prompt_usage_scope=prompt_usage_scope,
        ),
        tmp_path,
    )


def _task(session: Session, task_id: str, message: str, agent: str = "echo") -> Task:
    return Task(
        task_id=task_id,
        session_id=session.session_id,
        agent=agent,
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
async def test_client_paired_root_updates_dedup_to_one_stream(tmp_path):
    """Live `devin acp` emits every root UsageUpdate twice: bare plus a
    {"parentAgentId": "root"} annotated copy of the same counters
    (sess_fafc98c2f9). parentAgentId is a parent pointer — both copies are
    the root stream, the second member of each pair dedups, and the
    transcript gets two usage events rather than four."""
    client = _BridgeClient("sess_dup", tmp_path, "devin")
    client.reset_turn()
    root_ctx = {_COG + "subagent_context": {"parentAgentId": "root"}}
    first = {_COG + "inputTokens": 12072, _COG + "outputTokens": 55}
    second = {
        _COG + "inputTokens": 12219,
        _COG + "cachedReadTokens": 12071,
        _COG + "outputTokens": 56,
    }
    for update in (
        _upd(12127, first, size=262000),
        _upd(12127, {**first, **root_ctx}, size=262000),
        _upd(12275, second, size=262000),
        _upd(12275, {**second, **root_ctx}, size=262000),
    ):
        await client.session_update("sess_dup", update)

    final = client.run.finish()
    assert final["input"] == 12219
    assert final["cached_read"] == 12071
    assert final["output"] == 111
    assert final["total"] == 12330
    assert final["streams"] == 1
    assert final["used"] == 12275 and final["size"] == 262000
    # The persisted per-stream baseline holds only the real stream.
    assert set(client.run.stream_baselines) == {""}

    events = [e for e in read_events("sess_dup", tmp_path) if e["type"] == "usage"]
    assert len(events) == 2  # the annotated re-emissions are suppressed
    assert events[-1]["data"]["consumed"]["total"] == 12330
    assert "root" not in json.dumps(events)


@pytest.mark.asyncio
async def test_run_turn_paired_root_updates_aggregate_once(tmp_path):
    """End to end over the real ACP wire: ECHO_USAGE_PAIRED_ROOT replays the
    live devin paired emission; the run aggregates one stream and persists
    only real stream baselines."""
    adapter = AcpAdapter(
        AgentConfig(
            name="devin",
            protocol="acp",
            command=[sys.executable, str(ECHO)],
            env={"ECHO_USAGE_PAIRED_ROOT": "1"},
        ),
        tmp_path,
    )
    session = Session(session_id="sess_pair", agent="devin", cwd=str(tmp_path))
    try:
        result = await adapter.run_turn(session, _task(session, "t_pair", "hi", agent="devin"))
        assert result.run_usage["input"] == 12219
        assert result.run_usage["cached_read"] == 12071
        assert result.run_usage["output"] == 111
        assert result.run_usage["total"] == 12330
        assert result.run_usage["streams"] == 1
        assert result.run_usage["used"] == 12275 and result.run_usage["size"] == 262000
        assert set(session.usage_baseline["streams"]) == {""}

        events = [e for e in read_events("sess_pair", tmp_path) if e["type"] == "usage"]
        assert len(events) == 2
    finally:
        await adapter.shutdown(session)


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


@pytest.mark.asyncio
async def test_run_turn_prompt_usage_scope_turn_never_zeroes(tmp_path):
    """prompt_usage_scope="turn" (claude-agent-acp semantics): every
    PromptResponse.usage is this turn's usage — identical follow-up
    snapshots count in full instead of deltaing to zero."""
    adapter = _adapter(tmp_path, prompt_usage_scope="turn")
    session = Session(session_id="sess_pt", agent="echo", cwd=str(tmp_path))
    try:
        first = await adapter.run_turn(session, _task(session, "t_pt1", "one"))
        second = await adapter.run_turn(session, _task(session, "t_pt2", "two"))
    finally:
        await adapter.shutdown(session)
    for result in (first, second):
        assert result.run_usage["input"] == 1
        assert result.run_usage["output"] == 2
        assert result.run_usage["total"] == 3
        assert result.run_usage["quality"] == "exact"


@pytest.mark.asyncio
async def test_run_turn_failed_revive_is_a_fresh_conversation(tmp_path):
    """A stale native id whose session/load fails falls through to
    session/new: the conversation did NOT continue, so the first usage
    snapshot counts in full rather than hiding behind an estimate."""
    adapter = _adapter(tmp_path, env={"ECHO_FAIL_LOAD": "1"}, revivable=True)
    session = Session(
        session_id="sess_fresh",
        agent="echo",
        cwd=str(tmp_path),
        native_session_id="echo-stale",
        usage_baseline={
            "cid": "echo-stale",
            "counters": {"input": 9, "output": 9, "total": 18},
        },
    )
    try:
        result = await adapter.run_turn(session, _task(session, "t_fr", "hi"))
        assert result.run_usage["input"] == 1
        assert result.run_usage["output"] == 2
        assert result.run_usage["total"] == 3
        assert result.run_usage["quality"] == "exact"
        # The baseline re-anchors on the new native conversation.
        assert session.usage_baseline["cid"] == "echo-session"
    finally:
        await adapter.shutdown(session)


@pytest.mark.asyncio
async def test_run_turn_revived_conversation_still_deltas(tmp_path):
    """The other half of the resumed decision: a successful revive continues
    the conversation, so an unchanged snapshot deltas to zero against the
    persisted baseline — a fresh conversation would have counted it."""
    adapter = _adapter(tmp_path, revivable=True)
    session = Session(
        session_id="sess_rev",
        agent="echo",
        cwd=str(tmp_path),
        native_session_id="echo-session",
        usage_baseline={
            "cid": "echo-session",
            "counters": {"input": 1, "output": 2, "total": 3},
        },
    )
    try:
        result = await adapter.run_turn(session, _task(session, "t_rv", "hi"))
        assert result.run_usage == {"scope": "run", "quality": "exact"}
    finally:
        await adapter.shutdown(session)


@pytest.mark.asyncio
async def test_run_turn_respawned_worker_deltas_against_stream_baselines(tmp_path):
    """Conversation-cumulative UsageUpdate counters keep attributing only
    the new delta after the worker process is replaced: the persisted
    per-stream baseline, not zero, is the reference."""
    state = tmp_path / "echo-usage-state.json"
    adapter = _adapter(
        tmp_path,
        env={"ECHO_USAGE": "1", "ECHO_USAGE_STATE": str(state)},
        revivable=True,
    )
    session = Session(session_id="sess_respawn", agent="echo", cwd=str(tmp_path))
    try:
        first = await adapter.run_turn(session, _task(session, "t_rs1", "hi"))
        assert first.run_usage["total"] == 72
        assert session.usage_baseline["streams"]

        await adapter.shutdown(session)  # worker process dies

        second = await adapter.run_turn(session, _task(session, "t_rs2", "again"))
        # Turn-1 counters continue the conversation totals (root 30/13, sub
        # 40/6); only the new deltas count — not the whole snapshots.
        assert second.run_usage["input"] == 10
        assert second.run_usage["output"] == 7
        assert second.run_usage["total"] == 17
        assert second.run_usage["quality"] == "exact"
    finally:
        await adapter.shutdown(session)


@pytest.mark.asyncio
async def test_run_turn_error_keeps_partial_usage_and_persists_baselines(tmp_path):
    """A prompt that fails mid-stream still returns a TurnResult carrying
    the partial run_usage, closes the transcript turn, and persists the
    baselines for the next run."""
    adapter = _adapter(tmp_path, env={"ECHO_USAGE": "1", "ECHO_FAIL_PROMPT": "1"})
    session = Session(session_id="sess_err", agent="echo", cwd=str(tmp_path))
    try:
        result = await adapter.run_turn(session, _task(session, "t_err", "boom"))
        assert result.stop_reason == "error"
        assert result.error
        # All five updates landed before the raise; nothing is dropped.
        assert result.run_usage["input"] == 60
        assert result.run_usage["output"] == 12
        assert result.run_usage["total"] == 72
        assert session.usage_baseline["streams"]
        ends = [e for e in read_events("sess_err", tmp_path) if e["type"] == "turn_end"]
        assert ends and ends[-1]["data"]["stop_reason"] == "error"
    finally:
        await adapter.shutdown(session)
