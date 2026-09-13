"""Focused contract tests for the embedded dashboard UI and its HTTP surface.

The dashboard page is a single inline ``PAGE`` string in
``agent_bridge.share.dashboard``. These tests pin the behaviors the UI redesign
must preserve (folds, endpoints, escaping hooks, DOM ids, icon policy) plus a
small live-server smoke over the API — not a brittle markup snapshot.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_bridge.share import dashboard

PAGE = dashboard.PAGE

SESSION = {
    "session_id": "sess_1",
    "title": "Researcher",
    "agent": "devin",
    "model": "swe-2-medium",
    "effort": "medium",
    "cwd": "C:\\repo\\CharacterViewer.Unity",
    "proc_state": "busy",
    "turns": 2,
    "created_at": "2026-09-12T10:00:00Z",
    "last_active_at": "2026-09-12T10:05:00Z",
    "pid": 1234,
}

TRANSCRIPT = [
    {"type": "prompt_sent", "ts": "2026-09-12T10:00:01Z", "data": {"text": "Smoke test only."}},
    {"type": "thought_chunk", "ts": "2026-09-12T10:00:02Z", "data": {"text": "thinking about it"}},
    {"type": "message_chunk", "ts": "2026-09-12T10:00:03Z", "data": {"text": "Hello **world**"}},
    {
        "type": "tool_call",
        "ts": "2026-09-12T10:00:04Z",
        "data": {
            "tool_call_id": "tc1",
            "kind": "execute",
            "title": "Ran pwd, ls",
            "input": {"command": "pwd"},
        },
    },
    {
        "type": "tool_call_update",
        "ts": "2026-09-12T10:00:05Z",
        "data": {"tool_call_id": "tc1", "status": "completed"},
    },
    {"type": "turn_end", "ts": "2026-09-12T10:00:06Z", "data": {"stop_reason": "end_turn"}},
    {"type": "raw", "ts": "2026-09-12T10:00:07Z", "data": {"text": "dropped"}},
]


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.fixture
def dash(tmp_path, monkeypatch):
    """Run the dashboard Handler against a throwaway bridge data dir."""
    home = tmp_path / "bridge"
    (home / "transcripts").mkdir(parents=True)
    (home / "outbox").mkdir()
    state = {
        "sessions": [SESSION, {**SESSION, "session_id": "sess_2", "proc_state": "dead"}],
        "tasks": [
            {
                "task_id": "t1",
                "session_id": "sess_2",
                "agent": "devin",
                "status": "failed",
                "message": "boom",
            }
        ],
    }
    (home / "state.json").write_text(json.dumps(state), encoding="utf-8")
    lines = "".join(json.dumps(r) + "\n" for r in TRANSCRIPT)
    (home / "transcripts" / "sess_1.jsonl").write_text(lines, encoding="utf-8")
    monkeypatch.setattr(dashboard, "STATE_FILE", home / "state.json")
    monkeypatch.setattr(dashboard, "TRANSCRIPT_DIR", home / "transcripts")
    monkeypatch.setattr(dashboard, "OUTBOX_DIR", home / "outbox")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield home, f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


# ---------- PAGE contract ----------


def test_page_dom_ids_and_endpoints():
    for dom_id in ("sidebar", "sesslist", "sesshead", "content", "chatinput", "chatsend", "chatstatus", "live", "backtop"):
        assert f'id="{dom_id}"' in PAGE
    for ep in ("/api/overview", "/api/events", "/api/send", "/api/send_status", "/api/presence"):
        assert ep in PAGE
    assert "sendBeacon" in PAGE  # presence heartbeat on pagehide


def test_folds_stay_closed_initially():
    # Thinking and tool-input sections are created as <details> without `open`.
    assert 'curThink=document.createElement("details")' in PAGE
    assert "curThink.open" not in PAGE
    assert 'det.className="tooldetail"' in PAGE
    assert "det.open=!det.open" in PAGE  # row click toggles
    # prompt show-more folding preserved
    assert "clamp" in PAGE and 'className="expand"' in PAGE


def test_no_attachment_control_or_emoji_icons():
    assert "attach" not in PAGE.lower()
    assert "💭" not in PAGE
    assert "paper-plane" not in PAGE.lower()


def test_inline_svg_icon_policy():
    assert "function icon(" in PAGE
    assert 'aria-hidden="true"' in PAGE
    assert 'vb:"0 0 500 500"' in PAGE  # Devin brand mark viewBox
    assert "#2A6DCE" in PAGE
    assert "M3 12h18m-9-9l9 9-9 9" in PAGE  # RightArrow send glyph
    assert 'class="spin"' in PAGE or "spin()" in PAGE  # CSS spinner, no svg spinner


def test_status_mapping_labels():
    for label in ('"Running"', '"Starting"', '"Waiting"', '"Done"', '"Failed"'):
        assert label in PAGE
    for state in ("busy", "spawning", "ready", "idle_unloaded", "dead"):
        assert state in PAGE


def test_accessibility_and_responsive_hooks():
    for needle in (
        'role="listbox"',
        'role="option"',
        "aria-selected",
        "aria-current",
        "aria-live",
        "aria-label",
        ":focus-visible",
        "prefers-reduced-motion",
        "max-width:900px",
        "<button",
    ):
        assert needle in PAGE
    # light Ref theme tokens
    assert "#3366cc" in PAGE
    assert "Inter" in PAGE


def test_no_outer_frame_chrome():
    # The .app fills the viewport directly — no floating card/backdrop frame.
    for gone in (
        "padding:22px",
        "max-width:1440px",
        "border-radius:14px",
        "0 4px 24px rgba(30,50,80,.08)",
        "--bg:#eef2f7",
        "body{padding:0}",
    ):
        assert gone not in PAGE
    body = re.search(r"body\{([^}]*)\}", PAGE).group(1)
    assert "padding" not in body and "justify-content" not in body
    app = re.search(r"\.app\{([^}]*)\}", PAGE).group(1)
    assert "width:100%" in app
    assert "max-width" not in app and "border" not in app and "box-shadow" not in app
    # content measure is kept for readability
    assert re.search(r"\.block\{[^}]*max-width:960px", PAGE)


def test_theme_bootstrap_before_styles():
    # Synchronous head script resolves data-theme before the stylesheet is seen.
    head = PAGE.split("<style>")[0]
    assert "data-theme" in head and '"ab-theme"' in head
    assert "localStorage" in head and "prefers-color-scheme" in head
    assert PAGE.index("data-theme") < PAGE.index("<style>")
    for pref in ('"system"', '"light"', '"dark"'):
        assert pref in PAGE


def test_theme_dark_tokens_and_color_scheme():
    dark = re.search(r'\[data-theme="dark"\]\{([^}]*)\}', PAGE)
    assert dark, "dark token block missing"
    body = dark.group(1)
    for token in ("--text", "--dim", "--panel", "--panel2", "--panel3", "--border",
                  "--accent", "--accent-tint", "--green", "--amber", "--red",
                  "--on-accent", "--shadow"):
        assert token + ":" in body
    assert "color-scheme:dark" in body
    assert "color-scheme:light" in PAGE
    # send button contrast rides the token in both themes
    assert re.search(r"#chatsend\{[^}]*color:var\(--on-accent\)", PAGE)
    assert re.search(r"#backtop\{[^}]*box-shadow:var\(--shadow\)", PAGE)


def test_theme_radiogroup_control():
    assert 'role="radiogroup"' in PAGE
    assert 'aria-labelledby="themelbl"' in PAGE
    for pref in ("system", "light", "dark"):
        assert f'data-theme-pref="{pref}"' in PAGE
    for needle in ('role="radio"', "aria-checked", "tabIndex", "ArrowRight",
                   'matchMedia("(prefers-color-scheme: dark)")',
                   'addEventListener("change"', '"storage"', "themePref",
                   "applyTheme", "setThemePref"):
        assert needle in PAGE


def test_perf_architecture_hooks():
    # Batch flush: chunk handlers mark blocks dirty, never re-render per chunk.
    assert "const dirty=new Set()" in PAGE
    assert "function flushBlocks()" in PAGE
    assert "dirty.add(" in PAGE
    msg = re.search(r"function addMsg\(e\)\{([^}]*)", PAGE).group(1)
    assert "md(" not in msg and "innerHTML" not in msg
    # Chunked replay with per-session abort token + rAF slicing: a reset for
    # one session must not cancel another session's in-flight replay.
    for needle in ("replayGen[id]", "requestAnimationFrame", "startReplay",
                   "performance.now()", "replaying===id"):
        assert needle in PAGE
    # Per-session pane stash + bounded caches.
    for needle in ("function stashPane(", "function dropPane(", "function dropCache(",
                   "paneLru", "cacheLru", "MAX_PANES", "MAX_CACHE", "rendered"):
        assert needle in PAGE
    # Sidebar render signature gates the 3s overview poll rebuild.
    assert "lastSidebarSig" in PAGE
    # Contracts kept
    for needle in ("eventsCache", "offsets", "applyEvents", "renderSidebar"):
        assert needle in PAGE


def _jsdom_available():
    """Locate a jsdom install for the optional DOM benchmark."""
    node = shutil.which("node")
    if not node:
        return None
    for cand in (
        Path(os.environ["NODE_PATH"]) / "jsdom" if os.environ.get("NODE_PATH") else None,
        Path(__file__).parent / "node_modules" / "jsdom",
        Path.home() / "node_modules" / "jsdom",
    ):
        if cand and (cand / "package.json").exists():
            return str(cand.parent)
    return None


@pytest.mark.skipif(_jsdom_available() is None, reason="node/jsdom not installed")
def test_dashboard_replay_benchmark():
    """Executable check: chunked replay, batch flush, warm stash, sidebar sig.

    Runs scripts/bench_dashboard_replay.js against synthetic event streams
    under jsdom. Skips cleanly where node or jsdom is unavailable.
    """
    script = Path(__file__).parent.parent / "scripts" / "bench_dashboard_replay.js"
    env = {**os.environ, "NODE_PATH": _jsdom_available()}
    r = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=120, env=env
    )
    assert r.returncode == 0, r.stdout + r.stderr


# ---------- HTTP surface ----------


def test_index_and_overview(dash):
    _, base = dash
    code, body = _get(base + "/")
    assert code == 200
    assert b'id="sesslist"' in body and b'id="sesshead"' in body
    code, body = _get(base + "/api/overview")
    assert code == 200
    j = json.loads(body)
    assert {s["session_id"] for s in j["sessions"]} == {"sess_1", "sess_2"}
    assert j["tasks"][0]["status"] == "failed"
    assert set(j["tasks"][0]) <= {
        "task_id",
        "session_id",
        "agent",
        "status",
        "stop_reason",
        "message",
        "result_chars",
        "files_changed",
        "error",
    }


def test_events_normalized(dash):
    _, base = dash
    code, body = _get(base + "/api/events?session=sess_1&offset=0")
    assert code == 200
    j = json.loads(body)
    kinds = [e["t"] for e in j["events"]]
    assert kinds == ["prompt", "think", "msg", "tool", "tool_status", "turn"]
    assert j["offset"] > 0 and j["reset"] is False
    tool = next(e for e in j["events"] if e["t"] == "tool")
    assert tool["kind"] == "execute" and "pwd" in tool["title"]
    code, _ = _get(base + "/api/events?session=../evil&offset=0")
    assert code == 400


def test_send_flow_and_status(dash):
    home, base = dash
    code, body = _post(base + "/api/send", {"session": "sess_1", "text": "hello agent"})
    assert code == 200
    name = json.loads(body)["name"]
    assert re.fullmatch(r"msg_\d+_[0-9a-f]{8}\.json", name)
    queued = json.loads((home / "outbox" / name).read_text(encoding="utf-8"))
    assert queued["session_id"] == "sess_1" and queued["message"] == "hello agent"
    code, body = _get(base + f"/api/send_status?name={name}")
    assert code == 404 and json.loads(body)["pending"] is True
    code, body = _post(base + "/api/send", {"session": "nope", "text": "x"})
    assert code == 404
    code, body = _post(base + "/api/send", {"session": "sess_1", "text": "  "})
    assert code == 400


def test_presence_and_client_state(dash):
    _, base = dash
    code, body = _get(base + "/api/presence?id=testclient")
    assert code == 200 and json.loads(body)["clients"] >= 1
    code, body = _get(base + "/api/client_state")
    assert code == 200
    j = json.loads(body)
    assert j["clients"] >= 1 and j["open"] is True
    code, body = _get(base + "/api/presence?id=testclient&bye=1")
    assert code == 200
