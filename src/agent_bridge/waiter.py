"""``agent-bridge wait``: block a shell process, not a model turn, until tasks finish.

A coordinator that can run a shell command in the background and be woken
when it exits (Claude Code's background Bash) dispatches, starts this, and
ends its turn. Waiting costs no model calls; the exit wakes the coordinator.

It only reads ``state.json``: the Bridge instance that owns a task keeps
running it, and this process never touches a worker.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from agent_bridge.models import TERMINAL_STATUSES
from agent_bridge.paths import bridge_home, is_safe_id, state_path
from agent_bridge.persist import read_json
from agent_bridge.processes import owner_alive
from agent_bridge.public import compact, stop_reason_view

POLL_SEC = 1.0
# dispatch_task returns before its state.json flush lands, so a task the
# coordinator just started may be missing for a moment.
UNKNOWN_GRACE_SEC = 15.0

EXIT_DONE = 0
EXIT_UNKNOWN = 1
EXIT_TIMEOUT = 2

TERMINAL = {status.value for status in TERMINAL_STATUSES}
NEXT_STEP = "call wait_task(task_id) for each finished task to read its result"


def _rows(home: Path) -> dict[str, dict[str, Any]]:
    payload = read_json(state_path(home), {})
    if not isinstance(payload, dict):
        return {}
    return {
        row["task_id"]: row
        for row in payload.get("tasks") or []
        if isinstance(row, dict) and isinstance(row.get("task_id"), str)
    }


def _state(row: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """``(finished, summary)`` for one persisted task row."""
    status = str(row.get("status") or "unknown")
    if status in TERMINAL:
        return True, compact(
            {
                "task_id": row["task_id"],
                "status": status,
                "stop_reason": stop_reason_view(row.get("stop_reason"), status),
                "error": row.get("error") if status == "failed" else None,
            }
        )
    if not owner_alive(row.get("owner_pid"), row.get("owner_create_time")):
        # The Bridge running it is gone; the turn can never finish.
        return True, {"task_id": row["task_id"], "status": status, "owner": "lost"}
    return False, {"task_id": row["task_id"], "status": status}


def wait_for_tasks(
    task_ids: Sequence[str],
    *,
    home: Path | None = None,
    any_done: bool = False,
    timeout_sec: float | None = None,
    poll_sec: float = POLL_SEC,
    unknown_grace_sec: float = UNKNOWN_GRACE_SEC,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, dict[str, Any]]:
    """Wait until every task (or, with ``any_done``, one) is finished.

    Returns an exit code and the report printed as JSON.
    """
    ids = list(dict.fromkeys(task_ids))
    if not ids:
        raise ValueError("give at least one task id")
    bad = [task_id for task_id in ids if not is_safe_id(task_id)]
    if bad:
        raise ValueError(f"not a task id: {', '.join(bad)}")
    root = home or bridge_home()
    started = clock()
    while True:
        rows = _rows(root)
        done: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        unknown: list[str] = []
        for task_id in ids:
            row = rows.get(task_id)
            if row is None:
                unknown.append(task_id)
                continue
            finished, summary = _state(row)
            (done if finished else pending).append(summary)
        elapsed = clock() - started
        if unknown and elapsed >= unknown_grace_sec:
            return EXIT_UNKNOWN, compact({"unknown": unknown, "done": done, "pending": pending})
        if not unknown and (not pending or (any_done and done)):
            return EXIT_DONE, compact({"done": done, "pending": pending, "next": NEXT_STEP})
        if timeout_sec is not None and elapsed >= timeout_sec:
            return EXIT_TIMEOUT, compact({"timed_out": True, "done": done, "pending": pending, "unknown": unknown})
        sleep(poll_sec)


WAIT_HELP = """Usage: agent-bridge wait <task_id> [<task_id> ...] [--any] [--timeout SEC]

Blocks until the tasks finish, then prints one JSON report. Run it as a
background job so the coordinator's turn can end; its exit is the signal.
  --any          return as soon as one task finishes
  --timeout SEC  give up after SEC seconds (exit 2); default: no limit
Exit codes: 0 finished, 1 unknown task id, 2 timed out.
"""


def main(args: Sequence[str]) -> int:
    ids: list[str] = []
    any_done = False
    timeout: float | None = None
    items = list(args)
    while items:
        item = items.pop(0)
        if item in {"-h", "--help"}:
            print(WAIT_HELP, end="")
            return EXIT_DONE
        if item == "--any":
            any_done = True
        elif item == "--timeout":
            if not items:
                raise SystemExit("--timeout needs a number of seconds")
            timeout = float(items.pop(0))
        elif item.startswith("-"):
            raise SystemExit(f"unknown option: {item}\n{WAIT_HELP}")
        else:
            ids.append(item)
    try:
        code, report = wait_for_tasks(ids, any_done=any_done, timeout_sec=timeout)
    except ValueError as exc:
        raise SystemExit(f"{exc}\n{WAIT_HELP}") from exc
    print(json.dumps(report, ensure_ascii=False))
    return code
