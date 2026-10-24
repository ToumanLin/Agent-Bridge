"""The MCP tools' public payloads: compact projections of Registry's dicts.

Registry keeps its rich internal snapshots (the dashboard, CLI, and tests read
those). Every MCP answer goes through here instead, so the coordinator only
sees fields it can act on: no process ids, no duplicate counters, no prose
hints, no echo of what the coordinator just sent, and no key whose value is
null, false, or empty unless that value is the answer (``dispatch_enabled``).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

# Per-worker facts that never change between calls. They used to ride on every
# result as hint prose; list_agents carries them once.
AGENT_NOTES = {
    "grok": "observed model is the live sampler; the worker's self-reported Grok version is not",
    "kimi": "an empty result with warnings is a failed turn, not a no-op",
    "opencode": "model/effort are the last values Bridge set, not a live sampler",
    "claude": "model/effort are the last values Bridge set, not a live sampler",
    "cursor": "model/effort are what Cursor confirmed after mapping, not a live sampler",
    "devin": "the effort level is part of the model id; effort is ignored",
    "codex": "model is a codex slug such as gpt-5.6-sol, not a display name; leave it unset for the default",
    "antigravity": "model is an agy slug such as gemini-3.7-flash; leave it unset for the default",
}

RUNNING = {"queued", "running"}


def compact(data: Mapping[str, Any], keep: Iterable[str] = ()) -> dict[str, Any]:
    """Drop null / false / empty values, except for keys in ``keep``."""
    kept = set(keep)
    return {
        key: value
        for key, value in data.items()
        if key in kept or not (value is None or value is False or value == [] or value == {} or value == "")
    }


def quota_view(quota: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not quota:
        return None
    status = quota.get("status") or "unknown"
    windows = [
        compact(
            {
                "name": window.get("name"),
                "remaining_percent": window.get("remaining_percent"),
                "resets_at": window.get("resets_at"),
            }
        )
        for window in quota.get("windows") or []
    ]
    return compact(
        {
            "status": status,
            "windows": windows,
            "balance": quota.get("balance"),
            "stale": bool(quota.get("stale")),
            # detail explains unknown/exhausted; on ok it is noise.
            "detail": quota.get("detail") if status != "ok" else None,
        }
    )


def agent_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """One usable worker. Missing ones are only named (agents_view.unavailable):
    their probe detail is an install hint for a person, not the coordinator."""
    name = row.get("agent")
    return compact(
        {
            "agent": name,
            "note": AGENT_NOTES.get(str(name)),
            "route": row.get("route"),
            "quota": quota_view(row.get("quota")),
        }
    )


def agents_view(
    agents: list[Mapping[str, Any]],
    env: Mapping[str, Any],
    coordinator: Mapping[str, Any],
) -> dict[str, Any]:
    return compact(
        {
            "mode": coordinator.get("mode"),
            "dispatch_enabled": bool(coordinator.get("dispatch_enabled")),
            "instructions": coordinator.get("instructions"),
            "warnings": list(env.get("warnings") or []),
            "agents": [agent_view(row) for row in agents if row.get("available")],
            "unavailable": [row.get("agent") for row in agents if not row.get("available")],
        },
        keep=("dispatch_enabled", "agents"),
    )


def stop_reason_view(reason: Any, status: Any) -> str | None:
    """end_turn is the normal case and says nothing; a pause is the MCP cancel.

    A reason that only repeats the status (cancelled/cancelled) says nothing either.
    """
    if reason in {None, "end_turn"}:
        return None
    reason = "cancelled" if reason == "paused" else str(reason)
    return None if reason == status else reason


def _owner(snapshot: Mapping[str, Any]) -> str | None:
    if not snapshot.get("remote"):
        return None
    if snapshot.get("owner_lost"):
        return "lost"
    owner = snapshot.get("owner") or {}
    return "sibling" if owner.get("alive", True) else "lost"


def _files(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    files = list(snapshot.get("files_changed") or [])
    total = snapshot.get("files_changed_total") or len(files)
    return compact({"files_changed": files, "files_more": max(0, total - len(files)) or None})


# Keys of a run_usage record the coordinator does not act on: bookkeeping, and
# the whole conversation's counters on a resumed run (this run's are the answer).
USAGE_META = {"scope", "v", "conversation_total"}


def usage_view(usage: Any) -> dict[str, Any] | None:
    """run_usage without its bookkeeping keys, or None when it counted nothing.

    A turn cancelled before the worker reported usage leaves only the
    bookkeeping keys behind; that block says nothing and is dropped. quality
    is shown only when it is an estimate; exact is the norm.
    """
    if not isinstance(usage, Mapping):
        return None
    view = {key: value for key, value in usage.items() if key not in USAGE_META}
    if view.get("quality") == "exact":
        del view["quality"]
    if not any(isinstance(value, (int, float)) and not isinstance(value, bool) for value in view.values()):
        return None
    return view


def failure_view(failure: Any) -> dict[str, Any] | None:
    """ACP failure diagnostics. prompt_may_have_been_sent stays even when false:
    it tells the coordinator whether the prompt could already have run."""
    if not isinstance(failure, Mapping):
        return None
    return compact(failure, keep=("prompt_may_have_been_sent",)) or None


def _resumable(row: Mapping[str, Any]) -> bool:
    """A row already continued by another task is not offered for resuming again."""
    return bool(row.get("resumable")) and not row.get("resumed_by")


def _warnings(snapshot: Mapping[str, Any]) -> list[str]:
    warnings = list(snapshot.get("warnings") or [])
    if snapshot.get("files_changed_truncated"):
        warnings.append("files_changed is truncated; run git status in cwd for the full set")
    return warnings


def task_view(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """wait_task / cancel_task answer for one task (local or sibling-owned)."""
    status = snapshot.get("status")
    owner = _owner(snapshot)
    if status in RUNNING:
        # Progress only: usage arrives once, with the finished answer.
        return compact(
            {
                "task_id": snapshot.get("task_id"),
                "session_id": snapshot.get("session_id"),
                "status": status,
                "owner": owner,
                "elapsed_sec": snapshot.get("elapsed_sec"),
                "silent_for_sec": snapshot.get("silent_for_sec"),
                "activity": snapshot.get("recent_activity"),
                # A queued/running row whose owner died can never finish.
                "resumable": bool(snapshot.get("resumable")) if owner == "lost" else None,
            }
        )
    failed = status == "failed"
    return compact(
        {
            "task_id": snapshot.get("task_id"),
            "session_id": snapshot.get("session_id"),
            "status": status,
            "stop_reason": stop_reason_view(snapshot.get("stop_reason"), status),
            "owner": owner,
            "error": snapshot.get("error") if failed else None,
            "error_kind": snapshot.get("error_kind") if failed else None,
            "failure": failure_view(snapshot.get("failure")) if failed else None,
            "resumable": _resumable(snapshot),
            "model": snapshot.get("observed_model") or snapshot.get("model"),
            "effort": snapshot.get("observed_effort") or snapshot.get("effort"),
            "elapsed_sec": snapshot.get("elapsed_sec"),
            **_files(snapshot),
            "run_usage": usage_view(snapshot.get("run_usage")),
            "warnings": _warnings(snapshot),
        }
    )


def result_view(page: Mapping[str, Any]) -> dict[str, Any]:
    """get_result answer: one page of text and where the next one starts."""
    return compact({"result": page.get("result_text") or "", "next_cursor": page.get("next_cursor")}, keep=("result",))


def with_result(view: dict[str, Any], page: Mapping[str, Any]) -> dict[str, Any]:
    """A terminal task_view plus the first result page, so most turns need no get_result."""
    return {**view, **result_view(page)}


def dispatch_view(result: Mapping[str, Any], warnings: list[str] | None = None) -> dict[str, Any]:
    return compact(
        {
            "task_id": result.get("task_id"),
            "session_id": result.get("session_id"),
            "reused": bool(result.get("reused")),
            "warnings": warnings or [],
        }
    )


def _seconds(stamp: Any) -> Any:
    """An ISO timestamp without its microseconds."""
    try:
        return datetime.fromisoformat(str(stamp)).replace(microsecond=0).isoformat()
    except ValueError:
        return stamp


def _same_dir(left: Any, right: str) -> bool:
    try:
        return os.path.normcase(Path(str(left)).resolve()) == os.path.normcase(Path(right).resolve())
    except (OSError, ValueError):
        return False


def task_row(row: Mapping[str, Any], title: str | None) -> dict[str, Any]:
    """list_tasks row. A continuation names its origin (resume_of); the
    origin's resumed_by would only say the same thing backwards."""
    remote = bool(row.get("remote"))
    owner = None
    if remote:
        owner = "lost" if row.get("owner_lost") or not (row.get("owner") or {}).get("alive", True) else "sibling"
    status = row.get("status")
    return compact(
        {
            "task_id": row.get("task_id"),
            "session_id": row.get("session_id"),
            "agent": row.get("agent"),
            "title": title,
            "status": status,
            "stop_reason": stop_reason_view(row.get("stop_reason"), status),
            "owner": owner,
            "resumable": _resumable(row),
            "resume_of": row.get("resume_of"),
            "created_at": _seconds(row.get("created_at")) if row.get("created_at") else None,
        }
    )


def tasks_view(
    rows: list[Mapping[str, Any]],
    session_titles: Mapping[str, str | None],
    *,
    limit: int,
    cwd: str | None = None,
) -> dict[str, Any]:
    """list_tasks answer: newest first, at most ``limit`` rows (0 = all),
    optionally only tasks run in ``cwd``. A session's title rides on its
    newest row only; older rows of the same session_id share it."""
    if cwd:
        rows = [row for row in rows if _same_dir(row.get("cwd"), cwd)]
    rows = sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)
    more = max(0, len(rows) - limit) if limit > 0 else 0
    if more:
        rows = rows[:limit]
    titled: set[str] = set()
    tasks = []
    for row in rows:
        session_id = str(row.get("session_id"))
        title = None if session_id in titled else session_titles.get(session_id)
        titled.add(session_id)
        tasks.append(task_row(row, title))
    return compact({"tasks": tasks, "tasks_more": more or None}, keep=("tasks",))
