"""Transcript normalization, event reading, and the live-usage tail reader.

This is the data-ingestion domain of ``share/dashboard.py``: raw bridge
``transcripts/*.jsonl`` records become compact dashboard events, and an
incremental per-session tail keeps the overview's live-usage map cheap.

``dashboard.py`` re-exports every name here so its import surface is
unchanged. Path-dependent functions take ``transcript_dir`` (and the
``open`` callable) as explicit parameters: ``dashboard.TRANSCRIPT_DIR`` is
rebound by ``--dir`` and monkeypatched in tests, so the caller must pass
its current value rather than letting this module cache a stale copy.
"""

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")

MAX_INPUT_CHARS = 4000

# The Antigravity adapter used to persist the raw stream-json object as
# data.payload with no normalized fields; the flat names below lift those
# records back into the schema the feed expects. Keep in sync with
# adapters/antigravity.py's write-side mapping.
_AGY_TOOL_KINDS = {
    "view_file": "read", "read_file": "read", "open_file": "read",
    "list_dir": "read",
    "find_by_name": "search", "find_in_file": "search", "grep_search": "search",
    "search_web": "search", "web_search": "search",
    "run_command": "execute", "send_command_input": "execute",
    "command_status": "execute", "run_terminal_command": "execute",
    "write_to_file": "edit", "replace_file_content": "edit",
    "multi_replace_file_content": "edit", "sed_file": "edit",
    "notebook_edit": "edit", "create_file": "edit",
    "read_url_content": "fetch", "fetch_url": "fetch",
}
_AGY_KIND_HINTS = (
    ("edit", ("write", "edit", "replace", "patch", "create", "delete", "rename")),
    ("search", ("search", "find", "grep")),
    ("execute", ("command", "exec", "shell", "terminal", "run")),
    ("read", ("read", "view", "list", "open", "show")),
    ("fetch", ("fetch", "url")),
)
_AGY_DONE_STATES = {"DONE", "FAILED", "FAILURE", "ERROR", "CANCELLED", "CANCELED", "INTERRUPTED"}


def _agy_tool_kind(name):
    key = str(name or "").strip().lower()
    if key in _AGY_TOOL_KINDS:
        return _AGY_TOOL_KINDS[key]
    for kind, markers in _AGY_KIND_HINTS:
        if any(m in key for m in markers):
            return kind
    return "tool"


