"""Projection details found by a live smoke run against Codex."""

from agent_bridge.public import task_row, task_view, usage_view
from agent_bridge.transcript import recent_activity


def _codex(kind: str, payload: dict) -> dict:
    return {"type": kind, "ts": "2026-10-20T00:00:00Z", "data": {"payload": payload}}


def test_codex_activity_shows_messages_and_commands_not_bare_raw():
    command = "\"pwsh.exe\" -Command 'Start-Sleep -Seconds 90'"
    events = [
        {"type": "prompt_sent", "data": {"text": "do it"}},
        _codex("raw", {"type": "thread.started", "thread_id": "t"}),
        _codex("raw", {"type": "turn.started"}),
        _codex(
            "message_chunk", {"type": "item.completed", "item": {"type": "agent_message", "text": "I'll wait first."}}
        ),
        _codex("raw", {"type": "item.started", "item": {"type": "command_execution", "command": command}}),
        _codex("raw", {"type": "item.completed", "item": {"type": "command_execution", "command": command}}),
        _codex("raw", {"type": "turn.completed", "usage": {"input_tokens": 1}}),
    ]
    assert recent_activity(events) == [
        "prompt_sent: do it",
        "message_chunk: I'll wait first.",
        f"command: {command}",
    ]


def test_usage_without_counters_is_dropped():
    assert usage_view({"scope": "run", "quality": "exact", "v": 1}) is None
    assert usage_view({"scope": "run", "v": 1, "partial": True}) is None
    assert usage_view(None) is None
    assert usage_view({"scope": "run", "quality": "exact", "v": 1, "input": 10, "output": 2, "total": 12}) == {
        "quality": "exact",
        "input": 10,
        "output": 2,
        "total": 12,
    }


def test_cancelled_task_without_usage_omits_run_usage():
    view = task_view(
        {
            "task_id": "t1",
            "session_id": "s1",
            "status": "cancelled",
            "stop_reason": "paused",
            "resumable": True,
            "run_usage": {"scope": "run", "quality": "exact", "v": 1},
        }
    )
    assert "run_usage" not in view
    assert view["stop_reason"] == "cancelled"
    assert view["resumable"] is True


def test_resumed_task_is_no_longer_offered_for_resume():
    snapshot = {"task_id": "t1", "session_id": "s1", "status": "cancelled", "resumable": True, "resumed_by": "t2"}
    assert "resumable" not in task_view(snapshot)
    row = task_row(snapshot, {})
    assert "resumable" not in row
    assert row["resumed_by"] == "t2"
