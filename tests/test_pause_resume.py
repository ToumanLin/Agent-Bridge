"""pause_task / resume_task lifecycle: graceful pause with partial-result
preservation, resume on the same session, dead-owner adoption across a
coordinator restart (including the orphaned-worker reap), remote rejection,
and Registry.stop cancellation safety.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_bridge.models import ProcState, Session, Task, TaskStatus
from agent_bridge.paths import pids_path, result_path, state_path
from agent_bridge.persist import atomic_write_json, read_json
from agent_bridge.processes import (
    process_create_time,
    process_image_name,
    reap_orphan_for_session,
)
from agent_bridge.registry import (
    NESTED_PAUSE_ERROR,
    NESTED_RESUME_ERROR,
    RESUME_DEFAULT_MESSAGE,
    Registry,
)

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def _dead_owner() -> tuple[int, float]:
    """A real pid + create_time that is already dead — like a crashed Bridge."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    create_time = process_create_time(proc.pid)
    proc.wait(timeout=30)
    return proc.pid, create_time


def _plant_dead_session(home: Path, session_id: str, cwd: str, owner: tuple[int, float]) -> None:
    payload = read_json(state_path(home), {}) or {}
    payload.setdefault("sessions", []).append(
        Session(
            session_id=session_id,
            agent="fake",
            cwd=cwd,
            proc_state=ProcState.busy,
            native_session_id=f"native-{session_id}",
            owner_pid=owner[0],
            owner_create_time=owner[1],
        ).model_dump(mode="json")
    )
    atomic_write_json(state_path(home), payload)


def _plant_dead_task(
    home: Path,
    task_id: str,
    session_id: str,
    cwd: str,
    owner: tuple[int, float],
    *,
    status: TaskStatus = TaskStatus.cancelled,
    paused: bool = False,
    stop_reason: str | None = None,
) -> None:
    payload = read_json(state_path(home), {}) or {}
    payload.setdefault("tasks", []).append(
        Task(
            task_id=task_id,
            session_id=session_id,
            agent="fake",
            message="in flight",
            cwd=cwd,
            status=status,
            paused=paused,
            stop_reason=stop_reason,
            owner_pid=owner[0],
            owner_create_time=owner[1],
        ).model_dump(mode="json")
    )
    atomic_write_json(state_path(home), payload)


