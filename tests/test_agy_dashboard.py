"""Regression tests for Antigravity transcript -> dashboard rendering.

The agy adapter used to persist every stream-json record opaquely as
``data={"payload": <raw obj>}`` while every consumer (dashboard
``normalize_event``, the feed JS, ``recent_activity``, the live-usage tail)
reads the flat normalized schema — producing blank Agent cards, Tool rows with
``input = "null"``, duplicate permanently-spinning tool rows and no live
usage. These tests pin both sides of the contract: the adapter emits flat
fields, and ``normalize_event`` unwraps legacy payload records already on disk.
"""

import json
import sys
from pathlib import Path

import pytest

from agent_bridge.adapters.antigravity import AgyAdapter, agy_tool_kind, classify_event
from agent_bridge.config import AgentConfig
from agent_bridge.models import Session, Task
from agent_bridge.share import dashboard
from agent_bridge.transcript import read_events, recent_activity

FAKE_AGY = Path(__file__).resolve().parent / "fake_agy.py"
CID = "c3b66b04-872b-4fbe-a3a4-058a026ef20a"

# Verbatim legacy records (data.payload shape) from a real agy transcript.
LEGACY_MSG = {
    "type": "message_chunk",
    "ts": "2026-09-14T10:00:01Z",
    "data": {
        "payload": {
            "event": "step_update",
            "step_update": {
                "step_index": 141,
                "state": "ACTIVE",
                "step_type": "agent_response",
                "text_delta": "### Root-Cause Diagnosis\n\nThere is *",
            },
        }
    },
}
LEGACY_TOOL_ACTIVE = {
    "type": "tool_call",
    "ts": "2026-09-14T10:00:02Z",
    "data": {
        "payload": {
            "event": "step_update",
            "step_update": {
                "conversation_id": CID,
                "step_index": 2,
                "state": "ACTIVE",
                "step_type": "tool",
                "tool_name": "find_by_name",
                "tool_info": {
                    "name": "find_by_name",
                    "parameters": {"Pattern": "*", "SearchDirectory": "C:/repo"},
                },
            },
        }
    },
}
LEGACY_TOOL_DONE = {
    "type": "tool_call",
    "ts": "2026-09-14T10:00:03Z",
    "data": {
        "payload": {
            "event": "step_update",
            "step_update": {
                "conversation_id": CID,
                "step_index": 2,
                "state": "DONE",
                "step_type": "tool",
                "tool_name": "find_by_name",
                "tool_info": {"name": "find_by_name", "output": "usage.py\nother.py"},
            },
        }
    },
}
LEGACY_USAGE_ONLY_STEP = {
    "type": "message_chunk",
    "ts": "2026-09-14T10:00:04Z",
    "data": {
        "payload": {
            "event": "step_update",
            "step_update": {
                "conversation_id": CID,
                "step_index": 7,
                "state": "DONE",
                "step_type": "agent_response",
                "usage": {"total_tokens": 900},
            },
        }
    },
}


def _norm(rec):
    return dashboard.normalize_event(rec)


# ---------- normalize_event: legacy payload records ----------


def test_legacy_message_chunk_recovers_text():
    ev = _norm(LEGACY_MSG)
    assert ev == {
        "t": "msg",
        "ts": "2026-09-14T10:00:01Z",
        "text": "### Root-Cause Diagnosis\n\nThere is *",
    }


def test_legacy_tool_active_recovers_row_fields():
    ev = _norm(LEGACY_TOOL_ACTIVE)
    assert ev["t"] == "tool"
    assert ev["id"] == f"{CID}:2"
    assert ev["title"] == "find_by_name"
    assert ev["kind"] == "search"
    assert json.loads(ev["input"]) == {"Pattern": "*", "SearchDirectory": "C:/repo"}
    assert "status" not in ev


def test_legacy_tool_done_becomes_completed_row_with_same_id():
    ev = _norm(LEGACY_TOOL_DONE)
    # A complete row (not a bare tool_status) so a truncated/missed ACTIVE
    # twin still leaves a rendered, already-completed tool entry. The
    # frontend folds it into the ACTIVE row by id when that row exists.
    assert ev["t"] == "tool"
    assert ev["id"] == f"{CID}:2"
    assert ev["title"] == "find_by_name"
    assert ev["status"] == "completed"
    assert ev["output"] == "usage.py\nother.py"


