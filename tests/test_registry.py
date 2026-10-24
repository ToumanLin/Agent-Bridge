from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import os
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_bridge.adapters.fake import FakeAdapter
from agent_bridge.models import (
    TERMINAL_STATUSES,
    ProcState,
    Session,
    Task,
    TaskStatus,
    TurnResult,
    iso,
)
from agent_bridge.paths import result_path, state_path, transcript_path
from agent_bridge.persist import atomic_write_json, read_json, read_json_strict
from agent_bridge.registry import (
    NESTED_CANCEL_ERROR,
    NESTED_DISPATCH_ERROR,
    NESTED_END_SESSION_ERROR,
    NESTED_PREFERENCES_ERROR,
    Registry,
)
from agent_bridge.transcript import append_event, read_events


@pytest.mark.asyncio
async def test_dispatch_records_model_and_effort(bridge_home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task(
            "fake",
            "tiny",
            cwd=str(work.resolve()),
            model="gemini-3.7-flash",
            effort="LOW",
        )
        assert dispatched["model"] == "gemini-3.7-flash"
        assert dispatched["effort"] == "low"
        session = registry.sessions[dispatched["session_id"]]
        assert session.model == "gemini-3.7-flash"
        assert session.effort == "low"
        with pytest.raises(ValueError, match="effort"):
            await registry.dispatch_task("fake", "bad", cwd=str(work.resolve()), effort="turbo")
    finally:
        await registry.stop()


def test_env_status_includes_config_warnings(bridge_home):
    registry = Registry.create(bridge_home)
    registry.config.warnings = ["unsupported agents.toml section(s) ignored: scheduler"]
    assert registry.env_status()["warnings"] == registry.config.warnings


@pytest.mark.asyncio
async def test_dispatch_wait_fake(bridge_home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "build foo", cwd=str(work.resolve()), title="demo")
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        assert "build foo" in waited["result_text"]
        sessions = registry.list_sessions()
        assert sessions[0]["title"] == "demo"
        assert registry.sessions[dispatched["session_id"]].proc_state == ProcState.ready
        transcript = registry.get_transcript(dispatched["session_id"])
        assert transcript["count"] >= 1
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_busy_session_rejected(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "2")
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        first = await registry.dispatch_task("fake", "slow", cwd=str(work.resolve()))
        with pytest.raises(RuntimeError, match="busy"):
            await registry.dispatch_task(
                "fake",
                "again",
                cwd=str(work.resolve()),
                session_id=first["session_id"],
            )
        await registry.cancel_task(first["task_id"])
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_followup_cwd_must_match(bridge_home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        first = await registry.dispatch_task("fake", "one", cwd=str(work.resolve()))
        await registry.wait_task(first["task_id"], timeout_sec=5)
        with pytest.raises(ValueError, match="same project folder"):
            await registry.dispatch_task(
                "fake",
                "two",
                cwd=str(other.resolve()),
                session_id=first["session_id"],
            )
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_cursor_followup_can_change_model(bridge_home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    bridge_home.mkdir(parents=True, exist_ok=True)
    (bridge_home / "agents.toml").write_text(
        '[agents.cursor]\nprotocol = "fake"\n', encoding="utf-8"
    )
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        first = await registry.dispatch_task(
            "cursor", "one", cwd=str(work.resolve()), model="cursor-model-a"
        )
        await registry.wait_task(first["task_id"], timeout_sec=5)
        switched = await registry.dispatch_task(
            "cursor",
            "two",
            cwd=str(work.resolve()),
            session_id=first["session_id"],
            model="cursor-model-b",
        )
        result = await registry.wait_task(switched["task_id"], timeout_sec=5)
        assert result["status"] == "completed"
        assert registry.sessions[first["session_id"]].model == "cursor-model-b"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_cursor_can_correct_model_before_acp_session_starts(bridge_home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    bridge_home.mkdir(parents=True, exist_ok=True)
    cursor_agent = Path(__file__).resolve().with_name("cursor_agent.py")
    command = [sys.executable, str(cursor_agent), "acp"]
    encoded = ", ".join(json.dumps(part) for part in command)
    (bridge_home / "agents.toml").write_text(
        "[agents.cursor]\n"
        'protocol = "acp"\n'
        f"command = [{encoded}]\n",
        encoding="utf-8",
    )
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        first = await registry.dispatch_task(
            "cursor", "one", cwd=str(work.resolve()), model="made-up-model"
        )
        waited = await registry.wait_task(first["task_id"], timeout_sec=15)
        assert waited["status"] == "failed"
        assert waited["error"] is not None
        assert "made-up-model" in waited["error"]
        session = registry.sessions[first["session_id"]]
        assert session.native_session_id is None
        assert session.pid is None

        second = await registry.dispatch_task(
            "cursor",
            "two",
            cwd=str(work.resolve()),
            session_id=first["session_id"],
            model="cursor-model-a",
        )
        waited = await registry.wait_task(second["task_id"], timeout_sec=15)
        assert waited["timed_out"] is False
        assert waited["status"] == "completed"
        session = registry.sessions[first["session_id"]]
        assert session.model == "cursor-model-a"
        assert session.native_session_id is not None
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_relative_cwd_rejected(bridge_home):
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        with pytest.raises(ValueError, match="absolute"):
            await registry.dispatch_task("fake", "x", cwd="relative")
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_dispatch_rejects_missing_cwd_and_file_path(bridge_home, tmp_path):
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        with pytest.raises(ValueError, match="does not exist"):
            await registry.dispatch_task("fake", "x", cwd=str(tmp_path / "missing"))
        file_path = tmp_path / "file.txt"
        file_path.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="directory"):
            await registry.dispatch_task("fake", "x", cwd=str(file_path))
        assert registry.sessions == {}
        assert registry.tasks == {}
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_bridge_restart_marks_running_failed(bridge_home):
    atomic_write_json(
        state_path(bridge_home),
        {
            "sessions": [
                Session(
                    session_id="sess_old",
                    agent="fake",
                    cwd=str(Path.cwd()),
                    proc_state=ProcState.busy,
                ).model_dump(mode="json")
            ],
            "tasks": [
                Task(
                    task_id="task_old",
                    session_id="sess_old",
                    agent="fake",
                    message="in flight",
                    cwd=str(Path.cwd()),
                    status=TaskStatus.running,
                ).model_dump(mode="json")
            ],
        },
    )
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        assert registry.tasks["task_old"].status == TaskStatus.failed
        assert registry.tasks["task_old"].error == "bridge_restarted"
        assert registry.sessions["sess_old"].proc_state == ProcState.idle_unloaded
        assert registry.sessions["sess_old"].owner_pid == registry._owner_pid
        assert registry.sessions["sess_old"].owner_create_time == registry._owner_create_time
        assert registry.tasks["task_old"].owner_pid == registry._owner_pid
        assert registry.tasks["task_old"].owner_create_time == registry._owner_create_time
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_sibling_instances_do_not_clobber_state(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setattr("agent_bridge.registry.owner_alive", lambda pid, create_time: True)
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    a = Registry.create(bridge_home, owner_pid=1001, owner_create_time=11.0)
    b = Registry.create(bridge_home, owner_pid=2002, owner_create_time=22.0)
    await a.start()
    try:
        dispatched = await a.dispatch_task("fake", "tiny", cwd=cwd)
        waited = await a.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        sess_a = dispatched["session_id"]
        task_a = dispatched["task_id"]
        assert a.sessions[sess_a].owner_pid == 1001
        assert a.tasks[task_a].owner_pid == 1001

        await b.start()
        try:
            assert sess_a not in b.sessions
            assert task_a not in b.tasks
            b.save()
            await b.flush_state()
            payload = read_json(state_path(bridge_home), {})
            assert any(row["session_id"] == sess_a for row in payload["sessions"])
            assert any(row["task_id"] == task_a for row in payload["tasks"])

            other = await b.dispatch_task("fake", "other", cwd=cwd)
            await b.flush_state()
            payload = read_json(state_path(bridge_home), {})
            session_ids = {row["session_id"] for row in payload["sessions"]}
            assert {sess_a, other["session_id"]} <= session_ids
            assert any(row["task_id"] == task_a for row in payload["tasks"])

            a.save()
            await a.flush_state()
            payload = read_json(state_path(bridge_home), {})
            session_ids = {row["session_id"] for row in payload["sessions"]}
            assert {sess_a, other["session_id"]} <= session_ids
            assert any(row["task_id"] == task_a for row in payload["tasks"])
            assert any(row["task_id"] == other["task_id"] for row in payload["tasks"])
        finally:
            await b.stop()
    finally:
        await a.stop()


@pytest.mark.asyncio
async def test_start_sweeps_orphan_result_files(bridge_home, monkeypatch):
    """A crash between the result write and the final save() leaves a file
    no task row references (A1). Files of live sibling tasks are kept."""
    monkeypatch.setattr("agent_bridge.registry.owner_alive", lambda pid, create_time: True)
    foreign = Task(
        task_id="task_foreign",
        session_id="sess_foreign",
        agent="fake",
        message="x",
        cwd=str(Path.cwd()),
        status=TaskStatus.completed,
        finished_at=iso(),
        owner_pid=9999,
        owner_create_time=1.0,
    )
    atomic_write_json(
        state_path(bridge_home),
        {"sessions": [], "tasks": [foreign.model_dump(mode="json")]},
    )
    results = bridge_home / "results"
    results.mkdir(parents=True, exist_ok=True)
    stale = results / "task_orphan.txt"
    stale.write_text("orphan", encoding="utf-8")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    # A fresh orphan may belong to a sibling that wrote the artifact before
    # its task row reached disk — the sweep must leave it alone.
    (results / "task_orphan_fresh.txt").write_text("recent", encoding="utf-8")
    (results / "task_foreign.txt").write_text("kept", encoding="utf-8")

    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        assert not stale.exists()
        assert (results / "task_orphan_fresh.txt").read_text(encoding="utf-8") == "recent"
        assert (results / "task_foreign.txt").read_text(encoding="utf-8") == "kept"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_check_task_reads_sibling_task_from_state(bridge_home, monkeypatch):
    """F5: a second instance answers read calls from the persisted row.
    Lifecycle calls (wait/cancel) stay owner-only."""
    monkeypatch.setattr("agent_bridge.registry.owner_alive", lambda pid, create_time: True)
    foreign = Task(
        task_id="task_remote",
        session_id="sess_remote",
        agent="fake",
        message="done elsewhere",
        cwd=str(Path.cwd()),
        status=TaskStatus.completed,
        result_text="remote result",
        finished_at=iso(),
        owner_pid=9999,
        owner_create_time=1.0,
    )
    atomic_write_json(
        state_path(bridge_home),
        {"sessions": [], "tasks": [foreign.model_dump(mode="json")]},
    )
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        assert "task_remote" not in registry.tasks
        snap = await registry.check_task("task_remote")
        assert snap["status"] == "completed"
        assert snap["from_state"] is True
        result = await registry.get_result("task_remote")
        assert result["result_text"] == "remote result"
        # Fork: with server.remote_tasks on, wait_task follows a live sibling's
        # row read-only (it never runs or cancels it); the in-memory lookup
        # that lifecycle calls use still does not know the task.
        waited = await registry.wait_task("task_remote", timeout_sec=0.1)
        assert waited["status"] == "completed"
        assert waited["remote"] is True
        with pytest.raises(KeyError, match="unknown task"):
            registry._require_task("task_remote")
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_check_task_from_state_reports_null_silence(bridge_home, monkeypatch):
    """A sibling's running task read from disk has no live silence data."""
    monkeypatch.setattr("agent_bridge.registry.owner_alive", lambda pid, create_time: True)
    foreign = Task(
        task_id="task_remote_run",
        session_id="sess_remote_run",
        agent="fake",
        message="running elsewhere",
        cwd=str(Path.cwd()),
        status=TaskStatus.running,
        started_at=iso(),
        owner_pid=9999,
        owner_create_time=1.0,
    )
    atomic_write_json(
        state_path(bridge_home),
        {"sessions": [], "tasks": [foreign.model_dump(mode="json")]},
    )
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        snap = await registry.check_task("task_remote_run")
        assert snap["from_state"] is True
        assert snap["silent_for_sec"] is None
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_result_survives_restart_when_artifact_write_fails(
    bridge_home, tmp_path, monkeypatch
):
    """With the result file unwritable, the state.json row is the only copy;
    it must carry the whole RESULT_STORE_MAX text, not a 6000-char tail."""
    import agent_bridge.registry as registry_mod

    original = registry_mod.atomic_write_text

    def fail_result_write(path, text, *args, **kwargs):
        if Path(path).parent.name == "results":
            raise OSError("disk full")
        return original(path, text, *args, **kwargs)

    monkeypatch.setattr(registry_mod, "atomic_write_text", fail_result_write)
    big = "z" * 20000

    async def big_turn(self, session, task):
        return TurnResult(text=big)

    monkeypatch.setattr(FakeAdapter, "run_turn", big_turn)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "big", cwd=str(tmp_path))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
    finally:
        await registry.stop()

    again = Registry.create(bridge_home)
    await again.start()
    try:
        result = await again.get_result(dispatched["task_id"])
        assert result["result_text"] == big
        assert len(result["result_text"]) == 20000
    finally:
        await again.stop()


def test_sibling_state_updates_are_serialized(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setattr("agent_bridge.registry.owner_alive", lambda pid, create_time: True)
    original_read = read_json_strict
    first_read = threading.Event()
    release_first = threading.Event()
    reads = 0
    reads_lock = threading.Lock()

    def blocked_read(*args, **kwargs):
        nonlocal reads
        with reads_lock:
            reads += 1
            current = reads
        if current == 1:
            first_read.set()
            assert release_first.wait(timeout=5)
        return original_read(*args, **kwargs)

    monkeypatch.setattr("agent_bridge.registry.read_json_strict", blocked_read)
    a = Registry.create(bridge_home, owner_pid=1001, owner_create_time=11.0)
    b = Registry.create(bridge_home, owner_pid=2002, owner_create_time=22.0)
    a.sessions["sess_a"] = Session(session_id="sess_a", agent="fake", cwd=str(tmp_path))
    b.sessions["sess_b"] = Session(session_id="sess_b", agent="fake", cwd=str(tmp_path))
    a._stamp_owner(a.sessions["sess_a"])
    b._stamp_owner(b.sessions["sess_b"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(a._write_state, a._own_rows())
        assert first_read.wait(timeout=5)
        second = pool.submit(b._write_state, b._own_rows())
        time.sleep(0.1)
        assert reads == 1
        release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)

    payload = read_json(state_path(bridge_home), {})
    assert {row["session_id"] for row in payload["sessions"]} == {"sess_a", "sess_b"}


@pytest.mark.asyncio
async def test_failed_state_flush_keeps_snapshot_and_reports_failure(
    bridge_home, tmp_path, monkeypatch
):
    registry = Registry.create(bridge_home)
    registry.sessions["sess_retry"] = Session(
        session_id="sess_retry",
        agent="fake",
        cwd=str(tmp_path),
    )
    original_write = registry._write_state
    attempts = 0

    def fail_once(own):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(5, "synthetic sharing/access failure")
        original_write(own)

    monkeypatch.setattr(registry, "_write_state", fail_once)
    registry.save()

    # flush_state degrades instead of raising: the failed write is retried
    # by the same call (STATE_FLUSH_ATTEMPTS) and the snapshot lands.
    await registry.flush_state()
    assert registry._pending_state is None
    payload = read_json(state_path(bridge_home), {})
    assert {row["session_id"] for row in payload["sessions"]} == {"sess_retry"}


@pytest.mark.asyncio
async def test_persistent_state_write_failure_degrades_flush_without_raising(
    bridge_home, tmp_path, monkeypatch
):
    registry = Registry.create(bridge_home)
    registry.sessions["sess_dead_disk"] = Session(
        session_id="sess_dead_disk",
        agent="fake",
        cwd=str(tmp_path),
    )
    original_write = registry._write_state

    def fail_always(own):
        raise PermissionError(5, "synthetic sharing/access failure")

    monkeypatch.setattr(registry, "_write_state", fail_always)
    registry.save()

    # Never raises: a broken disk must not kill the caller. The newest
    # snapshot is kept for a later retry and surfaced via _state_error.
    await registry.flush_state()
    assert registry._state_error is not None
    assert registry._pending_state is not None

    monkeypatch.setattr(registry, "_write_state", original_write)
    await registry.flush_state()
    assert registry._pending_state is None
    assert registry._state_error is None
    payload = read_json(state_path(bridge_home), {})
    assert {row["session_id"] for row in payload["sessions"]} == {"sess_dead_disk"}


@pytest.mark.asyncio
async def test_flush_state_ignores_saves_issued_during_the_wait(
    bridge_home, tmp_path, monkeypatch
):
    """A save() landing mid-flush must not trip a spurious persist error."""
    import threading

    registry = Registry.create(bridge_home)
    registry.sessions["sess_race"] = Session(
        session_id="sess_race",
        agent="fake",
        cwd=str(tmp_path),
    )
    original_write = registry._write_state
    started = threading.Event()
    gate = threading.Event()

    def slow_write(own):
        started.set()
        gate.wait(10)
        original_write(own)

    monkeypatch.setattr(registry, "_write_state", slow_write)
    registry.save()
    flush = asyncio.create_task(registry.flush_state())
    await asyncio.to_thread(started.wait, 10)  # first write in-flight, blocked
    registry.save()  # lands while flush_state is awaiting the first write
    gate.set()
    await asyncio.wait_for(flush, timeout=10)  # must not raise
    assert registry._flushed_gen >= 1


@pytest.mark.asyncio
async def test_wait_task_persisted_ignores_saves_issued_during_the_wait(
    bridge_home, tmp_path, monkeypatch
):
    """persisted answers whether this task's save landed; a save() issued by
    another task while the flush runs must not false-negative it."""
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "tiny", cwd=str(work.resolve()))
        original_flush = registry.flush_state

        async def flush_then_save():
            done = await original_flush()
            registry.save()  # a newer save is in flight when persisted is read
            return done

        monkeypatch.setattr(registry, "flush_state", flush_then_save)
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        assert waited["persisted"] is True
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_wait_task_reports_unpersisted_when_writes_fail(
    bridge_home, tmp_path, monkeypatch
):
    """A broken disk degrades wait_task to persisted=False instead of raising."""
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    original_write = registry._write_state
    try:
        dispatched = await registry.dispatch_task("fake", "tiny", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["persisted"] is True

        def fail_always(own):
            raise PermissionError(5, "synthetic sharing/access failure")

        monkeypatch.setattr(registry, "_write_state", fail_always)
        registry.save()
        degraded = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert degraded["status"] == "completed"
        assert degraded["persisted"] is False
    finally:
        monkeypatch.setattr(registry, "_write_state", original_write)
        await registry.stop()


@pytest.mark.asyncio
async def test_start_raises_when_state_never_persists(bridge_home, monkeypatch):
    """start() cannot adopt/merge on stale disk, so it still fails fast."""
    registry = Registry.create(bridge_home)

    def fail_always(own):
        raise PermissionError(5, "synthetic sharing/access failure")

    monkeypatch.setattr(registry, "_write_state", fail_always)
    with pytest.raises(RuntimeError, match=r"could not persist state\.json"):
        await registry.start()


@pytest.mark.asyncio
async def test_check_task_terminal_waits_for_slow_state_write(
    bridge_home, tmp_path, monkeypatch
):
    """check_task must not answer terminal before the terminal row is on disk."""
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    original_write = registry._write_state
    try:

        def slow_write(own):
            time.sleep(0.3)
            original_write(own)

        monkeypatch.setattr(registry, "_write_state", slow_write)
        dispatched = await registry.dispatch_task("fake", "tiny", cwd=str(work.resolve()))
        terminal = {status.value for status in TERMINAL_STATUSES}
        checked = {}
        for _ in range(200):
            checked = await registry.check_task(dispatched["task_id"])
            if checked["status"] in terminal:
                break
            await asyncio.sleep(0.05)
        assert checked["status"] == "completed"
        assert checked["persisted"] is True
        rows = read_json(state_path(bridge_home), {}).get("tasks", [])
        row = next(r for r in rows if r["task_id"] == dispatched["task_id"])
        assert row["status"] == "completed"
    finally:
        monkeypatch.setattr(registry, "_write_state", original_write)
        await registry.stop()


@pytest.mark.asyncio
async def test_dispatch_returns_after_task_row_and_request_binding_on_disk(
    bridge_home, tmp_path, monkeypatch
):
    """A dispatch answer implies the task row and request_id binding landed;
    a coordinator crash before that would otherwise replay a duplicate."""
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    original_write = registry._write_state
    try:

        def slow_write(own):
            time.sleep(0.3)
            original_write(own)

        monkeypatch.setattr(registry, "_write_state", slow_write)
        request_id = str(uuid.uuid4())
        dispatched = await registry.dispatch_task(
            "fake", "tiny", cwd=str(work.resolve()), request_id=request_id
        )
        assert dispatched["persisted"] is True
        payload = read_json(state_path(bridge_home), {})
        assert {r["task_id"] for r in payload.get("tasks", [])} == {dispatched["task_id"]}
        bindings = {r["request_id"]: r for r in payload.get("requests", [])}
        assert bindings[request_id]["task_id"] == dispatched["task_id"]
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=10)
        assert waited["persisted"] is True
    finally:
        monkeypatch.setattr(registry, "_write_state", original_write)
        await registry.stop()


@pytest.mark.asyncio
async def test_check_task_reports_unpersisted_when_writes_fail(
    bridge_home, tmp_path, monkeypatch
):
    """A broken disk degrades check_task to persisted=False; the in-memory
    result stays readable."""
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    original_write = registry._write_state
    try:

        def fail_always(own):
            raise PermissionError(5, "synthetic sharing/access failure")

        monkeypatch.setattr(registry, "_write_state", fail_always)
        dispatched = await registry.dispatch_task("fake", "tiny", cwd=str(work.resolve()))
        assert dispatched["persisted"] is False
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=10)
        assert waited["status"] == "completed"
        checked = await registry.check_task(dispatched["task_id"])
        assert checked["status"] == "completed"
        assert checked["persisted"] is False
        result = await registry.get_result(dispatched["task_id"])
        assert result["persisted"] is False
        assert result["result_text"]
    finally:
        monkeypatch.setattr(registry, "_write_state", original_write)
        await registry.stop()


@pytest.mark.asyncio
async def test_cancel_task_reports_persisted_on_terminal(bridge_home, tmp_path):
    """cancel_task on an already-terminal task returns persisted=True."""
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "tiny", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        cancelled = await registry.cancel_task(dispatched["task_id"])
        assert cancelled["status"] == "completed"
        assert cancelled["persisted"] is True
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_check_task_persists_terminal_row_during_finalize(
    bridge_home, tmp_path, monkeypatch
):
    """task.status flips terminal before _run_task's finally issues the final
    save (files_changed collection sits in between). A check_task inside that
    window must land the terminal row itself instead of answering
    persisted=True for a disk row that still says running."""
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        from agent_bridge import workspace

        real_merge = workspace.merge_files_changed

        def slow_merge(*args, **kwargs):
            time.sleep(1.0)
            return real_merge(*args, **kwargs)

        # The failure path reaches the pending-files_changed merge in finally
        # *after* status is already terminal — a deterministic widen window.
        async def boom(_self, _session, _task):
            raise RuntimeError("synthetic turn failure")

        monkeypatch.setattr("agent_bridge.registry.merge_files_changed", slow_merge)
        monkeypatch.setattr(FakeAdapter, "run_turn", boom)
        dispatched = await registry.dispatch_task("fake", "tiny", cwd=str(work.resolve()))
        terminal = {status.value for status in TERMINAL_STATUSES}
        checked = {}
        for _ in range(200):
            checked = await registry.check_task(dispatched["task_id"])
            if checked["status"] in terminal:
                break
            await asyncio.sleep(0.05)
        assert checked["status"] == "failed"
        assert checked["persisted"] is True
        rows = read_json(state_path(bridge_home), {}).get("tasks", [])
        row = next(r for r in rows if r["task_id"] == dispatched["task_id"])
        assert row["status"] == "failed"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_dispatch_cancelled_during_flush_still_runs_task(
    bridge_home, tmp_path, monkeypatch
):
    """If the MCP dispatch call is cancelled while awaiting the initial
    flush, the already-registered task must still start — otherwise a
    request_id retry would reuse a row that never runs."""
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        request_id = str(uuid.uuid4())
        real_flush = registry.flush_state
        gate = asyncio.Event()

        async def hung_flush():
            await gate.wait()
            return await real_flush()

        monkeypatch.setattr(registry, "flush_state", hung_flush)
        dispatching = asyncio.create_task(
            registry.dispatch_task(
                "fake", "tiny", cwd=str(work.resolve()), request_id=request_id
            )
        )
        await asyncio.sleep(0.05)  # let dispatch register the task and reach the flush
        dispatching.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dispatching
        monkeypatch.setattr(registry, "flush_state", real_flush)

        (task_id,) = [tid for tid in registry.tasks if registry.tasks[tid].agent == "fake"]
        waited = await registry.wait_task(task_id, timeout_sec=10)
        assert waited["status"] == "completed"
        retry = await registry.dispatch_task(
            "fake", "tiny", cwd=str(work.resolve()), request_id=request_id
        )
        assert retry["reused"] is True
        assert retry["task_id"] == task_id
    finally:
        await registry.stop()


def test_state_write_retries_bounded_permission_errors(bridge_home, monkeypatch):
    registry = Registry.create(bridge_home)
    original_write = atomic_write_json
    attempts = 0

    def fail_twice(path, payload):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(5, "synthetic sharing/access failure")
        original_write(path, payload)

    monkeypatch.setattr("agent_bridge.registry.atomic_write_json", fail_twice)
    monkeypatch.setattr("agent_bridge.registry.STATE_WRITE_RETRY_BASE_SEC", 0)
    registry._write_state(registry._own_rows())

    assert attempts == 3


def test_state_write_does_not_replace_invalid_json(bridge_home):
    path = state_path(bridge_home)
    path.write_text("{not valid json", encoding="utf-8")
    registry = Registry.create(bridge_home)

    with pytest.raises(json.JSONDecodeError):
        registry._write_state(registry._own_rows())

    assert path.read_text(encoding="utf-8") == "{not valid json"


@pytest.mark.asyncio
async def test_start_quarantines_corrupt_state(bridge_home, tmp_path):
    """A corrupt state.json must not lock every later startup out of working.

    _write_state keeps refusing to overwrite it, so start() has to move it aside
    or flush_state() would fail forever and the bridge could never boot again.
    """
    path = state_path(bridge_home)
    path.write_text("{not valid json", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)

    await registry.start()
    try:
        quarantined = sorted(bridge_home.glob("state.json.corrupt-*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text(encoding="utf-8") == "{not valid json"
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
    finally:
        await registry.stop()

    # The rewritten file is valid, so a second boot must not fail either.
    second = Registry.create(bridge_home)
    await second.start()
    await second.stop()
    assert len(sorted(bridge_home.glob("state.json.corrupt-*"))) == 1


@pytest.mark.asyncio
async def test_two_corruptions_in_one_second_keep_both_copies(bridge_home, monkeypatch):
    """A second quarantine must not replace the first recovery copy.

    The state lock serialises concurrent writers, not two starts that each find
    a corrupt file within the same wall-clock second, so the copy name has to
    carry its own uniqueness.
    """
    monkeypatch.setattr("agent_bridge.registry.time.strftime", lambda *_args: "20261001-120000")

    for _ in range(2):
        state_path(bridge_home).write_text("{not valid json", encoding="utf-8")
        registry = Registry.create(bridge_home)
        await registry.start()
        await registry.stop()

    quarantined = sorted(bridge_home.glob("state.json.corrupt-*"))
    assert len(quarantined) == 2
    assert all(item.read_text(encoding="utf-8") == "{not valid json" for item in quarantined)


@pytest.mark.asyncio
async def test_start_survives_non_object_state(bridge_home, tmp_path):
    """Valid JSON of the wrong shape must not raise out of payload.get()."""
    state_path(bridge_home).write_text("[1, 2, 3]", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    registry.sessions["sess_shape"] = Session(
        session_id="sess_shape", agent="fake", cwd=str(work.resolve())
    )

    await registry.start()
    try:
        payload = read_json(state_path(bridge_home), {})
        assert {row["session_id"] for row in payload["sessions"]} == {"sess_shape"}
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_dead_owner_records_are_adopted(bridge_home, monkeypatch):
    monkeypatch.setattr("agent_bridge.registry.owner_alive", lambda pid, create_time: False)
    cwd = str(Path.cwd())
    atomic_write_json(
        state_path(bridge_home),
        {
            "sessions": [
                Session(
                    session_id="sess_dead",
                    agent="fake",
                    cwd=cwd,
                    proc_state=ProcState.busy,
                    owner_pid=9999,
                    owner_create_time=1.0,
                ).model_dump(mode="json")
            ],
            "tasks": [
                Task(
                    task_id="task_dead",
                    session_id="sess_dead",
                    agent="fake",
                    message="in flight",
                    cwd=cwd,
                    status=TaskStatus.running,
                    owner_pid=9999,
                    owner_create_time=1.0,
                ).model_dump(mode="json")
            ],
        },
    )
    registry = Registry.create(bridge_home, owner_pid=2002, owner_create_time=22.0)
    await registry.start()
    try:
        task = registry.tasks["task_dead"]
        assert task.status == TaskStatus.failed
        assert task.error == "bridge_restarted"
        assert task.finished_at is not None
        assert task.owner_pid == 2002
        assert task.owner_create_time == 22.0
        session = registry.sessions["sess_dead"]
        assert session.proc_state == ProcState.idle_unloaded
        assert session.owner_pid == 2002
        assert session.owner_create_time == 22.0
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_legacy_records_without_owner_fields_are_adopted(bridge_home):
    cwd = str(Path.cwd())
    atomic_write_json(
        state_path(bridge_home),
        {
            "sessions": [
                {
                    "session_id": "sess_legacy",
                    "agent": "fake",
                    "cwd": cwd,
                    "proc_state": "busy",
                }
            ],
            "tasks": [
                {
                    "task_id": "task_legacy",
                    "session_id": "sess_legacy",
                    "agent": "fake",
                    "message": "in flight",
                    "cwd": cwd,
                    "status": "running",
                }
            ],
        },
    )
    registry = Registry.create(bridge_home, owner_pid=3003, owner_create_time=33.0)
    await registry.start()
    try:
        task = registry.tasks["task_legacy"]
        assert task.status == TaskStatus.failed
        assert task.error == "bridge_restarted"
        assert task.owner_pid == 3003
        assert task.owner_create_time == 33.0
        session = registry.sessions["sess_legacy"]
        assert session.proc_state == ProcState.idle_unloaded
        assert session.owner_pid == 3003
        assert session.owner_create_time == 33.0
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_failed_turn_keeps_its_accumulated_run_usage(bridge_home, tmp_path, monkeypatch):
    """A turn that ends in an error TurnResult still lands its accumulated
    run_usage on the failed task — check_task/wait_task expose it."""

    async def mid_stream_error(self, session, task):
        return TurnResult(
            text="partial",
            stop_reason="error",
            error="worker blew up",
            usage={"inputTokens": 60},
            run_usage={
                "scope": "run",
                "quality": "exact",
                "input": 60,
                "output": 12,
                "total": 72,
            },
        )

    monkeypatch.setattr(FakeAdapter, "run_turn", mid_stream_error)
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "boom", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "failed"
        assert waited["error"] == "worker blew up"
        assert waited["run_usage"]["total"] == 72
        checked = await registry.check_task(dispatched["task_id"])
        assert checked["run_usage"]["total"] == 72
        # `usage` stays the legacy raw snapshot — the total lives in run_usage.
        assert checked["usage"] == {"inputTokens": 60}
    finally:
        await registry.stop()


AGY_CAPACITY_503 = (
    "API error (attempt 1): UNAVAILABLE (code 503): "
    "No capacity available for model gemini-3.8-flash-medium on the server"
)


@pytest.mark.asyncio
async def test_transient_capacity_failure_is_retryable_and_resumable(bridge_home, tmp_path, monkeypatch):
    """A provider 503 lands as failed but classified: error_kind marks it
    transient_provider (not quota), retryable, resumable — and resume_task
    dispatches a continuation on the same session."""

    async def capacity_error(self, session, task):
        return TurnResult(
            text="partial report",
            stop_reason="error",
            error=AGY_CAPACITY_503,
            native_session_id="fake-conv",
        )

    monkeypatch.setattr(FakeAdapter, "run_turn", capacity_error)
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task(
            "fake", "survey", cwd=str(work.resolve()), model="gemini-3.8-flash-medium"
        )
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "failed"
        assert waited["error"] == AGY_CAPACITY_503
        assert waited["error_kind"] == "transient_provider"
        assert waited["retryable"] is True
        assert waited["resumable"] is True
        assert "transient provider failure" in waited["resume_hint"]
        assert "resume_task" in waited["resume_hint"]
        assert "resume_task" in waited["hint"]
        # check_task and list_tasks carry the same classification.
        checked = await registry.check_task(dispatched["task_id"])
        assert checked["error_kind"] == "transient_provider"
        assert checked["retryable"] is True
        row = next(t for t in registry.list_tasks() if t["task_id"] == dispatched["task_id"])
        assert row["error_kind"] == "transient_provider"
        assert row["retryable"] is True
        # resume_task continues the same session/conversation.
        resumed = await registry.resume_task(dispatched["task_id"])
        assert resumed["session_id"] == dispatched["session_id"]
        assert resumed["task_id"] != dispatched["task_id"]
        continued = await registry.wait_task(resumed["task_id"], timeout_sec=5)
        assert continued["status"] == "failed"
        assert registry.tasks[resumed["task_id"]].resume_of == dispatched["task_id"]
        assert registry.tasks[dispatched["task_id"]].resumed_by == resumed["task_id"]
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_non_transient_failure_is_not_marked_retryable(bridge_home, tmp_path, monkeypatch):
    async def auth_error(self, session, task):
        return TurnResult(text="", stop_reason="error", error="authentication required")

    monkeypatch.setattr(FakeAdapter, "run_turn", auth_error)
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "x", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "failed"
        assert waited["error_kind"] == "unknown"
        assert waited["retryable"] is False
        assert waited["resumable"] is True  # still resumable, just not transient
        assert "transient provider failure" not in waited["resume_hint"]
    finally:
        await registry.stop()


def _write_agy_config(bridge_home: Path) -> None:
    """Point the antigravity agent at the fake agy script so dispatches run."""
    bridge_home.mkdir(parents=True, exist_ok=True)
    fake = Path(__file__).resolve().with_name("fake_agy.py")
    command = ", ".join(json.dumps(part) for part in [sys.executable, str(fake)])
    (bridge_home / "agents.toml").write_text(
        "[agents.antigravity]\n"
        'protocol = "agy"\n'
        f"command = [{command}]\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_agy_effort_suffixed_model_validation_at_dispatch(bridge_home, tmp_path):
    _write_agy_config(bridge_home)
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        # Mismatched explicit effort fails before any task is created.
        with pytest.raises(ValueError, match="invalid model selection"):
            await registry.dispatch_task(
                "antigravity",
                "x",
                cwd=str(work.resolve()),
                model="gemini-3.8-flash-medium",
                effort="high",
            )
        assert registry.tasks == {}
        # Matching effort and omitted effort are valid.
        matching = await registry.dispatch_task(
            "antigravity",
            "x",
            cwd=str(work.resolve()),
            model="gemini-3.8-flash-medium",
            effort="medium",
        )
        assert (await registry.wait_task(matching["task_id"], timeout_sec=15))["status"] == "completed"
        # An effort stored on the session still guards follow-up dispatches:
        # switching to a slug that pins a different level conflicts with it.
        with pytest.raises(ValueError, match="invalid model selection"):
            await registry.dispatch_task(
                "antigravity",
                "x",
                cwd=str(work.resolve()),
                session_id=matching["session_id"],
                model="gemini-3.8-flash-low",
            )
        corrected = await registry.dispatch_task(
            "antigravity",
            "x",
            cwd=str(work.resolve()),
            session_id=matching["session_id"],
            model="gemini-3.8-flash-low",
            effort="low",
        )
        assert (await registry.wait_task(corrected["task_id"], timeout_sec=15))["status"] == "completed"
        # Unsuffixed slugs take any effort.
        free = await registry.dispatch_task(
            "antigravity",
            "x",
            cwd=str(work.resolve()),
            model="gemini-3.7-flash",
            effort="high",
        )
        assert (await registry.wait_task(free["task_id"], timeout_sec=15))["status"] == "completed"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_orphaned_running_task_recovers_partial_run_usage(bridge_home, monkeypatch):
    """A running task whose owner bridge died keeps the transcript's last
    usage snapshot — marked partial — instead of reporting nothing."""
    monkeypatch.setattr("agent_bridge.registry.owner_alive", lambda pid, create_time: False)
    cwd = str(Path.cwd())
    atomic_write_json(
        state_path(bridge_home),
        {
            "sessions": [
                Session(
                    session_id="sess_orphan",
                    agent="fake",
                    cwd=cwd,
                    proc_state=ProcState.busy,
                    owner_pid=9999,
                    owner_create_time=1.0,
                ).model_dump(mode="json")
            ],
            "tasks": [
                Task(
                    task_id="task_orphan",
                    session_id="sess_orphan",
                    agent="fake",
                    message="in flight",
                    cwd=cwd,
                    status=TaskStatus.running,
                    started_at="2000-01-01T00:00:00+00:00",
                    owner_pid=9999,
                    owner_create_time=1.0,
                ).model_dump(mode="json")
            ],
        },
    )
    append_event(
        "sess_orphan",
        "usage",
        {
            "update_type": "UsageUpdate",
            "consumed": {
                "scope": "run",
                "quality": "exact",
                "input": 60,
                "output": 12,
                "total": 72,
            },
        },
        bridge_home,
    )
    registry = Registry.create(bridge_home, owner_pid=2002, owner_create_time=22.0)
    await registry.start()
    try:
        task = registry.tasks["task_orphan"]
        assert task.status == TaskStatus.failed
        assert task.error == "bridge_restarted"
        assert task.run_usage["total"] == 72
        assert task.run_usage["partial"] is True
        snap = await registry.check_task("task_orphan")
        assert snap["run_usage"]["total"] == 72
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_orphaned_task_does_not_inherit_a_prior_runs_usage(bridge_home, monkeypatch):
    """Usage events older than the adopted task's started_at belong to a
    prior run on the session — never attributed to this one."""
    monkeypatch.setattr("agent_bridge.registry.owner_alive", lambda pid, create_time: False)
    cwd = str(Path.cwd())
    atomic_write_json(
        state_path(bridge_home),
        {
            "sessions": [
                Session(
                    session_id="sess_prior",
                    agent="fake",
                    cwd=cwd,
                    proc_state=ProcState.busy,
                    owner_pid=9999,
                    owner_create_time=1.0,
                ).model_dump(mode="json")
            ],
            "tasks": [
                Task(
                    task_id="task_prior",
                    session_id="sess_prior",
                    agent="fake",
                    message="in flight",
                    cwd=cwd,
                    status=TaskStatus.running,
                    # The task started after the only usage event on disk.
                    started_at="2999-01-01T00:00:00+00:00",
                    owner_pid=9999,
                    owner_create_time=1.0,
                ).model_dump(mode="json")
            ],
        },
    )
    append_event(
        "sess_prior",
        "usage",
        {"update_type": "UsageUpdate", "consumed": {"scope": "run", "quality": "exact", "total": 99}},
        bridge_home,
    )
    registry = Registry.create(bridge_home, owner_pid=2002, owner_create_time=22.0)
    await registry.start()
    try:
        task = registry.tasks["task_prior"]
        assert task.status == TaskStatus.failed
        assert task.run_usage == {}
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_get_result_includes_workspace_writes(bridge_home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    result_text = "结" * 8_605

    async def write_then_ok(self, session, task):
        (Path(task.cwd) / "smoke.txt").write_text("hello-bridge\n", encoding="utf-8")
        hidden = Path(task.cwd) / ".sessions"
        hidden.mkdir()
        (hidden / "log.jsonl").write_text("{}\n", encoding="utf-8")
        return TurnResult(text=result_text, files_changed=[])

    monkeypatch.setattr(FakeAdapter, "run_turn", write_then_ok)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task(
            "fake",
            "write smoke",
            cwd=str(work.resolve()),
            model="gemini-3.7-flash",
            effort="low",
        )
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        assert waited["files_changed"] == ["smoke.txt"]
        assert waited["model"] == "gemini-3.7-flash"
        assert waited["effort"] == "low"
        assert waited["observed_model"] is None
        result = await registry.get_result(dispatched["task_id"])
        assert result["model"] == "gemini-3.7-flash"
        assert "observed_model" in result
        assert result["result_text"] == result_text
        assert result["has_more"] is False
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_get_result_reads_complete_unicode_result_in_pages(
    bridge_home, tmp_path, monkeypatch
):
    work = tmp_path / "work"
    work.mkdir()
    full_text = "汉🙂abc\n" * 25_000

    async def long_turn(self, session, task):
        return TurnResult(text=full_text)

    monkeypatch.setattr(FakeAdapter, "run_turn", long_turn)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "long", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        assert waited["result_truncated"] is True
        assert waited["result_total_chars"] == len(full_text)
        assert len(registry.tasks[dispatched["task_id"]].result_text) < len(full_text)
        assert result_path(dispatched["task_id"], bridge_home).read_text(
            encoding="utf-8"
        ) == full_text

        cursor = 0
        parts: list[str] = []
        while True:
            page = await registry.get_result(
                dispatched["task_id"], cursor=cursor, max_chars=17_000
            )
            assert page["result_complete"] is True
            assert page["result_source"] == "artifact"
            assert page["result_total_chars"] == len(full_text)
            parts.append(page["result_text"])
            if not page["has_more"]:
                assert page["next_cursor"] is None
                break
            cursor = page["next_cursor"]
        assert "".join(parts) == full_text
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_get_result_pages_artifact_without_full_read(bridge_home, tmp_path, monkeypatch):
    """get_result must decode only the requested window, not the whole artifact."""
    work = tmp_path / "work"
    work.mkdir()
    full_text = "汉🙂abc\n" * 25_000

    async def long_turn(self, session, task):
        return TurnResult(text=full_text)

    monkeypatch.setattr(FakeAdapter, "run_turn", long_turn)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "long", cwd=str(work.resolve()))
        await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        artifact = result_path(dispatched["task_id"], bridge_home)
        assert artifact.is_file()

        real_read_text = Path.read_text

        def no_full_artifact_read(self, *args, **kwargs):
            if self == artifact:
                raise AssertionError("get_result must not read the whole artifact")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", no_full_artifact_read)
        page = await registry.get_result(dispatched["task_id"], cursor=0, max_chars=1000)
        assert page["result_text"] == full_text[:1000]
        assert page["result_total_chars"] == len(full_text)
        assert page["has_more"] is True
        assert page["next_cursor"] == 1000
        tail = await registry.get_result(dispatched["task_id"], cursor=len(full_text) - 10)
        assert tail["result_text"] == full_text[-10:]
        assert tail["has_more"] is False
        assert tail["next_cursor"] is None
        edge = await registry.get_result(dispatched["task_id"], cursor=len(full_text))
        assert edge["result_text"] == "" and edge["has_more"] is False
        with pytest.raises(ValueError, match="cursor exceeds"):
            await registry.get_result(dispatched["task_id"], cursor=len(full_text) + 1)
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_task_lifecycle_logging_is_sparse(bridge_home, tmp_path, caplog):
    work = tmp_path / "work"
    work.mkdir()
    caplog.set_level(logging.INFO, logger="agent_bridge.registry")
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task(
            "fake", "do not log this prompt", cwd=str(work.resolve())
        )
        await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        messages = [record.getMessage() for record in caplog.records]
        lifecycle = [message for message in messages if message.startswith("task_")]
        assert len([message for message in lifecycle if message.startswith("task_dispatched")]) == 1
        assert len([message for message in lifecycle if message.startswith("task_finished")]) == 1
        assert all("do not log this prompt" not in message for message in lifecycle)
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_kimi_silent_failure_becomes_a_warning(bridge_home, tmp_path, monkeypatch):
    """Kimi answers end_turn on a failed turn; wire.jsonl is the only witness."""
    work = tmp_path / "work"
    work.mkdir()
    bridge_home.mkdir(parents=True, exist_ok=True)
    # Run the real kimi branch in the registry against a fake transport.
    (bridge_home / "agents.toml").write_text(
        '[agents.kimi]\nprotocol = "fake"\n', encoding="utf-8"
    )
    monkeypatch.setattr(
        "agent_bridge.registry.observe_kimi_session",
        lambda native_id: {
            "model": "kimi-code/k3-256k",
            "effort": "high",
            "failure": "failed: provider.api_error: 402 membership",
        },
    )

    async def empty_turn(self, session, task):
        return TurnResult(text="", stop_reason="end_turn")

    monkeypatch.setattr(FakeAdapter, "run_turn", empty_turn)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("kimi", "do it", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        assert waited["stop_reason"] == "end_turn"
        assert waited["observed_model"] == "kimi-code/k3-256k"
        assert waited["observed_effort"] == "high"
        assert any("402 membership" in w for w in waited["warnings"])
        result = await registry.get_result(dispatched["task_id"])
        assert "end_turn with empty text" in result["hint"]
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_kimi_clean_turn_adds_no_warning(bridge_home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    bridge_home.mkdir(parents=True, exist_ok=True)
    (bridge_home / "agents.toml").write_text(
        '[agents.kimi]\nprotocol = "fake"\n', encoding="utf-8"
    )
    monkeypatch.setattr(
        "agent_bridge.registry.observe_kimi_session",
        lambda native_id: {"model": "kimi-code/k3", "effort": "low", "failure": None},
    )
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("kimi", "do it", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        assert waited["warnings"] == []
        assert waited["observed_model"] == "kimi-code/k3"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_opencode_observed_model_comes_from_the_adapter(bridge_home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    bridge_home.mkdir(parents=True, exist_ok=True)
    (bridge_home / "agents.toml").write_text(
        '[agents.opencode]\nprotocol = "fake"\n', encoding="utf-8"
    )

    async def applied_turn(self, session, task):
        return TurnResult(
            text="done",
            stop_reason="end_turn",
            observed_model="opencode/x-preview-f-free",
            observed_effort="high",
        )

    monkeypatch.setattr(FakeAdapter, "run_turn", applied_turn)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task(
            "opencode",
            "do it",
            cwd=str(work.resolve()),
            model="opencode/x-preview-f-free",
            effort="max",
        )
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        assert waited["model"] == "opencode/x-preview-f-free"
        assert waited["effort"] == "max"
        assert waited["observed_model"] == "opencode/x-preview-f-free"
        assert waited["observed_effort"] == "high"
        result = await registry.get_result(dispatched["task_id"])
        assert "last values Bridge successfully set" in result["hint"]
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_claude_observed_model_comes_from_the_adapter(bridge_home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    bridge_home.mkdir(parents=True, exist_ok=True)
    (bridge_home / "agents.toml").write_text(
        '[agents.claude]\nprotocol = "fake"\n', encoding="utf-8"
    )

    async def applied_turn(self, session, task):
        return TurnResult(
            text="done",
            stop_reason="end_turn",
            observed_model="sonnet",
            observed_effort="xhigh",
        )

    monkeypatch.setattr(FakeAdapter, "run_turn", applied_turn)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task(
            "claude",
            "do it",
            cwd=str(work.resolve()),
            model="sonnet",
            effort="max",
        )
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        assert waited["model"] == "sonnet"
        assert waited["effort"] == "max"
        assert waited["observed_model"] == "sonnet"
        assert waited["observed_effort"] == "xhigh"
        result = await registry.get_result(dispatched["task_id"])
        assert "Claude Code observed_model/effort" in result["hint"]
        assert "last values Bridge successfully set" in result["hint"]
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_late_turn_result_does_not_overwrite_cancelled(bridge_home, tmp_path, monkeypatch):
    """cancel_task's timeout path finalizes the task; the turn ending later must not flip it back."""
    work = tmp_path / "work"
    work.mkdir()

    async def finalize_then_finish(self, session, task):
        # Simulate cancel_task's 15s-timeout path having already finalized.
        task.status = TaskStatus.cancelled
        task.stop_reason = "cancelled"
        task.finished_at = iso()
        return TurnResult(text="late result", stop_reason="end_turn")

    monkeypatch.setattr(FakeAdapter, "run_turn", finalize_then_finish)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "slow", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "cancelled"
        assert waited["stop_reason"] == "cancelled"
        assert "late result" in waited["result_text"]
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_old_terminal_tasks_are_pruned(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setattr("agent_bridge.registry.TASK_KEEP_PER_SESSION", 2)
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        first = await registry.dispatch_task("fake", "t0", cwd=str(work.resolve()))
        await registry.wait_task(first["task_id"], timeout_sec=5)
        task_ids = [first["task_id"]]
        for index in range(3):
            more = await registry.dispatch_task(
                "fake",
                f"t{index + 1}",
                cwd=str(work.resolve()),
                session_id=first["session_id"],
            )
            await registry.wait_task(more["task_id"], timeout_sec=5)
            task_ids.append(more["task_id"])
        assert task_ids[0] not in registry.tasks
        assert task_ids[-1] in registry.tasks
        assert not result_path(task_ids[0], bridge_home).exists()
        assert result_path(task_ids[-1], bridge_home).is_file()
        terminal = [t for t in registry.tasks.values() if t.session_id == first["session_id"]]
        assert len(terminal) <= 3  # 2 kept terminal + possibly the newest
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_wait_task_survives_prune_before_waiter_resumes(bridge_home, tmp_path, monkeypatch):
    """A dispatch-time _prune can evict a task between its done event and the
    waiter's wakeup; wait_task must still return the completed snapshot."""
    monkeypatch.setattr("agent_bridge.registry.TASK_KEEP_PER_SESSION", 0)
    gate = asyncio.Event()
    real_run_turn = FakeAdapter.run_turn

    async def gated(self, session, task):
        await gate.wait()
        return await real_run_turn(self, session, task)

    monkeypatch.setattr(FakeAdapter, "run_turn", gated)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "one", cwd=str(tmp_path))
        task_id = dispatched["task_id"]
        waiter = asyncio.create_task(registry.wait_task(task_id, timeout_sec=5))
        for _ in range(3):
            await asyncio.sleep(0)  # let wait_task bind the task and park on _done
        assert not waiter.done()

        schedule_idle = registry._schedule_idle

        def prune_before_waiter_resumes(session_id):
            # _run_task sets _done before _schedule_idle with no await in
            # between, so the waiter cannot run until this returns.
            registry._prune()
            schedule_idle(session_id)

        monkeypatch.setattr(registry, "_schedule_idle", prune_before_waiter_resumes)
        gate.set()
        waited = await waiter
        assert task_id not in registry.tasks
        assert waited["timed_out"] is False
        assert waited["task_id"] == task_id
        assert waited["status"] == "completed"
        assert "[fake:fake] one" in waited["result_text"]
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_idle_exit_disabled_server_remains_usable_after_long_idle(bridge_home, tmp_path):
    registry = Registry.create(bridge_home)
    registry.config.server.idle_exit_sec = 0
    await registry.start()
    try:
        assert registry._watchdog is None
        registry._last_activity = time.monotonic() - 86400
        assert registry.idle_exit_due() is False
        dispatched = await registry.dispatch_task("fake", "after idle", cwd=str(tmp_path))
        result = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert result["status"] == "completed"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_idle_exit_due_predicate(bridge_home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    registry.config.server.idle_exit_sec = 30
    await registry.start()
    try:
        assert registry.idle_exit_due() is False

        registry._last_activity = time.monotonic() - registry.config.server.idle_exit_sec - 1
        assert registry.idle_exit_due() is True

        registry.tasks["task_busy"] = Task(
            task_id="task_busy",
            session_id="sess_busy",
            agent="fake",
            message="hold",
            cwd=str(work.resolve()),
            status=TaskStatus.running,
        )
        assert registry.idle_exit_due() is False

        registry.tasks["task_busy"].status = TaskStatus.queued
        assert registry.idle_exit_due() is False

        registry.tasks["task_busy"].status = TaskStatus.completed
        assert registry.idle_exit_due() is True

        registry.config.server.idle_exit_sec = 0
        assert registry.idle_exit_due() is False
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_coordinator_status_default_auto_never_blocks(bridge_home, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_BRIDGE_MODE", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        status = registry.coordinator_status()
        assert status["mode"] == "auto"
        assert status["instructions"] is None
        assert status["hint"]
        assert status["runtime_context"] == "coordinator"
        assert status["dispatch_enabled"] is True
        dispatched = await registry.dispatch_task("fake", "hi", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_set_preferences_applies_now_and_persists(bridge_home, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_BRIDGE_MODE", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        result = registry.set_preferences(mode="safe", instructions="Coding goes to grok.")
        assert result["coordinator"]["mode"] == "manual"
        assert result["coordinator"]["instructions"] == "Coding goes to grok."
        assert result["notes"]
        # immediate effect in the running instance: manual gate is live
        with pytest.raises(RuntimeError, match="manual"):
            await registry.dispatch_task("fake", "hi", cwd=str(work.resolve()))
        with pytest.raises(ValueError, match="unknown coordinator mode"):
            registry.set_preferences(mode="turbo")
    finally:
        await registry.stop()

    # a fresh instance over the same home reads the persisted overlay
    fresh = Registry.create(bridge_home)
    assert fresh.coordinator_status()["mode"] == "manual"
    assert fresh.coordinator_status()["instructions"] == "Coding goes to grok."


@pytest.mark.asyncio
async def test_manual_mode_blocks_dispatch_without_user_request(bridge_home, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_BRIDGE_MODE", raising=False)
    bridge_home.mkdir(parents=True, exist_ok=True)
    (bridge_home / "agents.toml").write_text(
        """
[coordinator]
mode = "manual"
instructions = "Coding goes to grok."
""",
        encoding="utf-8",
    )
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        status = registry.coordinator_status()
        assert status["mode"] == "manual"
        assert "user_requested" in status["hint"]
        assert status["instructions"] == "Coding goes to grok."
        with pytest.raises(RuntimeError, match="manual"):
            await registry.dispatch_task("fake", "hi", cwd=str(work.resolve()))
        dispatched = await registry.dispatch_task(
            "fake", "hi", cwd=str(work.resolve()), user_requested=True
        )
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_worker_status_disables_dispatch(bridge_home):
    registry = Registry.create(bridge_home, runtime_context="worker")
    status = registry.coordinator_status()
    assert status["runtime_context"] == "worker"
    assert status["dispatch_enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "auto", "eager"])
@pytest.mark.parametrize("user_requested", [False, True])
async def test_worker_context_blocks_dispatch_before_validation(
    bridge_home, monkeypatch, mode, user_requested
):
    monkeypatch.delenv("AGENT_BRIDGE_MODE", raising=False)
    bridge_home.mkdir(parents=True, exist_ok=True)
    (bridge_home / "agents.toml").write_text(
        f'[coordinator]\nmode = "{mode}"\n',
        encoding="utf-8",
    )
    registry = Registry.create(bridge_home, runtime_context="worker")
    with pytest.raises(RuntimeError, match="nested dispatch is disabled") as exc:
        await registry.dispatch_task(
            "not-an-agent",
            "x",
            cwd="relative",
            user_requested=user_requested,
        )
    assert NESTED_DISPATCH_ERROR in str(exc.value)


def test_worker_context_set_preferences_does_not_write(bridge_home):
    bridge_home.mkdir(parents=True, exist_ok=True)
    path = bridge_home / "agents.toml"
    original = "[coordinator]\nmode = \"auto\"\ninstructions = \"keep\"\n"
    path.write_text(original, encoding="utf-8")
    mtime = path.stat().st_mtime
    registry = Registry.create(bridge_home, runtime_context="worker")
    with pytest.raises(RuntimeError, match="preference updates are disabled") as exc:
        registry.set_preferences(mode="eager", instructions="changed")
    assert NESTED_PREFERENCES_ERROR in str(exc.value)
    assert path.read_text(encoding="utf-8") == original
    assert path.stat().st_mtime == mtime


def test_registry_detects_worker_context_from_env(bridge_home, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_PARENT_CONTEXT", "worker")
    registry = Registry.create(bridge_home)
    assert registry.runtime_context == "worker"
    assert registry.dispatch_enabled is False


@pytest.mark.asyncio
async def test_worker_context_blocks_cancel_and_end_before_lookup(bridge_home):
    registry = Registry.create(bridge_home, runtime_context="worker")
    with pytest.raises(RuntimeError, match="task cancellation is disabled") as cancel_exc:
        await registry.cancel_task("task_missing")
    assert NESTED_CANCEL_ERROR in str(cancel_exc.value)
    with pytest.raises(RuntimeError, match="session shutdown is disabled") as end_exc:
        await registry.end_session("sess_missing")
    assert NESTED_END_SESSION_ERROR in str(end_exc.value)


def test_registry_injection_overrides_env(bridge_home, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_PARENT_CONTEXT", "worker")
    registry = Registry.create(bridge_home, runtime_context="coordinator")
    assert registry.runtime_context == "coordinator"
    assert registry.dispatch_enabled is True


@pytest.mark.asyncio
async def test_oneshot_adapter_marks_session_idle_unloaded(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setattr(FakeAdapter, "resident", False)
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        first = await registry.dispatch_task("fake", "first turn", cwd=str(work.resolve()))
        waited = await registry.wait_task(first["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        session = registry.sessions[first["session_id"]]
        assert session.proc_state == ProcState.idle_unloaded
        assert first["session_id"] not in {
            row["session_id"] for row in registry.list_sessions(active_only=True)
        }
        assert first["session_id"] in {row["session_id"] for row in registry.list_sessions()}
        second = await registry.dispatch_task(
            "fake",
            "second turn",
            cwd=str(work.resolve()),
            session_id=first["session_id"],
        )
        second_wait = await registry.wait_task(second["task_id"], timeout_sec=5)
        assert second_wait["status"] == "completed"
        assert second["session_id"] == first["session_id"]
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_workspace_snapshot_runs_off_loop(bridge_home, tmp_path, monkeypatch):
    def slow_snapshot(cwd: str | Path) -> dict[str, tuple[int, int]]:
        time.sleep(0.3)
        return {}

    monkeypatch.setattr("agent_bridge.registry.snapshot_workspace", slow_snapshot)
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    ticks = 0

    async def counter() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.02)

    ticker = asyncio.create_task(counter())
    try:
        dispatched = await registry.dispatch_task("fake", "snap", cwd=str(work.resolve()))
        await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert ticks >= 10
    finally:
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker
        await registry.stop()


@pytest.mark.asyncio
async def test_files_changed_is_capped(bridge_home, tmp_path, monkeypatch):
    paths = [f"gen/{i:04d}.txt" for i in range(500)]
    monkeypatch.setattr("agent_bridge.registry.merge_files_changed", lambda *args, **kwargs: (list(paths), []))
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "many files", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert len(waited["files_changed"]) == 200
        assert waited["files_changed"] == paths[:200]
        assert waited["files_changed_total"] == 500
        assert waited["files_changed_truncated"] is True
        result = await registry.get_result(dispatched["task_id"])
        assert "first 200 of 500" in result["hint"]
        checked = await registry.check_task(dispatched["task_id"])
        assert checked["files_changed_total"] == 500
        assert checked["files_changed_truncated"] is True
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_files_changed_uncapped_when_under_limit(bridge_home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "tiny", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["files_changed_truncated"] is False
        assert waited["files_changed_total"] == len(waited["files_changed"])
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_running_files_changed_is_pending_until_collection(
    bridge_home, tmp_path, monkeypatch
):
    work = tmp_path / "work"
    work.mkdir()
    wrote = asyncio.Event()
    release = asyncio.Event()

    async def write_then_wait(self, session, task):
        (Path(task.cwd) / "created.txt").write_text("done\n", encoding="utf-8")
        wrote.set()
        await release.wait()
        return TurnResult(text="done")

    monkeypatch.setattr(FakeAdapter, "run_turn", write_then_wait)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "write", cwd=str(work.resolve()))
        await asyncio.wait_for(wrote.wait(), timeout=5)

        running = await registry.check_task(dispatched["task_id"])
        assert running["status"] == "running"
        assert running["files_changed"] == []
        assert running["files_changed_total"] == 0
        assert running["files_changed_state"] == "pending"

        release.set()
        finished = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert finished["files_changed"] == ["created.txt"]
        assert finished["files_changed_total"] == 1
        assert finished["files_changed_state"] == "collected"
    finally:
        release.set()
        await registry.stop()


@pytest.mark.asyncio
async def test_failed_turn_collects_workspace_changes(bridge_home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()

    async def write_then_fail(self, session, task):
        (Path(task.cwd) / "partial.txt").write_text("partial\n", encoding="utf-8")
        raise RuntimeError("synthetic turn failure")

    monkeypatch.setattr(FakeAdapter, "run_turn", write_then_fail)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "fail", cwd=str(work.resolve()))
        finished = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert finished["status"] == "failed"
        assert finished["files_changed"] == ["partial.txt"]
        assert finished["files_changed_state"] == "collected"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_cancelled_turn_collects_workspace_changes(bridge_home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    wrote = asyncio.Event()

    async def write_until_cancelled(self, session, task):
        (Path(task.cwd) / "cancelled.txt").write_text("partial\n", encoding="utf-8")
        wrote.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(FakeAdapter, "run_turn", write_until_cancelled)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "cancel", cwd=str(work.resolve()))
        await asyncio.wait_for(wrote.wait(), timeout=5)
        cancelled = await registry.cancel_task(dispatched["task_id"])
        assert cancelled["status"] == "cancelled"
        assert cancelled["files_changed"] == ["cancelled.txt"]
        assert cancelled["files_changed_state"] == "collected"
    finally:
        await registry.stop()


def _dead_session(session_id: str, cwd: str, last_active_at: str, proc_state: ProcState = ProcState.dead) -> Session:
    return Session(
        session_id=session_id,
        agent="fake",
        cwd=cwd,
        proc_state=proc_state,
        last_active_at=last_active_at,
    )


def _completed_task(task_id: str, session_id: str, cwd: str, created_at: str) -> Task:
    return Task(
        task_id=task_id,
        session_id=session_id,
        agent="fake",
        message="x",
        cwd=cwd,
        status=TaskStatus.completed,
        created_at=created_at,
    )


def test_prune_sessions_by_count(bridge_home, tmp_path):
    cwd = str(tmp_path)
    registry = Registry.create(bridge_home)
    base = datetime.now(UTC)
    for index in range(60):
        session_id = f"sess_{index:03d}"
        task_id = f"task_{index:03d}"
        stamp = iso(base + timedelta(seconds=index))
        registry.sessions[session_id] = _dead_session(session_id, cwd, stamp)
        registry.tasks[task_id] = _completed_task(task_id, session_id, cwd, stamp)
        registry._done[task_id] = asyncio.Event()
        result_path(task_id, bridge_home).write_text("ok\n", encoding="utf-8")
    registry._prune()
    kept = {f"sess_{index:03d}" for index in range(10, 60)}
    assert set(registry.sessions) == kept
    for index in range(10):
        assert f"task_{index:03d}" not in registry.tasks
        assert f"task_{index:03d}" not in registry._done
        assert not result_path(f"task_{index:03d}", bridge_home).exists()
    for index in range(10, 60):
        assert f"task_{index:03d}" in registry.tasks
        assert result_path(f"task_{index:03d}", bridge_home).is_file()


def test_prune_sessions_by_age(bridge_home, tmp_path):
    cwd = str(tmp_path)
    registry = Registry.create(bridge_home)
    now = datetime.now(UTC)
    registry.sessions["sess_old"] = _dead_session(
        "sess_old", cwd, iso(now - timedelta(days=20)), ProcState.idle_unloaded
    )
    registry.sessions["sess_mid"] = _dead_session(
        "sess_mid", cwd, iso(now - timedelta(days=2)), ProcState.idle_unloaded
    )
    registry.sessions["sess_new"] = _dead_session(
        "sess_new", cwd, iso(now), ProcState.idle_unloaded
    )
    registry._prune()
    assert "sess_old" not in registry.sessions
    assert {"sess_mid", "sess_new"} <= set(registry.sessions)


def test_prune_never_touches_active(bridge_home, tmp_path):
    cwd = str(tmp_path)
    registry = Registry.create(bridge_home)
    old = iso(datetime.now(UTC) - timedelta(days=20))
    for session_id, state in (
        ("sess_busy", ProcState.busy),
        ("sess_ready", ProcState.ready),
        ("sess_spawning", ProcState.spawning),
    ):
        registry.sessions[session_id] = _dead_session(session_id, cwd, old, state)
    held = _dead_session("sess_held", cwd, old, ProcState.idle_unloaded)
    registry.sessions["sess_held"] = held
    registry._adapters["sess_held"] = FakeAdapter(registry.config.get("fake"), registry.home)
    queued = _dead_session("sess_queued", cwd, old, ProcState.idle_unloaded)
    registry.sessions["sess_queued"] = queued
    registry.tasks["task_queued"] = Task(
        task_id="task_queued",
        session_id="sess_queued",
        agent="fake",
        message="x",
        cwd=cwd,
        status=TaskStatus.queued,
        created_at=old,
    )
    registry._prune()
    assert set(registry.sessions) >= {"sess_busy", "sess_ready", "sess_spawning", "sess_held", "sess_queued"}
    assert "task_queued" in registry.tasks


def test_prune_tasks_global_cap(bridge_home, tmp_path):
    cwd = str(tmp_path)
    registry = Registry.create(bridge_home)
    base = datetime.now(UTC)
    index = 0
    for session_n in range(15):
        session_id = f"sess_{session_n:02d}"
        registry.sessions[session_id] = _dead_session(session_id, cwd, iso(base))
        for _ in range(20):
            task_id = f"task_{index:03d}"
            registry.tasks[task_id] = _completed_task(
                task_id, session_id, cwd, iso(base + timedelta(seconds=index))
            )
            index += 1
    registry._prune()
    assert len(registry.tasks) <= 200
    assert "task_000" not in registry.tasks
    assert "task_299" in registry.tasks
    remaining = sorted(registry.tasks)
    assert remaining == [f"task_{n:03d}" for n in range(100, 300)]


@pytest.mark.asyncio
async def test_start_prunes_stale_sessions(bridge_home, tmp_path):
    cwd = str(tmp_path)
    base = datetime.now(UTC)
    atomic_write_json(
        state_path(bridge_home),
        {
            "sessions": [
                _dead_session(
                    f"sess_{index:03d}",
                    cwd,
                    iso(base + timedelta(seconds=index)),
                ).model_dump(mode="json")
                for index in range(60)
            ],
            "tasks": [],
        },
    )
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        assert len(registry.sessions) == 50
        disk = read_json(state_path(bridge_home), {})
        assert len(disk["sessions"]) == 50
        assert {row["session_id"] for row in disk["sessions"]} == set(registry.sessions)
    finally:
        await registry.stop()


def test_get_transcript_survives_pruned_session(bridge_home):
    session_id = "sess_pruned"
    path = transcript_path(session_id, bridge_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"ts":"2026-01-01T00:00:00+00:00","type":"message_chunk","data":{"text":"kept"}}\n',
        encoding="utf-8",
    )
    registry = Registry.create(bridge_home)
    page = registry.get_transcript(session_id)
    assert page["total_matching"] == 1
    assert page["events"][0]["data"]["text"] == "kept"
    with pytest.raises(KeyError, match="unknown session"):
        registry.get_transcript("sess_never")


def test_get_transcript_rejects_unsafe_session_ids(bridge_home):
    # A *.jsonl outside transcripts/ must not be reachable through the id.
    outside = bridge_home / "outside.jsonl"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(
        '{"ts":"2026-01-01T00:00:00+00:00","type":"message_chunk","data":{"text":"x"}}\n',
        encoding="utf-8",
    )
    registry = Registry.create(bridge_home)
    for bad in ("../outside", "..\\outside", "..", "a/b", "a\\b", "", "sess x", ".hidden"):
        with pytest.raises(KeyError, match="unknown session"):
            registry.get_transcript(bad)
    # Valid [A-Za-z0-9_-]+ ids keep the pruned-session transcript fallback.
    kept = transcript_path("sess_OK-1_2", bridge_home)
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_text(
        '{"ts":"2026-01-01T00:00:00+00:00","type":"message_chunk","data":{"text":"kept"}}\n',
        encoding="utf-8",
    )
    assert registry.get_transcript("sess_OK-1_2")["total_matching"] == 1


@pytest.mark.asyncio
async def test_stop_waits_for_running_tasks(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "5")
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    dispatched = await registry.dispatch_task("fake", "slow", cwd=str(work.resolve()))
    await asyncio.sleep(0.1)
    assert registry.tasks[dispatched["task_id"]].status == TaskStatus.running
    started = time.monotonic()
    await registry.stop()
    assert time.monotonic() - started < 3
    assert registry.tasks[dispatched["task_id"]].status == TaskStatus.cancelled
    assert all(task.done() for task in registry._bg.values())
    assert registry._idle == {}
    payload = read_json(state_path(bridge_home), {})
    row = next(item for item in payload["tasks"] if item["task_id"] == dispatched["task_id"])
    assert row["status"] == "cancelled"


def test_schedule_idle_noop_while_stopping(bridge_home, tmp_path):
    registry = Registry.create(bridge_home)
    registry.sessions["sess_idle"] = Session(
        session_id="sess_idle",
        agent="grok",
        cwd=str(tmp_path),
    )
    registry._stopping = True
    registry._schedule_idle("sess_idle")
    assert registry._idle == {}


@pytest.mark.asyncio
async def test_stall_watchdog_fails_silent_turn(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setattr("agent_bridge.registry.STALL_POLL_SEC", 0.2)
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "10")
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    registry.config.agents["fake"].stall_timeout_sec = 1
    await registry.start()
    try:
        started = time.monotonic()
        dispatched = await registry.dispatch_task("fake", "silent", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=8)
        assert time.monotonic() - started < 6
        assert waited["status"] == "failed"
        assert waited["stop_reason"] == "stalled"
        assert waited["error"] is not None and "no output for 1s" in waited["error"]
        assert waited["silent_for_sec"] is None
        assert "stall_timeout_sec" in (waited.get("hint") or "")
        assert registry._bg == {}
        events = read_events(dispatched["session_id"], bridge_home)
        assert any(
            event.get("type") == "error" and (event.get("data") or {}).get("stalled") is True
            for event in events
        )
        await registry.flush_state()
        payload = read_json(state_path(bridge_home), {})
        row = next(item for item in payload["tasks"] if item["task_id"] == dispatched["task_id"])
        assert row["status"] == "failed"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_stall_watchdog_resets_on_worker_output(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setattr("agent_bridge.registry.STALL_POLL_SEC", 0.2)
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "2.5")
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    registry.config.agents["fake"].stall_timeout_sec = 1
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "ticking", cwd=str(work.resolve()))
        task_id = dispatched["task_id"]
        session_id = dispatched["session_id"]

        async def _tick() -> None:
            while registry.tasks[task_id].status not in TERMINAL_STATUSES:
                append_event(session_id, "message_chunk", {"text": "tick"}, bridge_home)
                await asyncio.sleep(0.3)

        ticker = asyncio.create_task(_tick())
        try:
            waited = await registry.wait_task(task_id, timeout_sec=8)
            assert waited["status"] == "completed"
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_stall_watchdog_disabled_when_zero(bridge_home, tmp_path, monkeypatch):
    async def _boom(*args, **kwargs):
        raise AssertionError("_stall_watch should not start when stall_timeout_sec is 0")

    monkeypatch.setattr(Registry, "_stall_watch", _boom)
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "0.3")
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    registry.config.agents["fake"].stall_timeout_sec = 0
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "ok", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_check_task_reports_silent_for_sec(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "2")
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "slow", cwd=str(work.resolve()))
        await asyncio.sleep(0.5)
        checked = await registry.check_task(dispatched["task_id"])
        assert isinstance(checked["silent_for_sec"], int)
        assert 0 <= checked["silent_for_sec"] <= 2
        assert checked["stall_timeout_sec"] == 1800
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        assert waited["silent_for_sec"] is None
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_cancel_immediately_after_dispatch_returns_fast(bridge_home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "tiny", cwd=str(work.resolve()))
        started = time.monotonic()
        cancelled = await registry.cancel_task(dispatched["task_id"])
        assert time.monotonic() - started < 3
        assert cancelled["status"] == "cancelled"
        assert registry._bg == {}
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=1)
        assert waited["status"] == "cancelled"
        assert registry.sessions[dispatched["session_id"]].proc_state != ProcState.spawning
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_cancel_task_still_wins_over_stall(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setattr("agent_bridge.registry.STALL_POLL_SEC", 0.2)
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "10")
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    registry.config.agents["fake"].stall_timeout_sec = 1
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "slow", cwd=str(work.resolve()))
        await asyncio.sleep(0.05)
        cancelled = await registry.cancel_task(dispatched["task_id"])
        assert cancelled["status"] == "cancelled"
        assert cancelled["stop_reason"] == "cancelled"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_save_writes_off_the_event_loop(bridge_home, tmp_path, monkeypatch):
    writer_threads: list[int] = []

    def spy(path, payload):
        writer_threads.append(threading.get_ident())
        return atomic_write_json(path, payload)

    monkeypatch.setattr("agent_bridge.registry.atomic_write_json", spy)
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "tiny", cwd=str(work.resolve()))
        await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        await registry.flush_state()
        main = threading.get_ident()
        assert writer_threads
        assert all(tid != main for tid in writer_threads)
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_save_coalesces_bursts(bridge_home, tmp_path, monkeypatch):
    writes: list[object] = []

    def spy(path, payload):
        writes.append(payload)
        return atomic_write_json(path, payload)

    monkeypatch.setattr("agent_bridge.registry.atomic_write_json", spy)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        start_writes = len(writes)
        registry.sessions["sess_burst"] = Session(
            session_id="sess_burst",
            agent="fake",
            cwd=str(tmp_path),
        )
        for _ in range(20):
            registry.save()
        await registry.flush_state()
        assert len(writes) - start_writes <= 2
        payload = read_json(state_path(bridge_home), {})
        assert any(row["session_id"] == "sess_burst" for row in payload["sessions"])
    finally:
        await registry.stop()


def test_save_without_running_loop_writes_synchronously(bridge_home, tmp_path):
    registry = Registry.create(bridge_home)
    registry.sessions["sess_sync"] = Session(
        session_id="sess_sync",
        agent="fake",
        cwd=str(tmp_path),
    )
    registry.save()
    payload = read_json(state_path(bridge_home), {})
    assert any(row["session_id"] == "sess_sync" for row in payload["sessions"])


@pytest.mark.asyncio
async def test_stop_flushes_state(bridge_home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    dispatched = await registry.dispatch_task("fake", "tiny", cwd=str(work.resolve()))
    await registry.wait_task(dispatched["task_id"], timeout_sec=5)
    await registry.stop()
    payload = read_json(state_path(bridge_home), {})
    row = next(item for item in payload["tasks"] if item["task_id"] == dispatched["task_id"])
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_stall_cancel_finishes_after_turn_returns(bridge_home, tmp_path, monkeypatch):
    """The turn usually returns before adapter.cancel() has finished reaping;
    tearing down the watchdog must not abort that cleanup halfway."""
    monkeypatch.setattr("agent_bridge.registry.STALL_POLL_SEC", 0.2)
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "10")
    cancel_finished = asyncio.Event()
    original_cancel = FakeAdapter.cancel

    async def slow_cancel(self, session):
        await original_cancel(self, session)
        await asyncio.sleep(0.5)
        cancel_finished.set()

    monkeypatch.setattr(FakeAdapter, "cancel", slow_cancel)
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    registry.config.agents["fake"].stall_timeout_sec = 1
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "silent", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=8)
        assert waited["stop_reason"] == "stalled"
        assert not cancel_finished.is_set()
        await asyncio.sleep(0.8)
        assert cancel_finished.is_set()
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_outbox_message_dispatches(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_POLL_SEC", 0.05)
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "hi", cwd=str(work.resolve()))
        await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        outbox = bridge_home / "outbox"
        outbox.mkdir(exist_ok=True)
        (outbox / "msg_1_abcdef12.json").write_text(
            json.dumps({"session_id": dispatched["session_id"], "message": "follow up"}),
            encoding="utf-8",
        )
        done = outbox / "done" / "msg_1_abcdef12.json"
        for _ in range(100):
            if done.exists():
                break
            await asyncio.sleep(0.05)
        assert done.exists(), "outbox message was never delivered"
        result = json.loads(done.read_text(encoding="utf-8"))
        assert result["ok"], result
        waited = await registry.wait_task(result["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        assert "follow up" in waited["result_text"]
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_outbox_unknown_session_reports_error(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_POLL_SEC", 0.05)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        outbox = bridge_home / "outbox"
        outbox.mkdir(exist_ok=True)
        (outbox / "msg_2_abcdef12.json").write_text(
            json.dumps({"session_id": "sess_missing", "message": "hi"}),
            encoding="utf-8",
        )
        done = outbox / "done" / "msg_2_abcdef12.json"
        for _ in range(100):
            if done.exists():
                break
            await asyncio.sleep(0.05)
        assert done.exists(), "outbox message was never resolved"
        result = json.loads(done.read_text(encoding="utf-8"))
        assert result["ok"] is False
        assert "unknown session" in result["error"]
    finally:
        await registry.stop()


async def _outbox_wait_done(outbox: Path, name: str, tries: int = 100) -> dict:
    done = outbox / "done" / name
    for _ in range(tries):
        if done.exists():
            return json.loads(done.read_text(encoding="utf-8"))
        await asyncio.sleep(0.05)
    raise AssertionError(f"outbox message {name} was never resolved")


@pytest.mark.asyncio
async def test_outbox_busy_survives_old_attempt_cap_then_delivers(bridge_home, tmp_path, monkeypatch):
    """A queued message behind a busy session is not dropped at the old
    ~600-attempt cadence budget: it stays requeued (waiting_busy) and is
    delivered once the session frees."""
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_POLL_SEC", 0.05)
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_BUSY_RETRY_SEC", 0.05)
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "2")
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        first = await registry.dispatch_task("fake", "slow", cwd=str(work.resolve()))
        outbox = bridge_home / "outbox"
        outbox.mkdir(exist_ok=True)
        name = "msg_3_abcdef12.json"
        # attempts far beyond the retired 600-retry cap: under the old code the
        # very next claim would drop the message.
        (outbox / name).write_text(
            json.dumps(
                {
                    "session_id": first["session_id"],
                    "message": "still there?",
                    "ts": time.time(),
                    "attempts": 700,
                }
            ),
            encoding="utf-8",
        )
        await asyncio.sleep(0.4)  # several claim/requeue cycles while busy
        assert not (outbox / "done" / name).exists(), "busy message was dropped at the old cap"
        rec = None
        for _ in range(40):  # the file is briefly claimed each cycle
            path = outbox / name
            if path.exists():
                rec = json.loads(path.read_text(encoding="utf-8"))
                if int(rec.get("attempts") or 0) > 700:
                    break
            await asyncio.sleep(0.05)
        assert rec is not None, "message vanished instead of being requeued"
        assert rec["state"] == "waiting_busy" and int(rec["attempts"]) > 700
        awaited = await registry.wait_task(first["task_id"], timeout_sec=8)
        assert awaited["status"] == "completed"
        result = await _outbox_wait_done(outbox, name)
        assert result["ok"], result
        waited = await registry.wait_task(result["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        assert "still there?" in waited["result_text"]
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_outbox_message_expires_past_deadline(bridge_home, tmp_path, monkeypatch):
    """The only drop for a queued message is the wall-clock expiry."""
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_POLL_SEC", 0.05)
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")  # stays busy the whole test
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        first = await registry.dispatch_task("fake", "slow", cwd=str(work.resolve()))
        outbox = bridge_home / "outbox"
        outbox.mkdir(exist_ok=True)
        name = "msg_4_abcdef12.json"
        (outbox / name).write_text(
            json.dumps(
                {
                    "session_id": first["session_id"],
                    "message": "stale",
                    "ts": time.time() - 25 * 3600,  # past the 24h queue deadline
                }
            ),
            encoding="utf-8",
        )
        result = await _outbox_wait_done(outbox, name)
        assert result["ok"] is False
        assert "expired" in result["error"]
        await registry.cancel_task(first["task_id"])
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_outbox_source_and_task_id_reach_transcript(bridge_home, tmp_path, monkeypatch):
    """Dashboard-dispatched tasks carry source=dashboard onto the prompt card."""
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_POLL_SEC", 0.05)
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        first = await registry.dispatch_task("fake", "hi", cwd=str(work.resolve()))
        await registry.wait_task(first["task_id"], timeout_sec=5)
        outbox = bridge_home / "outbox"
        outbox.mkdir(exist_ok=True)
        name = "msg_5_abcdef12.json"
        (outbox / name).write_text(
            json.dumps(
                {
                    "session_id": first["session_id"],
                    "message": "from the dashboard",
                    "ts": time.time(),
                }
            ),
            encoding="utf-8",
        )
        result = await _outbox_wait_done(outbox, name)
        assert result["ok"], result
        task = registry.tasks[result["task_id"]]
        assert task.source == "dashboard"
        # dispatch returns at queue time; wait for the turn so the buffered
        # prompt_sent/turn_end events are flushed to the transcript.
        await registry.wait_task(result["task_id"], timeout_sec=5)
        events = read_events(first["session_id"], bridge_home)
        prompts = [e for e in events if e["type"] == "prompt_sent"]
        assert prompts[-1]["data"]["source"] == "dashboard"
        assert prompts[-1]["data"]["task_id"] == result["task_id"]
        turns = [e for e in events if e["type"] == "turn_end"]
        assert turns[-1]["data"]["task_id"] == result["task_id"]
        # The ordinary MCP dispatch earlier stayed source-less.
        assert registry.tasks[first["task_id"]].source is None
        assert prompts[0]["data"].get("source") is None
        assert prompts[0]["data"]["task_id"] == first["task_id"]
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_outbox_stranded_claim_is_rescued(bridge_home, tmp_path, monkeypatch):
    """A stale *.claim left by a crashed bridge is restored and delivered."""
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_POLL_SEC", 0.05)
    # Deterministic owner liveness: only pid 4242 counts as a live claim owner.
    monkeypatch.setattr("agent_bridge.registry.owner_alive", lambda pid, create_time=None: pid == 4242)
    work = tmp_path / "work"
    work.mkdir()
    outbox = bridge_home / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    name = "msg_6_abcdef12.json"
    body = json.dumps({"session_id": "sess_pending", "message": "rescued", "ts": time.time()})
    claim = outbox / f"{name}.9999.claim"
    claim.write_text(body, encoding="utf-8")
    old = time.time() - 120
    os.utime(claim, (old, old))
    # A claim held by a *live* pid must not be touched.
    held = outbox / "msg_7_abcdef12.json.4242.claim"
    held.write_text(
        json.dumps({"session_id": "sess_pending", "message": "not yours"}),
        encoding="utf-8",
    )
    os.utime(held, (old, old))
    registry = Registry.create(bridge_home)
    # The session the rescued message targets must exist before start so the
    # loop can deliver immediately after the rescue.
    session = Session(
        session_id="sess_pending",
        agent="fake",
        cwd=str(work.resolve()),
        proc_state=ProcState.idle_unloaded,
    )
    registry.sessions[session.session_id] = session
    await registry.start()
    try:
        for _ in range(100):  # the rescue sweep runs at outbox-loop startup
            if not claim.exists():
                break
            await asyncio.sleep(0.05)
        assert not claim.exists(), "stranded claim was not rescued"
        assert held.exists(), "a live owner's claim was stolen"
        result = await _outbox_wait_done(outbox, name)
        assert result["ok"], result
        waited = await registry.wait_task(result["task_id"], timeout_sec=5)
        assert "rescued" in waited["result_text"]
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_failed_task_writes_turn_end_boundary(bridge_home, tmp_path, monkeypatch):
    """A task that dies inside the adapter still gets a transcript boundary so
    the dashboard shows a finished turn instead of a silent gap."""
    work = tmp_path / "work"
    work.mkdir()

    async def boom(self, session, task):
        raise RuntimeError("spawn exploded")

    monkeypatch.setattr(FakeAdapter, "run_turn", boom)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "go", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "failed"
        events = read_events(dispatched["session_id"], bridge_home)
        turns = [e for e in events if e["type"] == "turn_end"]
        assert len(turns) == 1
        assert turns[0]["data"]["stop_reason"] == "error"
        assert turns[0]["data"]["task_id"] == dispatched["task_id"]
        assert "spawn exploded" in turns[0]["data"]["error"]
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_worker_error_and_warnings_are_sanitized_before_persisting(
    bridge_home, tmp_path, monkeypatch
):
    """H-04: worker-derived text crosses into state.json/transcripts — scrub it."""
    secret = "tp-leak0123456789abcdef"

    async def leaky_turn(self, session, task):
        return TurnResult(
            text="",
            stop_reason="error",
            error=f"worker boom: Authorization: Bearer {secret} api_key={secret}",
            warnings=[f"Set-Cookie: session={secret}"],
        )

    monkeypatch.setattr(FakeAdapter, "run_turn", leaky_turn)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "leak", cwd=str(tmp_path))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "failed"
        assert secret not in (waited["error"] or "")
        assert "[redacted]" in (waited["error"] or "")
        assert secret not in json.dumps(waited["warnings"])
        registry.save()
        state = json.loads(state_path(bridge_home).read_text(encoding="utf-8"))
        assert secret not in json.dumps(state)
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_run_usage_persists_survives_prune_and_reload(bridge_home, tmp_path, monkeypatch):
    """Task.run_usage is the accumulated per-run token metric: persisted at
    finalization, exposed by every task API, unchanged by reload + pruning."""
    work = tmp_path / "work"
    work.mkdir()
    real_run_turn = FakeAdapter.run_turn

    async def with_usage(self, session, task):
        result = await real_run_turn(self, session, task)
        result.usage = {"input_tokens": 10, "output_tokens": 4}
        result.run_usage = {
            "scope": "run",
            "quality": "exact",
            "input": 10,
            "output": 4,
            "total": 14,
        }
        return result

    monkeypatch.setattr(FakeAdapter, "run_turn", with_usage)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "count me", cwd=str(work.resolve()))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        assert waited["run_usage"]["total"] == 14
        assert waited["usage"]["input_tokens"] == 10
        assert (await registry.check_task(dispatched["task_id"]))["run_usage"]["total"] == 14
        assert (await registry.get_result(dispatched["task_id"]))["run_usage"]["total"] == 14
        task = registry.tasks[dispatched["task_id"]]
        assert task.run_usage["total"] == 14
    finally:
        await registry.stop()

    state = json.loads((bridge_home / "state.json").read_text(encoding="utf-8"))
    row = next(t for t in state["tasks"] if t["task_id"] == dispatched["task_id"])
    assert row["run_usage"]["total"] == 14

    registry2 = Registry.create(bridge_home)
    await registry2.start()
    try:
        reloaded = registry2.tasks[dispatched["task_id"]]
        assert reloaded.run_usage["total"] == 14
        # Task pruning drops old terminal tasks but never mutates survivors.
        for i in range(30):
            old = Task(
                task_id=f"old-{i}",
                session_id=dispatched["session_id"],
                agent="fake",
                message="old",
                cwd=str(work.resolve()),
                status=TaskStatus.completed,
                created_at=f"2020-01-01T00:{i // 60:02d}:{i % 60:02d}",
            )
            registry2.tasks[old.task_id] = old
        registry2._prune_tasks()
        assert registry2.tasks[dispatched["task_id"]].run_usage["total"] == 14
    finally:
        await registry2.stop()


def test_run_usage_defaults_empty_for_old_state():
    """state.json written before run_usage existed loads with empty dicts."""
    task = Task.model_validate(
        {
            "task_id": "t_old",
            "session_id": "s_old",
            "agent": "devin",
            "message": "x",
            "cwd": ".",
            "usage": {"used": 500, "size": 1000},
        }
    )
    assert task.run_usage == {}
    assert task.usage["used"] == 500
    result = TurnResult.model_validate({"text": "ok"})
    assert result.run_usage == {}


def test_task_snapshot_exposes_live_partial_run_usage(bridge_home):
    """A running task's snapshot surfaces the latest normalized usage event
    as a marked partial; the persisted Task.run_usage is untouched, and a
    stale event from a previous run is never attributed to the new one."""
    registry = Registry.create(bridge_home)
    session = Session(
        session_id="sess_live", agent="fake", cwd=".", proc_state=ProcState.busy
    )
    task = Task(
        task_id="t_live",
        session_id="sess_live",
        agent="fake",
        message="x",
        cwd=".",
        status=TaskStatus.running,
        started_at=iso(),
    )
    registry.sessions[session.session_id] = session
    registry.tasks[task.task_id] = task
    append_event(
        "sess_live",
        "usage",
        {
            "update_type": "UsageUpdate",
            "consumed": {
                "scope": "run",
                "quality": "exact",
                "input": 40,
                "output": 9,
                "total": 49,
            },
        },
        bridge_home,
    )
    snap = registry._task_snapshot(task)
    assert snap["run_usage"]["total"] == 49
    assert snap["run_usage"]["partial"] is True
    assert task.run_usage == {}  # persisted record untouched

    late = Task(
        task_id="t_late",
        session_id="sess_live",
        agent="fake",
        message="x",
        cwd=".",
        status=TaskStatus.running,
        started_at="2999-01-01T00:00:00Z",
    )
    registry.tasks[late.task_id] = late
    assert registry._task_snapshot(late)["run_usage"] == {}


@pytest.mark.asyncio
async def test_turn_timeout_sec_caps_chatty_turn(bridge_home, tmp_path, monkeypatch):
    """H-16: a worker emitting worker-type events forever still hits the cap."""
    import asyncio as _asyncio

    async def chatty_turn(self, session, task):
        from agent_bridge.transcript import append_event

        for _ in range(30):
            append_event(session.session_id, "thought_chunk", {"text": "..."}, self.home)
            await _asyncio.sleep(0.1)
        return TurnResult(text="done")

    monkeypatch.setattr(FakeAdapter, "run_turn", chatty_turn)
    registry = Registry.create(bridge_home)
    registry.config.agents["fake"].turn_timeout_sec = 1
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "chatty", cwd=str(tmp_path))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=10)
        assert waited["status"] == "failed"
        assert waited["stop_reason"] == "timed_out"
        assert "turn_timeout_sec" in (waited["error"] or "")
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_silent_turn_still_stalls(bridge_home, tmp_path, monkeypatch):
    """Silence semantics: with no worker events at all, stall_timeout fires."""

    async def silent_turn(self, session, task):
        await asyncio.sleep(10)
        return TurnResult(text="done")

    monkeypatch.setattr(FakeAdapter, "run_turn", silent_turn)
    registry = Registry.create(bridge_home)
    registry.config.agents["fake"].stall_timeout_sec = 1
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "quiet", cwd=str(tmp_path))
        waited = await registry.wait_task(dispatched["task_id"], timeout_sec=10)
        assert waited["status"] == "failed"
        assert waited["stop_reason"] == "stalled"
    finally:
        await registry.stop()
