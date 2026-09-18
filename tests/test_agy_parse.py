import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent_bridge.adapters.antigravity import (
    AgyAdapter,
    _BrainThoughtTail,
    _scoped_usage,
    agy_model_effort,
    check_agy_model_effort,
    classify_event,
    collect_tool_paths,
    conversation_id_of,
    is_agy_tool_schema_error,
    is_result_event,
    recovered_agy_tool_error,
    result_error_of,
    result_text_of,
    text_delta_of,
)
from agent_bridge.config import AgentConfig
from agent_bridge.models import Session, Task
from agent_bridge.transcript import read_events

FAKE_AGY = Path(__file__).resolve().parent / "fake_agy.py"

INIT = {
    "event": "init",
    "conversation_id": "c3b66b04-872b-4fbe-a3a4-058a026ef20a",
    "init": {"cwd": "/home/user/project", "tools": ["write_to_file"], "permission_mode": "always-proceed"},
}
STEP_TEXT = {
    "event": "step_update",
    "step_update": {
        "conversation_id": "c3b66b04-872b-4fbe-a3a4-058a026ef20a",
        "step_index": 3,
        "state": "DONE",
        "step_type": "agent_response",
        "text_delta": "Git rebase rewrites history.\n",
    },
}
RESULT = {
    "event": "result",
    "result": {
        "conversation_id": "c3b66b04-872b-4fbe-a3a4-058a026ef20a",
        "status": "SUCCESS",
        "response": "Git rebase rewrites history.\n",
        "usage": {"total_tokens": 11007},
    },
}
ERROR = {
    "event": "result",
    "result": {"conversation_id": "", "status": "ERROR", "response": "", "error": "authentication required"},
}


def test_init_is_not_a_result():
    assert conversation_id_of(INIT) == "c3b66b04-872b-4fbe-a3a4-058a026ef20a"
    assert is_result_event(INIT) is False
    assert result_text_of(INIT) == ""


def test_text_delta_and_result_envelope():
    assert text_delta_of(STEP_TEXT) == "Git rebase rewrites history.\n"
    assert is_result_event(RESULT) is True
    assert result_text_of(RESULT) == "Git rebase rewrites history.\n"
    assert result_error_of(RESULT) is None


def test_error_result():
    assert is_result_event(ERROR) is True
    assert result_error_of(ERROR) == "authentication required"


CODECONTENT_ERROR = (
    "declaring permissions: cortex tool write_to_file: convert tool call for "
    "permissions: model output error: invalid tool call error (invalid_signature) "
    "CodeContent is a required parameter. Please follow the function call schema exactly."
)


def test_tool_schema_error_is_not_a_hard_failure_when_the_turn_recovered():
    assert is_agy_tool_schema_error(CODECONTENT_ERROR) is True
    assert is_agy_tool_schema_error("authentication required") is False
    recovered = {
        "event": "result",
        "result": {
            "status": "ERROR",
            "response": "Implemented scoreboard and 26 tests passed.\n",
            "error": CODECONTENT_ERROR,
        },
    }
    assert recovered_agy_tool_error(recovered, recovered["result"]["response"], 0) is True
    assert recovered_agy_tool_error(recovered, "", 0) is False
    assert recovered_agy_tool_error(ERROR, "authentication required", 1) is False


def test_collect_tool_paths():
    files: set[str] = set()
    collect_tool_paths(
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_info": {"name": "write_to_file", "parameters": {"Path": "/tmp/a.txt"}},
            },
        },
        files,
    )
    assert files == {"/tmp/a.txt"}


def test_collect_tool_paths_skips_read_only_tools():
    files: set[str] = set()
    for name, key in (("view_file", "AbsolutePath"), ("list_dir", "Path"), ("grep_search", "Path")):
        collect_tool_paths(
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "tool_name": name,
                    "tool_info": {"name": name, "parameters": {key: "/tmp/read-only.txt"}},
                },
            },
            files,
        )
    assert files == set()
    collect_tool_paths(
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_info": {"name": "replace_file_content", "parameters": {"TargetFile": "/tmp/b.txt"}},
            },
        },
        files,
    )
    assert files == {"/tmp/b.txt"}