def test_legacy_usage_only_message_step_drops():
    # DONE agent_response steps carry usage but no text_delta; painting them
    # was the source of the blank Agent card.
    assert _norm(LEGACY_USAGE_ONLY_STEP) is None


def test_truncated_and_raw_records_stay_dropped():
    assert _norm({"type": "raw", "data": {"truncated": True}}) is None
    assert _norm({"type": "raw", "data": {"payload": {"event": "init"}}}) is None
    assert _norm({"type": "message_chunk", "data": {"text": ""}}) is None


def test_flat_acp_records_are_not_rewritten():
    # Guard against double-unwrapping: the flat schema passes through as before.
    flat_tool = {
        "type": "tool_call",
        "ts": "t",
        "data": {
            "update_type": "ToolCallStart",
            "title": "Read file",
            "tool_call_id": "read:0",
            "kind": "read",
            "input": '{"file_path": "/x/usage.py"}',
        },
    }
    ev = _norm(flat_tool)
    assert ev["t"] == "tool" and ev["id"] == "read:0" and ev["kind"] == "read"
    assert ev["title"] == "Read file"
    assert json.loads(ev["input"]) == {"file_path": "/x/usage.py"}
    assert "status" not in ev
    assert _norm({"type": "message_chunk", "ts": "t", "data": {"text": "hi"}}) == {
        "t": "msg",
        "ts": "t",
        "text": "hi",
    }


def test_tool_without_input_no_longer_renders_null():
    ev = _norm(
        {"type": "tool_call", "ts": "t", "data": {"tool_call_id": "x:1", "title": "ls"}}
    )
    assert ev["input"] == ""  # was the literal string "null"


def test_new_records_with_flat_fields_and_payload_prefer_flat():
    # Write-side now emits both; real data keys must win over payload-derived.
    rec = {
        "type": "message_chunk",
        "ts": "t",
        "data": {
            "text": "normalized text",
            "payload": LEGACY_MSG["data"]["payload"],
        },
    }
    assert _norm(rec) == {"t": "msg", "ts": "t", "text": "normalized text"}


# ---------- write-side classification ----------


def test_classify_event_shapes():
    step = {
        "conversation_id": CID,
        "step_index": 5,
        "state": "ACTIVE",
        "step_type": "tool",
        "tool_name": "run_command",
        "tool_info": {"name": "run_command", "parameters": {"CommandLine": "ls"}},
    }
    et, data = classify_event({"event": "step_update", "step_update": step}, step, "tool", "step_update", "", CID)
    assert et == "tool_call"
    assert data["tool_call_id"] == f"{CID}:5"
    assert data["kind"] == "execute"
    assert json.loads(data["input"]) == {"CommandLine": "ls"}

    done = {**step, "state": "DONE"}
    et, data = classify_event({"event": "step_update", "step_update": done}, done, "tool", "step_update", "", CID)
    assert et == "tool_call_update"
    assert data == {"tool_call_id": f"{CID}:5", "status": "completed", "title": "run_command"}

    resp = {"step_type": "agent_response", "text_delta": "hi"}
    et, data = classify_event({"step_update": resp}, resp, "agent_response", "step_update", "hi", CID)
    assert et == "message_chunk" and data == {"text": "hi"}

    quiet = {"step_type": "agent_response", "state": "DONE", "usage": {"total_tokens": 3}}
    et, _ = classify_event({"step_update": quiet}, quiet, "agent_response", "step_update", "", CID)
    assert et == "raw"  # no text -> no empty Agent card

    err = {"step_type": "error_message", "error": "quota exceeded"}
    et, data = classify_event({"step_update": err}, err, "error_message", "step_update", "", CID)
    assert et == "error" and data["error"] == "quota exceeded"
    et, _ = classify_event({"step_update": {"step_type": "error_message"}}, {"step_type": "error_message"}, "error_message", "step_update", "", CID)
    assert et == "raw"


