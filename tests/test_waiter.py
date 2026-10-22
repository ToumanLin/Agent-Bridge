"""agent-bridge wait: a shell-side wait that ends a coordinator's turn instead of holding it."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import psutil
import pytest

from agent_bridge.cli import main as cli_main
from agent_bridge.paths import state_path
from agent_bridge.persist import atomic_write_json
from agent_bridge.registry import Registry
from agent_bridge.waiter import EXIT_DONE, EXIT_TIMEOUT, EXIT_UNKNOWN, NEXT_STEP, wait_for_tasks

ME = (os.getpid(), psutil.Process().create_time())


def _dead_owner() -> tuple[int, float]:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    create_time = psutil.Process(proc.pid).create_time()
    proc.wait(timeout=30)
    return proc.pid, create_time


def _plant(home, *rows):
    atomic_write_json(state_path(home), {"sessions": [], "tasks": list(rows)})


def _row(task_id, status, owner=ME, **extra):
    return {"task_id": task_id, "status": status, "owner_pid": owner[0], "owner_create_time": owner[1], **extra}


class FakeClock:
    """Each sleep advances time and, optionally, rewrites state.json."""

    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.sleeps = 0
        self.on_sleep = on_sleep

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self.sleeps)


def _wait(home, ids, fake, **kwargs):
    return wait_for_tasks(ids, home=home, sleep=fake.sleep, clock=fake.clock, **kwargs)


def test_returns_once_every_task_is_finished(bridge_home):
    _plant(bridge_home, _row("t1", "running"), _row("t2", "running"))

    def finish(n):
        if n == 2:
            _plant(bridge_home, _row("t1", "completed", stop_reason="end_turn"), _row("t2", "running"))
        if n == 4:
            _plant(
                bridge_home,
                _row("t1", "completed", stop_reason="end_turn"),
                _row("t2", "failed", stop_reason="error", error="boom"),
            )

    fake = FakeClock(finish)
    code, report = _wait(bridge_home, ["t1", "t2"], fake)
    assert code == EXIT_DONE
    assert fake.sleeps == 4
    assert report == {
        "done": [
            {"task_id": "t1", "status": "completed"},
            {"task_id": "t2", "status": "failed", "stop_reason": "error", "error": "boom"},
        ],
        "next": NEXT_STEP,
    }


def test_any_returns_on_the_first_finished_task(bridge_home):
    _plant(bridge_home, _row("t1", "running"), _row("t2", "running"))
    fake = FakeClock(
        lambda n: _plant(bridge_home, _row("t1", "running"), _row("t2", "cancelled", stop_reason="paused"))
    )
    code, report = _wait(bridge_home, ["t1", "t2"], fake, any_done=True)
    assert code == EXIT_DONE
    assert report["done"] == [{"task_id": "t2", "status": "cancelled"}]
    assert report["pending"] == [{"task_id": "t1", "status": "running"}]


def test_running_task_of_a_dead_bridge_counts_as_finished(bridge_home):
    _plant(bridge_home, _row("t1", "running", owner=_dead_owner()))
    code, report = _wait(bridge_home, ["t1"], FakeClock())
    assert code == EXIT_DONE
    assert report["done"] == [{"task_id": "t1", "status": "running", "owner": "lost"}]


def test_timeout_reports_what_is_still_pending(bridge_home):
    _plant(bridge_home, _row("t1", "running"))
    code, report = _wait(bridge_home, ["t1"], FakeClock(), timeout_sec=3)
    assert code == EXIT_TIMEOUT
    assert report == {"timed_out": True, "pending": [{"task_id": "t1", "status": "running"}]}


def test_unknown_task_waits_out_the_flush_grace_then_fails(bridge_home):
    _plant(bridge_home)
    fake = FakeClock()
    code, report = _wait(bridge_home, ["t9"], fake, unknown_grace_sec=5)
    assert code == EXIT_UNKNOWN
    assert report == {"unknown": ["t9"]}
    assert fake.now >= 5


def test_task_that_appears_during_the_grace_is_waited_on(bridge_home):
    _plant(bridge_home)
    fake = FakeClock(lambda n: n == 2 and _plant(bridge_home, _row("t1", "completed")))
    code, report = _wait(bridge_home, ["t1"], fake, unknown_grace_sec=10)
    assert code == EXIT_DONE
    assert report["done"] == [{"task_id": "t1", "status": "completed"}]


def test_rejects_bad_ids(bridge_home):
    with pytest.raises(ValueError, match="at least one"):
        wait_for_tasks([], home=bridge_home)
    with pytest.raises(ValueError, match="not a task id"):
        wait_for_tasks(["../state"], home=bridge_home)


def test_cli_prints_one_json_report_and_exits_with_its_code(bridge_home, capsys):
    _plant(bridge_home, _row("t1", "completed"), _row("t2", "running"))
    with pytest.raises(SystemExit) as done:
        cli_main(["wait", "t1"])
    assert done.value.code == EXIT_DONE
    assert json.loads(capsys.readouterr().out)["done"] == [{"task_id": "t1", "status": "completed"}]

    with pytest.raises(SystemExit) as timed_out:
        cli_main(["wait", "t2", "--timeout", "0"])
    assert timed_out.value.code == EXIT_TIMEOUT
    assert json.loads(capsys.readouterr().out)["timed_out"] is True

    with pytest.raises(SystemExit, match="unknown option"):
        cli_main(["wait", "t1", "--forever"])


@pytest.mark.asyncio
async def test_waits_for_a_real_task_without_touching_the_registry(bridge_home, tmp_path, monkeypatch):
    """The owning Bridge runs the task; the waiter only watches state.json."""
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "1.0")
    work = tmp_path / "work"
    work.mkdir()
    registry = Registry.create(bridge_home)
    registry.config.dashboard.enabled = False
    await registry.start()
    try:
        sent = await registry.dispatch_task("fake", "hello", cwd=str(work.resolve()))
        code, report = await asyncio.to_thread(
            wait_for_tasks, [sent["task_id"]], home=bridge_home, poll_sec=0.1, timeout_sec=30
        )
        assert code == EXIT_DONE
        assert report["done"] == [{"task_id": sent["task_id"], "status": "completed"}]
        assert registry.tasks[sent["task_id"]].status.value == "completed"
    finally:
        await registry.stop()