@pytest.mark.asyncio
async def test_pause_running_task_preserves_partial(bridge_home, tmp_path, monkeypatch):
    """A running turn paused mid-flight ends cancelled/paused with its
    streamed partial text, transcript events, usage fields, and native
    session id all preserved — and reports resumable."""
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_PARTIAL", "1")
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "long running", cwd=cwd)
        task_id = dispatched["task_id"]
        session_id = dispatched["session_id"]
        await asyncio.sleep(0.2)
        assert registry.tasks[task_id].status == TaskStatus.running

        paused = await registry.pause_task(task_id)
        assert paused["status"] == "cancelled"
        assert paused["stop_reason"] == "paused"
        assert paused["paused"] is True
        assert paused["resumable"] is True
        assert "resume_task" in paused["resume_hint"]
        assert "partial progress" in paused["result_text"]

        task = registry.tasks[task_id]
        assert task.status == TaskStatus.cancelled
        assert task.paused is True
        # The graceful path persisted the partial artifact.
        artifact = result_path(task_id, bridge_home)
        assert artifact.is_file()
        assert "partial progress" in artifact.read_text(encoding="utf-8")

        # Transcript kept the streamed chunk and a turn boundary.
        transcript = registry.get_transcript(session_id)
        kinds = [e["type"] for e in transcript["events"]]
        assert "prompt_sent" in kinds and "message_chunk" in kinds and "turn_end" in kinds

        # The native conversation survives for the continuation.
        assert registry.sessions[session_id].native_session_id == f"fake-{session_id}"

        rows = {row["task_id"]: row for row in registry.list_tasks()}
        assert rows[task_id]["paused"] is True
        assert rows[task_id]["resumable"] is True
        assert "resume_task" in rows[task_id]["resume_hint"]

        result = registry.get_result(task_id)
        assert result["paused"] is True
        assert result["resumable"] is True
        assert "partial progress" in result["result_text"]

        # Pausing an already-paused task is an idempotent no-op.
        again = await registry.pause_task(task_id)
        assert again["status"] == "cancelled" and again["paused"] is True
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_pause_queued_task_is_immediate(bridge_home, tmp_path, monkeypatch):
    """Pausing a task whose turn never started lands paused without
    waiting out the graceful-cancel window."""
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "not started", cwd=cwd)
        started = time.monotonic()
        paused = await registry.pause_task(dispatched["task_id"])
        assert time.monotonic() - started < 5
        assert paused["status"] == "cancelled"
        assert paused["stop_reason"] == "paused"
        assert paused["paused"] is True
        assert paused["resumable"] is True
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_pause_then_resume_same_session(bridge_home, tmp_path, monkeypatch):
    """pause → resume dispatches a NEW continuation task on the same
    session/native conversation; the old row stays final and auditable."""
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "first leg", cwd=cwd)
        task_id = dispatched["task_id"]
        session_id = dispatched["session_id"]
        await asyncio.sleep(0.2)
        await registry.pause_task(task_id)
        native = registry.sessions[session_id].native_session_id
        turns = registry.sessions[session_id].turns

        resumed = await registry.resume_task(task_id)
        new_id = resumed["task_id"]
        assert resumed["resumed_from"] == task_id
        assert resumed["session_id"] == session_id
        assert new_id != task_id

        # Old row unchanged in identity: still the paused terminal row, now
        # pointing forward at its continuation.
        old = registry.check_task(task_id)
        assert old["status"] == "cancelled"
        assert old["paused"] is True
        assert old["resumed_by"] == new_id
        assert registry.tasks[task_id].message == "first leg"

        new_task = registry.tasks[new_id]
        assert new_task.resume_of == task_id
        assert new_task.source == "resume"
        assert new_task.message == RESUME_DEFAULT_MESSAGE

        # Same session + native conversation.
        monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "0.01")
        waited = await registry.wait_task(new_id, timeout_sec=5)
        assert waited["status"] == "completed"
        assert registry.sessions[session_id].native_session_id == native
        assert registry.sessions[session_id].turns == turns + 1

        listed = {row["task_id"]: row for row in registry.list_tasks()}
        assert listed[new_id]["resume_of"] == task_id
        assert listed[task_id]["resumed_by"] == new_id
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_resume_uses_custom_message(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "leg one", cwd=cwd)
        await asyncio.sleep(0.2)
        await registry.pause_task(dispatched["task_id"])
        monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "0.01")
        resumed = await registry.resume_task(
            dispatched["task_id"], message="finish only step 3 now"
        )
        assert registry.tasks[resumed["task_id"]].message == "finish only step 3 now"
        waited = await registry.wait_task(resumed["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_resume_rejects_running_completed_unknown(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        running = await registry.dispatch_task("fake", "still going", cwd=cwd)
        await asyncio.sleep(0.2)
        with pytest.raises(RuntimeError, match="still running"):
            await registry.resume_task(running["task_id"])

        monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "0.01")
        done = await registry.dispatch_task("fake", "quick", cwd=cwd)
        waited = await registry.wait_task(done["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        with pytest.raises(RuntimeError, match="already completed"):
            await registry.resume_task(done["task_id"])
        with pytest.raises(RuntimeError, match="already completed"):
            await registry.pause_task(done["task_id"])

        with pytest.raises(KeyError, match="unknown task"):
            await registry.resume_task("task_nope")
        with pytest.raises(KeyError, match="unknown task"):
            await registry.pause_task("task_nope")

        await registry.cancel_task(running["task_id"])
        # A plain-cancelled task is also resumable (same conversation continues).
        resumed = await registry.resume_task(running["task_id"])
        assert resumed["resumed_from"] == running["task_id"]
        await registry.wait_task(resumed["task_id"], timeout_sec=5)
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_pause_resume_rejected_in_nested_context(bridge_home, monkeypatch):
    registry = Registry.create(bridge_home, runtime_context="worker")
    await registry.start()
    try:
        assert registry.dispatch_enabled is False
        with pytest.raises(RuntimeError, match=NESTED_PAUSE_ERROR):
            await registry.pause_task("task_x")
        with pytest.raises(RuntimeError, match=NESTED_RESUME_ERROR):
            await registry.resume_task("task_x")
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_restart_resume_adopts_dead_owner(bridge_home, tmp_path, monkeypatch):
    """Instance A pauses a task then dies. Instance B boots, adopts the
    dead-owned rows, and resume_task continues on the same session and
    native conversation id."""
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive",
        lambda pid, create_time=None: pid == os.getpid(),
    )
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    a = Registry.create(bridge_home, owner_pid=9999, owner_create_time=1.0)
    await a.start()
    dispatched = await a.dispatch_task("fake", "first leg", cwd=cwd)
    task_id = dispatched["task_id"]
    session_id = dispatched["session_id"]
    await asyncio.sleep(0.2)
    await a.pause_task(task_id)
    await a.flush_state()
    native = a.sessions[session_id].native_session_id
    # Simulate the host kill: freeze A's writes, drop its asyncio leftovers
    # without running stop() so nothing is re-saved or torn down.
    monkeypatch.setattr(a, "save", lambda: None)
    for t in (a._watchdog, a._outbox_task):
        if t is not None:
            t.cancel()

    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "0.01")
    b = Registry.create(bridge_home)
    await b.start()
    try:
        # Boot-time adoption kept the paused row truthful.
        adopted = b.tasks[task_id]
        assert adopted.status == TaskStatus.cancelled
        assert adopted.paused is True
        assert adopted.owner_pid == b._owner_pid
        assert b.sessions[session_id].native_session_id == native

        resumed = await b.resume_task(task_id)
        waited = await b.wait_task(resumed["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        assert resumed["session_id"] == session_id
        assert b.sessions[session_id].native_session_id == native
        assert b.tasks[resumed["task_id"]].resume_of == task_id
        assert b.tasks[task_id].resumed_by == resumed["task_id"]
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_resume_adopts_dead_owner_at_runtime(bridge_home, tmp_path, monkeypatch):
    """A dead owner's rows landing AFTER this instance booted are adopted
    lazily: resume_task takes over the paused task and its session."""
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive",
        lambda pid, create_time=None: pid == os.getpid(),
    )
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    owner = _dead_owner()
    b = Registry.create(bridge_home)
    await b.start()
    try:
        _plant_dead_session(bridge_home, "sess_orph", cwd, owner)
        _plant_dead_task(
            bridge_home,
            "task_orph",
            "sess_orph",
            cwd,
            owner,
            paused=True,
            stop_reason="paused",
        )
        resumed = await b.resume_task("task_orph")
        assert resumed["resumed_from"] == "task_orph"
        assert resumed["session_id"] == "sess_orph"
        waited = await b.wait_task(resumed["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        assert b.sessions["sess_orph"].native_session_id == "native-sess_orph"
        assert b.tasks["task_orph"].owner_pid == b._owner_pid
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_resume_delegates_to_live_remote_owner(bridge_home, tmp_path, monkeypatch):
    """A paused task a LIVE sibling still owns is never driven by the caller:
    resume_task routes the request through the shared outbox, the owning
    instance runs the normal resume path, and the caller relays the real new
    task — pause/cancel stay rejected, and the continuation is a remote row
    the caller can poll like any other sibling task."""
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_POLL_SEC", 0.05)
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_FOREIGN_RETRY_SEC", 0.1)
    monkeypatch.setattr("agent_bridge.registry.RESUME_DELEGATE_POLL_SEC", 0.05)
    monkeypatch.setattr("agent_bridge.registry.REMOTE_TASK_POLL_SEC", 0.05)
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive",
        lambda pid, create_time=None: pid in (4242, 2002, os.getpid()),
    )
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    a = Registry.create(bridge_home, owner_pid=4242, owner_create_time=7.0)
    await a.start()
    try:
        dispatched = await a.dispatch_task("fake", "sibling work", cwd=cwd)
        task_id = dispatched["task_id"]
        session_id = dispatched["session_id"]
        await asyncio.sleep(0.2)
        await a.pause_task(task_id)
        await a.flush_state()
        monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "0.01")

        b = Registry.create(bridge_home, owner_pid=2002, owner_create_time=22.0)
        await b.start()
        try:
            assert task_id not in b.tasks
            # The remote view resolves read-only and resumable — the hint
            # names the routed path, not a rejection.
            checked = b.check_task(task_id)
            assert checked["remote"] is True
            assert checked["paused"] is True
            assert checked["resumable"] is True
            assert "routes" in checked["resume_hint"]

            resumed = await asyncio.wait_for(b.resume_task(task_id), timeout=30)
            new_id = resumed["task_id"]
            assert resumed["resumed_from"] == task_id
            assert resumed["session_id"] == session_id
            assert new_id != task_id
            assert resumed["delegated"] is True

            # The continuation was dispatched and is owned by A — the audit
            # links live on the owning instance's rows.
            assert a.tasks[task_id].resumed_by == new_id
            assert a.tasks[new_id].resume_of == task_id
            assert a.tasks[new_id].source == "resume"
            assert a.tasks[new_id].owner_pid == 4242
            assert a.tasks[new_id].session_id == session_id

            # B sees both rows as remote and can poll the new one to done.
            remote_new = b.check_task(new_id)
            assert remote_new["remote"] is True
            assert remote_new["owner"]["pid"] == 4242
            waited = await b.wait_task(new_id, timeout_sec=15)
            assert waited["status"] == "completed", waited
            assert waited["remote"] is True

            # Pause and cancel on a live sibling's row are still refused.
            with pytest.raises(RuntimeError, match="cannot be paused"):
                await b.pause_task(task_id)
            with pytest.raises(RuntimeError, match="cannot be cancelled"):
                await b.cancel_task(task_id)

            # Exactly one continuation ever landed on disk.
            disk = read_json(state_path(bridge_home), {})
            conts = [r for r in disk["tasks"] if r.get("resume_of") == task_id]
            assert len(conts) == 1 and conts[0]["task_id"] == new_id
        finally:
            await b.stop()
    finally:
        await a.stop()


@pytest.mark.asyncio
async def test_resume_delegation_replays_request_id(bridge_home, tmp_path, monkeypatch):
    """Replaying a delegated resume_task with the same request_id returns the
    already-dispatched continuation — the persisted resumed_by dedupes the
    retry even though the caller never owned the task."""
    import uuid

    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_POLL_SEC", 0.05)
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_FOREIGN_RETRY_SEC", 0.1)
    monkeypatch.setattr("agent_bridge.registry.RESUME_DELEGATE_POLL_SEC", 0.05)
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive",
        lambda pid, create_time=None: pid in (4242, 2002, os.getpid()),
    )
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    a = Registry.create(bridge_home, owner_pid=4242, owner_create_time=7.0)
    await a.start()
    try:
        dispatched = await a.dispatch_task("fake", "sibling work", cwd=cwd)
        task_id = dispatched["task_id"]
        await asyncio.sleep(0.2)
        await a.pause_task(task_id)
        await a.flush_state()
        monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "0.01")

        b = Registry.create(bridge_home, owner_pid=2002, owner_create_time=22.0)
        await b.start()
        try:
            rid = str(uuid.uuid4())
            first = await asyncio.wait_for(
                b.resume_task(task_id, request_id=rid), timeout=30
            )
            replay = await asyncio.wait_for(
                b.resume_task(task_id, request_id=rid), timeout=30
            )
            assert replay["task_id"] == first["task_id"]
            assert replay["resumed_from"] == task_id
            # The owner also answers a plain repeat with the same continuation.
            again = await a.resume_task(task_id)
            assert again["task_id"] == first["task_id"]
            disk = read_json(state_path(bridge_home), {})
            conts = [r for r in disk["tasks"] if r.get("resume_of") == task_id]
            assert len(conts) == 1
        finally:
            await b.stop()
    finally:
        await a.stop()