def test_agy_tool_kind_mapping():
    assert agy_tool_kind("find_by_name") == "search"
    assert agy_tool_kind("view_file") == "read"
    assert agy_tool_kind("run_command") == "execute"
    assert agy_tool_kind("write_to_file") == "edit"
    assert agy_tool_kind("read_url_content") == "fetch"
    assert agy_tool_kind("custom_thing") == "tool"


# ---------- end-to-end: adapter transcript -> dashboard events ----------


def _adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgyAdapter:
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.resolve_command",
        lambda command, fallbacks=None: [sys.executable, str(FAKE_AGY)],
    )
    return AgyAdapter(
        AgentConfig(name="antigravity", protocol="agy", command=["agy"]),
        tmp_path,
    )


@pytest.mark.asyncio
async def test_run_turn_writes_normalized_transcript(tmp_path, monkeypatch):
    """The round-trip that would have caught the bug: every agy record must
    reach the dashboard feed with renderable fields, tool rows must pair with
    their status, and usage must be live-readable."""
    monkeypatch.setenv("FAKE_AGY_MODE", "rich")
    adapter = _adapter(tmp_path, monkeypatch)
    session = Session(session_id="sess_rich", agent="antigravity", cwd=str(tmp_path))
    task = Task(
        task_id="task_rich",
        session_id=session.session_id,
        agent="antigravity",
        message="hello",
        cwd=str(tmp_path),
    )
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "end_turn"

    records = read_events(session.session_id, tmp_path)
    types = [r["type"] for r in records]
    assert "tool_call" in types and "tool_call_update" in types and "usage" in types

    # Every written record carries the flat schema (payload is diagnostic only).
    for r in records:
        d = r.get("data") or {}
        if r["type"] == "message_chunk":
            assert d.get("text"), "empty message_chunk would paint a blank card"
        if r["type"] == "tool_call":
            assert d.get("tool_call_id") and d.get("title")

    events = [ev for r in records if (ev := _norm(r)) is not None]
    msgs = [e for e in events if e["t"] == "msg"]
    assert msgs and all(e["text"] for e in msgs)
    assert any("echo:hello" in e["text"] for e in msgs)

    tools = [e for e in events if e["t"] == "tool"]
    assert len(tools) == 1  # one ACTIVE step -> one row; DONE is an update
    tool = tools[0]
    assert tool["id"] == "conv-fake-agy:1"
    assert tool["title"] == "find_by_name"
    assert tool["kind"] == "search"
    assert json.loads(tool["input"]) == {"Pattern": "*.py", "SearchDirectory": "C:/repo"}

    statuses = [e for e in events if e["t"] == "tool_status"]
    assert {"id": "conv-fake-agy:1", "status": "completed"} in [
        {"id": e["id"], "status": e["status"]} for e in statuses
    ]
    assert events.index(tool) < next(
        i for i, e in enumerate(events) if e["t"] == "tool_status"
    )

    usages = [e for e in events if e["t"] == "usage"]
    assert usages and usages[-1]["consumed"]["total"] == 42

    # wait_task/check_task activity summaries see real text/titles, not bare kinds.
    activity = recent_activity(records)
    assert any("echo:hello" in a for a in activity)
    assert any("find_by_name" in a for a in activity)


@pytest.mark.asyncio
async def test_run_turn_live_usage_tail_finds_consumed(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_MODE", "rich")
    adapter = _adapter(tmp_path, monkeypatch)
    session = Session(session_id="sess_live", agent="antigravity", cwd=str(tmp_path))
    task = Task(
        task_id="task_live",
        session_id=session.session_id,
        agent="antigravity",
        message="hello",
        cwd=str(tmp_path),
    )
    await adapter.run_turn(session, task)
    blob = (tmp_path / "transcripts" / "sess_live.jsonl").read_bytes()
    consumed = dashboard._usage_consumed_last(blob, 0.0)
    assert consumed is not None
    assert consumed["total"] == 42
    assert consumed["scope"] == "run"