def _legacy_payload_flat(d):
    """Recover flat normalized fields from a legacy ``{"payload": <obj>}`` record.

    Payload-derived values only fill gaps — real ``data`` keys always win, so
    new records that carry both shapes are unaffected.
    """
    p = d.get("payload")
    if not isinstance(p, dict):
        return d
    step = p.get("step_update")
    if not isinstance(step, dict):
        step = {}
    flat = {}
    text = step.get("text_delta") or p.get("text_delta") or p.get("delta")
    if not (isinstance(text, str) and text):
        for source in (step, p):
            for key in ("text", "thought", "summary", "message", "error"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    text = value
                    break
            if text:
                break
    if isinstance(text, str) and text:
        flat["text"] = text
        flat["error"] = text
    info = step.get("tool_info")
    info = info if isinstance(info, dict) else {}
    is_tool = step.get("step_type") == "tool" or p.get("event") in {"tool", "tool_call"}
    if is_tool:
        name = info.get("name") or step.get("tool_name") or p.get("tool_name") or p.get("name")
        if name:
            flat["title"] = str(name)
            flat["kind"] = _agy_tool_kind(name)
        call_id = step.get("tool_call_id") or p.get("tool_call_id")
        if not (isinstance(call_id, str) and call_id):
            cid = step.get("conversation_id") or p.get("conversation_id") or "agy"
            call_id = f"{cid}:{step.get('step_index', p.get('step_index', 0))}"
        flat["tool_call_id"] = call_id
        params = info.get("parameters")
        if params is not None:
            flat["input"] = json.dumps(params, ensure_ascii=False, default=str)
        state = str(step.get("state") or p.get("state") or "").upper()
        if state in _AGY_DONE_STATES:
            err = info.get("error") or step.get("error")
            flat["status"] = "failed" if state != "DONE" or err else "completed"
            out = info.get("output") or step.get("output") or err
            if isinstance(out, str) and out.strip():
                flat["output"] = out[:4000]
            elif out is not None:
                flat["output"] = json.dumps(out, ensure_ascii=False, default=str)[:4000]
    merged = dict(flat)
    for k, v in d.items():
        if k != "payload" and v is not None:
            merged[k] = v
    return merged


def normalize_event(rec):
    """Convert a raw transcript JSONL record into a compact dashboard event."""
    t = rec.get("type")
    ts = rec.get("ts")
    d = rec.get("data") or {}
    if isinstance(d.get("payload"), dict):
        d = _legacy_payload_flat(d)

    if t == "prompt_sent":
        return {
            "t": "prompt",
            "ts": ts,
            "text": d.get("text", ""),
            "src": d.get("source"),
            "task": d.get("task_id"),
        }
    if t == "message_chunk":
        text = d.get("text", "")
        if not text:
            return None  # usage-only DONE steps carry no text to paint
        return {"t": "msg", "ts": ts, "text": text}
    if t == "thought_chunk":
        text = d.get("text", "")
        if not text:
            return None
        return {"t": "think", "ts": ts, "text": text}
    if t == "tool_call":
        raw_input = d.get("input")
        inp = raw_input
        if isinstance(raw_input, str):
            try:
                inp = json.loads(raw_input)
            except Exception:
                inp = raw_input
        if inp is None:
            s = ""
        elif isinstance(inp, str):
            s = inp
        else:
            s = json.dumps(inp, ensure_ascii=False)
        if s and len(s) > MAX_INPUT_CHARS:
            s = s[:MAX_INPUT_CHARS] + "…"
        ev = {
            "t": "tool",
            "ts": ts,
            "id": d.get("tool_call_id"),
            "kind": d.get("kind") or "tool",
            "title": d.get("title") or "",
            "input": s,
        }
        # Legacy DONE steps normalize to a complete row; addTool folds it into
        # the ACTIVE twin's row by id instead of painting a duplicate.
        if d.get("status"):
            ev["status"] = d["status"]
        if d.get("output"):
            ev["output"] = d["output"]
        return ev
    if t == "tool_call_update":
        status = d.get("status")
        if not status:
            return None
        return {"t": "tool_status", "ts": ts, "id": d.get("tool_call_id"), "status": status}
    if t == "usage":
        return {"t": "usage", "ts": ts, "consumed": d.get("consumed")}
    if t == "turn_end":
        return {
            "t": "turn",
            "ts": ts,
            "stop_reason": d.get("stop_reason"),
            "task": d.get("task_id"),
            "error": d.get("error"),
        }
    if t == "error":
        text = d.get("error")
        if not isinstance(text, str) or not text:
            text = d.get("text") or "error"
        return {"t": "error", "ts": ts, "text": str(text)}
    return None


def read_events(session_id, offset, transcript_dir, open_fn=open):
    """Read normalized events from a transcript starting at byte offset."""
    if not SAFE_ID.match(session_id or ""):
        return None, 0
    path = Path(transcript_dir) / f"{session_id}.jsonl"
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    if offset > size:  # file shrank / rotated — restart
        offset = 0
    with open_fn(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], offset
    chunk = data[: cut + 1]
    new_offset = offset + cut + 1
    events = []
    for line in chunk.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        ev = normalize_event(rec)
        if ev:
            events.append(ev)
    return events, new_offset


# Incremental per-session transcript tail reader for the batched live-usage
# map. Each entry caches the byte offset just past the last consumed newline
# plus the run's started_at, so steady-state polls cost ~one stat() plus the
# appended bytes — never a full transcript reread. A first sighting seeds
# near the tail (usage events almost always post-date it) with one bounded
# full-scan fallback; a file shrink/rotation or a new started_at resets.
LIVE_SEED_BYTES = 512 * 1024
_LIVE_TAIL: dict[str, dict] = {}
_LIVE_LOCK = threading.Lock()


def _instant(value) -> float | None:
    """Epoch seconds for an ISO-8601 timestamp, else None.

    State and transcripts may mix ``Z`` with numeric offsets (``+00:00``,
    ``+08:00``…), and other bridge writers can use local offsets — only an
    absolute-instant compare attributes events correctly across those
    forms. A naive stamp is read as UTC, matching the bridge's own
    convention; anything unparseable fails closed so the record is ignored
    rather than mis-attributed.
    """
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _usage_consumed_last(blob: bytes, started_epoch: float) -> dict | None:
    """Last usage record's consumed snapshot at/after ``started_epoch``.

    Events older than the run start belong to a prior run on the reusable
    session — the same attribution rule the bridge applies when it recovers
    a dead run's partial. ``blob`` must contain only complete JSONL lines.
    """
    found = None
    for line in blob.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "usage":
            continue
        ts = _instant(rec.get("ts"))
        if ts is None or ts < started_epoch:
            continue
        consumed = (rec.get("data") or {}).get("consumed")
        if isinstance(consumed, dict) and consumed:
            found = consumed
    return found


def _live_consumed(session_id: str, started_at: str, transcript_dir, open_fn=open) -> dict | None:
    """Latest consumed snapshot for the current run on ``session_id``."""
    if not SAFE_ID.match(session_id or ""):
        return None
    started_epoch = _instant(started_at)
    if started_epoch is None:
        return None
    path = Path(transcript_dir) / f"{session_id}.jsonl"
    with _LIVE_LOCK:
        try:
            size = path.stat().st_size
        except OSError:
            _LIVE_TAIL.pop(session_id, None)
            return None
        ent = _LIVE_TAIL.get(session_id)
        if ent is None:
            offset = max(0, size - LIVE_SEED_BYTES)
            ent = _LIVE_TAIL[session_id] = {
                "offset": offset,
                "mid": offset > 0,       # seed landed inside a record
                "started": started_at,
                "consumed": None,
                "seeded": offset > 0,    # one bounded full-scan fallback left
            }
        elif ent["started"] != started_at:
            # New run on the same session: earlier bytes belong to the prior
            # run and can never qualify again.
            ent["started"] = started_at
            ent["consumed"] = None
        if size < ent["offset"]:  # truncated/rotated — rescan from scratch
            ent.update(offset=0, mid=False, consumed=None, seeded=False)
        if size == ent["offset"]:
            return ent["consumed"]
        offset = ent["offset"]
        try:
            with open_fn(path, "rb") as f:
                f.seek(offset)
                data = f.read()
        except OSError:
            return ent["consumed"]
        cut = data.rfind(b"\n")
        if cut < 0:
            return ent["consumed"]
        blob = data[: cut + 1]
        ent["offset"] = offset + cut + 1
        if ent["mid"]:
            ent["mid"] = False
            blob = blob[blob.find(b"\n") + 1 :]  # drop the cut-open record
        found = _usage_consumed_last(blob, started_epoch)
        if found is not None:
            ent["consumed"] = found
            ent["seeded"] = False
        elif ent["seeded"] and ent["consumed"] is None:
            # Nothing qualifying in the seed window of a larger file: scan
            # the whole transcript once, then stay incremental forever.
            ent["seeded"] = False
            try:
                blob = path.read_bytes()
            except OSError:
                blob = b""
            found = _usage_consumed_last(blob, started_epoch)
            if found is not None:
                ent["consumed"] = found
        return ent["consumed"]


def live_usage_map(tasks, transcript_dir, open_fn=open) -> dict:
    """session_id -> {task_id, consumed} for every task still running.

    Covers sibling/remote-owned tasks uniformly: all bridge instances merge
    into the same state.json and share this transcript dir, so tailing by
    session_id needs no owner check. Finished sessions are pruned from the
    incremental cache on every pass.
    """
    running = {}
    for t in tasks:
        if not isinstance(t, dict) or t.get("status") != "running":
            continue
        sid = str(t.get("session_id") or "")
        if not t.get("started_at") or not SAFE_ID.match(sid):
            continue
        running[sid] = t
    with _LIVE_LOCK:
        for sid in list(_LIVE_TAIL):
            if sid not in running:
                del _LIVE_TAIL[sid]
    live = {}
    for sid, t in running.items():
        consumed = _live_consumed(sid, str(t.get("started_at")), transcript_dir, open_fn)
        if consumed:
            live[sid] = {"task_id": t.get("task_id"), "consumed": consumed}
    return live
