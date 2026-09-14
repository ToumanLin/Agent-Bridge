from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import psutil

from agent_bridge.adapters import build_adapter
from agent_bridge.adapters.antigravity import check_agy_model_effort
from agent_bridge.adapters.base import Adapter
from agent_bridge.config import (
    COORDINATOR_MODE_HINTS,
    AgentConfig,
    AppConfig,
    load_config,
    normalize_coordinator_mode,
    write_coordinator_overlay,
)
from agent_bridge.grok_observe import observe_grok_session
from agent_bridge.kimi_observe import observe_kimi_session
from agent_bridge.models import (
    DEFAULT_WAIT_SEC,
    TERMINAL_STATUSES,
    ProcState,
    Session,
    Task,
    TaskStatus,
    iso,
    normalize_effort,
)
from agent_bridge.paths import ensure_home, is_safe_id, result_path, state_path, transcript_path
from agent_bridge.persist import atomic_write_json, atomic_write_text, read_json, read_json_strict
from agent_bridge.probes import probe_agent
from agent_bridge.processes import (
    count_sibling_servers,
    owner_alive,
    process_create_time,
    reap_orphan_for_session,
    reap_orphans,
)
from agent_bridge.quota import (
    QuotaCache,
    classify_error,
    fetch_quota,
    looks_like_quota_error,
    looks_like_transient_error,
    provider_table,
    unknown_quota,
)
from agent_bridge.transcript import (
    append_event,
    flush_pending,
    flush_session,
    forget_worker_activity,
    mark_worker_activity,
    page_events,
    read_events,
    read_events_tail,
    recent_activity,
    worker_silence_sec,
)
from agent_bridge.worker_env import build_worker_env, describe_env, install_host_env, is_worker_context
from agent_bridge.workspace import merge_files_changed, snapshot_workspace

log = logging.getLogger(__name__)

RuntimeContext = Literal["coordinator", "worker"]
NESTED_DISPATCH_ERROR = (
    "nested dispatch is disabled: this Agent Bridge instance was inherited inside a worker process"
)
NESTED_PREFERENCES_ERROR = (
    "preference updates are disabled: this Agent Bridge instance was inherited inside a worker process"
)
NESTED_CANCEL_ERROR = (
    "task cancellation is disabled: this Agent Bridge instance was inherited inside a worker process"
)
NESTED_END_SESSION_ERROR = (
    "session shutdown is disabled: this Agent Bridge instance was inherited inside a worker process"
)
NESTED_PAUSE_ERROR = (
    "task pause is disabled: this Agent Bridge instance was inherited inside a worker process"
)
NESTED_RESUME_ERROR = (
    "task resume is disabled: this Agent Bridge instance was inherited inside a worker process"
)

RESULT_TAIL = 6000
# Explicit get_result calls can read up to this many characters per page.
RESULT_PAGE_MAX_CHARS = 60000
# get_result decodes at most this many characters per read() while locating a
# page window, so paging never materializes the whole artifact.
RESULT_READ_CHUNK_CHARS = 65536
# Retained only as a fallback if the one-time result artifact write fails.
RESULT_STORE_MAX = 30000
# Terminal tasks kept per session; older ones are pruned so state.json does
# not grow without bound over a long-lived Bridge.
TASK_KEEP_PER_SESSION = 20
FILES_CHANGED_MAX = 200
SESSION_KEEP_INACTIVE = 50
SESSION_RETAIN_SEC = 14 * 86400
TASK_KEEP_TOTAL = 200
STOP_TASK_GRACE_SEC = 15
STALL_POLL_SEC = 30
STALL_CANCEL_GRACE_SEC = 15
OUTBOX_POLL_SEC = 1.0
# How long a requeued message waits before the next claim attempt.
OUTBOX_BUSY_RETRY_SEC = 2.0
OUTBOX_FOREIGN_RETRY_SEC = 10.0
# Wall-clock lifetime of a queued dashboard message. A message behind a busy
# session (or a live foreign owner) is requeued until it can be delivered; it
# is only dropped once its total queue age passes this deadline. Kept long so
# user-typed messages survive even very long turns.
OUTBOX_MAX_AGE_SEC = 24 * 3600.0
# A *.claim file older than this is a stranded rename left by a dead bridge;
# the outbox loop restores it to a plain message so it can be delivered.
OUTBOX_CLAIM_STALE_SEC = 60.0
# How often the running outbox loop re-sweeps for stranded claims — the
# startup-only rescue used to miss siblings that crashed mid-claim later.
OUTBOX_CLAIM_SWEEP_SEC = 60.0
# Poll cadence for wait_task on a sibling-owned (remote) task row.
REMOTE_TASK_POLL_SEC = 1.5
# pause_task first lets the adapter's own cancel land the partial result
# through the normal run_turn return path; only a turn still running after
# this window is force-cancelled.
PAUSE_GRACE_SEC = 10.0
# resume_task continuation prompt when the caller gives no message: explicit
# enough that any worker can pick the paused work back up safely.
RESUME_DEFAULT_MESSAGE = (
    "This conversation's previous turn was paused before it finished. Pick up "
    "the work where it left off, complete the remaining steps, and report the "
    "final result."
)
# resume_task delegation across Bridge instances rides the shared outbox: a
# req_resume_*.json record is executed only by the instance owning the task
# row (live siblings requeue it for the owner, a dead owner's row is adopted
# by the claimer), and its dispatch result lands in outbox/done/ for the
# caller to relay. The caller also watches the task row's resumed_by so a
# lost done record cannot hide a dispatched continuation.
RESUME_DELEGATE_TIMEOUT_SEC = 60.0
RESUME_DELEGATE_POLL_SEC = 0.5
# After the recorded owner dies mid-delegation, a sibling may already be
# mid-delivery of the queued resume; wait this long for its done record or
# the resumed_by stamp before adopting the row and resuming locally.
RESUME_FALLBACK_GRACE_SEC = 5.0
# done/req_* records a dead requester never consumed are swept after this.
RESUME_DONE_RETAIN_SEC = 3600.0


def _parse_outbox_claim(name: str) -> tuple[str, int | None, float | None]:
    """Split ``name.<pid>[.<create_time>].claim`` into (base, pid, create_time).

    New claims carry the owner bridge's pid + process create time so a
    recycled pid cannot pass for a live owner; legacy ``name.<pid>.claim``
    parses with create_time None and falls back to pid-only liveness.
    """
    stem = name[: -len(".claim")] if name.endswith(".claim") else name
    # create_time is a float whose repr itself contains a dot, so the new
    # shape ends in THREE numeric tail segments; try the widest parse first.
    parts = stem.rsplit(".", 3)
    if len(parts) == 4:
        try:
            return parts[0], int(parts[1]), float(f"{parts[2]}.{parts[3]}")
        except ValueError:
            pass
    parts = stem.rsplit(".", 2)
    if len(parts) == 3:
        try:
            return parts[0], int(parts[1]), float(parts[2])
        except ValueError:
            pass
    head, _, pid_text = stem.rpartition(".")
    try:
        return head, int(pid_text), None
    except ValueError:
        pass
    return stem, None, None


def _read_file_tail(path: Path, limit: int = RESULT_TAIL) -> str:
    """Last ``limit`` bytes of a UTF-8 file, decoded — mirrors ``_tail``."""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - limit))
        return fh.read().decode("utf-8", errors="ignore")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _resolve_runtime_context(runtime_context: RuntimeContext | None) -> RuntimeContext:
    if runtime_context is None:
        return "worker" if is_worker_context() else "coordinator"
    if runtime_context not in ("coordinator", "worker"):
        raise ValueError(f"unknown runtime_context {runtime_context!r}; use coordinator or worker")
    return runtime_context


def _partial_run_usage(events: list[dict[str, Any]], started_at: str) -> dict[str, Any]:
    """The last ``usage`` event's ``consumed`` snapshot at/after ``started_at``.

    Events older than ``started_at`` belong to a prior run on this reusable
    session. Only the most recent usage event is consulted — it already
    carries the run's accumulated consumption.
    """
    for event in reversed(events):
        if event.get("type") != "usage":
            continue
        if str(event.get("ts") or "") < started_at:
            break
        consumed = (event.get("data") or {}).get("consumed")
        if isinstance(consumed, dict) and consumed:
            return consumed
        break
    return {}


def _session_last_active_ts(last_active_at: str) -> float:
    try:
        return datetime.fromisoformat(last_active_at).timestamp()
    except (ValueError, TypeError, OSError, OverflowError):
        return 0.0


def _tail(text: str, limit: int = RESULT_TAIL) -> str:
    if len(text.encode("utf-8")) <= limit:
        return text
    encoded = text.encode("utf-8")
    return encoded[-limit:].decode("utf-8", errors="ignore")


def _read_result_window(path: Path, cursor: int, max_chars: int) -> str:
    """Decode only the ``[cursor, cursor + max_chars)`` character window of a
    UTF-8 result artifact. Character cursors cannot be byte-seeked, so the
    skip is chunked: a page never holds more than one extra chunk in memory."""
    with path.open("r", encoding="utf-8") as fh:
        remaining = cursor
        while remaining > 0:
            skipped = fh.read(min(remaining, RESULT_READ_CHUNK_CHARS))
            if not skipped:
                break
            remaining -= len(skipped)
        parts: list[str] = []
        needed = max_chars
        while needed > 0:
            chunk = fh.read(needed)
            if not chunk:
                break
            parts.append(chunk)
            needed -= len(chunk)
        return "".join(parts)