@pytest.mark.asyncio
async def test_resume_delegation_owner_death_adopts_locally(bridge_home, tmp_path, monkeypatch):
    """The recorded owner dying mid-delegation falls back to lazy adoption:
    the caller adopts the row after a short grace and runs the continuation
    itself — no idle_exit wait, and exactly one continuation lands."""
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_POLL_SEC", 0.05)
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_FOREIGN_RETRY_SEC", 0.1)
    monkeypatch.setattr("agent_bridge.registry.RESUME_DELEGATE_POLL_SEC", 0.05)
    monkeypatch.setattr("agent_bridge.registry.RESUME_FALLBACK_GRACE_SEC", 0.2)
    monkeypatch.setattr("agent_bridge.registry.REMOTE_TASK_POLL_SEC", 0.05)
    alive = {4242: True}
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive",
        lambda pid, create_time=None: pid == os.getpid() or alive.get(pid, False),
    )
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    a = Registry.create(bridge_home, owner_pid=4242, owner_create_time=7.0)
    await a.start()
    dispatched = await a.dispatch_task("fake", "sibling work", cwd=cwd)
    task_id = dispatched["task_id"]
    session_id = dispatched["session_id"]
    await asyncio.sleep(0.2)
    await a.pause_task(task_id)
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "0.01")

    b = Registry.create(bridge_home, owner_pid=2002, owner_create_time=22.0)
    await b.start()
    try:
        # First the owner is alive-but-unresponsive — the orphaned-bridge
        # shape: its pid still passes liveness, so the caller must queue and
        # wait rather than steal the row, but its outbox loop is gone so
        # nothing can claim the request.
        monkeypatch.setattr(a, "save", lambda: None)
        for bg in (a._watchdog, a._outbox_task):
            if bg is not None:
                bg.cancel()
        resume_call = asyncio.ensure_future(b.resume_task(task_id))
        outbox = bridge_home / "outbox"
        for _ in range(100):
            if resume_call.done():
                pytest.fail("resume finished while its owner was still live")
            if any(p.name.startswith("req_resume_") for p in outbox.iterdir()):
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("delegated resume request never reached the outbox")
        # Confirm the caller really waits on a live owner before the flip.
        await asyncio.sleep(0.3)
        assert not resume_call.done()
        # Now the owner actually dies mid-delegation.
        alive[4242] = False

        resumed = await asyncio.wait_for(resume_call, timeout=30)
        new_id = resumed["task_id"]
        assert resumed["resumed_from"] == task_id
        assert resumed["session_id"] == session_id
        assert new_id != task_id
        # B adopted the paused row and dispatched the continuation itself.
        assert b.tasks[task_id].owner_pid == 2002
        assert b.tasks[task_id].resumed_by == new_id
        assert b.tasks[new_id].owner_pid == 2002
        assert b.tasks[new_id].resume_of == task_id
        assert b.tasks[new_id].source == "resume"
        waited = await b.wait_task(new_id, timeout_sec=15)
        assert waited["status"] == "completed"
        # No second continuation was dispatched by any path.
        disk = read_json(state_path(bridge_home), {})
        conts = [r for r in disk["tasks"] if r.get("resume_of") == task_id]
        assert len(conts) == 1 and conts[0]["task_id"] == new_id
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_resume_rejects_live_remote_when_remote_tasks_disabled(
    bridge_home, tmp_path, monkeypatch
):
    """remote_tasks=false keeps the sibling isolation contract: a live
    sibling's paused row is invisible to control ops — resume must not
    route around it through the outbox either."""
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive",
        lambda pid, create_time=None: pid in (4242, 2002, os.getpid()),
    )
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    a = Registry.create(bridge_home, owner_pid=4242, owner_create_time=7.0)
    await a.start()
    try:
        dispatched = await a.dispatch_task("fake", "sibling work", cwd=cwd)
        task_id = dispatched["task_id"]
        await asyncio.sleep(0.2)
        await a.pause_task(task_id)

        (bridge_home / "agents.toml").write_text(
            "[server]\nremote_tasks = false\n", encoding="utf-8"
        )
        b = Registry.create(bridge_home, owner_pid=2002, owner_create_time=22.0)
        await b.start()
        try:
            with pytest.raises(KeyError, match="unknown task"):
                await b.resume_task(task_id)
        finally:
            await b.stop()
    finally:
        await a.stop()


