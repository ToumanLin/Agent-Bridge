"""Projection details found by a live smoke run against Codex."""

from agent_bridge.public import task_row, task_view, tasks_view, usage_view
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
        "input": 10,
        "output": 2,
        "total": 12,
    }


def test_usage_keeps_estimate_and_drops_conversation_total():
    usage = {
        "scope": "run",
        "quality": "estimate",
        "v": 1,
        "input": 10,
        "total": 10,
        "conversation_total": {"input": 900, "total": 900},
    }
    assert usage_view(usage) == {"quality": "estimate", "input": 10, "total": 10}


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
    assert "stop_reason" not in view  # it would only repeat status
    assert view["resumable"] is True


def test_stop_reason_kept_when_it_says_more_than_status():
    view = task_view({"task_id": "t1", "session_id": "s1", "status": "failed", "stop_reason": "stalled"})
    assert view["stop_reason"] == "stalled"


def test_running_task_carries_progress_not_usage():
    view = task_view(
        {
            "task_id": "t1",
            "session_id": "s1",
            "status": "running",
            "elapsed_sec": 3,
            "run_usage": {"scope": "run", "quality": "exact", "v": 1, "input": 10, "total": 10},
        }
    )
    assert "run_usage" not in view


def test_resumed_task_is_no_longer_offered_for_resume():
    snapshot = {"task_id": "t1", "session_id": "s1", "status": "cancelled", "resumable": True, "resumed_by": "t2"}
    assert "resumable" not in task_view(snapshot)
    row = task_row(snapshot, None)
    assert "resumable" not in row
    assert "resumed_by" not in row  # the continuation's resume_of carries the link


def _row(task_id: str, session_id: str, created_at: str, cwd: str) -> dict:
    return {"task_id": task_id, "session_id": session_id, "status": "completed", "created_at": created_at, "cwd": cwd}


def test_tasks_view_is_newest_first_limited_and_titled_once(tmp_path):
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir()
    there.mkdir()
    rows = [
        _row("t1", "s1", "2026-10-20T22:47:13.763186+00:00", str(here)),
        _row("t2", "s1", "2026-10-20T22:47:30.887288+00:00", str(here)),
        _row("t3", "s2", "2026-10-20T22:48:00.000001+00:00", str(there)),
    ]
    titles = {"s1": "smoke", "s2": "elsewhere"}

    view = tasks_view(rows, titles, limit=2)
    assert [row["task_id"] for row in view["tasks"]] == ["t3", "t2"]
    assert view["tasks_more"] == 1
    assert view["tasks"][1]["created_at"] == "2026-10-20T22:47:30+00:00"

    mine = tasks_view(rows, titles, limit=0, cwd=str(here))
    assert [row["task_id"] for row in mine["tasks"]] == ["t2", "t1"]
    assert mine["tasks"][0]["title"] == "smoke"
    assert "title" not in mine["tasks"][1]
    assert "tasks_more" not in mine

    assert tasks_view([], titles, limit=20) == {"tasks": []}