class Registry:
    def __init__(
        self,
        home: Path,
        config: AppConfig,
        *,
        owner_pid: int | None = None,
        owner_create_time: float | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> None:
        self.home = home
        self.config = config
        self.sessions: dict[str, Session] = {}
        self.tasks: dict[str, Task] = {}
        self._requests: dict[str, tuple[tuple, str]] = {}
        self._adapters: dict[str, Adapter] = {}
        self._done: dict[str, asyncio.Event] = {}
        # Serializes the resumed_by-check + dispatch section of a resume on
        # one task: the outbox delivery of a delegated request and a local
        # fallback can reach _resume_owned concurrently, and only one may
        # dispatch a continuation.
        self._resume_locks: dict[str, asyncio.Lock] = {}
        self._idle: dict[str, asyncio.Task[None]] = {}
        self._bg: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._last_activity = time.monotonic()
        self._watchdog: asyncio.Task[None] | None = None
        self._outbox_task: asyncio.Task[None] | None = None
        self._owner_pid = os.getpid() if owner_pid is None else owner_pid
        self._owner_create_time = (
            process_create_time(os.getpid()) if owner_create_time is None else owner_create_time
        )
        self.runtime_context = _resolve_runtime_context(runtime_context)
        self.dispatch_enabled = self.runtime_context == "coordinator"
        self._sibling_cache: tuple[float, int] | None = None
        self._quota_cache = QuotaCache(config.quota.cache_sec)
        self._stopping = False
        self._stop_started = False
        self._stop_done = asyncio.Event()
        # Set when a stop() caller is cancelled mid-teardown: the shutdown
        # body watches it to abandon the linger wait early instead of sitting
        # out linger_max_sec.
        self._stop_interrupted = asyncio.Event()
        self._pending_state: dict[str, list[dict]] | None = None
        self._flush_task: asyncio.Task[None] | None = None

    @classmethod
    def create(
        cls,
        home: Path | None = None,
        config: AppConfig | None = None,
        *,
        owner_pid: int | None = None,
        owner_create_time: float | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> Registry:
        resolved = ensure_home(home)
        return cls(
            resolved,
            config or load_config(resolved),
            owner_pid=owner_pid,
            owner_create_time=owner_create_time,
            runtime_context=runtime_context,
        )

    def _stamp_owner(self, record: Session | Task) -> None:
        record.owner_pid = self._owner_pid
        record.owner_create_time = self._owner_create_time

    def _is_mine(self, owner_pid: int | None, owner_create_time: float | None) -> bool:
        return owner_pid == self._owner_pid and owner_create_time == self._owner_create_time

    def _foreign_live(self, owner_pid: int | None, owner_create_time: float | None) -> bool:
        if owner_pid is None and owner_create_time is None:
            return False
        if self._is_mine(owner_pid, owner_create_time):
            return False
        return owner_alive(owner_pid, owner_create_time)

    def _merge_owned(
        self,
        disk_rows: list,
        mine: dict[str, dict],
        id_key: str,
    ) -> list[dict]:
        merged: dict[str, dict] = {}
        for raw in disk_rows:
            if not isinstance(raw, dict):
                continue
            key = raw.get(id_key)
            if not isinstance(key, str):
                continue
            owner_pid = raw.get("owner_pid")
            owner_create_time = raw.get("owner_create_time")
            if self._is_mine(owner_pid, owner_create_time):
                continue
            if self._foreign_live(owner_pid, owner_create_time) or key not in mine:
                merged[key] = raw
        merged.update(mine)
        return list(merged.values())

    def _own_rows(self) -> dict[str, list[dict]]:
        return {
            "sessions": [s.model_dump(mode="json") for s in self.sessions.values()],
            "tasks": [t.model_dump(mode="json") for t in self.tasks.values()],
        }

    def _write_state(self, own: dict[str, list[dict]]) -> None:
        # Live siblings may interleave a read-merge-write; each instance only
        # rewrites its own records, so the next save converges. Uses the
        # snapshot in `own` plus owner identity, never self.sessions / self.tasks.
        path = state_path(self.home)
        # A locked state.json must not read as empty — the merge would drop
        # every live sibling row. Surface the denial: the flush loop logs it
        # and the next save() converges.
        merged = None
        for _ in range(4):
            disk = read_json_strict(path, {})
            if not isinstance(disk, dict):
                disk = {}
            merged = {
                "sessions": self._merge_owned(
                    disk.get("sessions") or [],
                    {row["session_id"]: row for row in own["sessions"]},
                    "session_id",
                ),
                "tasks": self._merge_owned(
                    disk.get("tasks") or [],
                    {row["task_id"]: row for row in own["tasks"]},
                    "task_id",
                ),
            }
            # Close the read-merge-write window: a sibling write landing
            # between our read and our replace would be clobbered by a merge
            # built on the stale read. Re-read just before writing; when the
            # file still matches, the merge basis is current.
            if read_json_strict(path, {}) == disk:
                break
        atomic_write_json(path, merged)

    def save(self) -> None:
        self._pending_state = self._own_rows()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            own, self._pending_state = self._pending_state, None
            self._write_state(own)
            return
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = loop.create_task(self._flush_state_loop(), name="state-flush")

    async def _flush_state_loop(self) -> None:
        # No await between seeing _pending_state is None and returning, so
        # save() cannot observe a still-running flush task and skip creating
        # a new one after this loop has already decided to exit.
        while self._pending_state is not None:
            own, self._pending_state = self._pending_state, None
            try:
                await asyncio.to_thread(self._write_state, own)
            except Exception:
                # A failed write must not kill the flush task: stop() awaits
                # it, and the next save() has to be able to retry.
                log.exception("could not write state.json")

    async def flush_state(self) -> None:
        """Wait until every save() so far is on disk."""
        task = self._flush_task
        if task is not None and not task.done():
            await task

    def touch_activity(self) -> None:
        self._last_activity = time.monotonic()

    def idle_exit_due(self) -> bool:
        idle_sec = self.config.server.idle_exit_sec
        if idle_sec <= 0:
            return False
        if time.monotonic() - self._last_activity < idle_sec:
            return False
        return all(
            task.status not in {TaskStatus.queued, TaskStatus.running} for task in self.tasks.values()
        )

    async def _idle_exit_watchdog(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                if not self.idle_exit_due():
                    continue
                idle_sec = self.config.server.idle_exit_sec
                log.info(
                    "no MCP activity for %s seconds and no queued/running tasks; self-exiting",
                    idle_sec,
                )
                # Clear before stop() so a CancelledError from stop cancelling
                # this task cannot skip os._exit once the exit decision is made.
                self._watchdog = None
                try:
                    await self.stop()
                finally:
                    os._exit(0)
        except asyncio.CancelledError:
            raise

    async def _outbox_loop(self) -> None:
        """Deliver dashboard chat messages dropped into ``home/outbox``.

        Each message is a ``msg_*.json`` file ``{session_id, message}``.
        Claiming is an atomic rename; only the instance owning the session
        keeps the file — siblings requeue it so the right owner picks it up.
        Delivery results land in ``outbox/done/`` for the dashboard to read.
        """
        outbox = self.home / "outbox"
        outbox.mkdir(exist_ok=True)
        self._outbox_rescue_claims(outbox)
        last_sweep = time.monotonic()
        cooldown: dict[str, float] = {}
        while True:
            await asyncio.sleep(OUTBOX_POLL_SEC)
            now = time.monotonic()
            if now - last_sweep >= OUTBOX_CLAIM_SWEEP_SEC:
                # A sibling can crash mid-claim at any time, not only before
                # this instance started — rescue stale claims periodically.
                last_sweep = now
                self._outbox_rescue_claims(outbox)
                self._outbox_sweep_done(outbox)
            for name, until in list(cooldown.items()):
                if until <= now:
                    del cooldown[name]
            try:
                names = sorted(
                    p.name
                    for p in outbox.iterdir()
                    if p.is_file() and p.suffix == ".json" and p.name not in cooldown
                )
            except OSError:
                continue
            for name in names:
                claimed = outbox / self._outbox_claim_name(name)
                try:
                    os.replace(outbox / name, claimed)
                except OSError:
                    continue
                try:
                    retry_after = await self._outbox_deliver(outbox, claimed, name)
                except Exception:
                    log.exception("outbox delivery failed for %s", name)
                    retry_after = OUTBOX_BUSY_RETRY_SEC
                if retry_after is None:
                    claimed.unlink(missing_ok=True)
                else:
                    with contextlib.suppress(OSError):
                        os.replace(claimed, outbox / name)
                    cooldown[name] = time.monotonic() + retry_after

    def _outbox_done(self, outbox: Path, name: str, payload: dict) -> None:
        done_dir = outbox / "done"
        done_dir.mkdir(exist_ok=True)
        with contextlib.suppress(OSError):
            atomic_write_json(done_dir / name, payload)

    def _outbox_claim_name(self, name: str) -> str:
        """Claim file name encoding this instance's owner identity.

        ``name.<pid>.<create_time>.claim`` lets a rescuer detect a recycled
        pid (create_time mismatch ⇒ dead owner); the dashboard glob
        ``name.*.claim`` still matches. Without a known create time the
        legacy ``name.<pid>.claim`` shape keeps pid-only liveness.
        """
        if self._owner_create_time is None:
            return f"{name}.{self._owner_pid}.claim"
        return f"{name}.{self._owner_pid}.{self._owner_create_time}.claim"

    def _outbox_rescue_claims(self, outbox: Path) -> None:
        """Restore stranded ``*.claim`` renames left by a crashed bridge.

        A crash between the atomic ``name -> name.<pid>[.<ctime>].claim``
        rename and the requeue/done step used to drop the message silently:
        the scan filters on ``*.json`` and never looks back. Stale claims
        whose owning pid (+ create time, when encoded) is dead are renamed
        back to ``msg_*.json`` — or deleted when a ``done/`` result was
        already recorded. Runs at outbox-loop start and periodically
        afterwards; a claim whose owner is verifiably alive is never taken.
        """
        cutoff = time.time() - OUTBOX_CLAIM_STALE_SEC
        try:
            claims = [p for p in outbox.iterdir() if p.is_file() and p.name.endswith(".claim")]
        except OSError:
            return
        for claim in claims:
            try:
                if claim.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            base, pid, create_time = _parse_outbox_claim(claim.name)
            if (outbox / "done" / base).exists() or (outbox / base).exists():
                claim.unlink(missing_ok=True)
                continue
            if pid and owner_alive(pid, create_time):
                continue  # another live bridge still holds this claim
            try:
                os.replace(claim, outbox / base)
                log.info("outbox rescued stranded claim %s", claim.name)
            except OSError:
                log.warning("could not rescue outbox claim %s", claim.name)

    def _outbox_sweep_done(self, outbox: Path) -> None:
        """Drop delegated-resume answers whose requester never read them.

        ``done/msg_*`` records are consumed by the dashboard's status poll;
        ``done/req_*`` records are consumed by the delegating Bridge — which
        may die first — so the sweep removes them past a retention age.
        """
        done_dir = outbox / "done"
        cutoff = time.time() - RESUME_DONE_RETAIN_SEC
        try:
            stale = [
                path
                for path in done_dir.iterdir()
                if path.is_file()
                and path.name.startswith("req_")
                and path.stat().st_mtime < cutoff
            ]
        except OSError:
            return
        for path in stale:
            with contextlib.suppress(OSError):
                path.unlink()

    def _lookup_dead_session(self, session_id: str) -> tuple[Session | None, bool]:
        """(session, foreign) — read-only lookup of a disk session row.

        A row whose owner bridge is verifiably alive is foreign: it stays
        untouched and the caller must not adopt it. Dead-owner and ownerless
        rows come back as a validated Session, not yet inserted.
        """
        disk = read_json(state_path(self.home), {})
        for raw in (disk or {}).get("sessions") or []:
            if not isinstance(raw, dict) or raw.get("session_id") != session_id:
                continue
            if self._foreign_live(raw.get("owner_pid"), raw.get("owner_create_time")):
                return None, True
            try:
                return Session.model_validate(raw), False
            except Exception:
                return None, False
        return None, False

    async def _adopt_dead_session(self, session_id: str) -> tuple[Session | None, bool]:
        """(session, foreign) — adopt a session whose owner bridge is gone.

        The session's recorded orphan worker is reaped first so a follow-up
        spawn never leaves two executors driving one native conversation.
        Used by the outbox delivery path, dispatch_task follow-ups,
        resume_task, end_session, and cancel_task adoption.
        """
        session, foreign = self._lookup_dead_session(session_id)
        if session is None:
            return None, foreign
        reaped = await asyncio.to_thread(reap_orphan_for_session, self.home, session_id)
        if reaped is not None:
            log.warning(
                "reaped orphan worker pid=%s before adopting session %s",
                reaped,
                session_id,
            )
        self._stamp_owner(session)
        if session.proc_state in {ProcState.busy, ProcState.spawning, ProcState.ready}:
            session.proc_state = ProcState.idle_unloaded
        session.pid = None
        self.sessions[session.session_id] = session
        self.save()
        return session, False

    def _recover_run_usage(self, task: Task) -> None:
        """Recover a dead run's partial consumption from the transcript tail.

        A task being finalized as ``bridge_restarted`` never got its final
        run_usage write; whatever usage events the dead owner flushed (or a
        same-process caller buffered) are the best record left. Marked
        ``partial`` so it is never mistaken for a complete run total.
        """
        if task.run_usage or not task.started_at or not is_safe_id(task.session_id):
            return
        consumed = _partial_run_usage(
            read_events_tail(task.session_id, self.home), task.started_at
        )
        if consumed:
            task.run_usage = {**consumed, "partial": True}

    def _disk_task_row(self, task_id: str) -> dict | None:
        """The raw persisted row for ``task_id``, whatever its owner.

        Uses the strict read: callers that decide adoption or dispatch must
        distinguish "row absent" from "state locked" — a phantom empty read
        would make a live foreign row look adoptable or gone.
        """
        if not is_safe_id(task_id):
            return None
        payload = read_json_strict(state_path(self.home), {})
        if not isinstance(payload, dict):
            return None
        for raw in payload.get("tasks") or []:
            if isinstance(raw, dict) and raw.get("task_id") == task_id:
                return raw
        return None

    def _adopt_dead_task(self, task_id: str) -> tuple[Task | None, bool]:
        """(task, foreign) — take over a dead owner's task row on disk.

        Mirrors start()-time adoption for a single row so control operations
        (resume_task, cancel_task) work when the owner died after this
        instance booted: a live-foreign row is never touched, an adopted row
        still queued/running finalizes as failed/bridge_restarted, and the
        row lands in memory stamped with this instance's owner identity.
        """
        if not self.config.server.remote_tasks:
            return None, False
        raw = self._disk_task_row(task_id)
        if raw is None:
            return None, False
        if self._is_mine(raw.get("owner_pid"), raw.get("owner_create_time")):
            return None, False
        if self._foreign_live(raw.get("owner_pid"), raw.get("owner_create_time")):
            return None, True
        try:
            task = Task.model_validate(raw)
        except Exception:
            return None, False
        # session_id becomes a transcript path component downstream.
        if not is_safe_id(task.session_id):
            return None, False
        self._stamp_owner(task)
        if task.status in {TaskStatus.queued, TaskStatus.running}:
            if task.paused:
                # The pause intent was persisted before the owner died;
                # honor it — the row ends cancelled/paused, not failed.
                task.status = TaskStatus.cancelled
                task.stop_reason = "paused"
            else:
                task.status = TaskStatus.failed
                task.error = "bridge_restarted"
            task.finished_at = iso()
            self._recover_run_usage(task)
        self.tasks[task.task_id] = task
        done = asyncio.Event()
        done.set()
        self._done[task.task_id] = done
        self.save()
        log.info(
            "task_adopted task_id=%s session_id=%s status=%s",
            task.task_id,
            task.session_id,
            task.status.value,
        )
        return task, False

    async def _outbox_deliver(self, outbox: Path, claimed: Path, name: str) -> float | None:
        rec = read_json(claimed, {})
        if not isinstance(rec, dict):
            rec = {}
        if rec.get("kind") == "resume":
            return await self._outbox_resume(outbox, claimed, name, rec)
        session_id = str(rec.get("session_id") or "")
        text = str(rec.get("message") or "").strip()
        if not session_id or not text:
            self._outbox_done(
                outbox,
                name,
                {"ok": False, "state": "error", "error": "missing session_id or message", "error_code": "invalid_record"},
            )
            return None
        # Queue age counts from the dashboard's enqueue timestamp. Senders that
        # omit it get stamped once here so the wall-clock deadline still applies.
        queued_ts = rec.get("ts")
        if not isinstance(queued_ts, (int, float)):
            queued_ts = time.time()
            rec["ts"] = queued_ts
        if time.time() - queued_ts > OUTBOX_MAX_AGE_SEC:
            self._outbox_done(
                outbox,
                name,
                {
                    "ok": False,
                    "state": "error",
                    "error": "message expired in queue (24h limit); nothing was delivered",
                    "error_code": "expired",
                },
            )
            return None
        session = self.sessions.get(session_id)
        foreign = False
        if session is None:
            session, foreign = await self._adopt_dead_session(session_id)
        if session is None:
            if foreign:
                # Requeued without an attempts increment, but the persisted
                # ts still bounds total queue age via the check above.
                rec["state"] = "waiting_owner"
                with contextlib.suppress(OSError):
                    atomic_write_json(claimed, rec)
                return OUTBOX_FOREIGN_RETRY_SEC
            self._outbox_done(
                outbox,
                name,
                {"ok": False, "state": "error", "error": f"unknown session {session_id}", "error_code": "unknown_session"},
            )
            return None
        try:
            result = await self.dispatch_task(
                agent=session.agent,
                message=text,
                cwd=session.cwd,
                session_id=session.session_id,
                user_requested=True,
                source="dashboard",
            )
        except RuntimeError as exc:
            if "is busy with" not in str(exc):
                self._outbox_done(
                    outbox, name, {"ok": False, "state": "error", "error": str(exc), "error_code": "dispatch_failed"}
                )
                return None
            rec["attempts"] = int(rec.get("attempts") or 0) + 1
            rec["state"] = "waiting_busy"
            with contextlib.suppress(OSError):
                atomic_write_json(claimed, rec)
            return OUTBOX_BUSY_RETRY_SEC
        except Exception as exc:
            self._outbox_done(
                outbox,
                name,
                {"ok": False, "state": "error", "error": f"{type(exc).__name__}: {exc}", "error_code": "dispatch_error"},
            )
            return None
        self._outbox_done(
            outbox,
            name,
            {
                "ok": True,
                "state": "dispatched",
                "task_id": result.get("task_id"),
                "session_id": session.session_id,
            },
        )
        log.info(
            "outbox_dispatch name=%s session_id=%s task_id=%s",
            name,
            session.session_id,
            result.get("task_id"),
        )
        return None

    async def _outbox_resume(self, outbox: Path, claimed: Path, name: str, rec: dict) -> float | None:
        """Execute a delegated ``resume_task`` for the instance owning the task.

        Non-owner bridges enqueue ``req_resume_*.json`` records; the instance
        that owns the task row runs the normal resume path and reports through
        ``done/`` for the requester to relay. A row owned by a live sibling is
        requeued for that sibling (single-owner safety is preserved — the
        claimer never drives it); a dead owner's row is adopted and resumed by
        whichever live instance claims the record first. Records past their
        ``expire_ts`` (the requester's bounded wait) resolve as expired so a
        caller that already gave up cannot dispatch a surprise continuation.
        """
        task_id = str(rec.get("task_id") or "")
        if not is_safe_id(task_id):
            self._outbox_done(
                outbox,
                name,
                {"ok": False, "state": "error", "error": "missing or invalid task_id", "code": "invalid_record"},
            )
            return None
        queued_ts = rec.get("ts")
        if not isinstance(queued_ts, (int, float)):
            queued_ts = time.time()
            rec["ts"] = queued_ts
        expire_ts = rec.get("expire_ts")
        if not isinstance(expire_ts, (int, float)):
            expire_ts = queued_ts + OUTBOX_MAX_AGE_SEC
        if time.time() > expire_ts or time.time() - queued_ts > OUTBOX_MAX_AGE_SEC:
            self._outbox_done(
                outbox,
                name,
                {
                    "ok": False,
                    "state": "error",
                    "error": "delegated resume expired in queue; nothing was dispatched",
                    "code": "expired",
                },
            )
            return None
        task = self.tasks.get(task_id)
        if task is None:
            row = self._disk_task_row(task_id)
            if row is not None and not self._is_mine(
                row.get("owner_pid"), row.get("owner_create_time")
            ):
                if owner_alive(row.get("owner_pid"), row.get("owner_create_time")) or (
                    not self.config.server.remote_tasks
                ):
                    # A live sibling owns the row (or this instance never
                    # touches remote rows): leave the request queued for an
                    # instance allowed to serve it.
                    rec["state"] = "waiting_owner"
                    with contextlib.suppress(OSError):
                        atomic_write_json(claimed, rec)
                    return OUTBOX_FOREIGN_RETRY_SEC
                task, _foreign = self._adopt_dead_task(task_id)
            if task is None:
                self._outbox_done(
                    outbox,
                    name,
                    {"ok": False, "state": "error", "error": f"unknown task {task_id}", "code": "unknown_task"},
                )
                return None
        try:
            result = await self._resume_owned(
                task,
                message=rec.get("message"),
                request_id=rec.get("request_id"),
            )
        except Exception as exc:
            self._outbox_done(
                outbox,
                name,
                {"ok": False, "state": "error", "error": f"{type(exc).__name__}: {exc}", "code": "resume_failed"},
            )
            return None
        # save() only schedules the async flusher; the done record must not
        # outrun the state rows it points at — the requester reads the new
        # task_id/session_id off disk the moment it sees this answer.
        await self.flush_state()
        self._outbox_done(outbox, name, {"ok": True, "state": "resumed", **result})
        log.info(
            "outbox_resume name=%s task_id=%s new_task_id=%s",
            name,
            task_id,
            result.get("task_id"),
        )
        return None

    async def start(self) -> None:
        self._stopping = False
        self._stop_started = False
        self._stop_done = asyncio.Event()
        self._stop_interrupted = asyncio.Event()
        install_host_env(self.config.env)
        reap_orphans(self.home)
        payload = read_json(state_path(self.home), {})
        for raw in payload.get("sessions") or []:
            session = Session.model_validate(raw)
            if self._foreign_live(session.owner_pid, session.owner_create_time):
                continue
            self._stamp_owner(session)
            if session.proc_state in {ProcState.busy, ProcState.spawning, ProcState.ready}:
                session.proc_state = ProcState.idle_unloaded
            session.pid = None
            self.sessions[session.session_id] = session
        for raw in payload.get("tasks") or []:
            task = Task.model_validate(raw)
            if self._foreign_live(task.owner_pid, task.owner_create_time):
                continue
            self._stamp_owner(task)
            if task.status in {TaskStatus.queued, TaskStatus.running}:
                if task.paused:
                    # Same as a mid-pause crash on the owner: the persisted
                    # intent ends the row cancelled/paused, not failed.
                    task.status = TaskStatus.cancelled
                    task.stop_reason = "paused"
                else:
                    task.status = TaskStatus.failed
                    task.error = "bridge_restarted"
                task.finished_at = iso()
                self._recover_run_usage(task)
            self.tasks[task.task_id] = task
            done = asyncio.Event()
            done.set()
            self._done[task.task_id] = done
        # Stamp adopted owners onto disk first so a following prune is not
        # undone by _merge_owned treating the old unowned rows as foreign.
        self.save()
        await self.flush_state()
        self._prune()
        self.save()
        await self.flush_state()
        self.touch_activity()
        if self.config.server.idle_exit_sec > 0:
            self._watchdog = asyncio.create_task(
                self._idle_exit_watchdog(),
                name="idle-exit-watchdog",
            )
        if self.dispatch_enabled:
            self._outbox_task = asyncio.create_task(self._outbox_loop(), name="outbox")

    async def stop(self) -> None:
        # Idempotent and safe under a concurrent caller (idle watchdog vs.
        # lifespan shutdown): the second caller waits for the first stop to
        # finish instead of re-running the teardown.
        if self._stop_started:
            await self._stop_done.wait()
            return
        self._stop_started = True
        self._stopping = True
        # Teardown — worker shutdown, transcript flush, final state save — is
        # not optional, so it runs as an inner task nothing cancels and this
        # caller keeps waiting for it through a CancelledError. Interruption
        # still wins over the linger wait via _stop_interrupted, so a second
        # Ctrl+C abandons linger instead of sitting out linger_max_sec.
        shutdown = asyncio.ensure_future(self._shutdown())
        interrupted = False
        try:
            while not shutdown.done():
                try:
                    await asyncio.shield(shutdown)
                except asyncio.CancelledError:
                    interrupted = True
                    self._stop_interrupted.set()
        finally:
            self._stop_done.set()
        if interrupted:
            raise asyncio.CancelledError()

    async def _shutdown(self) -> None:
        await self._quota_cache.close()
        watchdog = self._watchdog
        self._watchdog = None
        if watchdog is not None:
            watchdog.cancel()
        outbox_task = self._outbox_task
        self._outbox_task = None
        if outbox_task is not None:
            outbox_task.cancel()
        for idle in list(self._idle.values()):
            idle.cancel()
        self._idle.clear()
        bgs = [task for task in self._bg.values() if not task.done()]
        linger_max = self.config.server.linger_max_sec
        if bgs and self.config.server.shutdown_policy == "linger" and linger_max > 0:
            # Opt-in: the host went away orderly (stdin EOF / lifespan),
            # but in-flight turns keep their pipes and run to completion
            # — transcript, result, and state all land as usual — up to
            # the linger deadline, which cancels what is left. A cancelled
            # stop() ends the wait early via _stop_interrupted.
            log.info(
                "shutdown_policy=linger: holding shutdown up to %ss for %d in-flight task(s)",
                linger_max,
                len(bgs),
            )
            all_done = asyncio.gather(*bgs, return_exceptions=True)
            interrupt_wait = asyncio.ensure_future(self._stop_interrupted.wait())
            waitables: set[asyncio.Future[Any]] = {all_done, interrupt_wait}
            try:
                await asyncio.wait(
                    waitables,
                    timeout=linger_max,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                interrupt_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await interrupt_wait
            bgs = [task for task in bgs if not task.done()]
            if bgs:
                why = "interrupted" if self._stop_interrupted.is_set() else f"deadline {linger_max}s reached"
                log.warning(
                    "linger %s with %d task(s) still in flight; cancelling",
                    why,
                    len(bgs),
                )
        for bg in bgs:
            bg.cancel()
        if bgs:
            _done, pending = await asyncio.wait(bgs, timeout=STOP_TASK_GRACE_SEC)
            if pending:
                log.warning(
                    "%d task(s) did not finish cancelling within %ss",
                    len(pending),
                    STOP_TASK_GRACE_SEC,
                )
        for session_id, adapter in list(self._adapters.items()):
            session = self.sessions.get(session_id)
            if session is not None:
                try:
                    await adapter.shutdown(session)
                except Exception:
                    log.exception("shutdown failed for %s", session_id)
                if session.proc_state != ProcState.dead:
                    session.proc_state = ProcState.idle_unloaded
        self._adapters.clear()
        try:
            flush_pending(self.home)
        except OSError:
            log.exception("could not flush transcripts during shutdown")
        self.save()
        await self.flush_state()

    def _adapter_for(self, session: Session) -> Adapter:
        existing = self._adapters.get(session.session_id)
        if existing is not None:
            return existing
        adapter = build_adapter(self.config.get(session.agent), self.home, self.config.env)
        self._adapters[session.session_id] = adapter
        return adapter

    def _busy_task(self, session_id: str) -> Task | None:
        for task in self.tasks.values():
            if task.session_id == session_id and task.status in {TaskStatus.queued, TaskStatus.running}:
                return task
        return None

    async def list_agents(self) -> list[dict]:
        async def describe(cfg: AgentConfig) -> dict:
            row = await probe_agent(cfg, self.config.env)
            row["quota"] = await self._quota_for(cfg, available=bool(row.get("available")))
            return row

        return list(await asyncio.gather(*(describe(cfg) for cfg in self.config.agents.values())))

    async def _quota_for(self, cfg: AgentConfig, *, available: bool) -> dict:
        """The ``quota`` block for one ``list_agents`` row. Never raises, never gates ``available``."""
        quota_cfg = self.config.quota
        if not quota_cfg.enabled:
            return unknown_quota("quota lookup is disabled ([quota] enabled = false)").model_dump(mode="json")
        if not available:
            return unknown_quota("worker command not found").model_dump(mode="json")
        try:
            env = await asyncio.to_thread(build_worker_env, cfg.env, config=self.config.env, log_fill=False)
        except Exception as exc:
            log.warning("could not build the worker env for %s quota lookup: %s", cfg.name, exc)
            return unknown_quota(f"worker environment unavailable: {type(exc).__name__}").model_dump(mode="json")
        return await fetch_quota(
            cfg,
            env,
            cache=self._quota_cache,
            timeout_sec=quota_cfg.timeout_sec,
            providers=provider_table(self.config),
        )

    SIBLING_CACHE_SEC = 60

    async def _sibling_count(self) -> int:
        now = time.monotonic()
        if self._sibling_cache is not None:
            cached_at, count = self._sibling_cache
            if now - cached_at < self.SIBLING_CACHE_SEC:
                return count
        try:
            count = await asyncio.to_thread(count_sibling_servers)
        except (psutil.Error, OSError) as exc:
            log.warning("could not count sibling agent-bridge servers: %s", exc)
            count = 0
        self._sibling_cache = (time.monotonic(), count)
        return count

    async def env_status(self) -> dict:
        status = describe_env(self.config.env)
        if self.config.warnings:
            status.setdefault("warnings", []).extend(self.config.warnings)
        siblings = await self._sibling_count()
        if siblings > 0:
            warnings = status.setdefault("warnings", [])
            warnings.append(
                f"{siblings} other agent-bridge server instance(s) running on this machine "
                "(each coordinator host holds its own; abandoned ones self-exit after "
                "server.idle_exit_sec)"
            )
        return status

    def coordinator_status(self) -> dict:
        cfg = self.config.coordinator
        return {
            "mode": cfg.mode,
            "hint": COORDINATOR_MODE_HINTS.get(cfg.mode, COORDINATOR_MODE_HINTS["auto"]),
            "instructions": cfg.instructions or None,
            "runtime_context": self.runtime_context,
            "dispatch_enabled": self.dispatch_enabled,
        }

    def set_preferences(
        self,
        *,
        mode: str | None = None,
        instructions: str | None = None,
    ) -> dict:
        if not self.dispatch_enabled:
            raise RuntimeError(NESTED_PREFERENCES_ERROR)
        if mode is None and instructions is None:
            raise ValueError("provide mode and/or instructions")
        if mode is not None:
            mode = normalize_coordinator_mode(mode, strict=True)
        path = write_coordinator_overlay(self.home, mode=mode, instructions=instructions)
        # The running instance applies the change immediately; the file makes
        # it stick for every Bridge instance started after this.
        if mode is not None:
            self.config.coordinator.mode = mode
        if instructions is not None:
            self.config.coordinator.instructions = instructions.strip()
        notes = [
            "active in this Bridge instance now; other running instances pick it up at their next start"
        ]
        if mode is not None and os.environ.get("AGENT_BRIDGE_MODE"):
            notes.append(
                "this host pins mode via AGENT_BRIDGE_MODE, which outranks the file "
                "after a restart; the saved mode applies to hosts without that pin"
            )
        return {"coordinator": self.coordinator_status(), "path": str(path), "notes": notes}

    async def dispatch_task(
        self,
        agent: str,
        message: str,
        cwd: str,
        session_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        title: str | None = None,
        user_requested: bool = False,
        request_id: str | None = None,
        source: str | None = None,
    ) -> dict:
        if not self.dispatch_enabled:
            raise RuntimeError(NESTED_DISPATCH_ERROR)
        if self._stopping:
            raise RuntimeError("bridge is shutting down; not accepting new tasks")
        if self.config.coordinator.mode == "manual" and not user_requested:
            raise RuntimeError(
                "coordinator mode is manual: dispatch only when the user explicitly "
                "asked for a worker on this task. If they did, retry with "
                "user_requested=true; otherwise do the work yourself."
            )
        if request_id is not None:
            try:
                request_id = str(uuid.UUID(request_id))
            except ValueError:
                raise ValueError("request_id must be a UUID") from None
        cwd_path = Path(cwd)
        if not cwd_path.is_absolute():
            raise ValueError("cwd must be an absolute path")
        if not cwd_path.exists():
            raise ValueError(f"cwd does not exist: {cwd_path}")
        if not cwd_path.is_dir():
            raise ValueError(f"cwd is not a directory: {cwd_path}")
        effort = normalize_effort(effort)
        agent_cfg = self.config.get(agent)
        if agent_cfg.protocol == "agy" and not session_id:
            # agy rejects a -low/-medium/-high model slug paired with a
            # different --effort; fail the dispatch instead of the turn.
            # Existing-session combinations are checked under the lock, once
            # inherited model/effort are known.
            check_agy_model_effort(model, effort)
        # Compare supplied arguments, not selections inherited from a mutable session.
        request = (
            agent,
            message,
            str(cwd_path.resolve()),
            session_id,
            model,
            effort,
            title,
            user_requested,
            source,
        )
        async with self._lock:
            # stop() can interleave while this dispatch waited on the lock;
            # a task created now would outlive the _bg teardown.
            if self._stopping:
                raise RuntimeError("bridge is shutting down; not accepting new tasks")
            if request_id is not None and request_id in self._requests:
                previous, task_id = self._requests[request_id]
                if previous != request:
                    raise ValueError("request_id is already bound to a different dispatch request")
                task = self.tasks[task_id]
                return {
                    "task_id": task.task_id,
                    "session_id": task.session_id,
                    "agent": task.agent,
                    "model": task.model,
                    "effort": task.effort,
                    "request_id": request_id,
                    "reused": True,
                }
            if session_id:
                session = self.sessions.get(session_id)
                if session is None:
                    # The session's owner may have died after this instance
                    # booted: adopt the dead-owner row (reaping its orphaned
                    # worker) instead of answering "unknown session" until
                    # the next restart.
                    session, foreign = await self._adopt_dead_session(session_id)
                    if session is None:
                        if foreign:
                            raise RuntimeError(
                                f"session {session_id} is owned by another live "
                                "Bridge instance and cannot be driven from here"
                            )
                        raise KeyError(f"unknown session {session_id}")
                if session.agent != agent:
                    raise ValueError(f"session {session_id} belongs to agent {session.agent}, not {agent}")
                if Path(session.cwd).resolve() != cwd_path.resolve():
                    raise ValueError(
                        f"session {session.session_id} is bound to {session.cwd}; "
                        "follow-up cwd must be the same project folder"
                    )
                busy = self._busy_task(session.session_id)
                if busy is not None:
                    raise RuntimeError(
                        f"session {session.session_id} is busy with {busy.task_id}; call wait_task first"
                    )
            else:
                session = Session(
                    session_id=_new_id("sess"),
                    agent=agent,
                    cwd=str(cwd_path.resolve()),
                    model=model,
                    effort=effort,
                    title=title,
                    proc_state=ProcState.spawning,
                )
                self._stamp_owner(session)
                self.sessions[session.session_id] = session
            if agent_cfg.protocol == "agy" and session_id:
                check_agy_model_effort(model or session.model, effort or session.effort)
            if model:
                session.model = model
            if effort:
                session.effort = effort
            if title:
                session.title = title
            if session_id is None:
                session.cwd = str(cwd_path.resolve())
            session.last_active_at = iso()
            task = Task(
                task_id=_new_id("task"),
                session_id=session.session_id,
                agent=agent,
                message=message,
                cwd=session.cwd,
                model=model or session.model,
                effort=effort or session.effort,
                status=TaskStatus.queued,
                source=source,
            )
            self._stamp_owner(task)
            self.tasks[task.task_id] = task
            if request_id is not None:
                self._requests[request_id] = (request, task.task_id)
            self._done[task.task_id] = asyncio.Event()
            self._cancel_idle(session.session_id)
            self._prune()
            self.save()
            log.info(
                "task_dispatched task_id=%s session_id=%s agent=%s",
                task.task_id,
                session.session_id,
                agent,
            )
            self._bg[task.task_id] = asyncio.create_task(self._run_task(task.task_id), name=f"task-{task.task_id}")
        return {
            "task_id": task.task_id,
            "session_id": session.session_id,
            "agent": agent,
            "model": session.model,
            "effort": session.effort,
            **({"request_id": request_id, "reused": False} if request_id is not None else {}),
        }

    async def _run_task(self, task_id: str) -> None:
        started_monotonic = time.monotonic()
        task = self.tasks[task_id]
        session = self.sessions[task.session_id]
        adapter = self._adapter_for(session)
        task.status = TaskStatus.running
        task.started_at = iso()
        session.proc_state = ProcState.busy
        session.last_active_at = iso()
        self.save()
        watch = None
        try:
            mark_worker_activity(session.session_id, self.home)
            before = await asyncio.to_thread(snapshot_workspace, task.cwd)
            limit = self.config.get(session.agent).stall_timeout_sec
            watch = (
                asyncio.create_task(
                    self._stall_watch(task, session, adapter, limit),
                    name=f"stall-{task_id}",
                )
                if limit > 0
                else None
            )
            result = await adapter.run_turn(session, task)
            if result.native_session_id:
                session.native_session_id = result.native_session_id
            task.result_chars = len(result.text)
            task.warnings = list(result.warnings)
            try:
                atomic_write_text(result_path(task.task_id, self.home), result.text)
                task.result_text = _tail(result.text)
            except OSError as exc:
                task.result_text = _tail(result.text, RESULT_STORE_MAX)
                task.warnings.append(
                    f"full result persistence failed: {type(exc).__name__}: {exc}"
                )
                log.exception("could not persist full result for task %s", task.task_id)
            full_changed = await asyncio.to_thread(
                merge_files_changed, task.cwd, result.files_changed, before
            )
            task.files_changed_total = len(full_changed)
            task.files_changed = full_changed[:FILES_CHANGED_MAX]
            task.files_changed_truncated = len(full_changed) > FILES_CHANGED_MAX
            task.usage = result.usage
            task.run_usage = result.run_usage
            if session.agent == "grok":
                observed = await asyncio.to_thread(observe_grok_session, session.cwd, session.native_session_id)
                task.observed_model = observed["model"]
                task.observed_effort = observed["effort"]
            elif session.agent == "kimi":
                observed = await asyncio.to_thread(observe_kimi_session, session.native_session_id)
                task.observed_model = observed["model"]
                task.observed_effort = observed["effort"]
                if observed["failure"]:
                    # Kimi answered end_turn, so nothing above this line knows
                    # the turn failed. Say so where the coordinator looks.
                    task.warnings.append(
                        f"kimi reported end_turn but the turn failed: {observed['failure']}"
                    )
            else:
                # OpenCode (and any later ACP worker) has no on-disk sampler
                # log. Report the last model/effort the adapter applied.
                task.observed_model = result.observed_model
                task.observed_effort = result.observed_effort
            # cancel_task's timeout path may already have finalized this task
            # as cancelled; a late turn result must not overwrite that.
            if task.status not in TERMINAL_STATUSES:
                task.stop_reason = (
                    "paused" if task.paused and result.stop_reason == "cancelled" else result.stop_reason
                )
                if result.error:
                    task.status = TaskStatus.failed
                    task.error = result.error
                elif result.stop_reason == "cancelled":
                    task.status = TaskStatus.cancelled
                else:
                    task.status = TaskStatus.completed
            session.turns += 1
        except asyncio.CancelledError:
            if task.status not in TERMINAL_STATUSES:
                task.status = TaskStatus.cancelled
                task.stop_reason = "paused" if task.paused else "cancelled"
        except Exception as exc:
            log.exception("task %s failed", task_id)
            if task.status not in TERMINAL_STATUSES:
                task.status = TaskStatus.failed
                task.error = str(exc)
                task.stop_reason = "error"
            # A turn that dies before/inside run_turn leaves no transcript
            # boundary — the dashboard would show a silently empty transcript
            # for a message it believes was delivered. Close it out here.
            with contextlib.suppress(OSError):
                append_event(
                    session.session_id,
                    "turn_end",
                    {
                        "stop_reason": "error",
                        "task_id": task.task_id,
                        "error": str(exc)[:500],
                    },
                    self.home,
                )
        finally:
            if watch is not None:
                watch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watch
            if task.finished_at is None:
                task.finished_at = iso()
            # A Kimi quota failure arrives as a warning on a "completed" turn.
            # Transient-classified text (e.g. "deadline exceeded") can trip a
            # quota marker without being plan exhaustion — never invalidate on it.
            if (task.status == TaskStatus.failed and looks_like_quota_error(task.error)
                and not looks_like_transient_error(task.error)) or any(
                looks_like_quota_error(w) and not looks_like_transient_error(w)
                for w in task.warnings
            ):
                # The cached "ok" is now a lie; make the next list_agents re-read it.
                self._quota_cache.invalidate(session.agent)
            session.last_active_at = iso()
            if session.proc_state != ProcState.dead:
                session.proc_state = ProcState.ready if adapter.resident else ProcState.idle_unloaded
            try:
                flush_session(session.session_id, self.home)
            except OSError:
                log.exception("could not flush transcript for task %s", task.task_id)
            self._done[task_id].set()
            self._bg.pop(task_id, None)
            self.save()
            duration_ms = int((time.monotonic() - started_monotonic) * 1000)
            log_method = log.warning if task.status == TaskStatus.failed else log.info
            log_method(
                "task_finished task_id=%s session_id=%s agent=%s status=%s "
                "duration_ms=%s stop_reason=%s error=%r",
                task.task_id,
                task.session_id,
                task.agent,
                task.status.value,
                duration_ms,
                task.stop_reason,
                (task.error or "")[:500],
            )
            self._schedule_idle(session.session_id)

    async def _stall_watch(
        self,
        task: Task,
        session: Session,
        adapter: Adapter,
        limit: int,
    ) -> None:
        while True:
            silence = worker_silence_sec(session.session_id, self.home) or 0.0
            remaining = limit - silence
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, STALL_POLL_SEC))
        if task.status in TERMINAL_STATUSES:
            return
        log.warning(
            "task %s stalled: no worker output for %ss; cancelling the turn",
            task.task_id,
            limit,
        )
        task.status = TaskStatus.failed
        task.stop_reason = "stalled"
        task.error = (
            f"worker produced no output for {limit}s (stall_timeout_sec); "
            "Bridge cancelled the turn"
        )
        append_event(
            session.session_id,
            "error",
            {"error": task.error, "stalled": True, "stall_timeout_sec": limit},
            self.home,
        )
        # The turn normally returns (and _run_task tears this watch down)
        # while cancel() is still reaping the worker; shield so that
        # cleanup runs to completion instead of dying with the watch.
        await asyncio.shield(self._cancel_stalled(adapter, session, task.task_id))
        try:
            await asyncio.wait_for(self._done[task.task_id].wait(), timeout=STALL_CANCEL_GRACE_SEC)
        except TimeoutError:
            bg = self._bg.get(task.task_id)
            if bg is not None:
                bg.cancel()

    async def _cancel_stalled(self, adapter: Adapter, session: Session, task_id: str) -> None:
        try:
            await adapter.cancel(session)
        except Exception:
            log.exception("stall cancel failed for task %s", task_id)

    def _drop_task(self, task_id: str) -> None:
        self.tasks.pop(task_id, None)
        self._requests = {
            key: binding for key, binding in self._requests.items() if binding[1] != task_id
        }
        self._done.pop(task_id, None)
        self._resume_locks.pop(task_id, None)
        try:
            result_path(task_id, self.home).unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove pruned result for task %s", task_id)

    def _prune_sessions(self) -> None:
        now = time.time()
        candidates = [
            session
            for session in self.sessions.values()
            if session.proc_state in {ProcState.dead, ProcState.idle_unloaded}
            and session.session_id not in self._adapters
            and self._busy_task(session.session_id) is None
        ]
        candidates.sort(key=lambda item: _session_last_active_ts(item.last_active_at), reverse=True)
        drop = [
            session
            for index, session in enumerate(candidates)
            if index >= SESSION_KEEP_INACTIVE
            or now - _session_last_active_ts(session.last_active_at) > SESSION_RETAIN_SEC
        ]
        for session in drop:
            session_id = session.session_id
            self.sessions.pop(session_id, None)
            forget_worker_activity(session_id, self.home)
            self._cancel_idle(session_id)
            for task in [item for item in self.tasks.values() if item.session_id == session_id]:
                self._drop_task(task.task_id)
            log.info(
                "pruned session %s (%s, last active %s)",
                session_id,
                session.proc_state.value,
                session.last_active_at,
            )

    def _prune_tasks(self) -> None:
        by_session: dict[str, list[Task]] = {}
        for task in self.tasks.values():
            if task.status in TERMINAL_STATUSES:
                by_session.setdefault(task.session_id, []).append(task)
        for terminal in by_session.values():
            if len(terminal) <= TASK_KEEP_PER_SESSION:
                continue
            terminal.sort(key=lambda item: item.created_at)
            for old in terminal[: len(terminal) - TASK_KEEP_PER_SESSION]:
                self._drop_task(old.task_id)
        terminal_all = [task for task in self.tasks.values() if task.status in TERMINAL_STATUSES]
        if len(terminal_all) <= TASK_KEEP_TOTAL:
            return
        terminal_all.sort(key=lambda item: item.created_at)
        for old in terminal_all[: len(terminal_all) - TASK_KEEP_TOTAL]:
            self._drop_task(old.task_id)

    def _prune(self) -> None:
        self._prune_sessions()
        self._prune_tasks()

    def _cancel_idle(self, session_id: str) -> None:
        idle = self._idle.pop(session_id, None)
        if idle:
            idle.cancel()

    def _schedule_idle(self, session_id: str) -> None:
        if self._stopping:
            return
        session = self.sessions.get(session_id)
        if session is None:
            return
        adapter = self._adapters.get(session_id)
        if adapter is not None and not adapter.resident:
            return
        try:
            cfg = self.config.get(session.agent)
        except KeyError:
            return
        if cfg.idle_unload_sec <= 0:
            return
        self._cancel_idle(session_id)

        async def _idle() -> None:
            await asyncio.sleep(cfg.idle_unload_sec)
            current = self.sessions.get(session_id)
            if current is None or self._busy_task(session_id):
                return
            adapter = self._adapters.get(session_id)
            if adapter is not None:
                try:
                    await adapter.shutdown(current)
                except Exception:
                    log.exception("idle unload failed for %s", session_id)
            current.proc_state = ProcState.idle_unloaded
            self.save()

        self._idle[session_id] = asyncio.create_task(_idle(), name=f"idle-{session_id}")

    def _disk_task_rows(self) -> list[dict]:
        """Task rows as persisted in ``state.json`` right now (all owners)."""
        payload = read_json(state_path(self.home), {})
        if not isinstance(payload, dict):
            return []
        return [row for row in payload.get("tasks") or [] if isinstance(row, dict)]

    def _remote_task(self, task_id: str) -> Task | None:
        """Resolve a task row owned by another Bridge instance, read-only.

        Only consulted after ``self.tasks`` misses: live siblings' rows are
        skipped at start() and never enter memory. Rows owned by *this*
        instance (or dead-owner rows already adopted) are deliberately not
        remote — adoption happens once, at start().
        """
        if not self.config.server.remote_tasks:
            return None
        if not is_safe_id(task_id):
            return None
        for raw in self._disk_task_rows():
            if raw.get("task_id") != task_id:
                continue
            if self._is_mine(raw.get("owner_pid"), raw.get("owner_create_time")):
                return None
            try:
                task = Task.model_validate(raw)
            except Exception:
                return None
            # session_id becomes a transcript path component downstream.
            if not is_safe_id(task.session_id):
                return None
            return task
        return None

    def _remote_task_snapshot(self, task: Task, include_result: bool = False) -> dict:
        payload = self._task_snapshot(task)
        alive = owner_alive(task.owner_pid, task.owner_create_time)
        payload["remote"] = True
        payload["owner"] = {
            "pid": task.owner_pid,
            "create_time": task.owner_create_time,
            "alive": alive,
        }
        # Worker-silence bookkeeping lives in the owning process; nothing
        # truthful to report for a sibling's turn.
        payload["silent_for_sec"] = None
        # _task_snapshot computed these as a local row; recompute with owner
        # liveness so a remote paused/dead-owner row reports honestly.
        resumable, resume_hint = self._resume_fields(task, remote_alive=alive)
        payload["resumable"] = resumable
        payload["resume_hint"] = resume_hint
        if task.status in {TaskStatus.queued, TaskStatus.running} and not alive:
            payload["owner_lost"] = True
            payload["hint"] = (
                "The Bridge instance owning this task is gone; it cannot finish. "
                "Any partial work is in get_transcript / get_result."
            )
        if include_result:
            artifact = result_path(task.task_id, self.home)
            if artifact.is_file():
                try:
                    preview = _read_file_tail(artifact)
                except OSError:
                    preview = _tail(task.result_text)
                total = task.result_chars or len(artifact.read_text(encoding="utf-8", errors="replace"))
            else:
                preview = _tail(task.result_text)
                total = task.result_chars or len(task.result_text)
            payload["result_text"] = preview
            payload["result_total_chars"] = total
            payload["result_truncated"] = total > len(preview)
            if "hint" not in payload:
                payload["hint"] = self._result_hint(
                    task,
                    "Sibling-owned task (remote): the owning Bridge instance ran it; "
                    "this is a read-only view. Use get_result for the complete "
                    "final result and get_transcript for the detailed turn log.",
                )
        return payload

    async def _wait_remote_task(self, task_id: str, timeout_sec: float) -> dict:
        """Poll a sibling-owned task's state.json row until it resolves.

        The owning instance keeps executing; this only watches the disk row
        until it turns terminal, the owner process dies mid-flight
        (``owner_lost``), the result artifact lands, or the caller's timeout
        elapses. Foreign rows are never mutated or executed here.
        """
        deadline = time.monotonic() + timeout_sec
        task = self._remote_task(task_id)
        if task is None and not result_path(task_id, self.home).is_file():
            raise KeyError(f"unknown task {task_id}")
        last: Task | None = task
        while True:
            if task is not None:
                last = task
                # Only the row's own terminal status counts: the result
                # artifact is written before the final state flush lands, so
                # an artifact-first peek would report a still-running task as
                # finished. The artifact alone covers the vanished-row case in
                # the branch below instead.
                done = task.status in TERMINAL_STATUSES
                if done or not owner_alive(task.owner_pid, task.owner_create_time):
                    return {
                        "timed_out": False,
                        **self._remote_task_snapshot(task, include_result=True),
                    }
            elif last is not None and result_path(task_id, self.home).is_file():
                # The row vanished (owner pruned/rewrote it) but its final
                # artifact landed — report the last row we saw.
                return {
                    "timed_out": False,
                    **self._remote_task_snapshot(last, include_result=True),
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if last is None:
                    raise KeyError(f"unknown task {task_id}")
                return {"timed_out": True, **self._remote_task_snapshot(last)}
            await asyncio.sleep(min(REMOTE_TASK_POLL_SEC, remaining))
            task = self._remote_task(task_id)

    def list_tasks(self, active_only: bool = False) -> list[dict]:
        """Stable task rows: this instance's tasks plus sibling-owned ones.

        Remote rows are read-only views of other live Bridge instances' work
        (and rows whose owner already died, marked ``owner_lost`` when still
        queued/running). De-duplicated by task_id; result bodies stay out of
        the listing — get_result pages them on demand.
        """
        rows: list[dict] = []

        def row_for(task: Task, *, remote: bool) -> dict:
            if remote:
                alive = owner_alive(task.owner_pid, task.owner_create_time)
                owner = {"pid": task.owner_pid, "create_time": task.owner_create_time, "alive": alive}
            else:
                alive = True
                owner = {"pid": self._owner_pid, "create_time": self._owner_create_time, "alive": True}
            resumable, resume_hint = self._resume_fields(
                task, remote_alive=alive if remote else None
            )
            row = {
                "task_id": task.task_id,
                "session_id": task.session_id,
                "agent": task.agent,
                "status": task.status.value,
                "stop_reason": task.stop_reason,
                "error": task.error,
                "error_kind": classify_error(task.error)
                if task.status == TaskStatus.failed
                else None,
                "retryable": (
                    task.status == TaskStatus.failed
                    and classify_error(task.error) == "transient_provider"
                ),
                "source": task.source,
                "model": task.model,
                "effort": task.effort,
                "created_at": task.created_at,
                "started_at": task.started_at,
                "finished_at": task.finished_at,
                "remote": remote,
                "owner": owner,
                "owner_lost": (
                    not alive and task.status in {TaskStatus.queued, TaskStatus.running}
                ),
                "paused": task.paused,
                "resumable": resumable,
            }
            if resume_hint is not None:
                row["resume_hint"] = resume_hint
            if task.resume_of is not None:
                row["resume_of"] = task.resume_of
            if task.resumed_by is not None:
                row["resumed_by"] = task.resumed_by
            return row

        for task in self.tasks.values():
            if active_only and task.status in TERMINAL_STATUSES:
                continue
            rows.append(row_for(task, remote=False))
        if self.config.server.remote_tasks:
            seen = set(self.tasks)
            for raw in self._disk_task_rows():
                task_id = raw.get("task_id")
                if not isinstance(task_id, str) or task_id in seen:
                    continue
                if self._is_mine(raw.get("owner_pid"), raw.get("owner_create_time")):
                    continue
                try:
                    task = Task.model_validate(raw)
                except Exception:
                    continue
                if not is_safe_id(task.task_id) or not is_safe_id(task.session_id):
                    continue
                if active_only and task.status in TERMINAL_STATUSES:
                    continue
                rows.append(row_for(task, remote=True))
                seen.add(task_id)
        rows.sort(key=lambda row: str(row.get("created_at") or ""))
        return rows

    def server_status(self) -> dict:
        """Lifecycle policy block surfaced through ``list_agents``."""
        cfg = self.config.server
        return {
            "idle_exit_sec": cfg.idle_exit_sec,
            "shutdown_policy": cfg.shutdown_policy,
            "linger_max_sec": cfg.linger_max_sec,
            "remote_tasks": cfg.remote_tasks,
        }

    def _resume_fields(self, task: Task, *, remote_alive: bool | None) -> tuple[bool, str | None]:
        """``(resumable, resume_hint)`` for snapshots and listings.

        ``remote_alive`` is None for this instance's own rows (the session
        lookup decides), True for a live sibling's row (read-only here —
        only that owner may resume it), False for a dead owner's row
        (resume_task can adopt it). A task is resumable when its turn ended
        unfinished — paused, cancelled, or failed — on a session that can
        still take a follow-up turn.
        """
        if task.status in {TaskStatus.queued, TaskStatus.running}:
            if remote_alive is True:
                return False, "a live sibling Bridge owns this turn — only it can pause or cancel it"
            if remote_alive is False:
                return True, (
                    f"the owning Bridge died mid-run — resume_task(task_id=\"{task.task_id}\") adopts "
                    "this session (reaping its orphaned worker), finalizes the row as "
                    "failed/bridge_restarted, and continues the same conversation"
                )
            return False, (
                f"in-flight — pause_task(task_id=\"{task.task_id}\") ends the turn and keeps it "
                "resumable; cancel_task cancels it outright"
            )
        if remote_alive is True:
            if task.status == TaskStatus.completed:
                return False, (
                    "owned by a live sibling Bridge and already completed — "
                    "only that instance can send a follow-up on it"
                )
            return True, (
                f"owned by a live sibling Bridge — resume_task(task_id=\"{task.task_id}\") "
                "routes the resume to that instance through the shared queue and relays "
                "the new task it dispatches"
            )
        session = self.sessions.get(task.session_id)
        if session is not None and session.proc_state == ProcState.dead:
            return False, "the session was ended; dispatch_task starts a fresh task"
        if task.status == TaskStatus.completed:
            return False, f"dispatch_task(session_id=\"{task.session_id}\") sends a follow-up on this conversation"
        # cancelled or failed: the turn ended unfinished and the conversation
        # can continue on the same session.
        if remote_alive is False:
            return True, (
                f"the owning Bridge is gone — resume_task(task_id=\"{task.task_id}\") adopts this "
                "session (reaping its orphaned worker) and continues the same conversation"
            )
        why = "paused" if task.paused else task.status.value
        hint = (
            f"this {why} turn kept its partial result, transcript, and session — "
            f"resume_task(task_id=\"{task.task_id}\") dispatches a continuation turn on the same "
            "conversation, or dispatch_task(session_id=...) sends a free-form follow-up"
        )
        if task.status == TaskStatus.failed and looks_like_transient_error(task.error):
            hint = (
                "the error looks like a transient provider failure (capacity/overload, "
                "not quota or a bad request) — retrying it later is likely to work. "
                + hint
            )
        return True, hint

    @staticmethod
    def _result_hint(task: Task, prefix: str) -> str:
        hint = prefix
        if task.agent == "grok":
            hint += (
                " Grok system-prompt identity is not the selected model; "
                "use observed_model from this payload."
            )
        if task.agent == "kimi":
            hint += (
                " Kimi reports a failed turn as end_turn with empty text; "
                "an empty result is only clean if warnings is empty."
            )
        if task.agent == "opencode":
            hint += (
                " OpenCode observed_model/effort are the last values Bridge "
                "successfully set on the session after mapping, not a live sampler."
            )
        if task.agent == "claude":
            hint += (
                " Claude Code observed_model/effort are the last values Bridge "
                "successfully set on the session after mapping, not a live sampler."
            )
        if task.agent == "cursor":
            hint += (
                " Cursor observed_model is the requested ID after Cursor confirmed "
                "its mapped ACP options; observed_effort is Cursor's confirmed thought "
                "level. Neither is a live sampler."
            )
        if task.agent == "devin":
            hint += (
                " Devin observed_model is the last id Bridge set on the session; "
                "observed_effort is always null because the level is part of the model id."
            )
        if task.status == TaskStatus.failed and classify_error(task.error) == "transient_provider":
            hint += (
                " This is a transient provider failure (capacity/overload), not quota "
                "or a bad request: resume_task continues the same conversation with "
                "its partial result, or retry later / dispatch on a different model."
            )
        if task.files_changed_truncated:
            hint += (
                f" files_changed lists the first {FILES_CHANGED_MAX} of "
                f"{task.files_changed_total} paths; run git status in cwd for the full set."
            )
        if task.stop_reason == "stalled":
            hint += (
                " The worker went silent for stall_timeout_sec and Bridge cancelled the turn; "
                "read get_transcript for its last activity, then either dispatch a narrower "
                f"task on the same session_id or raise [agents.{task.agent}] stall_timeout_sec "
                "if that step was legitimately long."
            )
        return hint

    def _task_snapshot(self, task: Task, include_result: bool = False) -> dict:
        events = read_events_tail(task.session_id, self.home)
        payload: dict[str, Any] = {
            "task_id": task.task_id,
            "session_id": task.session_id,
            "remote": False,
            "owner": {
                "pid": self._owner_pid,
                "create_time": self._owner_create_time,
                "alive": True,
            },
            "agent": task.agent,
            "status": task.status.value,
            "stop_reason": task.stop_reason,
            "error": task.error,
            "error_kind": classify_error(task.error)
            if task.status == TaskStatus.failed
            else None,
            "retryable": (
                task.status == TaskStatus.failed
                and classify_error(task.error) == "transient_provider"
            ),
            "warnings": task.warnings,
            "files_changed": task.files_changed,
            "files_changed_total": task.files_changed_total,
            "files_changed_truncated": task.files_changed_truncated,
            "usage": task.usage,
            "run_usage": task.run_usage,
            "model": task.model,
            "effort": task.effort,
            "observed_model": task.observed_model,
            "observed_effort": task.observed_effort,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "recent_activity": recent_activity(events),
        }
        resumable, resume_hint = self._resume_fields(task, remote_alive=None)
        payload["paused"] = task.paused
        payload["resumable"] = resumable
        payload["resume_hint"] = resume_hint
        if task.resume_of is not None:
            payload["resume_of"] = task.resume_of
        if task.resumed_by is not None:
            payload["resumed_by"] = task.resumed_by
        # While the task is still running its persisted run_usage is empty;
        # surface the latest normalized "usage" transcript event as a marked
        # live partial instead. Events older than started_at belong to a
        # prior run on this reusable session. The final Task.run_usage
        # written at finalization stays the authoritative record.
        if task.status == TaskStatus.running and task.started_at and not task.run_usage:
            consumed = _partial_run_usage(events, task.started_at)
            if consumed:
                payload["run_usage"] = {**consumed, "partial": True}
        if task.started_at:
            start = datetime.fromisoformat(task.started_at)
            end = datetime.fromisoformat(task.finished_at) if task.finished_at else datetime.fromisoformat(iso())
            payload["elapsed_sec"] = max(0, int((end - start).total_seconds()))
        else:
            payload["elapsed_sec"] = 0
        if include_result:
            preview = _tail(task.result_text)
            total_chars = task.result_chars or len(task.result_text)
            payload["result_text"] = preview
            payload["result_total_chars"] = total_chars
            payload["result_truncated"] = total_chars > len(preview)
            payload["hint"] = self._result_hint(
                task,
                "Use get_result for the complete final result and get_transcript "
                "for the detailed turn log.",
            )
        payload["silent_for_sec"] = (
            int(worker_silence_sec(task.session_id, self.home) or 0)
            if task.status == TaskStatus.running
            else None
        )
        cfg = self.config.agents.get(task.agent)
        payload["stall_timeout_sec"] = cfg.stall_timeout_sec if cfg is not None else None
        return payload

    async def wait_task(self, task_id: str, timeout_sec: float = DEFAULT_WAIT_SEC) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            return await self._wait_remote_task(task_id, timeout_sec)
        event = self._done.setdefault(task_id, asyncio.Event())
        if task.status not in TERMINAL_STATUSES:
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout_sec)
            except TimeoutError:
                return {"timed_out": True, **self._task_snapshot(task)}
        # Snapshot the held Task: a dispatch-time _prune may have evicted it
        # from self.tasks while this waiter slept on the done event.
        return {"timed_out": False, **self._task_snapshot(task, include_result=True)}

    def check_task(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if task is not None:
            return self._task_snapshot(task)
        remote = self._remote_task(task_id)
        if remote is None:
            raise KeyError(f"unknown task {task_id}")
        return self._remote_task_snapshot(remote)

    def get_result(
        self,
        task_id: str,
        cursor: int = 0,
        max_chars: int = RESULT_PAGE_MAX_CHARS,
    ) -> dict:
        if cursor < 0:
            raise ValueError("cursor must be non-negative")
        if not 1 <= max_chars <= RESULT_PAGE_MAX_CHARS:
            raise ValueError(f"max_chars must be between 1 and {RESULT_PAGE_MAX_CHARS}")
        task = self.tasks.get(task_id)
        remote = False
        if task is None:
            task = self._remote_task(task_id)
            if task is None:
                raise KeyError(f"unknown task {task_id}")
            remote = True
        path = result_path(task.task_id, self.home)
        artifact = path.is_file()
        if artifact:
            # result_chars is len(result.text), recorded with the artifact
            # write, so the artifact's character count is already known and
            # only the requested window is decoded. An artifact persisted
            # without result_chars falls back to counting it once.
            total = task.result_chars
            try:
                if not total:
                    total = len(path.read_text(encoding="utf-8"))
                if cursor > total:
                    raise ValueError(f"cursor exceeds result length ({total})")
                page = _read_result_window(path, cursor, max_chars)
            except OSError as exc:
                log.warning("could not read full result for task %s: %s", task.task_id, exc)
                artifact = False
        if not artifact:
            text = task.result_text
            total = task.result_chars or len(text)
            if cursor > len(text):
                raise ValueError(f"cursor exceeds result length ({len(text)})")
            page = text[cursor : min(len(text), cursor + max_chars)]
        bound = total if artifact else len(text)
        end = min(bound, cursor + max_chars)
        has_more = end < bound
        payload = self._remote_task_snapshot(task) if remote else self._task_snapshot(task)
        payload.update(
            {
                "result_text": page,
                "result_offset": cursor,
                "result_total_chars": total,
                "next_cursor": end if has_more else None,
                "has_more": has_more,
                "result_truncated": has_more,
                "result_complete": artifact,
                "result_source": "artifact" if artifact else "legacy_state",
                "hint": self._result_hint(
                    task,
                    "Continue with next_cursor while has_more is true. "
                    "Use get_transcript for the detailed turn log.",
                ),
            }
        )
        return payload

    def get_transcript(self, session_id: str, offset: int = 0, limit: int = 50, kinds: list[str] | None = None) -> dict:
        if not is_safe_id(session_id) or (
            session_id not in self.sessions and not transcript_path(session_id, self.home).is_file()
        ):
            raise KeyError(f"unknown session {session_id}")
        events = read_events(session_id, self.home)
        return page_events(events, offset=offset, limit=limit, kinds=kinds)

    async def cancel_task(self, task_id: str) -> dict:
        if not self.dispatch_enabled:
            raise RuntimeError(NESTED_CANCEL_ERROR)
        task = self.tasks.get(task_id)
        if task is None:
            remote = self._remote_task(task_id)
            if remote is None:
                raise KeyError(f"unknown task {task_id}")
            if owner_alive(remote.owner_pid, remote.owner_create_time):
                raise RuntimeError(
                    f"task {task_id} is owned by another Bridge instance and "
                    "cannot be cancelled from here (remote tasks are read-only)"
                )
            # The owning instance is gone: adopt the row (finalizing an
            # in-flight turn as failed/bridge_restarted) and its session so
            # the orphaned worker is reaped instead of left running.
            adopted, _foreign = self._adopt_dead_task(task_id)
            if adopted is None:
                raise KeyError(f"unknown task {task_id}")
            await self._adopt_dead_session(adopted.session_id)
            return self._task_snapshot(adopted)
        if task.status in TERMINAL_STATUSES:
            return self._task_snapshot(task)
        session = self.sessions[task.session_id]
        adapter = self._adapters.get(session.session_id)
        if adapter is not None:
            await adapter.cancel(session)
        bg = self._bg.get(task_id)
        if bg is not None:
            bg.cancel()
            await asyncio.wait({bg}, timeout=15)
        else:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._done[task_id].wait(), timeout=15)
        if not self._done[task_id].is_set():
            # _run_task never ran (cancelled before its first step) or did not
            # finish in time; close the record here.
            task.status = TaskStatus.cancelled
            task.stop_reason = "cancelled"
            task.finished_at = iso()
            if task.started_at is None and session.proc_state == ProcState.spawning:
                session.proc_state = ProcState.idle_unloaded
            self._bg.pop(task_id, None)
            self._done[task_id].set()
            self.save()
        return self._task_snapshot(self.tasks.get(task_id, task))

    async def pause_task(self, task_id: str) -> dict:
        """End the in-flight turn while keeping the task resumable.

        This is a graceful cancel, not frozen execution: the adapter's own
        cancel runs first so the partial result, usage, transcript, and
        native session id land through the normal return path; only a turn
        still running after PAUSE_GRACE_SEC is force-cancelled. The row ends
        terminal — status cancelled, stop_reason "paused", paused=True —
        and resume_task continues the same conversation as a new task.
        """
        if not self.dispatch_enabled:
            raise RuntimeError(NESTED_PAUSE_ERROR)
        task = self.tasks.get(task_id)
        if task is None:
            if self._remote_task(task_id) is not None:
                raise RuntimeError(
                    f"task {task_id} is owned by another Bridge instance and "
                    "cannot be paused from here (remote tasks are read-only)"
                )
            raise KeyError(f"unknown task {task_id}")
        if task.status in TERMINAL_STATUSES:
            if task.paused:
                # already paused: idempotent
                return self._task_snapshot(task, include_result=True)
            raise RuntimeError(
                f"task {task_id} is already {task.status.value}; "
                "only a queued/running task can be paused"
            )
        # Persist the pause intent before touching the worker: even a crash
        # mid-pause leaves a truthful row instead of an abandoned "running".
        task.paused = True
        self.save()
        session = self.sessions.get(task.session_id)
        adapter = self._adapters.get(task.session_id) if session is not None else None
        if session is not None and adapter is not None:
            try:
                await adapter.cancel(session)
            except Exception:
                log.exception("pause: adapter cancel failed for task %s", task_id)
        done_event = self._done.get(task_id)
        bg = self._bg.get(task_id)
        if bg is not None and task.status == TaskStatus.running:
            # Grace first: the adapter cancel lets the worker return its
            # partial result through the normal run_turn path.
            finished, _pending = await asyncio.wait({bg}, timeout=PAUSE_GRACE_SEC)
            if not finished:
                bg.cancel()
                await asyncio.wait({bg}, timeout=STOP_TASK_GRACE_SEC)
        elif bg is not None:
            # Still queued — no turn is running, so there is nothing to wait
            # out gracefully; cancel immediately.
            bg.cancel()
            await asyncio.wait({bg}, timeout=STOP_TASK_GRACE_SEC)
        elif done_event is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(done_event.wait(), timeout=PAUSE_GRACE_SEC)
        if done_event is None or not done_event.is_set():
            # _run_task never started (queued) or could not finish in time;
            # close the record here so the pause is terminal, not dangling.
            task.status = TaskStatus.cancelled
            task.stop_reason = "paused"
            task.finished_at = iso()
            if (
                session is not None
                and task.started_at is None
                and session.proc_state == ProcState.spawning
            ):
                session.proc_state = ProcState.idle_unloaded
            self._bg.pop(task_id, None)
            if done_event is not None:
                done_event.set()
            with contextlib.suppress(OSError):
                append_event(
                    task.session_id,
                    "turn_end",
                    {"stop_reason": "paused", "task_id": task.task_id},
                    self.home,
                )
            self.save()
        log.info("task_paused task_id=%s session_id=%s", task_id, task.session_id)
        return self._task_snapshot(self.tasks.get(task_id, task), include_result=True)

    async def resume_task(
        self,
        task_id: str,
        message: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        """Continue an unfinished task's work as a new task on the same session.

        Accepts paused, cancelled, and failed rows — including a dead owner's
        row, which is adopted first (its orphaned worker is reaped before a
        replacement spawns). A row owned by a *live* sibling Bridge is never
        driven from here: the resume is routed through the shared outbox to
        the owning instance, which runs the exact path below and reports the
        new task back within a bounded wait; when the recorded owner dies
        mid-delegation the row is adopted locally instead.

        The original row stays final and auditable: it is never rewritten; it
        gains ``resumed_by`` and the new task carries ``resume_of``. ``message``
        defaults to an explicit "pick up the paused work" continuation prompt;
        ``request_id`` deduplicates like dispatch_task's, and a task that
        already has a continuation returns it instead of dispatching again —
        that dedupe survives restarts and owner changes.
        """
        if not self.dispatch_enabled:
            raise RuntimeError(NESTED_RESUME_ERROR)
        if self._stopping:
            raise RuntimeError("bridge is shutting down; not accepting new tasks")
        task = self.tasks.get(task_id)
        if task is None:
            task, foreign = self._adopt_dead_task(task_id)
            if task is None:
                if foreign:
                    return await self._resume_via_owner(task_id, message, request_id)
                raise KeyError(f"unknown task {task_id}")
        return await self._resume_owned(task, message=message, request_id=request_id)

    async def _resume_owned(
        self,
        task: Task,
        *,
        message: str | None,
        request_id: str | None,
    ) -> dict:
        """Resume a task this instance owns — the local path, never delegated.

        Shared by resume_task and the outbox delivery of a delegated resume so
        the audit contract is identical wherever it executes: validation
        (running/completed), an idempotent answer when ``resumed_by`` already
        records a continuation, then a normal dispatch_task carrying
        ``source="resume"`` plus the resume_of/resumed_by links.
        """
        if task.status in {TaskStatus.queued, TaskStatus.running}:
            raise RuntimeError(
                f"task {task.task_id} is still {task.status.value}; "
                "pause_task or cancel_task it, or wait for it to finish"
            )
        if task.status == TaskStatus.completed:
            raise RuntimeError(
                f"task {task.task_id} already completed; send a follow-up with "
                f"dispatch_task(session_id={task.session_id}) instead"
            )
        # The resumed_by check and the continuation dispatch must serialize
        # per task: a delegated request delivered through the outbox can
        # overlap a local fallback resume of the same row in this process.
        lock = self._resume_locks.setdefault(task.task_id, asyncio.Lock())
        async with lock:
            if task.resumed_by is not None:
                continuation = self.tasks.get(task.resumed_by)
                if continuation is not None:
                    # A previous resume (or a delegated request this instance
                    # already served) dispatched the continuation; replaying
                    # returns it instead of starting a second turn on the same
                    # conversation.
                    return {
                        "resumed_from": task.task_id,
                        "task_id": continuation.task_id,
                        "session_id": continuation.session_id,
                        "agent": continuation.agent,
                        "model": continuation.model,
                        "effort": continuation.effort,
                        "reused": True,
                        **({"request_id": request_id} if request_id is not None else {}),
                    }
                # The recorded continuation was pruned or never persisted;
                # fall through and dispatch a fresh one — resumed_by is
                # re-stamped.
            session = self.sessions.get(task.session_id)
            if session is None:
                session, foreign = await self._adopt_dead_session(task.session_id)
                if session is None:
                    if foreign:
                        raise RuntimeError(
                            f"session {task.session_id} is owned by another live "
                            "Bridge instance and cannot be adopted here"
                        )
                    raise KeyError(
                        f"session {task.session_id} for task {task.task_id} is gone; "
                        "dispatch a fresh task instead"
                    )
            if session.proc_state == ProcState.dead:
                raise RuntimeError(
                    f"session {session.session_id} was ended; dispatch a fresh task instead"
                )
            latest = self._disk_task_row(task.task_id)
            if latest is not None and latest.get("resumed_by") and not task.resumed_by:
                # A sibling instance persisted a continuation between our
                # checks and dispatch (delegated resume racing a local
                # fallback): report that continuation instead of starting a
                # second turn.
                return self._delegated_resume_result(task.task_id, row=latest, delegated=False)
            text = (message or "").strip() or RESUME_DEFAULT_MESSAGE
            result = await self.dispatch_task(
                agent=session.agent,
                message=text,
                cwd=session.cwd,
                session_id=session.session_id,
                user_requested=True,
                request_id=request_id,
                source="resume",
            )
            new_task = self.tasks.get(result["task_id"])
            if new_task is not None:
                new_task.resume_of = task.task_id
            task.resumed_by = result["task_id"]
            self.save()
            return {"resumed_from": task.task_id, **result}

    def _delegated_resume_poll(self, done_path: Path, task_id: str) -> tuple[str, Any]:
        """One observation of the delegated-resume channels.

        Returns ``("done", payload)`` when the executing instance answered,
        ``("continued", row)`` once the task row carries ``resumed_by`` — the
        continuation is already dispatched even if the done record was lost —
        ``("waiting", row)`` while a live non-owner holds the row,
        ``("adoptable", row)`` when the recorded owner is dead or absent, and
        ``("gone", None)`` when the row vanished entirely.
        """
        if done_path.is_file():
            return "done", read_json(done_path, {})
        try:
            row = self._disk_task_row(task_id)
        except PermissionError:
            # A persistently locked state file proves nothing about the row —
            # keep waiting on the last known live owner rather than calling
            # it gone or adoptable.
            return "waiting", {}
        if row is None:
            return "gone", None
        if row.get("resumed_by"):
            return "continued", row
        owner_pid = row.get("owner_pid")
        owner_create_time = row.get("owner_create_time")
        if self._is_mine(owner_pid, owner_create_time) or not owner_alive(
            owner_pid, owner_create_time
        ):
            return "adoptable", row
        return "waiting", row

    def _delegated_resume_result(
        self,
        task_id: str,
        *,
        payload: dict | None = None,
        row: dict | None = None,
        delegated: bool = True,
    ) -> dict:
        """Normalize a delegated answer into resume_task's response shape.

        ``payload`` is the executing instance's done record; ``row`` is the
        task's persisted row used when ``resumed_by`` landed but the done
        record never did — the continuation's own row supplies the fields.
        """
        if payload is not None:
            result = {
                key: value
                for key, value in payload.items()
                if key not in {"ok", "state", "error", "error_code", "code"}
            }
            result.setdefault("resumed_from", task_id)
            result["delegated"] = delegated
            return result
        row = row or {}
        continuation_id = row.get("resumed_by")
        continuation = (
            self._disk_task_row(continuation_id)
            if isinstance(continuation_id, str)
            else None
        ) or {}
        return {
            "resumed_from": task_id,
            "task_id": continuation_id,
            "session_id": continuation.get("session_id") or row.get("session_id"),
            "agent": continuation.get("agent") or row.get("agent"),
            "model": continuation.get("model"),
            "effort": continuation.get("effort"),
            "delegated": delegated,
        }

    async def _resume_via_owner(
        self,
        task_id: str,
        message: str | None,
        request_id: str | None,
    ) -> dict:
        """Route the resume to the live Bridge instance owning the task row.

        Drops a ``req_resume_*.json`` record into the shared outbox; the owning
        instance (or the first live instance to adopt a dead owner's row)
        claims it, runs ``_resume_owned``, and answers through ``done/``.
        While waiting, the task row's ``resumed_by`` is watched as well — it
        proves a continuation was dispatched even when the done record is
        lost. If the recorded owner dies mid-wait, a short grace covers an
        in-flight claim, then the row is adopted and resumed locally. The
        request expires with this wait so it can never dispatch afterwards.
        """
        if request_id is not None:
            try:
                request_id = str(uuid.UUID(request_id))
            except ValueError:
                raise ValueError("request_id must be a UUID") from None
        outbox = self.home / "outbox"
        try:
            outbox.mkdir(exist_ok=True)
            name = f"req_resume_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.json"
            record: dict[str, Any] = {
                "kind": "resume",
                "task_id": task_id,
                "message": message,
                "request_id": request_id,
                "ts": time.time(),
                "expire_ts": time.time() + RESUME_DELEGATE_TIMEOUT_SEC,
                "requester_pid": self._owner_pid,
                "requester_create_time": self._owner_create_time,
            }
            atomic_write_json(outbox / name, record)
        except OSError as exc:
            raise RuntimeError(
                f"task {task_id} is owned by another live Bridge instance and "
                f"the resume could not be queued for it: {exc}"
            ) from exc
        done_path = outbox / "done" / name
        deadline = time.monotonic() + RESUME_DELEGATE_TIMEOUT_SEC
        last_owner: dict[str, Any] = {}
        grace_until: float | None = None
        try:
            while True:
                state, data = self._delegated_resume_poll(done_path, task_id)
                if state == "done":
                    with contextlib.suppress(OSError):
                        done_path.unlink()
                    payload = data if isinstance(data, dict) else {}
                    if payload.get("ok"):
                        return self._delegated_resume_result(task_id, payload=payload)
                    raise RuntimeError(
                        str(payload.get("error"))
                        or f"delegated resume of task {task_id} failed"
                    )
                if state == "continued":
                    return self._delegated_resume_result(task_id, row=data)
                if state == "gone":
                    raise KeyError(f"unknown task {task_id}")
                if state == "waiting":
                    last_owner = {
                        "pid": data.get("owner_pid"),
                        "create_time": data.get("owner_create_time"),
                    }
                    grace_until = None
                elif state == "adoptable":
                    if grace_until is None:
                        # The recorded owner died; a sibling may already hold
                        # a claim on the queued request — give it a moment.
                        grace_until = time.monotonic() + RESUME_FALLBACK_GRACE_SEC
                    elif time.monotonic() >= grace_until:
                        adopted, foreign = self._adopt_dead_task(task_id)
                        local = adopted if adopted is not None else self.tasks.get(task_id)
                        if local is not None:
                            log.info(
                                "resume_delegate_adopted task_id=%s (recorded owner died)",
                                task_id,
                            )
                            # Withdraw the queued request before dispatching —
                            # once this instance owns the row it must not let a
                            # queued copy race the local continuation.
                            with contextlib.suppress(OSError):
                                (outbox / name).unlink(missing_ok=True)
                            return await self._resume_owned(
                                local, message=message, request_id=request_id
                            )
                        if not foreign:
                            raise KeyError(f"unknown task {task_id}")
                        # A sibling adopted the row first and will serve the
                        # queued request; give the new owner a fresh window.
                        grace_until = time.monotonic() + RESUME_FALLBACK_GRACE_SEC
                if self._stopping:
                    raise RuntimeError("bridge is shutting down; delegated resume aborted")
                if time.monotonic() >= deadline:
                    owner_pid = last_owner.get("pid")
                    owner = f"pid {owner_pid}" if owner_pid is not None else "its owner"
                    raise RuntimeError(
                        f"task {task_id} is owned by another live Bridge instance "
                        f"({owner}); the delegated resume did not finish within "
                        f"{int(RESUME_DELEGATE_TIMEOUT_SEC)}s. The request is now "
                        "expired — check_task / wait_task will report any "
                        "continuation the owner still dispatched, or retry resume_task."
                    )
                await asyncio.sleep(RESUME_DELEGATE_POLL_SEC)
        finally:
            # Withdraw the request if it was never claimed — a late delivery
            # after a local fallback or a caller timeout must not dispatch a
            # second continuation.
            with contextlib.suppress(OSError):
                (outbox / name).unlink(missing_ok=True)

    def list_sessions(self, active_only: bool = False) -> list[dict]:
        rows = []
        for session in self.sessions.values():
            if active_only and session.proc_state in {ProcState.dead, ProcState.idle_unloaded}:
                continue
            rows.append(
                {
                    "session_id": session.session_id,
                    "agent": session.agent,
                    "cwd": session.cwd,
                    "native_session_id": session.native_session_id,
                    "proc_state": session.proc_state.value,
                    "turns": session.turns,
                    "title": session.title,
                    "model": session.model,
                    "effort": session.effort,
                    "last_active_at": session.last_active_at,
                }
            )
        return rows

    async def end_session(self, session_id: str) -> dict:
        if not self.dispatch_enabled:
            raise RuntimeError(NESTED_END_SESSION_ERROR)
        session = self.sessions.get(session_id)
        if session is None:
            # A session whose owner died after this instance booted is
            # adoptable here; a live sibling's session is not ours to end.
            session, foreign = await self._adopt_dead_session(session_id)
            if session is None:
                if foreign:
                    raise RuntimeError(
                        f"session {session_id} is owned by another live "
                        "Bridge instance and cannot be ended from here"
                    )
                raise KeyError(f"unknown session {session_id}")
        busy = self._busy_task(session_id)
        if busy is not None:
            await self.cancel_task(busy.task_id)
        adapter = self._adapters.pop(session_id, None)
        if adapter is not None:
            await adapter.shutdown(session)
        self._cancel_idle(session_id)
        forget_worker_activity(session_id, self.home)
        session.proc_state = ProcState.dead
        session.pid = None
        self.save()
        return {"session_id": session_id, "proc_state": session.proc_state.value}
