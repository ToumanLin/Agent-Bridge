"""The MCP tools' public payloads: compact projections of Registry's dicts.

Registry keeps its rich internal snapshots (the dashboard, CLI, and tests read
those). Every MCP answer goes through here instead, so the coordinator only
sees fields it can act on: no process ids, no duplicate counters, no prose
hints, and no key whose value is null, false, or empty unless that value is
the answer (``available``, ``dispatch_enabled``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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
    available = bool(row.get("available"))
    name = row.get("agent")
    return compact(
        {
            "agent": name,
            "available": available,
            "detail": None if available else row.get("detail"),
            "note": AGENT_NOTES.get(str(name)),
            "quota": quota_view(row.get("quota")) if available else None,
        },
        keep=("available",),
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
            "agents": [agent_view(row) for row in agents],
        },
        keep=("dispatch_enabled", "agents"),
    )


def _stop(reason: Any) -> str | None:
    """end_turn is the normal case and says nothing; a pause is the MCP cancel."""
    if reason in {None, "end_turn"}:
        return None
    return "cancelled" if reason == "paused" else str(reason)


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
        usage = snapshot.get("run_usage") or None
        return compact(
            {
                "task_id": snapshot.get("task_id"),
                "session_id": snapshot.get("session_id"),
                "status": status,
                "owner": owner,
                "elapsed_sec": snapshot.get("elapsed_sec"),
                "silent_for_sec": snapshot.get("silent_for_sec"),
                "activity": snapshot.get("recent_activity"),
                "run_usage": usage,
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
            "stop_reason": _stop(snapshot.get("stop_reason")),
            "owner": owner,
            "error": snapshot.get("error") if failed else None,
            "error_kind": snapshot.get("error_kind") if failed else None,
            "resumable": bool(snapshot.get("resumable")),
            "model": snapshot.get("observed_model") or snapshot.get("model"),
            "effort": snapshot.get("observed_effort") or snapshot.get("effort"),
            "elapsed_sec": snapshot.get("elapsed_sec"),
            **_files(snapshot),
            "run_usage": snapshot.get("run_usage") or None,
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
            "continues": result.get("resumed_from"),
            "reused": bool(result.get("reused")),
            "warnings": warnings or [],
        }
    )


def task_row(row: Mapping[str, Any], session_titles: Mapping[str, str | None]) -> dict[str, Any]:
    """list_tasks row."""
    remote = bool(row.get("remote"))
    owner = None
    if remote:
        owner = "lost" if row.get("owner_lost") or not (row.get("owner") or {}).get("alive", True) else "sibling"
    return compact(
        {
            "task_id": row.get("task_id"),
            "session_id": row.get("session_id"),
            "agent": row.get("agent"),
            "title": session_titles.get(str(row.get("session_id"))),
            "status": row.get("status"),
            "stop_reason": _stop(row.get("stop_reason")),
            "owner": owner,
            "resumable": bool(row.get("resumable")),
            "resume_of": row.get("resume_of"),
            "resumed_by": row.get("resumed_by"),
            "created_at": row.get("created_at"),
        }
    )