@pytest.mark.asyncio
async def test_resume_reaps_orphaned_worker_before_respawn(bridge_home, tmp_path, monkeypatch):
    """Lazy session adoption reaps the dead owner's recorded worker first —
    the new continuation never shares the conversation with a stray executor."""
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive",
        lambda pid, create_time=None: pid == os.getpid(),
    )
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    owner = _dead_owner()
    orphan = subprocess.Popen(SLEEPER)
    try:
        b = Registry.create(bridge_home)
        await b.start()
        try:
            _plant_dead_session(bridge_home, "sess_orph2", cwd, owner)
            _plant_dead_task(
                bridge_home,
                "task_orph2",
                "sess_orph2",
                cwd,
                owner,
                paused=True,
                stop_reason="paused",
            )
            table = read_json(pids_path(bridge_home), {}) or {}
            table["sess_orph2"] = {
                "pid": orphan.pid,
                "create_time": process_create_time(orphan.pid),
                "image_name": process_image_name(orphan.pid),
                "owner_pid": owner[0],
                "owner_create_time": owner[1],
            }
            atomic_write_json(pids_path(bridge_home), table)

            resumed = await b.resume_task("task_orph2")
            assert resumed["session_id"] == "sess_orph2"
            orphan.wait(timeout=15)
            assert orphan.poll() is not None
            assert "sess_orph2" not in (read_json(pids_path(bridge_home), {}) or {})
            waited = await b.wait_task(resumed["task_id"], timeout_sec=5)
            assert waited["status"] == "completed"
        finally:
            await b.stop()
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait(timeout=10)