def test_agy_model_and_effort_precede_print(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.resolve_command",
        lambda command, fallbacks=None: ["agy"],
    )
    adapter = AgyAdapter(
        AgentConfig(name="antigravity", protocol="agy", command=["agy"]),
        tmp_path,
    )
    session = Session(
        session_id="sess_m",
        agent="antigravity",
        cwd=str(tmp_path),
        model="gemini-3.7-flash",
        effort="low",
    )
    task = Task(
        task_id="task_m",
        session_id=session.session_id,
        agent="antigravity",
        message="do it",
        cwd=str(tmp_path),
        model="gemini-3.7-flash",
        effort="low",
    )
    cmd = adapter._build_cmd(session, task)
    assert cmd.index("--model") < cmd.index("-p")
    assert cmd.index("--effort") < cmd.index("-p")
    assert cmd.index("--add-dir") < cmd.index("-p")
    assert cmd.index("--new-project") < cmd.index("-p")
    assert cmd[cmd.index("--model") + 1] == "gemini-3.7-flash"
    assert cmd[cmd.index("--effort") + 1] == "low"
    assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path)
    assert "--conversation" not in cmd
    assert "--input-format" in cmd
    assert cmd[cmd.index("-p") + 1] == ""
    assert task.message not in cmd


def test_agy_follow_up_uses_conversation_not_new_project(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.resolve_command",
        lambda command, fallbacks=None: ["agy"],
    )
    adapter = AgyAdapter(
        AgentConfig(name="antigravity", protocol="agy", command=["agy"]),
        tmp_path,
    )
    session = Session(
        session_id="sess_c",
        agent="antigravity",
        cwd=str(tmp_path),
        native_session_id="conv-1",
    )
    task = Task(
        task_id="task_c",
        session_id=session.session_id,
        agent="antigravity",
        message="again",
        cwd=str(tmp_path),
    )
    cmd = adapter._build_cmd(session, task)
    assert cmd.index("--conversation") < cmd.index("-p")
    assert cmd[cmd.index("--conversation") + 1] == "conv-1"
    assert "--new-project" not in cmd
    assert "--input-format" in cmd
    assert cmd[cmd.index("-p") + 1] == ""
    assert task.message not in cmd


def _agy_cmd(tmp_path, monkeypatch, model, effort):
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.resolve_command",
        lambda command, fallbacks=None: ["agy"],
    )
    adapter = AgyAdapter(
        AgentConfig(name="antigravity", protocol="agy", command=["agy"]),
        tmp_path,
    )
    session = Session(
        session_id="sess_eff",
        agent="antigravity",
        cwd=str(tmp_path),
        model=model,
        effort=effort,
    )
    task = Task(
        task_id="task_eff",
        session_id=session.session_id,
        agent="antigravity",
        message="do it",
        cwd=str(tmp_path),
        model=model,
        effort=effort,
    )
    return adapter._build_cmd(session, task)


def test_agy_effort_suffixed_slug_parsing():
    assert agy_model_effort("gemini-3.8-flash-medium") == "medium"
    assert agy_model_effort("gemini-3.8-flash-high") == "high"
    assert agy_model_effort("gemini-3.7-flash") is None
    assert agy_model_effort("medium") is None  # no hyphen, not a suffix
    assert agy_model_effort(None) is None
    # check_agy_model_effort compares after the Bridge->agy effort mapping.
    check_agy_model_effort("gemini-3.8-flash-medium", "medium")
    check_agy_model_effort("gemini-3.8-flash-high", "max")  # max -> high
    check_agy_model_effort("gemini-3.8-flash-medium", None)
    check_agy_model_effort("gemini-3.7-flash", "high")


def test_agy_mismatched_explicit_effort_fails_early(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="invalid model selection"):
        _agy_cmd(tmp_path, monkeypatch, "gemini-3.8-flash-medium", "high")
    with pytest.raises(ValueError, match="invalid model selection") as excinfo:
        check_agy_model_effort("gemini-3.8-flash-medium", "low")
    assert "-low" in str(excinfo.value) and "omit effort" in str(excinfo.value)


