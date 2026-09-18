from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from agent_bridge.adapters.base import STDIO_LIMIT, Adapter
from agent_bridge.config import AgentConfig
from agent_bridge.models import Session, Task, TurnResult, agy_effort
from agent_bridge.processes import (
    drop_pid,
    process_create_time,
    process_image_name,
    reap_subprocess,
    record_pid,
    resolve_command,
)
from agent_bridge.transcript import append_event
from agent_bridge.usage import DeltaUsage
from agent_bridge.worker_env import build_worker_env

log = logging.getLogger(__name__)

STDERR_TAIL_LIMIT = 16000
_RESULT_STATUSES = {"SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING"}
_TOOL_SCHEMA_MARKERS = (
    "invalid tool call error",
    "invalid_signature",
    "codecontent is a required parameter",
    "convert tool call for permissions",
)


def _scoped_usage(usage: dict[str, Any], resumed: bool) -> dict[str, Any]:
    if not usage:
        return usage
    return {**usage, "scope": "conversation" if resumed else "turn"}


def conversation_id_of(obj: dict[str, Any]) -> str | None:
    for key in ("conversation_id", "conversationId"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for nested_key in ("init", "step_update", "result"):
        nested = obj.get(nested_key)
        if isinstance(nested, dict):
            found = conversation_id_of(nested)
            if found:
                return found
    return None


def is_result_event(obj: dict[str, Any]) -> bool:
    if obj.get("event") == "result":
        return True
    if obj.get("type") in {"result", "final", "turn_complete", "completed"}:
        return True
    return obj.get("status") in _RESULT_STATUSES and ("response" in obj or "error" in obj)


def unwrap_result(obj: dict[str, Any]) -> dict[str, Any]:
    nested = obj.get("result")
    if isinstance(nested, dict) and (nested.get("status") or nested.get("response") or nested.get("error")):
        return nested
    return obj


def text_delta_of(obj: dict[str, Any]) -> str:
    step = obj.get("step_update")
    if isinstance(step, dict):
        delta = step.get("text_delta")
        if isinstance(delta, str) and delta:
            return delta
    for key in ("text_delta", "delta"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def result_text_of(obj: dict[str, Any]) -> str:
    payload = unwrap_result(obj)
    for key in ("response", "result", "text", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            nested = result_text_of(value)
            if nested:
                return nested
    content = payload.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def result_error_of(obj: dict[str, Any]) -> str | None:
    payload = unwrap_result(obj)
    status = str(payload.get("status") or "")
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error
    if status and status != "SUCCESS":
        return status
    return None


def is_agy_tool_schema_error(error: str | None) -> bool:
    """agy/cortex rejected a model tool call while converting it for permissions.

    This is not a Bridge write adapter. Headless agy executes tools itself; the
    model sometimes omits ``CodeContent`` on ``write_to_file``. The run can still
    recover, write files, and exit 0 with a full response.
    """
    if not error:
        return False
    text = error.lower()
    return any(marker in text for marker in _TOOL_SCHEMA_MARKERS)


# agy model slugs ending in an effort level pin that level: agy rejects e.g.
# --model gemini-3.8-flash-medium --effort high outright instead of running.
_AGY_EFFORT_SUFFIXES = ("low", "medium", "high")


def agy_model_effort(model: str | None) -> str | None:
    """The effort level an agy model slug already pins, else None."""
    if not model:
        return None
    _head, sep, tail = model.strip().rpartition("-")
    if not sep:
        return None
    tail = tail.lower()
    return tail if tail in _AGY_EFFORT_SUFFIXES else None


def check_agy_model_effort(model: str | None, effort: str | None) -> None:
    """Fail early when an explicit effort contradicts the slug's pinned level.

    ``effort`` is a Bridge-level token (off/low/medium/high/max); it is mapped
    onto agy's low/medium/high before comparing.
    """
    pinned = agy_model_effort(model)
    mapped = agy_effort(effort)
    if pinned is None or mapped is None or mapped == pinned:
        return
    raise ValueError(
        f'invalid model selection: --model "{model}" already pins effort={pinned}; '
        f"effort {effort!r} maps to {mapped} and conflicts with --effort — "
        f"use the -{mapped} variant of this model or omit effort"
    )


def stream_json_prompt(message: str) -> bytes:
    return (
        json.dumps(
            {"event": "user", "message": {"role": "user", "content": message}},
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def recovered_agy_tool_error(
    result: dict[str, Any],
    response: str,
    exit_code: int | None,
) -> bool:
    error = result_error_of(result)
    if not is_agy_tool_schema_error(error):
        return False
    if not (response or "").strip():
        return False
    return exit_code in (0, None)


# Tool names that can write to the workspace. Read-only tools (view_file,
# list_dir, grep_search, ...) also carry path params; counting those would
# report files the agent merely looked at. Under-matching is safe because the
# disk snapshot diff in merge_files_changed still catches every real write.
_MUTATING_TOOL_MARKERS = (
    "write",
    "edit",
    "replace",
    "delete",
    "remove",
    "move",
    "rename",
    "patch",
    "create",
)


def collect_tool_paths(obj: dict[str, Any], into: set[str]) -> None:
    step = obj.get("step_update") if isinstance(obj.get("step_update"), dict) else obj
    if not isinstance(step, dict):
        return
    info = step.get("tool_info")
    if not isinstance(info, dict):
        return
    name = str(info.get("name") or step.get("tool_name") or "").lower()
    if not any(marker in name for marker in _MUTATING_TOOL_MARKERS):
        return
    params = info.get("parameters")
    if not isinstance(params, dict):
        return
    for key in ("Path", "path", "file", "filePath", "TargetFile", "AbsolutePath"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            into.add(value)


# --- transcript normalization -------------------------------------------------
#
# Every consumer of the transcript (the dashboard's normalize_event, the feed
# JS, recent_activity, the live-usage tail) expects the flat schema the ACP
# adapter writes: data.text / data.title / data.input / data.tool_call_id /
# data.kind / data.status / data.consumed. agy stream-json records carry those
# values inside step_update, so classification alone is not enough — the fields
# must be lifted out at write time.

_TOOL_IO_LIMIT = 500
_TOOL_PATH_KEYS = ("AbsolutePath", "TargetFile", "filePath", "Path", "path", "file")
_TOOL_FAIL_STATES = {"FAILED", "FAILURE", "ERROR", "CANCELLED", "CANCELED", "INTERRUPTED"}

_AGY_TOOL_KINDS = {
    "view_file": "read",
    "read_file": "read",
    "open_file": "read",
    "list_dir": "read",
    "find_by_name": "search",
    "find_in_file": "search",
    "grep_search": "search",
    "search_web": "search",
    "web_search": "search",
    "run_command": "execute",
    "send_command_input": "execute",
    "command_status": "execute",
    "run_terminal_command": "execute",
    "write_to_file": "edit",
    "replace_file_content": "edit",
    "multi_replace_file_content": "edit",
    "sed_file": "edit",
    "notebook_edit": "edit",
    "create_file": "edit",
    "read_url_content": "fetch",
    "fetch_url": "fetch",
}
# Substring fallbacks for names the table does not know. Ordered most to
# least specific so e.g. "command" never outranks an edit/search marker.
_AGY_KIND_HINTS = (
    ("edit", ("write", "edit", "replace", "patch", "create", "delete", "rename")),
    ("search", ("search", "find", "grep")),
    ("execute", ("command", "exec", "shell", "terminal", "run")),
    ("read", ("read", "view", "list", "open", "show")),
    ("fetch", ("fetch", "url")),
)


def agy_tool_kind(name: str) -> str:
    """Map an agy tool name to the dashboard's kind taxonomy."""
    key = name.strip().lower()
    if key in _AGY_TOOL_KINDS:
        return _AGY_TOOL_KINDS[key]
    for kind, markers in _AGY_KIND_HINTS:
        if any(marker in key for marker in markers):
            return kind
    return "tool"


def _tool_io_summary(value: Any) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= _TOOL_IO_LIMIT else text[:_TOOL_IO_LIMIT] + "…"


def _first_text(*sources: dict[str, Any], keys: tuple[str, ...]) -> str:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _tool_call_id(obj: dict[str, Any], step: dict[str, Any], conversation_id: str | None) -> str:
    """Stable per-call id: conversation-scoped so resumed turns cannot collide."""
    explicit = step.get("tool_call_id") or obj.get("tool_call_id") or obj.get("id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    cid = step.get("conversation_id") or obj.get("conversation_id") or conversation_id or "agy"
    return f"{cid}:{step.get('step_index', obj.get('step_index', 0))}"


def _tool_path(step: dict[str, Any]) -> str | None:
    info = step.get("tool_info")
    params = info.get("parameters") if isinstance(info, dict) else None
    if not isinstance(params, dict):
        return None
    for key in _TOOL_PATH_KEYS:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _tool_event(
    obj: dict[str, Any], step: dict[str, Any], conversation_id: str | None
) -> tuple[str, dict[str, Any]]:
    """Normalize a tool step: ACTIVE starts a row; DONE/other terminal states
    close it as a tool_call_update the dashboard pairs by tool_call_id."""
    info = step.get("tool_info")
    if not isinstance(info, dict):
        info = {}
    name = str(info.get("name") or step.get("tool_name") or obj.get("tool_name") or "")
    state = str(step.get("state") or obj.get("state") or "").upper()
    error = _first_text(info, step, keys=("error", "error_message", "message"))
    call_id = _tool_call_id(obj, step, conversation_id)
    if state == "DONE" or state in _TOOL_FAIL_STATES:
        failed = state in _TOOL_FAIL_STATES or bool(error)
        data: dict[str, Any] = {
            "tool_call_id": call_id,
            "status": "failed" if failed else "completed",
        }
        if name:
            data["title"] = name
        output = _tool_io_summary(info.get("output") or error or None)
        if output is not None:
            data["output"] = output
        return "tool_call_update", data
    data = {"tool_call_id": call_id, "title": name or "tool", "kind": agy_tool_kind(name)}
    summary = _tool_io_summary(info.get("parameters"))
    if summary is not None:
        data["input"] = summary
    path = _tool_path(step)
    if path:
        data["path"] = path
        data["locations"] = [path]
    return "tool_call", data


def classify_event(
    obj: dict[str, Any],
    step: dict[str, Any],
    step_type: str,
    event: str,
    chunk: str,
    conversation_id: str | None,
) -> tuple[str, dict[str, Any]]:
    """Map one agy stream-json record to a transcript ``(type, data)`` pair.

    Records that carry no user-visible activity (init, results, usage-only
    DONE steps, text-less error/checkpoint steps) stay ``raw`` — kept for
    ``get_transcript`` diagnostics, dropped by the dashboard.
    """
    if event == "init":
        return "raw", {}
    # Explicit thought/reasoning shapes classify before the generic chunk
    # rule: a thinking record may carry text_delta, and letting `or chunk`
    # win would leak reasoning into the assistant's answer text.
    if step_type in {"checkpoint", "thought", "reasoning"} or event in {"thought", "reasoning"}:
        text = _first_text(
            step, obj, keys=("text", "text_delta", "thought", "thinking", "reasoning", "summary")
        )
        if text:
            return "thought_chunk", {"text": text}
        if step_type == "checkpoint":
            return "raw", {}
    if step_type == "agent_response" or chunk:
        if chunk:
            return "message_chunk", {"text": chunk}
        return "raw", {}
    if step_type == "tool" or event in {"tool", "tool_call"}:
        return _tool_event(obj, step, conversation_id)
    if step_type == "error_message":
        text = _first_text(step, obj, keys=("error", "text", "message", "text_delta", "detail"))
        return ("error", {"error": text}) if text else ("raw", {})
    return "raw", {}


# --- brain-transcript thought collection --------------------------------------
#
# agy's stdout stream-json never carries thinking text — only the
# usage.thinking_tokens counter. The full strings land in the conversation's
# private brain log at
#   ~/.gemini/antigravity-cli/brain/<cid>/.system_generated/logs/transcript_full.jsonl
# on type="PLANNER_RESPONSE" records in the string field "thinking". The tail
# below reads that file for the span of one turn and re-emits each new string
# as an ordinary thought_chunk so the existing dashboard pipeline renders it.
# It is strictly observational: nothing under ~/.gemini is created, written,
# truncated, or locked, and no failure here may fail the turn.

BRAIN_POLL_SEC = 0.4
BRAIN_DRAIN_GRACE_SEC = 0.3


def agy_brain_root() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "brain"


def agy_brain_log(conversation_id: str) -> Path:
    return (
        agy_brain_root()
        / conversation_id
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )


class _BrainThoughtTail:
    """Read-only, turn-scoped tail of one conversation's brain log.

    ``bind`` fixes the file and the pre-turn boundary: a resumed conversation
    snapshots the current end of file (called before agy launches) so earlier
    turns are never replayed; a new conversation owns the whole file, even
    the part written before the stdout init revealed the id.
    """

    def __init__(self, session_id: str, home: Path) -> None:
        self.session_id = session_id
        self._home = home
        self._path: Path | None = None
        self._offset = 0
        self._partial = b""
        # A resumed turn whose log is not yet readable keeps a pending
        # boundary: the first drain that can stat the file snaps to its EOF
        # instead of replaying earlier turns from byte 0.
        self._resumed_pending = False
        # Occurrence counts per thought text, per source. A logical thought
        # can surface both on stdout and in the brain log, so dedup pairs
        # occurrences across sources rather than collapsing identical text:
        # the nth record from one source is a duplicate only while it does
        # not outnumber the other source's count. Two distinct brain records
        # with identical text each emit; a stdout/brain pair emits once.
        self._from_stdout: Counter[str] = Counter()
        self._from_brain: Counter[str] = Counter()

    @property
    def bound(self) -> bool:
        return self._path is not None

    def bind(self, conversation_id: str, *, resumed: bool) -> None:
        if self._path is not None:
            return
        self._path = agy_brain_log(conversation_id)
        if resumed:
            try:
                self._offset = self._path.stat().st_size
            except OSError:
                self._resumed_pending = True

    def note_stdout(self, text: Any) -> bool:
        """Register a stdout-side thought text for cross-source dedup.

        True when this occurrence is new and should be emitted; False when
        the brain tail already delivered that logical thought.
        """
        if not isinstance(text, str) or not text:
            return True
        self._from_stdout[text] += 1
        return self._from_stdout[text] > self._from_brain[text]

    def drain(self) -> None:
        """Emit thinking strings flushed since the last pass, in file order.

        Missing files, partial tail lines, malformed records, and IO errors
        are skipped — nothing here may raise into the turn.
        """
        path = self._path
        if path is None:
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        if self._resumed_pending:
            # First readable observation of a resumed log: bytes already on
            # disk predate this turn, so the boundary lands at EOF and only
            # later appends are read.
            self._resumed_pending = False
            self._offset = size
            self._partial = b""
            return
        if size < self._offset:
            # Rotated or truncated mid-turn. Skipping to the new EOF avoids
            # replaying earlier turns' thinking from a rewritten file.
            self._offset = size
            self._partial = b""
            return
        if size == self._offset:
            return
        try:
            with path.open("rb") as fh:
                fh.seek(self._offset)
                blob = fh.read()
        except OSError:
            return
        self._offset += len(blob)
        lines = (self._partial + blob).split(b"\n")
        self._partial = lines.pop()
        for raw in lines:
            self._record(raw)

    def _record(self, raw: bytes) -> None:
        try:
            rec = json.loads(raw)
        except ValueError:
            return
        if not isinstance(rec, dict) or rec.get("type") != "PLANNER_RESPONSE":
            return
        thinking = rec.get("thinking")
        if not isinstance(thinking, str) or not thinking.strip():
            return
        self._from_brain[thinking] += 1
        if self._from_brain[thinking] <= self._from_stdout[thinking]:
            # stdout already delivered this logical thought.
            return
        try:
            append_event(self.session_id, "thought_chunk", {"text": thinking}, self._home)
        except Exception:
            log.debug("thought_chunk persist failed for %s", self.session_id, exc_info=True)

    async def finish(self) -> None:
        """Post-exit passes so a flush landing as agy dies is still caught."""
        if self._path is None:
            return
        self.drain()
        if BRAIN_DRAIN_GRACE_SEC > 0:
            await asyncio.sleep(BRAIN_DRAIN_GRACE_SEC)
            self.drain()


class AgyAdapter(Adapter):
    resident = False

    def __init__(self, agent: AgentConfig, home: Path, env_config=None) -> None:
        super().__init__(agent, home, env_config)
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        # Session ids whose current turn was cancelled via cancel()/shutdown().
        # Stopping the process makes agy stdout hit EOF, which is otherwise
        # indistinguishable from a normal end of stream.
        self._cancelled: set[str] = set()

    async def ensure_session(self, session: Session) -> None:
        return None

    def _build_cmd(self, session: Session, task: Task) -> list[str]:
        cmd = resolve_command(self.agent.command, self.agent.fallback_commands)
        model = task.model or session.model
        check_agy_model_effort(model, task.effort or session.effort)
        effort = agy_effort(task.effort or session.effort)
        # --model/--effort before -p: print mode can swallow later flags.
        if model:
            cmd += ["--model", model]
        if effort and agy_model_effort(model) is None:
            # A -low/-medium/-high slug already carries its level; resending
            # --effort is redundant and agy can reject the pair.
            cmd += ["--effort", effort]
        # Process cwd is the workspace, but default-cli-project can keep a
        # stale scratch root. Pin this turn to the requested folder before -p.
        if session.cwd:
            cmd += ["--add-dir", session.cwd]
        if session.native_session_id:
            cmd += ["--conversation", session.native_session_id]
        else:
            cmd += ["--new-project"]
        cmd += [
            "-p",
            "",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--print-timeout",
            self.agent.print_timeout or "120m",
        ]
        return cmd

    async def _watch_brain(self, thoughts: _BrainThoughtTail) -> None:
        """Poll the brain log so thoughts interleave with tool/answer events
        instead of collapsing to the end of the turn."""
        while True:
            try:
                thoughts.drain()
            except Exception:
                log.debug("agy brain drain failed for %s", thoughts.session_id, exc_info=True)
            await asyncio.sleep(BRAIN_POLL_SEC)

    async def _spawn(
        self, cmd: list[str], env: dict[str, str], cwd: str | None, kwargs: dict[str, Any]
    ) -> asyncio.subprocess.Process:
        """Create the agy subprocess under cancellation-safe ownership.

        ``create_subprocess_exec`` is itself a coroutine: a cancel delivered
        mid-spawn can leave a live child whose ``Process`` handle was never
        returned. Running the spawn inside a shielded task keeps the handle
        recoverable so the child is reaped instead of orphaned.
        """
        spawn = asyncio.ensure_future(
            asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
                limit=STDIO_LIMIT,
                **kwargs,
            )
        )
        try:
            return await asyncio.shield(spawn)
        except asyncio.CancelledError:
            proc: asyncio.subprocess.Process | None = None
            try:
                # The shielded spawn keeps running; wait briefly for the OS
                # to hand over the handle so the child can be reaped.
                proc = await asyncio.wait_for(asyncio.shield(spawn), timeout=10)
            except BaseException:
                if not spawn.done():
                    spawn.cancel()
                elif not spawn.cancelled() and spawn.exception() is None:
                    proc = spawn.result()
            await reap_subprocess(proc)
            raise

    async def _drain_stderr(self, proc: asyncio.subprocess.Process, session_id: str) -> str:
        if proc.stderr is None:
            return ""
        tail = ""
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return tail
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    tail = f"{tail}\n{text}"[-STDERR_TAIL_LIMIT:]
        except (ValueError, OSError):
            log.warning("stderr drain aborted for %s", session_id, exc_info=True)
            return tail

    async def _write_prompt(self, proc: asyncio.subprocess.Process, message: str) -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(stream_json_prompt(message))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            log.debug("agy closed stdin before reading the prompt for pid %s: %s", proc.pid, exc)
        finally:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                proc.stdin.close()

    def _finish_usage(
        self,
        session: Session,
        tracker: DeltaUsage,
        conversation_id: str | None,
        resumed: bool,
    ) -> dict[str, Any]:
        """Final run_usage; persist the conversation counters so the next
        resumed turn's delta is attributable even across a bridge restart."""
        if tracker.seen:
            prev = session.usage_baseline.get("counters")
            same_conv = session.usage_baseline.get("cid") == conversation_id
            merged = dict(prev) if same_conv and isinstance(prev, dict) else {}
            merged.update(tracker.latest)
            session.usage_baseline = {"cid": conversation_id, "counters": merged}
        return tracker.run_usage(resumed=resumed)

    async def run_turn(self, session: Session, task: Task) -> TurnResult:
        """Drive one agy turn.

        ``usage.scope`` is ``conversation`` when this turn resumes an existing
        conversation (those counters cover every turn so far); otherwise ``turn``.
        """
        cmd = self._build_cmd(session, task)
        env = build_worker_env(self.agent.env, config=self.env_config, worker_context=True)
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        append_event(
            session.session_id,
            "prompt_sent",
            {"text": task.message, "cmd": cmd[:6], "task_id": task.task_id, "source": task.source},
            self.home,
        )
        # A resumed conversation's pre-turn boundary must be fixed before
        # agy starts appending; a new conversation binds once its id arrives.
        thoughts = _BrainThoughtTail(session.session_id, self.home)
        if session.native_session_id:
            thoughts.bind(session.native_session_id, resumed=True)
        proc: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[str] | None = None
        brain_task: asyncio.Task[None] | None = None
        text_parts: list[str] = []
        conversation_id = session.native_session_id
        resumed = bool(session.native_session_id)
        usage: dict[str, Any] = {}
        # Resumed conversations report conversation-cumulative counters; the
        # persisted baseline from the previous turn isolates this run's delta.
        base = session.usage_baseline
        stored = base.get("counters") if isinstance(base.get("counters"), dict) else {}
        baseline = stored if resumed and base.get("cid") == session.native_session_id else {}
        tracker = DeltaUsage(baseline=baseline, count_first=not resumed)
        files: set[str] = set()
        last_result: dict[str, Any] | None = None
        usage_emit: tuple | None = None
        mid_error_steps = 0

        def mid_error_warnings() -> list[str]:
            # agy absorbs recoverable provider errors as opaque error_message
            # steps (no payload text); without a count, a turn that recovered
            # — or then died — leaves no trace of the instability.
            if not mid_error_steps:
                return []
            return [f"agy reported {mid_error_steps} mid-turn error step(s) before the turn ended"]

        try:
            # The spawn itself is inside the ownership block: a cancel or
            # error anywhere from here on still reaps the child and unwinds
            # the watcher/stderr tasks in the finally below.
            proc = await self._spawn(cmd, env, task.cwd or session.cwd, kwargs)
            self._procs[session.session_id] = proc
            self._cancelled.discard(session.session_id)
            if proc.pid:
                record_pid(
                    self.home,
                    session.session_id,
                    proc.pid,
                    process_create_time(proc.pid),
                    process_image_name(proc.pid),
                )
            stderr_task = asyncio.create_task(self._drain_stderr(proc, session.session_id))
            brain_task = asyncio.create_task(self._watch_brain(thoughts))
            await self._write_prompt(proc, task.message)
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                raw = line.decode("utf-8", errors="replace").rstrip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    append_event(session.session_id, "raw", {"text": raw[:2000]}, self.home)
                    text_parts.append(raw)
                    continue
                if not isinstance(obj, dict):
                    append_event(session.session_id, "raw", {"value": obj}, self.home)
                    continue
                cid = conversation_id_of(obj)
                if cid:
                    conversation_id = cid
                    if not thoughts.bound:
                        # New conversation: the whole brain file belongs to
                        # this turn, even bytes flushed before this line.
                        thoughts.bind(cid, resumed=False)
                event = str(obj.get("event") or obj.get("type") or "")
                raw_step = obj.get("step_update")
                step: dict[str, Any] = raw_step if isinstance(raw_step, dict) else {}
                step_type = str(step.get("step_type") or "")
                if step_type == "error_message":
                    mid_error_steps += 1
                chunk = text_delta_of(obj)
                event_type, data = classify_event(obj, step, step_type, event, chunk, conversation_id)
                if event_type == "thought_chunk" and not thoughts.note_stdout(data.get("text")):
                    # The brain tail already delivered this logical thought.
                    event_type, data = "raw", {}
                if event_type == "message_chunk":
                    text_parts.append(chunk)
                elif event_type in {"tool_call", "tool_call_update"}:
                    collect_tool_paths(obj, files)
                # The raw record rides along for get_transcript fidelity; past
                # the line cap it is dropped but the normalized fields survive
                # so large tool outputs can never blank the dashboard.
                if len(raw) < 4000:
                    data["payload"] = obj
                else:
                    data["truncated"] = True
                append_event(session.session_id, event_type, data, self.home)
                envelope = unwrap_result(obj) if is_result_event(obj) else obj
                seen = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else step.get("usage")
                if isinstance(seen, dict):
                    usage = seen
                    tracker.update(seen)
                    # Live token counters: one usage event per distinct run
                    # snapshot (immediate-flush, deduped like ACP's emit_key).
                    consumed = tracker.run_usage(resumed=resumed)
                    emit_key = tuple(sorted(consumed.items()))
                    if emit_key != usage_emit:
                        usage_emit = emit_key
                        evt: dict[str, Any] = {"consumed": consumed}
                        if len(raw) < 4000:
                            evt["payload"] = obj
                        else:
                            evt["truncated"] = True
                        append_event(session.session_id, "usage", evt, self.home)
                if is_result_event(obj):
                    last_result = unwrap_result(obj)
            try:
                await asyncio.wait_for(proc.wait(), timeout=60)
            except TimeoutError:
                log.warning("agy stdout closed but process lingered; killing %s", proc.pid)
                await reap_subprocess(proc)
            stderr_tail = await stderr_task
            await thoughts.finish()
            for note in mid_error_warnings():
                append_event(session.session_id, "warning", {"error": note}, self.home)
            if session.session_id in self._cancelled:
                append_event(
                    session.session_id,
                    "turn_end",
                    {"stop_reason": "cancelled", "task_id": task.task_id},
                    self.home,
                )
                return TurnResult(
                    text="".join(text_parts),
                    files_changed=sorted(files),
                    stop_reason="cancelled",
                    usage=_scoped_usage(usage, resumed),
                    run_usage=self._finish_usage(session, tracker, conversation_id, resumed),
                    native_session_id=conversation_id,
                    warnings=mid_error_warnings(),
                )
            exit_warnings: list[str] = mid_error_warnings()
            if proc.returncode not in (0, None):
                exit_warnings.append(f"agy exited with code {proc.returncode}")
            if last_result is not None:
                err = result_error_of(last_result)
                result_text = result_text_of(last_result) or "".join(text_parts)
                cid = conversation_id_of(last_result) or conversation_id
                if isinstance(last_result.get("usage"), dict):
                    usage = last_result["usage"]
                    tracker.update(usage)
                session.native_session_id = cid
                if err and last_result.get("status") != "SUCCESS":
                    if recovered_agy_tool_error(last_result, result_text, proc.returncode):
                        append_event(
                            session.session_id,
                            "warning",
                            {"error": err, "code": proc.returncode, "treated_as": "recovered_tool_schema"},
                            self.home,
                        )
                        append_event(
                            session.session_id,
                            "turn_end",
                            {"stop_reason": "end_turn", "task_id": task.task_id},
                            self.home,
                        )
                        return TurnResult(
                            text=result_text,
                            files_changed=sorted(files),
                            stop_reason="end_turn",
                            usage=_scoped_usage(usage, resumed),
                            run_usage=self._finish_usage(session, tracker, cid, resumed),
                            native_session_id=cid,
                            warnings=[err, *exit_warnings],
                        )
                    append_event(session.session_id, "error", {"error": err, "code": proc.returncode}, self.home)
                    return TurnResult(
                        text=result_text,
                        files_changed=sorted(files),
                        stop_reason="error",
                        error=err,
                        usage=_scoped_usage(usage, resumed),
                        run_usage=self._finish_usage(session, tracker, cid, resumed),
                        native_session_id=cid,
                        warnings=mid_error_warnings(),
                    )
                append_event(
                    session.session_id,
                    "turn_end",
                    {"stop_reason": "end_turn", "task_id": task.task_id},
                    self.home,
                )
                return TurnResult(
                    text=result_text,
                    files_changed=sorted(files),
                    stop_reason="end_turn",
                    usage=_scoped_usage(usage, resumed),
                    run_usage=self._finish_usage(session, tracker, cid, resumed),
                    native_session_id=cid,
                    warnings=exit_warnings,
                )
            if proc.returncode not in (0, None) and not text_parts:
                detail = stderr_tail.strip()
                error = f"agy exit {proc.returncode}"
                if detail:
                    error += f": {detail}"
                return TurnResult(
                    text="",
                    stop_reason="error",
                    error=error,
                    usage=_scoped_usage(usage, resumed),
                    run_usage=self._finish_usage(session, tracker, conversation_id, resumed),
                    warnings=mid_error_warnings(),
                )
            session.native_session_id = conversation_id
            append_event(
                session.session_id,
                "turn_end",
                {"stop_reason": "end_turn", "task_id": task.task_id},
                self.home,
            )
            return TurnResult(
                text="".join(text_parts),
                files_changed=sorted(files),
                stop_reason="end_turn",
                usage=_scoped_usage(usage, resumed),
                run_usage=self._finish_usage(session, tracker, conversation_id, resumed),
                native_session_id=conversation_id,
                warnings=exit_warnings,
            )
        except asyncio.CancelledError:
            await reap_subprocess(proc)
            thoughts.drain()
            for note in mid_error_warnings():
                append_event(session.session_id, "warning", {"error": note}, self.home)
            append_event(
                session.session_id,
                "turn_end",
                {"stop_reason": "cancelled", "task_id": task.task_id},
                self.home,
            )
            return TurnResult(
                text="".join(text_parts),
                files_changed=sorted(files),
                stop_reason="cancelled",
                usage=_scoped_usage(usage, resumed),
                run_usage=self._finish_usage(session, tracker, conversation_id, resumed),
                native_session_id=conversation_id,
                warnings=mid_error_warnings(),
            )
        except Exception:
            # e.g. readline() past STDIO_LIMIT. Without this, agy would keep
            # running with nobody reading its stdout and never get reaped.
            await reap_subprocess(proc)
            raise
        finally:
            if brain_task is not None:
                if not brain_task.done():
                    brain_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await brain_task
            if stderr_task is not None:
                if not stderr_task.done():
                    stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task
            self._procs.pop(session.session_id, None)
            self._cancelled.discard(session.session_id)
            drop_pid(self.home, session.session_id)

    async def cancel(self, session: Session) -> None:
        self._cancelled.add(session.session_id)
        proc = self._procs.get(session.session_id)
        if proc is not None:
            await reap_subprocess(proc)

    async def shutdown(self, session: Session) -> None:
        await self.cancel(session)
        self._procs.pop(session.session_id, None)