def test_reap_orphan_for_session_unit(bridge_home):
    """One-record variant: dead owner's worker killed, live owner's kept."""
    home = bridge_home
    home.mkdir(parents=True, exist_ok=True)
    owner = _dead_owner()
    orphan = subprocess.Popen(SLEEPER)
    live_worker = subprocess.Popen(SLEEPER)
    try:
        table = {
            "sess_dead": {
                "pid": orphan.pid,
                "create_time": process_create_time(orphan.pid),
                "image_name": process_image_name(orphan.pid),
                "owner_pid": owner[0],
                "owner_create_time": owner[1],
            },
            # Owned by this very test process: still alive — never reaped.
            "sess_mine": {
                "pid": live_worker.pid,
                "create_time": process_create_time(live_worker.pid),
                "image_name": process_image_name(live_worker.pid),
                "owner_pid": os.getpid(),
                "owner_create_time": process_create_time(os.getpid()),
            },
        }
        atomic_write_json(pids_path(home), table)
        assert reap_orphan_for_session(home, "sess_dead") == orphan.pid
        orphan.wait(timeout=15)
        # A live-owner record is untouched and not reaped.
        assert reap_orphan_for_session(home, "sess_mine") is None
        assert live_worker.poll() is None
        table = read_json(pids_path(home), {})
        assert "sess_dead" not in table and "sess_mine" in table
        assert reap_orphan_for_session(home, "sess_missing") is None
    finally:
        for proc in (orphan, live_worker):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