def test_agy_matching_effort_drops_the_redundant_flag(tmp_path, monkeypatch):
    cmd = _agy_cmd(tmp_path, monkeypatch, "gemini-3.8-flash-medium", "medium")
    assert cmd[cmd.index("--model") + 1] == "gemini-3.8-flash-medium"
    assert "--effort" not in cmd


def test_agy_effort_omitted_on_suffixed_slug_sends_model_only(tmp_path, monkeypatch):
    cmd = _agy_cmd(tmp_path, monkeypatch, "gemini-3.8-flash-medium", None)
    assert cmd[cmd.index("--model") + 1] == "gemini-3.8-flash-medium"
    assert "--effort" not in cmd


def test_agy_unsuffixed_slug_still_passes_effort(tmp_path, monkeypatch):
    cmd = _agy_cmd(tmp_path, monkeypatch, "gemini-3.7-flash", "high")
    assert cmd[cmd.index("--effort") + 1] == "high"


def _agy_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgyAdapter:
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.resolve_command",
        lambda command, fallbacks=None: [sys.executable, str(FAKE_AGY)],
    )
    return AgyAdapter(
        AgentConfig(name="antigravity", protocol="agy", command=["agy"]),
        tmp_path,
    )


@pytest.mark.asyncio
async def test_run_turn_sends_prompt_over_stdin(tmp_path, monkeypatch):
    adapter = _agy_adapter(tmp_path, monkeypatch)
    report = tmp_path / "len.txt"
    monkeypatch.setenv("FAKE_AGY_REPORT", str(report))
    session = Session(session_id="sess_stdin", agent="antigravity", cwd=str(tmp_path))
    task = Task(
        task_id="task_stdin",
        session_id=session.session_id,
        agent="antigravity",
        message="x" * 50000,
        cwd=str(tmp_path),
    )
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "end_turn"
    assert result.text.startswith("echo:")
    assert report.read_text(encoding="utf-8") == "50000"
    assert session.native_session_id == "conv-fake-agy"
    assert result.usage["scope"] == "turn"
    # scope="turn" counters are this run's total — exact, and persisted as
    # the conversation baseline for the next resumed turn's delta.
    assert result.run_usage["total"] == 1
    assert result.run_usage["quality"] == "exact"
    assert session.usage_baseline["cid"] == "conv-fake-agy"
    assert session.usage_baseline["counters"]["total"] == 1


@pytest.mark.asyncio
async def test_run_turn_labels_resumed_usage_as_conversation(tmp_path, monkeypatch):
    adapter = _agy_adapter(tmp_path, monkeypatch)
    session = Session(
        session_id="sess_resume",
        agent="antigravity",
        cwd=str(tmp_path),
        native_session_id="conv-1",
    )
    task = Task(
        task_id="task_resume",
        session_id=session.session_id,
        agent="antigravity",
        message="again",
        cwd=str(tmp_path),
    )
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "end_turn"
    assert result.usage["scope"] == "conversation"
    # Resumed with no baseline: the prior conversation's counters cannot be
    # attributed to this run — estimate, conversation total kept separate.
    assert result.run_usage["quality"] == "estimate"
    assert "total" not in result.run_usage
    assert result.run_usage["conversation_total"]["total"] == 1


@pytest.mark.asyncio
async def test_run_turn_resumed_with_baseline_reports_delta(tmp_path, monkeypatch):
    adapter = _agy_adapter(tmp_path, monkeypatch)
    session = Session(
        session_id="sess_resumed_base",
        agent="antigravity",
        cwd=str(tmp_path),
        native_session_id="conv-fake-agy",
        usage_baseline={"cid": "conv-fake-agy", "counters": {"total": 1}},
    )
    task = Task(
        task_id="task_resumed_base",
        session_id=session.session_id,
        agent="antigravity",
        message="again",
        cwd=str(tmp_path),
    )
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "end_turn"
    # Same cumulative snapshot -> zero run delta, exact quality.
    assert result.run_usage["quality"] == "exact"
    assert "total" not in result.run_usage
    assert result.run_usage["conversation_total"]["total"] == 1


