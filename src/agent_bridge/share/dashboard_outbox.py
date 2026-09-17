"""Outbox queue inspection, state loading, and process-ownership helpers.

This is the outbox/state domain of ``share/dashboard.py``: queued chat
records under ``outbox/``, ``state.json`` reads, and the owner-identity
checks that gate the session header's task controls.

``dashboard.py`` re-exports every name here so its import surface is
unchanged. Path-dependent functions take ``outbox_dir`` / ``state_file``
(and the ``open`` callable) as explicit parameters: ``dashboard.OUTBOX_DIR``
and ``dashboard.STATE_FILE`` are rebound by ``--dir`` and monkeypatched in
tests, so the caller must pass its current value rather than letting this
module cache a stale copy. The ownership helpers likewise take the
``psutil`` module (or ``None``) from the caller — it is an optional
dependency that stays dynamically resolved on the dashboard module.
"""

import contextlib
import json
import os
import re

# SAFE_ID is defined in dashboard_events; importing it keeps one shared
# regex object across both helper modules (dashboard.SAFE_ID is that same
# object). The top-level fallback covers direct execution — running
# ``python share/dashboard.py`` imports this module with no package
# context, so the sibling resolves on sys.path instead.
if __package__:
    from .dashboard_events import SAFE_ID
else:
    from dashboard_events import SAFE_ID

# Queued dashboard chat records live in outbox/ as msg_*.json until the
# bridge claims them; task-control requests are req_{pause,cancel,resume}_*.
# Both name shapes are what /api/send_status may poll and what
# /api/dequeue may act on.
MSG_NAME_RE = re.compile(r"^msg_\d+_[0-9a-f]{8}\.json$")
REQ_NAME_RE = re.compile(r"^req_(?:pause|cancel|resume)_\d+_[0-9a-f]{8}\.json$")
# Wall-clock bounds mirrored from the bridge (registry.py): a queued message
# dies after a day; a task-action request no instance serves within this
# window resolves as expired instead of firing on a stale task state.
OUTBOX_MSG_MAX_AGE_SEC = 24 * 3600.0
TASK_ACTION_EXPIRE_SEC = 900.0

_MY_CREATE_TIME: float | None = None
_MY_CREATE_TIME_SET = False


def _my_create_time(psutil_mod=None):
    """This dashboard process's create time, for outbox claim/request stamps.

    Matches the owner-identity shape the bridge stamps on its own claims so
    the registry's stranded-claim rescue can restore a dashboard claim left
    behind by a mid-dequeue crash. Without psutil the stamp degrades to the
    legacy pid-only shape, which the bridge parses the same way.
    """
    global _MY_CREATE_TIME, _MY_CREATE_TIME_SET
    if not _MY_CREATE_TIME_SET:
        _MY_CREATE_TIME_SET = True
        if psutil_mod is not None:
            with contextlib.suppress(Exception):
                _MY_CREATE_TIME = psutil_mod.Process().create_time()
    return _MY_CREATE_TIME


def _owner_alive(pid, create_time, psutil_mod=None):
    """Read-only liveness of a recorded bridge owner — the same identity rule
    the registry applies (pid + process create time, so a recycled pid never
    passes). Used for button enablement and dead-owner reporting; the bridge
    re-checks ownership authoritatively when it serves the outbox request, so
    a wrong answer here only affects which controls look usable.

    Without psutil the check fails closed on Windows and falls back to a
    pid-exists signal on POSIX."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if psutil_mod is None:
        if os.name != "posix":
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    try:
        proc = psutil_mod.Process(pid)
        if not proc.is_running():
            return False
        if create_time is not None and abs(proc.create_time() - float(create_time)) >= 1.0:
            return False
    except (psutil_mod.Error, TypeError, ValueError, OSError):
        return False
    return True


def _dash_claim_name(name, psutil_mod=None):
    """``name.<pid>[.<create_time>].claim`` — the same atomic-rename claim the
    bridge uses, stamped with the dashboard's own owner identity."""
    create_time = _my_create_time(psutil_mod)
    if create_time is None:
        return f"{name}.{os.getpid()}.claim"
    return f"{name}.{os.getpid()}.{create_time}.claim"


def _task_action_fields(task, dead_sessions, psutil_mod=None):
    """(resumable, remote, owner_lost) for one persisted task row — the
    registry's control gates computed read-only against the recorded owner
    identity so the session header offers pause/cancel/resume only when the
    outbox route can serve them.

    Mirrors ``Registry._resume_fields``: an in-flight row with a live owner
    must be paused/cancelled before it can resume; a dead owner's in-flight
    row resumes through dead-owner adoption; completed rows and dead sessions
    are not resumable. The dashboard owns no rows — every live owner is a
    sibling from its point of view, so ``remote`` simply means "a live bridge
    owns this row".
    """
    status = task.get("status")
    active = status in ("queued", "running")
    alive = _owner_alive(task.get("owner_pid"), task.get("owner_create_time"), psutil_mod)
    if active:
        resumable = not alive
    else:
        resumable = (
            status in ("cancelled", "failed")
            and str(task.get("session_id") or "") not in dead_sessions
        )
    return resumable, bool(alive), bool(active and not alive)


def outbox_queue(outbox_dir):
    """Queued dashboard chat records, grouped by session.

    A ``msg_*.json`` still sitting in outbox/ is the queue the bridge has not
    claimed yet — it survives page refresh because it lives on disk. Claimed
    (*.claim) records are mid-delivery and deliberately absent: they can no
    longer be recalled, so the UI must never resurrect or delete them. A
    requeued record keeps its bridge-annotated state (waiting_busy /
    waiting_owner) so the card can say *why* it is still waiting.
    """
    queue: dict[str, list[dict]] = {}
    try:
        entries = sorted(outbox_dir.iterdir())
    except OSError:
        return queue
    for path in entries:
        if not path.is_file() or not MSG_NAME_RE.match(path.name):
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        session_id = str(rec.get("session_id") or "")
        if not SAFE_ID.match(session_id):
            continue
        queue.setdefault(session_id, []).append(
            {
                "name": path.name,
                "message": str(rec.get("message") or ""),
                "ts": rec.get("ts"),
                "state": rec.get("state") or "queued",
                "attempts": rec.get("attempts") or 0,
            }
        )
    for items in queue.values():
        items.sort(
            key=lambda item: (
                item["ts"] if isinstance(item["ts"], (int, float)) else 0,
                item["name"],
            )
        )
    return queue


def load_state(state_file, open_fn=open):
    try:
        with open_fn(state_file, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return {"sessions": [], "tasks": []}