@pytest.mark.asyncio
async def test_resume_rejected_when_remote_tasks_disabled(bridge_home, tmp_path, monkeypatch):
    """remote_tasks=false keeps the sibling isolation contract: a dead
    owner's paused row stays invisible to control ops too."""
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive",
        lambda pid, create_time=None: pid == os.getpid(),
    )
    bridge_home.mkdir(parents=True, exist_ok=True)
    (bridge_home / "agents.toml").write_text("[server]\nremote_tasks = false\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    owner = _dead_owner()
    b = Registry.create(bridge_home)
    await b.start()
    try:
        _plant_dead_session(bridge_home, "sess_iso", cwd, owner)
        _plant_dead_task(
            bridge_home,
            "task_iso",
            "sess_iso",
            cwd,
            owner,
            paused=True,
            stop_reason="paused",
        )
        with pytest.raises(KeyError, match="unknown task"):
            await b.resume_task("task_iso")
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_resume_adopts_in_flight_dead_owner_as_bridge_restarted(
    bridge_home, tmp_path, monkeypatch
):
    """A dead owner's still-"running" row is adopted, finalized
    failed/bridge_restarted, then resumed on the same session."""
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive",
        lambda pid, create_time=None: pid == os.getpid(),
    )
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    owner = _dead_owner()
    b = Registry.create(bridge_home)
    await b.start()
    try:
        _plant_dead_session(bridge_home, "sess_mid", cwd, owner)
        _plant_dead_task(
            bridge_home,
            "task_mid",
            "sess_mid",
            cwd,
            owner,
            status=TaskStatus.running,
        )
        resumed = await b.resume_task("task_mid")
        old = b.tasks["task_mid"]
        assert old.status == TaskStatus.failed
        assert old.error == "bridge_restarted"
        assert old.resumed_by == resumed["task_id"]
        waited = await b.wait_task(resumed["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
        assert b.tasks[resumed["task_id"]].resume_of == "task_mid"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_cancel_task_adopts_dead_owner_and_reaps(bridge_home, tmp_path, monkeypatch):
    """cancel_task on a dead owner's in-flight row adopts it (finalizing it
    as bridge_restarted) and reaps the orphaned worker via the session."""
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive",
        lambda pid, create_time=None: pid == os.getpid(),
    )
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    owner = _dead_owner()
    orphan = subprocess.Popen(SLEEPER)
    try:
        b = Registry.create(bridge_home)
        await b.start()
        try:
            _plant_dead_session(bridge_home, "sess_cx", cwd, owner)
            _plant_dead_task(
                bridge_home, "task_cx", "sess_cx", cwd, owner, status=TaskStatus.running
            )
            table = read_json(pids_path(bridge_home), {}) or {}
            table["sess_cx"] = {
                "pid": orphan.pid,
                "create_time": process_create_time(orphan.pid),
                "image_name": process_image_name(orphan.pid),
                "owner_pid": owner[0],
                "owner_create_time": owner[1],
            }
            atomic_write_json(pids_path(bridge_home), table)

            snapshot = await b.cancel_task("task_cx")
            assert snapshot["status"] == "failed"
            assert snapshot["error"] == "bridge_restarted"
            assert snapshot["remote"] is False
            orphan.wait(timeout=15)
            assert orphan.poll() is not None
            assert "sess_cx" in b.sessions
        finally:
            await b.stop()
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait(timeout=10)


@pytest.mark.asyncio
async def test_dispatch_and_end_session_adopt_dead_owner_session(
    bridge_home, tmp_path, monkeypatch
):
    """dispatch_task(session_id=…) and end_session lazily adopt a dead
    owner's session instead of answering 'unknown session'."""
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive",
        lambda pid, create_time=None: pid == os.getpid(),
    )
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    owner = _dead_owner()
    b = Registry.create(bridge_home)
    await b.start()
    try:
        _plant_dead_session(bridge_home, "sess_follow", cwd, owner)
        followed = await b.dispatch_task("fake", "follow up", cwd=cwd, session_id="sess_follow")
        assert followed["session_id"] == "sess_follow"
        waited = await b.wait_task(followed["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"

        _plant_dead_session(bridge_home, "sess_end", cwd, owner)
        ended = await b.end_session("sess_end")
        assert ended["proc_state"] == "dead"
        assert b.sessions["sess_end"].proc_state == ProcState.dead
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_dispatch_rejects_live_foreign_session(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive",
        lambda pid, create_time=None: True,
    )
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    b = Registry.create(bridge_home)
    await b.start()
    try:
        _plant_dead_session(bridge_home, "sess_live", cwd, (4242, 7.0))
        with pytest.raises(RuntimeError, match="owned by another live"):
            await b.dispatch_task("fake", "hijack", cwd=cwd, session_id="sess_live")
        with pytest.raises(RuntimeError, match="owned by another live"):
            await b.end_session("sess_live")
        assert "sess_live" not in b.sessions
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_resume_request_id_dedup(bridge_home, tmp_path, monkeypatch):
    """Replaying resume_task with the same request_id returns the original
    continuation task instead of dispatching a second one."""
    import uuid

    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "leg", cwd=cwd)
        await asyncio.sleep(0.2)
        await registry.pause_task(dispatched["task_id"])
        monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "0.01")
        rid = str(uuid.uuid4())
        first = await registry.resume_task(dispatched["task_id"], request_id=rid)
        replay = await registry.resume_task(dispatched["task_id"], request_id=rid)
        assert replay["task_id"] == first["task_id"]
        assert replay["reused"] is True
        waited = await registry.wait_task(first["task_id"], timeout_sec=5)
        assert waited["status"] == "completed"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_old_disk_rows_without_pause_fields(bridge_home, monkeypatch):
    """Backward compatibility: a pre-upgrade task row (no paused/resume_*)
    validates and snapshots with paused=false."""
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive",
        lambda pid, create_time=None: pid == os.getpid(),
    )
    cwd = str(Path.cwd())
    atomic_write_json(
        state_path(bridge_home),
        {
            "sessions": [
                {
                    "session_id": "sess_old",
                    "agent": "fake",
                    "cwd": cwd,
                    "proc_state": "idle_unloaded",
                }
            ],
            "tasks": [
                {
                    "task_id": "task_old",
                    "session_id": "sess_old",
                    "agent": "fake",
                    "message": "legacy",
                    "cwd": cwd,
                    "status": "completed",
                    "result_text": "done",
                }
            ],
        },
    )
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        checked = registry.check_task("task_old")
        assert checked["paused"] is False
        assert checked["resumable"] is False
        assert "resume_of" not in checked
        assert checked["resume_hint"] is not None  # completed → follow-up hint
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_remote_snapshot_marks_dead_owner_paused_resumable(
    bridge_home, tmp_path, monkeypatch
):
    """A dead owner's paused row seen through the remote read path reports
    resumable — resume_task will adopt it."""
    monkeypatch.setattr("agent_bridge.registry.REMOTE_TASK_POLL_SEC", 0.05)
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive", lambda pid, create_time=None: False
    )
    b = Registry.create(bridge_home)
    await b.start()
    try:
        payload = read_json(state_path(bridge_home), {}) or {}
        payload.setdefault("tasks", []).append(
            Task(
                task_id="task_premote",
                session_id="sess_premote",
                agent="fake",
                message="was paused",
                cwd=str(Path.cwd()),
                status=TaskStatus.cancelled,
                paused=True,
                stop_reason="paused",
                owner_pid=5555,
                owner_create_time=3.0,
            ).model_dump(mode="json")
        )
        atomic_write_json(state_path(bridge_home), payload)
        checked = b.check_task("task_premote")
        assert checked["remote"] is True
        assert checked["paused"] is True
        assert checked["resumable"] is True
        assert "resume_task" in checked["resume_hint"]
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_resume_on_ended_session_rejected(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "leg", cwd=cwd)
        task_id = dispatched["task_id"]
        session_id = dispatched["session_id"]
        await asyncio.sleep(0.2)
        await registry.pause_task(task_id)
        await registry.end_session(session_id)
        with pytest.raises(RuntimeError, match="was ended"):
            await registry.resume_task(task_id)
        # And the snapshot reports it truthfully.
        checked = registry.check_task(task_id)
        assert checked["resumable"] is False
        assert "ended" in checked["resume_hint"]
    finally:
        await registry.stop()