def test_scoped_usage_leaves_empty_dict_alone():
    assert _scoped_usage({}, True) == {}


# --- brain-transcript thought tail --------------------------------------------

BRAIN_SESSION = "sess_bt"


def _brain_path(root: Path, cid: str) -> Path:
    return root / cid / ".system_generated" / "logs" / "transcript_full.jsonl"


def _append_brain(path: Path, *records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(rec if isinstance(rec, str) else json.dumps(rec))
            fh.write("\n")


def _tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _BrainThoughtTail:
    monkeypatch.setattr(
        "agent_bridge.adapters.antigravity.agy_brain_root", lambda: tmp_path / "brain"
    )
    return _BrainThoughtTail(BRAIN_SESSION, tmp_path)


def _thought_texts(home: Path, session_id: str = BRAIN_SESSION) -> list[str]:
    return [
        e["data"]["text"]
        for e in read_events(session_id, home)
        if e["type"] == "thought_chunk"
    ]


def test_classify_event_thought_shapes():
    step = {"step_type": "thought", "thinking": "hmm"}
    et, data = classify_event({"step_update": step}, step, "thought", "step_update", "", None)
    assert et == "thought_chunk" and data == {"text": "hmm"}

    obj = {"event": "reasoning", "reasoning": "top level"}
    et, data = classify_event(obj, {}, "", "reasoning", "", None)
    assert et == "thought_chunk" and data == {"text": "top level"}

    # Checkpoints without text stay raw.
    step = {"step_type": "checkpoint", "step_index": 4, "state": "DONE"}
    et, _ = classify_event({"step_update": step}, step, "checkpoint", "step_update", "", None)
    assert et == "raw"


def test_classify_event_thought_with_text_delta_is_not_answer_text():
    """A thought/reasoning record carrying text_delta must classify as a
    thought — the generic `or chunk` branch used to swallow it and leak the
    reasoning into the assistant's answer text."""
    step = {"step_type": "thought", "text_delta": "streamed reasoning"}
    et, data = classify_event(
        {"event": "step_update", "step_update": step},
        step,
        "thought",
        "step_update",
        "streamed reasoning",
        None,
    )
    assert et == "thought_chunk" and data == {"text": "streamed reasoning"}

    step = {"step_type": "reasoning", "text_delta": "delta thinking"}
    et, data = classify_event(
        {"step_update": step}, step, "reasoning", "step_update", "delta thinking", None
    )
    assert et == "thought_chunk" and data == {"text": "delta thinking"}

    obj = {"event": "thought", "text_delta": "top-level delta"}
    et, data = classify_event(obj, {}, "", "thought", "top-level delta", None)
    assert et == "thought_chunk" and data == {"text": "top-level delta"}


def test_brain_tail_new_conversation_reads_existing_and_appends(tmp_path, monkeypatch):
    tail = _tail(tmp_path, monkeypatch)
    path = _brain_path(tmp_path / "brain", "conv-new")
    _append_brain(
        path,
        {"type": "PLANNER_RESPONSE", "thinking": "thought one"},
        {"type": "PLANNER_RESPONSE", "thinking": "thought two"},
    )
    # Bound after the file already has content: a fresh conversation owns it.
    tail.bind("conv-new", resumed=False)
    tail.drain()
    assert _thought_texts(tmp_path) == ["thought one", "thought two"]

    _append_brain(path, {"type": "PLANNER_RESPONSE", "thinking": "thought three"})
    tail.drain()
    assert _thought_texts(tmp_path) == ["thought one", "thought two", "thought three"]

    tail.drain()  # no new bytes -> nothing re-emitted
    assert _thought_texts(tmp_path) == ["thought one", "thought two", "thought three"]


def test_brain_tail_resumed_starts_at_pre_turn_boundary(tmp_path, monkeypatch):
    tail = _tail(tmp_path, monkeypatch)
    path = _brain_path(tmp_path / "brain", "conv-old")
    _append_brain(path, {"type": "PLANNER_RESPONSE", "thinking": "prior turn thought"})
    tail.bind("conv-old", resumed=True)
    tail.drain()
    assert _thought_texts(tmp_path) == []

    _append_brain(path, {"type": "PLANNER_RESPONSE", "thinking": "this turn thought"})
    tail.drain()
    assert _thought_texts(tmp_path) == ["this turn thought"]


def test_brain_tail_resumed_missing_log_snaps_first_available_eof(tmp_path, monkeypatch):
    """A resumed bind that finds no readable log must not replay from byte 0
    when the file appears later. The boundary is deferred: the first drain
    that can stat the file snaps to its EOF — records already on disk are
    skipped, only subsequent appends belong to this turn."""
    tail = _tail(tmp_path, monkeypatch)
    tail.bind("conv-missing", resumed=True)  # brain dir never created
    tail.drain()
    assert _thought_texts(tmp_path) == []

    # The log appears already populated (archived history, delayed mount):
    # none of it is this turn's thinking.
    path = _brain_path(tmp_path / "brain", "conv-missing")
    _append_brain(path, {"type": "PLANNER_RESPONSE", "thinking": "prior turn thought"})
    tail.drain()
    assert _thought_texts(tmp_path) == []

    tail.bind("conv-other", resumed=False)  # second bind is ignored
    _append_brain(path, {"type": "PLANNER_RESPONSE", "thinking": "appeared mid-turn"})
    tail.drain()
    assert _thought_texts(tmp_path) == ["appeared mid-turn"]


def test_brain_tail_skips_malformed_and_non_thinking_records(tmp_path, monkeypatch):
    tail = _tail(tmp_path, monkeypatch)
    path = _brain_path(tmp_path / "brain", "conv-m")
    _append_brain(
        path,
        "not json at all",
        "[1, 2, 3]",
        '"just a string"',
        {"type": "TOOL_CALL", "thinking": "wrong record type"},
        {"type": "PLANNER_RESPONSE"},
        {"type": "PLANNER_RESPONSE", "thinking": 42},
        {"type": "PLANNER_RESPONSE", "thinking": ""},
        {"type": "PLANNER_RESPONSE", "thinking": "   "},
        {"type": "PLANNER_RESPONSE", "thinking": "real", "tool_calls": [{"name": "x"}]},
    )
    tail.bind("conv-m", resumed=False)
    tail.drain()
    assert _thought_texts(tmp_path) == ["real"]


def test_brain_tail_partial_line_waits_for_completion(tmp_path, monkeypatch):
    tail = _tail(tmp_path, monkeypatch)
    path = _brain_path(tmp_path / "brain", "conv-p")
    rec = json.dumps({"type": "PLANNER_RESPONSE", "thinking": "completed later"})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rec[: len(rec) // 2], encoding="utf-8")  # half a line, no \n
    tail.bind("conv-p", resumed=False)
    tail.drain()
    assert _thought_texts(tmp_path) == []

    with path.open("a", encoding="utf-8") as fh:
        fh.write(rec[len(rec) // 2 :] + "\n")
    tail.drain()
    assert _thought_texts(tmp_path) == ["completed later"]


def test_brain_tail_dedup_is_source_and_count_aware(tmp_path, monkeypatch):
    """Offset progression already gives exactly-once within the log, so text
    dedup must only pair occurrences ACROSS sources: two distinct brain
    records with identical text both survive, while one logical thought
    seen on stdout and in the log emits once."""
    tail = _tail(tmp_path, monkeypatch)
    path = _brain_path(tmp_path / "brain", "conv-d")
    _append_brain(
        path,
        {"type": "PLANNER_RESPONSE", "thinking": "same"},
        {"type": "PLANNER_RESPONSE", "thinking": "same"},
    )
    tail.bind("conv-d", resumed=False)
    tail.drain()
    assert _thought_texts(tmp_path) == ["same", "same"]

    # stdout-first pairing: the stdout occurrence stands, the brain twin
    # arriving later is the duplicate.
    assert tail.note_stdout("from stdout") is True
    _append_brain(path, {"type": "PLANNER_RESPONSE", "thinking": "from stdout"})
    tail.drain()
    assert _thought_texts(tmp_path) == ["same", "same"]

    # brain-first pairing: a stdout report of an already-emitted brain text
    # is suppressed the other direction.
    _append_brain(path, {"type": "PLANNER_RESPONSE", "thinking": "brain first"})
    tail.drain()
    assert _thought_texts(tmp_path) == ["same", "same", "brain first"]
    assert tail.note_stdout("brain first") is False

    # A further distinct brain record repeating that text still emits —
    # counts, not membership, decide duplication.
    _append_brain(path, {"type": "PLANNER_RESPONSE", "thinking": "brain first"})
    tail.drain()
    assert _thought_texts(tmp_path) == ["same", "same", "brain first", "brain first"]


def test_brain_tail_never_writes_under_brain_root(tmp_path, monkeypatch):
    tail = _tail(tmp_path, monkeypatch)
    path = _brain_path(tmp_path / "brain", "conv-ro")
    _append_brain(path, {"type": "PLANNER_RESPONSE", "thinking": "read me"})
    before = path.read_bytes()
    tail.bind("conv-ro", resumed=False)
    tail.drain()
    tail.drain()
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_brain_watcher_drains_until_cancelled(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    monkeypatch.setattr("agent_bridge.adapters.antigravity.agy_brain_root", lambda: brain)
    monkeypatch.setattr("agent_bridge.adapters.antigravity.BRAIN_POLL_SEC", 0.02)
    adapter = AgyAdapter(
        AgentConfig(name="antigravity", protocol="agy", command=["agy"]),
        tmp_path,
    )
    tail = _BrainThoughtTail("sess_bw", tmp_path)
    tail.bind("conv-w", resumed=False)
    watcher = asyncio.create_task(adapter._watch_brain(tail))
    try:
        _append_brain(
            _brain_path(brain, "conv-w"),
            {"type": "PLANNER_RESPONSE", "thinking": "live thought"},
        )
        for _ in range(100):
            await asyncio.sleep(0.02)
            if _thought_texts(tmp_path, "sess_bw"):
                break
        assert _thought_texts(tmp_path, "sess_bw") == ["live thought"]
    finally:
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher


@pytest.mark.asyncio
async def test_run_turn_early_exit_reports_stderr(tmp_path, monkeypatch):
    adapter = _agy_adapter(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_AGY_MODE", "early_exit")
    session = Session(session_id="sess_early", agent="antigravity", cwd=str(tmp_path))
    task = Task(
        task_id="task_early",
        session_id=session.session_id,
        agent="antigravity",
        message="hello",
        cwd=str(tmp_path),
    )
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "error"
    assert result.error is not None
    assert "agy exit 3" in result.error
    assert "fake agy refused to start" in result.error


@pytest.mark.asyncio
async def test_run_turn_capacity_503_keeps_partial_result_and_session(tmp_path, monkeypatch):
    """The real failure shape: mid-stream 503 with an absorbed error_message
    step, a partial response, and a live conversation id — all resumable."""
    adapter = _agy_adapter(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_AGY_MODE", "capacity_503")
    session = Session(session_id="sess_503", agent="antigravity", cwd=str(tmp_path))
    task = Task(
        task_id="task_503",
        session_id=session.session_id,
        agent="antigravity",
        message="hello",
        cwd=str(tmp_path),
        model="gemini-3.8-flash-medium",
    )
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == "error"
    assert result.error is not None
    assert "No capacity available for model gemini-3.8-flash-medium" in result.error
    # The partial response survives and the native conversation id is set, so
    # resume_task can continue the same conversation.
    assert result.text == "echo:hello...partial report"
    assert result.native_session_id == "conv-fake-agy"
    assert session.native_session_id == "conv-fake-agy"
    # The opaque error_message step agy absorbed is counted, not invisible.
    assert any("1 mid-turn error step" in w for w in result.warnings)
