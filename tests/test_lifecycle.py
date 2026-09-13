"""Remote (sibling-owned) task visibility, linger shutdown policy, and the
periodic outbox-claim rescue — the persistent-lifecycle stage 0/1 surface."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from agent_bridge.adapters.fake import FakeAdapter
from agent_bridge.models import Session, Task, TaskStatus
from agent_bridge.paths import result_path, state_path
from agent_bridge.persist import atomic_write_json, read_json
from agent_bridge.registry import Registry, _parse_outbox_claim


@pytest.mark.asyncio
async def test_sibling_task_is_visible_readonly(bridge_home, tmp_path, monkeypatch):
    """Instance B sees instance A's running task as remote, follows it to the
    final artifact, and never adopts or re-executes it."""
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "1.0")
    monkeypatch.setattr("agent_bridge.registry.REMOTE_TASK_POLL_SEC", 0.05)
    # Only A's injected owner identity counts as a live bridge.
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive",
        lambda pid, create_time=None: pid == 1001,
    )
    run_calls: list[str] = []
    real_run_turn = FakeAdapter.run_turn

    async def counted(self, session, task):
        run_calls.append(task.task_id)
        return await real_run_turn(self, session, task)

    monkeypatch.setattr(FakeAdapter, "run_turn", counted)

    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work.resolve())
    a = Registry.create(bridge_home, owner_pid=1001, owner_create_time=11.0)
    await a.start()
    try:
        dispatched = await a.dispatch_task("fake", "sibling work", cwd=cwd)
        task_id = dispatched["task_id"]
        session_id = dispatched["session_id"]
        await asyncio.sleep(0.1)
        assert a.tasks[task_id].status == TaskStatus.running

        b = Registry.create(bridge_home)
        await b.start()
        try:
            # start() skipped the live sibling's rows: no adoption.
            assert task_id not in b.tasks
            assert session_id not in b.sessions

            checked = b.check_task(task_id)
            assert checked["remote"] is True
            assert checked["status"] == "running"
            assert checked["owner"] == {"pid": 1001, "create_time": 11.0, "alive": True}
            assert checked["silent_for_sec"] is None
            assert "owner_lost" not in checked

            rows = {row["task_id"]: row for row in b.list_tasks()}
            assert rows[task_id]["remote"] is True
            assert rows[task_id]["owner"]["pid"] == 1001
            assert rows[task_id]["owner"]["alive"] is True
            assert rows[task_id]["owner_lost"] is False

            # B's own work runs normally alongside the remote view.
            own = await b.dispatch_task("fake", "mine", cwd=cwd)
            waited_own = await b.wait_task(own["task_id"], timeout_sec=5)
            assert waited_own["status"] == "completed"
            assert waited_own["remote"] is False
            assert waited_own["owner"]["pid"] == b._owner_pid

            # A remote task cannot be cancelled from here.
            with pytest.raises(RuntimeError, match="cannot be cancelled from here"):
                await b.cancel_task(task_id)

            # Follow the sibling's run to its terminal write + artifact.
            waited = await b.wait_task(task_id, timeout_sec=10)
            assert waited["timed_out"] is False
            assert waited["remote"] is True
            assert waited["status"] == "completed"
            assert "sibling work" in waited["result_text"]

            result = b.get_result(task_id)
            assert result["remote"] is True
            assert result["result_complete"] is True
            assert "sibling work" in result["result_text"]

            listed = {row["task_id"]: row for row in b.list_tasks()}
            assert listed[task_id]["remote"] is True
            assert listed[task_id]["status"] == "completed"

            # Read-only throughout: never adopted, never executed by B.
            assert task_id not in b.tasks
            assert b._bg == {}
            assert run_calls.count(task_id) == 1
        finally:
            await b.stop()
        assert a.tasks[task_id].status == TaskStatus.completed
    finally:
        await a.stop()


@pytest.mark.asyncio
async def test_remote_task_owner_lost_is_truthful(bridge_home, monkeypatch):
    """A queued/running row whose owner is dead reports owner_lost, never a
    fake liveness; wait_task resolves immediately instead of polling."""
    monkeypatch.setattr("agent_bridge.registry.REMOTE_TASK_POLL_SEC", 0.05)
    monkeypatch.setattr("agent_bridge.registry.owner_alive", lambda pid, create_time=None: False)
    b = Registry.create(bridge_home)
    await b.start()
    try:
        # A dead sibling's rows land on disk after this instance started.
        payload = read_json(state_path(bridge_home), {}) or {}
        payload.setdefault("sessions", []).append(
            Session(
                session_id="sess_orphan",
                agent="fake",
                cwd=str(Path.cwd()),
                owner_pid=5555,
                owner_create_time=3.0,
            ).model_dump(mode="json")
        )
        payload.setdefault("tasks", []).append(
            Task(
                task_id="task_orphan",
                session_id="sess_orphan",
                agent="fake",
                message="in flight",
                cwd=str(Path.cwd()),
                status=TaskStatus.running,
                owner_pid=5555,
                owner_create_time=3.0,
            ).model_dump(mode="json")
        )
        atomic_write_json(state_path(bridge_home), payload)

        checked = b.check_task("task_orphan")
        assert checked["remote"] is True
        assert checked["owner_lost"] is True
        assert checked["owner"] == {"pid": 5555, "create_time": 3.0, "alive": False}

        waited = await b.wait_task("task_orphan", timeout_sec=5)
        assert waited["timed_out"] is False
        assert waited["owner_lost"] is True

        rows = {row["task_id"]: row for row in b.list_tasks()}
        assert rows["task_orphan"]["remote"] is True
        assert rows["task_orphan"]["owner_lost"] is True

        assert "task_orphan" not in b.tasks
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_remote_tasks_disabled_keeps_local_only(bridge_home, tmp_path, monkeypatch):
    monkeypatch.setattr("agent_bridge.registry.owner_alive", lambda pid, create_time=None: True)
    bridge_home.mkdir(parents=True, exist_ok=True)
    (bridge_home / "agents.toml").write_text(
        "[server]\nremote_tasks = false\n", encoding="utf-8"
    )
    work = tmp_path / "work"
    work.mkdir()
    a = Registry.create(bridge_home, owner_pid=1001, owner_create_time=11.0)
    await a.start()
    try:
        dispatched = await a.dispatch_task("fake", "x", cwd=str(work.resolve()))
        task_id = dispatched["task_id"]
        b = Registry.create(bridge_home)
        await b.start()
        try:
            assert b.config.server.remote_tasks is False
            assert task_id not in b.tasks
            with pytest.raises(KeyError, match="unknown task"):
                b.check_task(task_id)
            with pytest.raises(KeyError, match="unknown task"):
                b.get_result(task_id)
            with pytest.raises(KeyError, match="unknown task"):
                await b.wait_task(task_id, timeout_sec=0)
            assert all(row["task_id"] != task_id for row in b.list_tasks())
        finally:
            await b.stop()
        waited = await a.wait_task(task_id, timeout_sec=5)
        assert waited["status"] == "completed"
    finally:
        await a.stop()


@pytest.mark.asyncio
async def test_remote_row_written_by_old_build_still_resolves(bridge_home, monkeypatch):
    """A state.json task row without any new fields still resolves remote."""
    monkeypatch.setattr("agent_bridge.registry.owner_alive", lambda pid, create_time=None: True)
    b = Registry.create(bridge_home)
    await b.start()
    try:
        payload = read_json(state_path(bridge_home), {}) or {}
        payload.setdefault("tasks", []).append(
            {
                "task_id": "task_v1",
                "session_id": "sess_v1",
                "agent": "fake",
                "message": "old shape",
                "cwd": str(Path.cwd()),
                "status": "completed",
                "result_text": "v1 result",
                "owner_pid": 4242,
                "owner_create_time": 7.0,
            }
        )
        atomic_write_json(state_path(bridge_home), payload)
        checked = b.check_task("task_v1")
        assert checked["remote"] is True
        assert checked["status"] == "completed"
        result = b.get_result("task_v1")
        assert result["remote"] is True
        assert "v1 result" in result["result_text"]
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_linger_lets_in_flight_task_finish(bridge_home, tmp_path, monkeypatch):
    """shutdown_policy=linger holds stop() until the running turn completes;
    transcript, result artifact, and terminal state land as usual."""
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "1.0")
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    registry.config.server.shutdown_policy = "linger"
    await registry.start()
    dispatched = await registry.dispatch_task("fake", "slow", cwd=str(work.resolve()))
    await asyncio.sleep(0.1)
    assert registry.tasks[dispatched["task_id"]].status == TaskStatus.running
    started = time.monotonic()
    await registry.stop()
    elapsed = time.monotonic() - started
    assert elapsed >= 0.5  # the turn was allowed to run to completion
    task = registry.tasks[dispatched["task_id"]]
    assert task.status == TaskStatus.completed
    assert task.finished_at is not None
    assert result_path(dispatched["task_id"], bridge_home).is_file()
    payload = read_json(state_path(bridge_home), {})
    row = next(item for item in payload["tasks"] if item["task_id"] == dispatched["task_id"])
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_linger_deadline_cancels_stragglers(bridge_home, tmp_path, monkeypatch):
    """linger_max_sec bounds the wait: work still running at the deadline is
    cancelled and the normal teardown proceeds — no unbounded hang."""
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    registry.config.server.shutdown_policy = "linger"
    registry.config.server.linger_max_sec = 0.5
    await registry.start()
    dispatched = await registry.dispatch_task("fake", "slow", cwd=str(work.resolve()))
    await asyncio.sleep(0.1)
    assert registry.tasks[dispatched["task_id"]].status == TaskStatus.running
    started = time.monotonic()
    await registry.stop()
    assert time.monotonic() - started < 15
    assert registry.tasks[dispatched["task_id"]].status == TaskStatus.cancelled
    payload = read_json(state_path(bridge_home), {})
    row = next(item for item in payload["tasks"] if item["task_id"] == dispatched["task_id"])
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_stop_cancelled_mid_linger_still_flushes_state(bridge_home, tmp_path, monkeypatch):
    """Registry.stop is cancellation-safe: if the hosting task is cancelled
    while linger waits, the wait is interrupted, in-flight work is cancelled,
    and flush/save/shutdown still run to completion."""
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "30")
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    registry.config.server.shutdown_policy = "linger"
    await registry.start()
    dispatched = await registry.dispatch_task("fake", "slow", cwd=str(work.resolve()))
    await asyncio.sleep(0.1)
    assert registry.tasks[dispatched["task_id"]].status == TaskStatus.running

    stopper = asyncio.ensure_future(registry.stop())
    await asyncio.sleep(0.1)
    stopper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopper

    # The interrupt drove the linger wait straight to straggler teardown.
    task = registry.tasks[dispatched["task_id"]]
    assert task.status == TaskStatus.cancelled
    assert task.stop_reason == "cancelled"
    assert task.finished_at is not None
    # Teardown really ran: adapters are gone and terminal state is on disk.
    assert registry._adapters == {}
    payload = read_json(state_path(bridge_home), {})
    row = next(item for item in payload["tasks"] if item["task_id"] == dispatched["task_id"])
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_linger_without_in_flight_work_exits_fast(bridge_home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    registry.config.server.shutdown_policy = "linger"
    await registry.start()
    dispatched = await registry.dispatch_task("fake", "tiny", cwd=str(work.resolve()))
    await registry.wait_task(dispatched["task_id"], timeout_sec=5)
    started = time.monotonic()
    await registry.stop()
    assert time.monotonic() - started < 5


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_concurrent_safe(bridge_home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    dispatched = await registry.dispatch_task("fake", "tiny", cwd=str(work.resolve()))
    await registry.wait_task(dispatched["task_id"], timeout_sec=5)
    await asyncio.gather(registry.stop(), registry.stop())
    await registry.stop()  # already done: returns immediately


@pytest.mark.asyncio
async def test_dispatch_rejected_while_stopping(bridge_home, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    await registry.start()
    registry._stopping = True
    with pytest.raises(RuntimeError, match="shutting down"):
        await registry.dispatch_task("fake", "x", cwd=str(work.resolve()))
    registry._stopping = False
    await registry.stop()


def test_parse_outbox_claim_names():
    # New shape carries pid + process create time.
    base, pid, ctime = _parse_outbox_claim("msg_1_abcdef12.json.4242.1700000000.5.claim")
    assert base == "msg_1_abcdef12.json"
    assert pid == 4242
    assert ctime == 1700000000.5
    # Legacy shape: pid only.
    base, pid, ctime = _parse_outbox_claim("msg_1_abcdef12.json.4242.claim")
    assert base == "msg_1_abcdef12.json"
    assert pid == 4242
    assert ctime is None
    # No parseable owner: treated as ownerless (rescuable).
    base, pid, ctime = _parse_outbox_claim("msg_1_abcdef12.json.claim")
    assert base == "msg_1_abcdef12.json"
    assert pid is None
    assert ctime is None


def test_outbox_claim_name_encodes_owner_identity(bridge_home):
    registry = Registry.create(bridge_home, owner_pid=4242, owner_create_time=1700000000.5)
    name = registry._outbox_claim_name("msg_1_abcdef12.json")
    assert name == "msg_1_abcdef12.json.4242.1700000000.5.claim"
    # The dashboard's name.*.claim glob still matches the new shape.
    import fnmatch

    assert fnmatch.fnmatch(name, "msg_1_abcdef12.json.*.claim")
    # A process whose create time could not be read keeps the legacy shape.
    registry._owner_create_time = None
    assert registry._outbox_claim_name("msg_1_abcdef12.json") == "msg_1_abcdef12.json.4242.claim"


def test_outbox_rescue_pid_and_ctime_rules(bridge_home, monkeypatch):
    """Live pid+ctime owner: never stolen. Recycled pid (ctime mismatch) and
    dead pid: rescued. Legacy claims fall back to pid-only liveness."""
    # pid 4242 is alive only when probed with its real create time, or with
    # a legacy claim's pid-only check (ctime=None).
    monkeypatch.setattr(
        "agent_bridge.registry.owner_alive",
        lambda pid, create_time=None: pid == 4242 and (create_time is None or create_time == 7.0),
    )
    registry = Registry.create(bridge_home)
    outbox = bridge_home / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"session_id": "sess_x", "message": "hi"})
    old = time.time() - 120

    def stale_claim(name: str) -> Path:
        claim = outbox / name
        claim.write_text(body, encoding="utf-8")
        os.utime(claim, (old, old))
        return claim

    held = stale_claim("msg_a_abcdef12.json.4242.7.0.claim")       # live owner, ctime matches
    recycled = stale_claim("msg_b_abcdef12.json.4242.8.0.claim")   # pid recycled: ctime mismatch
    dead = stale_claim("msg_c_abcdef12.json.9999.1.0.claim")       # dead pid
    legacy_held = stale_claim("msg_d_abcdef12.json.4242.claim")    # legacy claim, live pid
    legacy_dead = stale_claim("msg_e_abcdef12.json.9999.claim")    # legacy claim, dead pid
    fresh = outbox / "msg_f_abcdef12.json.9999.1.0.claim"          # too young to rescue
    fresh.write_text(body, encoding="utf-8")
    # A claim whose message was already resolved is just cleaned up.
    done_claim = stale_claim("msg_g_abcdef12.json.9999.1.0.claim")
    (outbox / "done").mkdir(exist_ok=True)
    (outbox / "done" / "msg_g_abcdef12.json").write_text("{}", encoding="utf-8")

    registry._outbox_rescue_claims(outbox)

    assert held.exists(), "a live matching pid+ctime claim was stolen"
    assert legacy_held.exists(), "a live owner's legacy claim was stolen"
    assert fresh.exists(), "a fresh claim was rescued too early"
    assert not recycled.exists() and (outbox / "msg_b_abcdef12.json").exists()
    assert not dead.exists() and (outbox / "msg_c_abcdef12.json").exists()
    assert not legacy_dead.exists() and (outbox / "msg_e_abcdef12.json").exists()
    assert not done_claim.exists()


@pytest.mark.asyncio
async def test_outbox_claim_sweep_runs_while_loop_lives(bridge_home, monkeypatch):
    """The rescue sweep repeats inside the outbox loop, so a sibling crashing
    mid-claim hours after startup is still rescued without a restart."""
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_POLL_SEC", 0.05)
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_CLAIM_SWEEP_SEC", 0.1)
    monkeypatch.setattr("agent_bridge.registry.owner_alive", lambda pid, create_time=None: False)
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        outbox = bridge_home / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        name = "msg_late_abcdef12.json"
        claim = outbox / f"{name}.9999.1.0.claim"
        claim.write_text(
            json.dumps({"session_id": "sess_x", "message": "rescued"}), encoding="utf-8"
        )
        old = time.time() - 120
        os.utime(claim, (old, old))
        for _ in range(100):
            if not claim.exists():
                break
            await asyncio.sleep(0.05)
        assert not claim.exists(), "stale claim was never rescued while the loop ran"
        # Rescued back to a deliverable message (it then fails unknown_session).
        for _ in range(100):
            if (outbox / "done" / name).exists():
                break
            await asyncio.sleep(0.05)
        result = json.loads((outbox / "done" / name).read_text(encoding="utf-8"))
        assert result["error_code"] == "unknown_session"
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_outbox_claim_written_with_pid_ctime(bridge_home, monkeypatch):
    """New claims name the owning process: name.<pid>.<create_time>.claim."""
    monkeypatch.setattr("agent_bridge.registry.OUTBOX_POLL_SEC", 0.05)
    seen: list[str] = []
    real_deliver = Registry._outbox_deliver

    async def spy(self, outbox, claimed, name):
        seen.append(claimed.name)
        return await real_deliver(self, outbox, claimed, name)

    monkeypatch.setattr(Registry, "_outbox_deliver", spy)
    registry = Registry.create(bridge_home, owner_pid=4242, owner_create_time=7.5)
    await registry.start()
    try:
        outbox = bridge_home / "outbox"
        outbox.mkdir(exist_ok=True)
        (outbox / "msg_9_abcdef12.json").write_text(
            json.dumps({"session_id": "sess_x", "message": "hi"}), encoding="utf-8"
        )
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.05)
        assert seen == ["msg_9_abcdef12.json.4242.7.5.claim"]
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_server_status_reports_policy(bridge_home):
    registry = Registry.create(bridge_home)
    assert registry.server_status() == {
        "idle_exit_sec": 7200,
        "shutdown_policy": "cancel",
        "linger_max_sec": 86400,
        "remote_tasks": True,
    }
