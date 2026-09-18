#!/usr/bin/env python3
"""Agent Bridge dashboard — view MCP bridge sessions and their conversations.

Reads state.json and transcripts/*.jsonl from the Agent Bridge data directory
and serves a live-updating web UI.

Usage:
    python dashboard.py [--port 8787] [--dir <bridge-data-dir>]

Then open http://127.0.0.1:8787
"""

import argparse
import contextlib
import json
import os
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import psutil
except ImportError:  # a standalone dashboard may run outside the bridge venv
    psutil = None

# This file doubles as a standalone script (python share/dashboard.py); with
# no package context the helper modules resolve as siblings on sys.path.
if __package__:
    from . import dashboard_events as _events
    from . import dashboard_outbox as _outbox
    from . import dashboard_page as _page
else:
    import dashboard_events as _events
    import dashboard_outbox as _outbox
    import dashboard_page as _page

BRIDGE_DIR = Path(os.environ.get("BRIDGE_DIR", Path(__file__).resolve().parent))
STATE_FILE = BRIDGE_DIR / "state.json"
TRANSCRIPT_DIR = BRIDGE_DIR / "transcripts"
OUTBOX_DIR = BRIDGE_DIR / "outbox"

# Open-tab presence: browser heartbeats via /api/presence; the bridge checks
# /api/client_state to decide whether to pop the dashboard up.
PRESENCE: dict[str, float] = {}  # client_id -> last-seen monotonic timestamp
PRESENCE_LOCK = threading.Lock()
# Chrome/Edge intensively throttle hidden-tab timers to ~one wake-up per
# minute (and sleeping/frozen tabs can miss one entirely), so the lease must
# span well over 60s or a backgrounded tab expires between beats and the
# bridge opens a duplicate. 180s spans two fully missed 60s wake-ups plus
# jitter; a genuinely closed tab still leaves promptly via the pagehide
# beacon.
PRESENCE_TTL = 180.0


def presence_update(client_id, bye):
    now = time.monotonic()
    with PRESENCE_LOCK:
        for cid, seen in list(PRESENCE.items()):
            if now - seen > PRESENCE_TTL:
                del PRESENCE[cid]
        if bye:
            PRESENCE.pop(client_id, None)
        elif client_id:
            PRESENCE[client_id] = now
        return len(PRESENCE)


def presence_count():
    now = time.monotonic()
    with PRESENCE_LOCK:
        for cid, seen in list(PRESENCE.items()):
            if now - seen > PRESENCE_TTL:
                del PRESENCE[cid]
        return len(PRESENCE)


SAFE_ID = _events.SAFE_ID

MAX_INPUT_CHARS = _events.MAX_INPUT_CHARS

# Outbox queue inspection, state loading, and the process-ownership checks
# live in dashboard_outbox; the names below keep this module's import
# surface identical. The path-dependent wrappers pass the current
# STATE_FILE / OUTBOX_DIR (rebound by --dir, monkeypatched in tests) plus
# this module's `open` and `psutil` (monkeypatched / optional), so every
# lookup stays dynamically resolved against this module's globals.
MSG_NAME_RE = _outbox.MSG_NAME_RE
REQ_NAME_RE = _outbox.REQ_NAME_RE
OUTBOX_MSG_MAX_AGE_SEC = _outbox.OUTBOX_MSG_MAX_AGE_SEC
TASK_ACTION_EXPIRE_SEC = _outbox.TASK_ACTION_EXPIRE_SEC


def _my_create_time():
    """This dashboard process's create time, for outbox claim/request stamps."""
    return _outbox._my_create_time(psutil)


def _owner_alive(pid, create_time):
    """Read-only liveness of a recorded bridge owner (pid + create time)."""
    return _outbox._owner_alive(pid, create_time, psutil)


def _dash_claim_name(name):
    """``name.<pid>[.<create_time>].claim`` — the bridge's atomic-rename claim."""
    return _outbox._dash_claim_name(name, psutil)