# ---------- dashboard task-action requests (req_pause_/req_cancel_) ----------
#
# The dashboard never mutates task state directly: it drops a req_* record in
# the shared outbox and the instance owning the task row runs the normal
# pause/cancel path, reporting through outbox/done/ for the dashboard's poll.


def _drop_req(home: Path, kind: str, task_id: str, **extra) -> str:
    """Write a req_<kind>_<ms>_<8hex>.json request the way the dashboard does."""
    import uuid

    name = f"req_{kind}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.json"
    rec = {
        "kind": kind,
        "task_id": task_id,
        "ts": time.time(),
        "expire_ts": time.time() + 900.0,
    }
    rec.update(extra)
    outbox = home / "outbox"
    outbox.mkdir(exist_ok=True)
    atomic_write_json(outbox / name, rec)
    return name


def _done_record(home: Path, name: str) -> dict | None:
    path = home / "outbox" / "done" / name
    if not path.is_file():
        return None
    return read_json(path, {})


async def _wait_done(home: Path, name: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = _done_record(home, name)
        if rec is not None:
            return rec
        await asyncio.sleep(0.05)
    pytest.fail(f"no done record for {name} within {timeout}s")


@pytest.mark.asyncio
async def test_outbox_pause_request_runs_owner_pause_path(bridge_home, tmp_path, monkeypatch):
    """A req_pause_* record is claimed by the owning instance and executed
    through the normal pause_task path — graceful cancel, paused terminal row,
    and a done record carrying the fields the dashboard reports on."""
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_PARTIAL", "1")
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_POLL_SEC", 0.05)
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "long running", cwd=cwd)
        task_id = dispatched["task_id"]
        await asyncio.sleep(0.2)
        assert registry.tasks[task_id].status == TaskStatus.running

        name = _drop_req(bridge_home, "pause", task_id)
        done = await _wait_done(bridge_home, name)
        assert done["ok"] is True
        assert done["state"] == "paused"
        assert done["task_id"] == task_id
        assert done["status"] == "cancelled"
        assert done["stop_reason"] == "paused"
        assert done["paused"] is True
        # The normal pause path ran: partial result, resumable flag, flush
        # ordering — state.json already shows the row the done record means.
        task = registry.tasks[task_id]
        assert task.status == TaskStatus.cancelled and task.paused is True
        assert "partial progress" in (task.result_text or "")
        disk = read_json(state_path(bridge_home), {})
        row = next(r for r in disk["tasks"] if r["task_id"] == task_id)
        assert row["status"] == "cancelled" and row["paused"] is True
        # The request record itself is consumed (claimed file is gone).
        assert not (bridge_home / "outbox" / name).exists()
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_outbox_cancel_request_runs_owner_cancel_path(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_POLL_SEC", 0.05)
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "long running", cwd=cwd)
        task_id = dispatched["task_id"]
        await asyncio.sleep(0.2)

        name = _drop_req(bridge_home, "cancel", task_id)
        done = await _wait_done(bridge_home, name)
        assert done["ok"] is True
        assert done["state"] == "cancelled"
        assert done["status"] == "cancelled"
        assert done["stop_reason"] == "cancelled"
        task = registry.tasks[task_id]
        assert task.status == TaskStatus.cancelled
        assert task.paused is not True  # plain cancel is not a pause
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_outbox_task_action_unknown_and_expired(bridge_home, tmp_path, monkeypatch):
    """Request-level guards: an unknown task id and a request past its
    expire_ts both resolve through done/ without touching any task."""
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_POLL_SEC", 0.05)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        name = _drop_req(bridge_home, "pause", "task_nope")
        done = await _wait_done(bridge_home, name)
        assert done["ok"] is False
        assert done["error_code"] == "unknown_task"

        name = _drop_req(bridge_home, "cancel", "task_nope", expire_ts=time.time() - 1)
        done = await _wait_done(bridge_home, name)
        assert done["ok"] is False
        assert done["error_code"] == "expired"

        # An invalid task id shape never leaves validation.
        name = _drop_req(bridge_home, "pause", "../bad")
        done = await _wait_done(bridge_home, name)
        assert done["ok"] is False
        assert done["error_code"] == "invalid_record"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_outbox_task_action_requeues_for_live_owner_then_serves(
    bridge_home, tmp_path, monkeypatch
):
    """Single-owner safety over the outbox: when the sibling that does not
    own the task claims the request first it requeues it (waiting_owner, no
    done record); once the recorded owner dies the claimer adopts the row
    and runs the cancel itself."""
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_POLL_SEC", 0.05)
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_FOREIGN_RETRY_SEC", 0.1)
    alive = {4242: True}
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive",
        lambda pid, create_time=None: pid == os.getpid() or alive.get(pid, False),
    )
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    a = Registry.create(bridge_home, owner_pid=4242, owner_create_time=7.0)
    await a.start()
    dispatched = await a.dispatch_task("fake", "sibling work", cwd=cwd)
    task_id = dispatched["task_id"]
    session_id = dispatched["session_id"]
    await asyncio.sleep(0.2)
    await a.flush_state()
    # Simulate the orphaned-bridge shape: A's pid still passes liveness but
    # its outbox loop is gone, so only B can observe the request — first as a
    # live-owner requeue, then as a dead-owner adoption.
    monkeypatch.setattr(a, "save", lambda: None)
    for bg in (a._watchdog, a._outbox_task):
        if bg is not None:
            bg.cancel()

    b = Registry.create(bridge_home, owner_pid=2002, owner_create_time=22.0)
    await b.start()
    try:
        name = _drop_req(bridge_home, "cancel", task_id)
        # B claims and requeues: the record returns to the outbox annotated
        # waiting_owner and no done answer exists while the owner is alive.
        req_path = bridge_home / "outbox" / name
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if req_path.is_file():
                rec = read_json(req_path, {})
                if rec.get("state") == "waiting_owner":
                    break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("request never requeued for the live owner")
        await asyncio.sleep(0.3)
        assert _done_record(bridge_home, name) is None
        assert task_id not in b.tasks  # never adopted while the owner is live

        # Owner dies -> B's next claim adopts the row and runs cancel_task.
        alive[4242] = False
        done = await _wait_done(bridge_home, name)
        assert done["ok"] is True
        assert done["task_id"] == task_id
        assert done["session_id"] == session_id
        # The adopted in-flight row was finalized by adoption (failed/
        # bridge_restarted), and cancel_task reports that terminal row.
        assert done["status"] == "failed"
        assert b.tasks[task_id].owner_pid == 2002
        assert b.tasks[task_id].error == "bridge_restarted"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_outbox_pause_on_terminal_task_reports_current_state(
    bridge_home, tmp_path, monkeypatch
):
    """Pausing an already-finished task through the outbox surfaces the
    truthful terminal snapshot (ok + the real status), never an invented
    'paused'."""
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "0.01")
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_POLL_SEC", 0.05)
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        dispatched = await registry.dispatch_task("fake", "quick", cwd=cwd)
        task_id = dispatched["task_id"]
        waited = await registry.wait_task(task_id, timeout_sec=5)
        assert waited["status"] == "completed"

        name = _drop_req(bridge_home, "pause", task_id)
        done = await _wait_done(bridge_home, name)
        # pause_task raises for a completed non-paused row; the dashboard sees
        # a failed action with the specific code, not a stuck request.
        assert done["ok"] is False
        assert done["error_code"] == "pause_failed"
        assert task_id in done["error"]
    finally:
        await registry.stop()