def _task_action_fields(task, dead_sessions):
    """(resumable, remote, owner_lost) for one persisted task row."""
    return _outbox._task_action_fields(task, dead_sessions, psutil)


def outbox_queue():
    """Queued dashboard chat records, grouped by session."""
    return _outbox.outbox_queue(OUTBOX_DIR)


def load_state():
    return _outbox.load_state(STATE_FILE, open)


def __getattr__(name):
    # The create-time cache lives in dashboard_outbox; resolving its two
    # globals here keeps dashboard._MY_CREATE_TIME* a live view of that
    # single cache instead of a stale copy taken at import time.
    if name in ("_MY_CREATE_TIME", "_MY_CREATE_TIME_SET"):
        return getattr(_outbox, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Transcript normalization, event reading, and the live-usage tail reader
# live in dashboard_events; the names below keep this module's import
# surface identical. The path-dependent wrappers pass the current
# TRANSCRIPT_DIR (rebound by --dir, monkeypatched in tests) plus this
# module's `open` (also monkeypatched) so both stay dynamically resolved.
_AGY_TOOL_KINDS = _events._AGY_TOOL_KINDS
_AGY_KIND_HINTS = _events._AGY_KIND_HINTS
_AGY_DONE_STATES = _events._AGY_DONE_STATES
_agy_tool_kind = _events._agy_tool_kind
_legacy_payload_flat = _events._legacy_payload_flat
normalize_event = _events.normalize_event
LIVE_SEED_BYTES = _events.LIVE_SEED_BYTES
_LIVE_TAIL = _events._LIVE_TAIL
_LIVE_LOCK = _events._LIVE_LOCK
_instant = _events._instant
_usage_consumed_last = _events._usage_consumed_last


def read_events(session_id, offset):
    """Read normalized events from a transcript starting at byte offset."""
    return _events.read_events(session_id, offset, TRANSCRIPT_DIR, open)


def _live_consumed(session_id: str, started_at: str) -> dict | None:
    """Latest consumed snapshot for the current run on ``session_id``."""
    return _events._live_consumed(session_id, started_at, TRANSCRIPT_DIR, open)


def live_usage_map(tasks) -> dict:
    """session_id -> {task_id, consumed} for every task still running.

    Covers sibling/remote-owned tasks uniformly: all bridge instances merge
    into the same state.json and share this transcript dir, so tailing by
    session_id needs no owner check. Finished sessions are pruned from the
    incremental cache on every pass.
    """
    return _events.live_usage_map(tasks, TRANSCRIPT_DIR, open)


# The single-page frontend document (HTML + CSS + JS) lives in
# dashboard_page; PAGE is re-exported so dashboard.PAGE resolves to
# the same string for package imports and direct script runs.
PAGE = _page.PAGE


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # The page is deliberately self-contained — inline script/style,
            # data: avatar images, same-origin /api calls only — so this CSP
            # pins that contract and hard-blocks any external network call a
            # future change might try to wire in.
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; img-src data:; "
                "connect-src 'self'; base-uri 'none'; form-action 'none'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/overview":
            state = load_state()
            dead_sessions = {
                str(s.get("session_id") or "")
                for s in state.get("sessions", [])
                if isinstance(s, dict) and s.get("proc_state") == "dead"
            }
            tasks = []
            for t in state.get("tasks", []):
                if not isinstance(t, dict):
                    continue
                row = {
                    k: t.get(k)
                    for k in (
                        "task_id",
                        "session_id",
                        "agent",
                        "status",
                        "stop_reason",
                        "paused",
                        "message",
                        "result_chars",
                        "files_changed",
                        "error",
                        "source",
                        "usage",
                        "run_usage",
                        "created_at",
                        "started_at",
                        "finished_at",
                        "resumed_by",
                        "resume_of",
                    )
                }
                (
                    row["resumable"],
                    row["remote"],
                    row["owner_lost"],
                ) = _task_action_fields(t, dead_sessions)
                tasks.append(row)
            self._json(
                {
                    "sessions": state.get("sessions", []),
                    "tasks": tasks,
                    # Queued chat records still sitting in outbox/, grouped by
                    # session — the queue cards above the composer render
                    # straight from this, so they survive refresh naturally.
                    "outbox": outbox_queue(),
                    # One batched lookup for every running task's live usage —
                    # the sidebar reads per-agent counters from here instead
                    # of issuing per-session transcript requests.
                    "live": live_usage_map(state.get("tasks", [])),
                }
            )
            return
        if u.path == "/api/events":
            q = parse_qs(u.query)
            session = (q.get("session") or [""])[0]
            try:
                offset = int((q.get("offset") or ["0"])[0])
            except ValueError:
                offset = 0
            path = TRANSCRIPT_DIR / f"{session}.jsonl"
            reset = False
            if path.exists() and offset > path.stat().st_size:
                reset = True
            events, new_offset = read_events(session, offset)
            if events is None:
                self._json({"error": "bad session", "error_code": "bad_session"}, 400)
                return
            self._json({"events": events, "offset": new_offset, "reset": reset})
            return
        if u.path == "/api/presence":
            q = parse_qs(u.query)
            client_id = (q.get("id") or [""])[0]
            bye = (q.get("bye") or ["0"])[0] in ("1", "true")
            self._json({"ok": True, "clients": presence_update(client_id, bye)})
            return
        if u.path == "/api/client_state":
            n = presence_count()
            self._json({"clients": n, "open": n > 0})
            return
        if u.path == "/api/send_status":
            q = parse_qs(u.query)
            name = (q.get("name") or [""])[0]
            if not (MSG_NAME_RE.match(name) or REQ_NAME_RE.match(name)):
                self._json({"error": "bad name", "error_code": "bad_name"}, 400)
                return
            done = OUTBOX_DIR / "done" / name
            if done.exists():
                # Consume the record before responding so a second poll can
                # never observe the same done payload.
                try:
                    payload = json.loads(done.read_text(encoding="utf-8", errors="replace"))
                finally:
                    done.unlink(missing_ok=True)
                self._json(payload)
                return
            # Inspectable pending states: the bridge annotates the requeued
            # record with "state" so the UI can say *why* it is still waiting.
            queued = OUTBOX_DIR / name
            if queued.is_file():
                rec = {}
                with contextlib.suppress(OSError, ValueError):
                    rec = json.loads(queued.read_text(encoding="utf-8", errors="replace"))
                if not isinstance(rec, dict):
                    rec = {}
                # Same expiry rule the bridge applies: expire_ts when the
                # requester set one, else the 24h queued-message bound — a
                # record past it resolves as expired instead of polling
                # forever while the bridge sweeps it.
                queued_ts = rec.get("ts")
                expire_ts = rec.get("expire_ts")
                if not isinstance(expire_ts, (int, float)) and isinstance(
                    queued_ts, (int, float)
                ):
                    expire_ts = queued_ts + OUTBOX_MSG_MAX_AGE_SEC
                if isinstance(expire_ts, (int, float)) and time.time() > expire_ts:
                    self._json(
                        {
                            "pending": False,
                            "ok": False,
                            "state": "expired",
                            "error": "request expired in queue; no bridge instance served it",
                            "error_code": "expired",
                        },
                        404,
                    )
                    return
                self._json(
                    {
                        "pending": True,
                        "state": rec.get("state") or "queued",
                        "attempts": rec.get("attempts") or 0,
                        "queued_at": rec.get("ts"),
                    },
                    404,
                )
                return
            if any(OUTBOX_DIR.glob(name + ".*.claim")):
                self._json({"pending": True, "state": "delivering"}, 404)
                return
            self._json(
                {
                    "pending": False,
                    "ok": False,
                    "state": "missing",
                    "error": "message is no longer queued and no result was recorded",
                    "error_code": "missing",
                },
                404,
            )
            return
        self._json({"error": "not found", "error_code": "not_found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/send":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 1 << 20)) or b"{}")
            except Exception:
                self._json({"ok": False, "error": "bad request", "error_code": "bad_request"}, 400)
                return
            session = str(payload.get("session") or "")
            text = str(payload.get("text") or "").strip()
            if not SAFE_ID.match(session):
                self._json({"ok": False, "error": "bad session", "error_code": "bad_session"}, 400)
                return
            if not text or len(text) > 20000:
                self._json({"ok": False, "error": "empty or too long", "error_code": "empty_or_too_long"}, 400)
                return
            state = load_state()
            known = {s.get("session_id") for s in state.get("sessions", [])}
            if session not in known:
                self._json({"ok": False, "error": "unknown session", "error_code": "unknown_session"}, 404)
                return
            OUTBOX_DIR.mkdir(exist_ok=True)
            name = f"msg_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.json"
            tmp = OUTBOX_DIR / (name + ".tmp")
            tmp.write_text(
                json.dumps(
                    {"session_id": session, "message": text, "ts": time.time()},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, OUTBOX_DIR / name)
            self._json({"ok": True, "name": name})
            return
        if u.path == "/api/dequeue":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 1 << 20)) or b"{}")
            except Exception:
                self._json({"ok": False, "error": "bad request", "error_code": "bad_request"}, 400)
                return
            name = str(payload.get("name") or "")
            # Strict name whitelist — the record can only ever resolve inside
            # outbox/, so a crafted name can never unlink an arbitrary path.
            if not MSG_NAME_RE.match(name):
                self._json({"ok": False, "error": "bad name", "error_code": "bad_name"}, 400)
                return
            src = OUTBOX_DIR / name
            done_path = OUTBOX_DIR / "done" / name
            # Claim through the same atomic rename the bridge uses — whoever
            # wins the rename owns the record. A dashboard crash between claim
            # and delete leaves a *.claim the bridge rescues back into the
            # queue, so a dequeue is never half-applied.
            claim = OUTBOX_DIR / _dash_claim_name(name)
            try:
                os.replace(src, claim)
            except OSError:
                # Lost the rename race — the bridge claimed it, it already
                # delivered, or it never existed. Report the live truth.
                if done_path.exists():
                    self._json(
                        {
                            "ok": False,
                            "error": "message already dispatched",
                            "error_code": "dispatched",
                        },
                        409,
                    )
                elif any(OUTBOX_DIR.glob(name + ".*.claim")):
                    self._json(
                        {
                            "ok": False,
                            "error": "message is mid-delivery and cannot be recalled",
                            "error_code": "delivering",
                        },
                        409,
                    )
                elif src.is_file():
                    self._json(
                        {
                            "ok": False,
                            "error": "queued message could not be removed",
                            "error_code": "dequeue_failed",
                        },
                        500,
                    )
                else:
                    self._json(
                        {
                            "ok": False,
                            "error": "message is no longer queued",
                            "error_code": "missing",
                        },
                        404,
                    )
                return
            try:
                rec = json.loads(claim.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                rec = None
            if not isinstance(rec, dict):
                # Unreadable payload: hand the record back to the queue
                # untouched rather than destroying data the user may want.
                with contextlib.suppress(OSError):
                    os.replace(claim, src)
                if claim.exists():
                    claim.unlink(missing_ok=True)
                self._json(
                    {
                        "ok": False,
                        "error": "invalid queued message record",
                        "error_code": "invalid_record",
                    },
                    409,
                )
                return
            claim.unlink(missing_ok=True)
            self._json(
                {
                    "ok": True,
                    "name": name,
                    "session_id": rec.get("session_id"),
                    "message": rec.get("message"),
                }
            )
            return
        if u.path == "/api/task_action":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(min(length, 1 << 20)) or b"{}")
            except Exception:
                self._json({"ok": False, "error": "bad request", "error_code": "bad_request"}, 400)
                return
            action = str(payload.get("action") or "")
            task_id = str(payload.get("task_id") or "")
            session_id = str(payload.get("session_id") or "")
            if action not in ("pause", "cancel", "resume"):
                self._json(
                    {
                        "ok": False,
                        "error": "action must be pause, cancel, or resume",
                        "error_code": "bad_action",
                    },
                    400,
                )
                return
            if not SAFE_ID.match(task_id):
                self._json({"ok": False, "error": "bad task_id", "error_code": "bad_task"}, 400)
                return
            if session_id and not SAFE_ID.match(session_id):
                self._json({"ok": False, "error": "bad session", "error_code": "bad_session"}, 400)
                return
            state = load_state()
            row = next(
                (
                    t
                    for t in state.get("tasks", [])
                    if isinstance(t, dict) and t.get("task_id") == task_id
                ),
                None,
            )
            if row is None:
                self._json(
                    {"ok": False, "error": f"unknown task {task_id}", "error_code": "unknown_task"},
                    404,
                )
                return
            if session_id and row.get("session_id") != session_id:
                # The page resolves the action against the session's latest
                # task — a mismatch means the row moved on (resumed or
                # superseded) between the overview read and the click, and
                # refusing is safer than acting on a task the user did not
                # intend.
                self._json(
                    {
                        "ok": False,
                        "error": "task belongs to another session",
                        "error_code": "wrong_session",
                    },
                    409,
                )
                return
            OUTBOX_DIR.mkdir(exist_ok=True)
            name = f"req_{action}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.json"
            record = {
                "kind": action,
                "task_id": task_id,
                "session_id": row.get("session_id"),
                "ts": time.time(),
                # Bounded queue age: a request no bridge instance can serve
                # inside the window resolves as expired rather than firing a
                # stale pause/cancel on a task that has long since moved on.
                "expire_ts": time.time() + TASK_ACTION_EXPIRE_SEC,
                "requester_pid": os.getpid(),
                "requester_create_time": _my_create_time(),
            }
            if action == "resume":
                record["request_id"] = str(uuid.uuid4())
                record["message"] = None
            tmp = OUTBOX_DIR / (name + ".tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, OUTBOX_DIR / name)
            self._json({"ok": True, "name": name, "task_id": task_id, "action": action})
            return
        # navigator.sendBeacon uses POST
        if u.path == "/api/presence":
            q = parse_qs(u.query)
            client_id = (q.get("id") or [""])[0]
            bye = (q.get("bye") or ["0"])[0] in ("1", "true")
            self._json({"ok": True, "clients": presence_update(client_id, bye)})
            return
        self._json({"error": "not found", "error_code": "not_found"}, 404)


class DashboardServer(ThreadingHTTPServer):
    """HTTP server that never shares its listen port.

    Windows treats SO_REUSEADDR as "bind alongside the existing listener", so
    a second dashboard could coexist with a split /api/presence table and
    report clients:0 while tabs heartbeat into the other process. Leaving the
    option off plus SO_EXCLUSIVEADDRUSE fails the second bind in both
    directions. On Unix SO_REUSEADDR only covers TIME_WAIT rebinding and the
    kernel already rejects dual listeners, so the default stays.
    """

    allow_reuse_address = os.name != "nt"

    if os.name == "nt":

        def server_bind(self):
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--dir", default=None, help="Agent Bridge data directory")
    args = ap.parse_args()
    global BRIDGE_DIR, STATE_FILE, TRANSCRIPT_DIR, OUTBOX_DIR
    if args.dir:
        BRIDGE_DIR = Path(args.dir).resolve()
        STATE_FILE = BRIDGE_DIR / "state.json"
        TRANSCRIPT_DIR = BRIDGE_DIR / "transcripts"
        OUTBOX_DIR = BRIDGE_DIR / "outbox"
    try:
        srv = DashboardServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print(f"Agent Bridge dashboard: cannot bind 127.0.0.1:{args.port}: {exc}")
        print("Another dashboard is already serving this port; not starting a second one.")
        raise SystemExit(1) from exc
    print(f"Agent Bridge dashboard → http://127.0.0.1:{args.port}")
    print(f"data dir: {BRIDGE_DIR}")
    with contextlib.suppress(KeyboardInterrupt):
        srv.serve_forever()


if __name__ == "__main__":
    main()
