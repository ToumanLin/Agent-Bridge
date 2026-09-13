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

from agent_bridge import dashboard as dashboard_launcher
from agent_bridge.share import dashboard

PAGE = dashboard.PAGE


def test_launcher_prefers_bundled_dashboard_over_stale_home_copy(tmp_path, monkeypatch):
    bundled = tmp_path / "package" / "dashboard.py"
    bundled.parent.mkdir()
    bundled.write_text("# bundled", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    (home / "dashboard.py").write_text("# stale", encoding="utf-8")
    launched = []
    monkeypatch.setattr(dashboard_launcher, "bundled_dashboard", lambda: bundled)
    monkeypatch.setattr(dashboard_launcher.subprocess, "Popen", lambda args, **kwargs: launched.append(args))

    assert dashboard_launcher._launch(home, "127.0.0.1", 8787)
    assert launched[0][1] == str(bundled)


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
    {
        "type": "prompt_sent",
        "ts": "2026-09-12T10:00:01Z",
        "data": {"text": "Smoke test only.", "source": "dashboard", "task_id": "t1"},
    },
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
    {
        "type": "usage",
        "ts": "2026-09-12T10:00:05Z",
        "data": {
            "update_type": "UsageUpdate",
            "consumed": {
                "scope": "run",
                "quality": "exact",
                "input": 25,
                "output": 9,
                "total": 34,
                "streams": 1,
                "used": 8,
                "size": 100,
            },
            "usage": {"input": 25, "output": 9, "used": 8, "size": 100},
        },
    },
    {
        "type": "turn_end",
        "ts": "2026-09-12T10:00:06Z",
        "data": {"stop_reason": "end_turn", "task_id": "t1"},
    },
    {"type": "error", "ts": "2026-09-12T10:00:07Z", "data": {"error": "stalled once"}},
    {"type": "raw", "ts": "2026-09-12T10:00:08Z", "data": {"text": "dropped"}},
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
                "created_at": "2026-09-12T10:00:00Z",
                "started_at": "2026-09-12T10:00:02Z",
                "finished_at": "2026-09-12T10:01:42Z",
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
    for dom_id in (
        "sidebar",
        "sesslist",
        "sesshead",
        "content",
        "chatinput",
        "chatsend",
        "chatstatus",
        "live",
        "backtop",
    ):
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
    # Centralized, immutable proc_state -> {label, tone} table; statusOf only
    # applies the dead latest-task Failed/Done override on top of it.
    m = re.search(r"const PROC_STATUS=Object\.freeze\(\{([\s\S]*?)\}\)", PAGE)
    assert m, "PROC_STATUS freeze map missing"
    body = m.group(1)
    for entry in (
        'busy:{label:"Running",tone:"running"}',
        'spawning:{label:"Starting",tone:"running"}',
        'ready:{label:"Ready",tone:"neutral"}',
        'idle_unloaded:{label:"Idle",tone:"neutral"}',
    ):
        assert entry in body
    assert '"Waiting"' not in PAGE
    assert "PROC_STATUS[st]" in PAGE
    dead = re.search(r'if\(st==="dead"\)\{([\s\S]*?)\}', PAGE).group(1)
    assert "latestTask" in dead and '"failed"' in dead
    # Executable coverage of all six state/result scenarios (and the duration
    # rules) lives in tests/dashboard_status_behavior.js, run by
    # test_dashboard_status_behavior_node below.


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
    for token in (
        "--text",
        "--dim",
        "--panel",
        "--panel2",
        "--panel3",
        "--border",
        "--accent",
        "--accent-tint",
        "--green",
        "--amber",
        "--red",
        "--on-accent",
        "--shadow",
    ):
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
    for needle in (
        'role="radio"',
        "aria-checked",
        "tabIndex",
        "ArrowRight",
        'matchMedia("(prefers-color-scheme: dark)")',
        'addEventListener("change"',
        '"storage"',
        "themePref",
        "applyTheme",
        "setThemePref",
    ):
        assert needle in PAGE


def test_theme_control_compact_layout():
    # Sidebar footer is a single compact row: label inline, horizontal icon+label radios.
    foot = re.search(r"\.side-foot\{([^}]*)\}", PAGE).group(1)
    assert "display:flex" in foot and "align-items:center" in foot
    # ~33px total: 4px vertical padding + ~24px radio + 1px border. Pin the
    # compact values so the footer can't creep back to ~43px.
    assert "padding:4px 12px" in foot and "gap:8px" in foot
    label = re.search(r"\.side-foot-label\{([^}]*)\}", PAGE).group(1)
    assert "margin:0" in label and "display:block" not in label
    btn = re.search(r'\.theme-options \[role="radio"\]\{([^}]*)\}', PAGE).group(1)
    assert "flex-direction:row" in btn and "flex-direction:column" not in btn
    # Radios stay ~24px tall (3px padding + 1px border each side) with readable
    # 11px text — a reasonable click target, not a shrunken strip.
    assert "padding:3px 4px" in btn and "font-size:11px" in btn
    assert re.search(r'\.theme-options \[role="radio"\]:focus-visible\{[^}]*outline:2px', PAGE)
    # Icons still injected ahead of each radio label from the existing icon set.
    assert "THEME_ICONS" in PAGE and "insertAdjacentHTML" in PAGE


def test_themed_scrollbars():
    # Standard properties (Firefox + modern Chromium/Safari) applied to every scroller.
    assert "scrollbar-width:thin" in PAGE
    sb = re.search(r"scrollbar-color:([^;}]+)", PAGE).group(1)
    assert "--dimmer" in sb and "transparent" in sb  # theme-aware thumb, invisible track
    # Chromium/WebKit pseudo-elements mirror the same tokens.
    for needle in (
        "::-webkit-scrollbar{",
        "::-webkit-scrollbar-track",
        "::-webkit-scrollbar-thumb",
        "::-webkit-scrollbar-thumb:hover",
    ):
        assert needle in PAGE
    # Chromium/WebKit scrollbars are pinned at exactly 6px.
    assert "::-webkit-scrollbar{width:6px;height:6px}" in PAGE
    # Thumb/track still resolve through theme tokens, not fixed colors.
    thumb = re.search(r"::-webkit-scrollbar-thumb\{([^}]*)\}", PAGE).group(1)
    assert "--dimmer" in thumb


def test_task_duration_plumbing():
    # Task timing fields are shipped and rendered next to the status label at
    # both sites (sidebar row + session header) as "<label> · <dur>".
    for needle in ("taskDur", "fmtDur", "durText", "durSpan", "tickDurations", '"sdur"', '"hdur"', "data-tid", '" · "'):
        assert needle in PAGE
    assert re.search(r"setInterval\(tickDurations,1000\)", PAGE)
    body = re.search(r"function taskDur\(t\)\{([\s\S]*?)\n\}", PAGE).group(1)
    # The clock starts strictly at a valid started_at — never created_at,
    # queue time, or session age.
    assert "Date.parse(t.started_at)" in body
    assert "created_at" not in body and "last_active_at" not in body
    # Only queued/running may be live; terminal statuses require finished_at.
    assert 't.status==="queued"||t.status==="running"' in body
    assert "Date.parse(t.finished_at)" in body
    # Whole-second floor math; the old Math.round/padStart version is gone.
    fmt = re.search(r"const fmtDur=([\s\S]*?)\nfunction taskDur", PAGE).group(1)
    assert "Math.floor" in fmt and "Math.round" not in fmt and "padStart" not in fmt
    # Live-ticking text must not join the sidebar signature (it would force a
    # #sesslist rebuild every second); only static task timestamps may.
    sig = re.search(r'const sig=([\s\S]*?)join\("\|"\)', PAGE).group(1)
    assert "Date.now" not in sig and "fmtDur" not in sig
    assert "started_at" in sig and "finished_at" in sig


def test_subagent_seed_avatars():
    # Codex-style seeded gradient avatars replace the provider glyph at both
    # icon sites; light/dark variants are resolved by CSS, not re-renders.
    for needle in (
        "SUBAV",
        "function subSeed(",
        "function agentAvatar(",
        "data:image/svg+xml,",
        "subav-wrap",
        "subav-l",
        "subav-d",
        'aria-hidden="true"',
        'draggable="false"',
    ):
        assert needle in PAGE
    assert re.search(r'\[data-theme="dark"\] \.subav-l\{display:none\}', PAGE)
    assert re.search(r'\[data-theme="dark"\] \.subav-d\{display:block\}', PAGE)
    # The gradient <img> itself stays static — no is-working class anywhere.
    assert "is-working" not in PAGE


def test_subagent_busy_pulse_not_ring():
    """The busy indicator is the avatar icon itself scale-pulsing with the
    Codex working-dot timing — never a rotating ring/arc overlay."""
    # The old rotating arc overlay is gone entirely.
    assert "subav-arc" not in PAGE
    assert "subav_arc" not in PAGE
    body = re.search(r"function agentAvatar\(s,size\)\{([\s\S]*?)\n\}", PAGE).group(1)
    # Busy is the only proc_state that animates; spawning stays static.
    assert 'proc_state==="busy"' in body
    assert "pulse" in body and "spin" not in body and "arc" not in body
    # Codex token-pulsing-dot timing: 1.25s ease-in-out scale 1 -> 1.25 -> 1.
    pulse = re.search(r"\.subav-wrap\.pulse\{([^}]*)\}", PAGE)
    assert pulse, "pulse rule missing"
    rule = pulse.group(1)
    assert "animation:ui-pulse 1.25s ease-in-out infinite" in rule
    assert "transform-origin:50%" in rule
    assert "will-change:transform" in rule
    assert "backface-visibility:hidden" in rule
    kf = re.search(r"@keyframes ui-pulse\{([\s\S]*?)\}\}", PAGE).group(1)
    assert "scale(1)" in kf and "scale(1.25)" in kf
    assert "rotate" not in kf and "translate" not in kf
    # Reduced motion still disables the pulse.
    rm = re.search(r"prefers-reduced-motion:reduce\)\{([\s\S]*?)\n\}", PAGE).group(1)
    assert ".subav-wrap.pulse{animation:none}" in rm


def test_header_icon_has_no_tint_block():
    avatar = re.search(r"\.avatar\{([^}]*)\}", PAGE)
    assert avatar, "avatar rule missing"
    rule = avatar.group(1)
    assert "background" not in rule and "accent-tint" not in rule
    # Icon slot stays sized and centered.
    assert "width:46px" in rule and "height:46px" in rule
    assert "align-items:center" in rule and "justify-content:center" in rule


def test_dark_theme_is_neutral_gray_black():
    dark = re.search(r'\[data-theme="dark"\]\{([^}]*)\}', PAGE).group(1)
    # The old GitHub-blue dark palette is gone.
    for gone in ("#161b22", "#1c2330", "#21262d", "#58a6ff", "#79b8ff", "#1f2e41", "#0d1117"):
        assert gone not in dark
    # Neutral gray-black panels + gray accent.
    for needle in (
        "--panel:#0f1113",
        "--panel2:#16181b",
        "--panel3:#1e2125",
        "--border:#292d32",
        "--accent:#8b949e",
        "--accent-tint:#23262b",
    ):
        assert needle in dark
    # Semantic colors survive.
    for token in ("--green:", "--amber:", "--red:"):
        assert token in dark
    # No bluish hardcoded backdrop tint either.
    assert "rgba(20,30,50" not in PAGE


def test_user_message_card_and_send_status():
    # Dashboard-authored prompts render a distinct "User Message" card;
    # coordinator prompts keep "Dispatched Message".
    assert '"User Message"' in PAGE or "User Message" in PAGE
    assert "Dispatched Message" in PAGE
    assert 'e.src==="dashboard"' in PAGE
    assert re.search(r"\.card\.prompt\.user\{[^}]*accent-tint", PAGE)
    # Honest send states: queued/waiting/delivering/dispatched — never "sent"
    # for a bare outbox enqueue, never "delivered" for a task the adapter may
    # still fail, and no fixed ~300s poll cap.
    for needle in ("waiting_busy", "waiting_owner", "delivering", "dispatched", "queued"):
        assert needle in PAGE
    assert "sent ✓" not in PAGE
    assert '"delivered' not in PAGE and "`delivered" not in PAGE
    assert "still queued" not in PAGE
    assert "sendState" in PAGE and "pollSendStatus" in PAGE


def test_turn_end_shows_duration():
    # turn-end dividers join tasks by task_id for the real task duration and
    # fall back to the preceding prompt timestamp on legacy transcripts.
    for needle in ("turnDurMs", "lastPromptTs", "e.task", "taskDur(tk)"):
        assert needle in PAGE
    # The redundant "turn ended · end_turn" wording is gone: end_turn is
    # implied, other stop reasons still render.
    assert 'e.stop_reason!=="end_turn"' in PAGE
    body = re.search(r"function addTurn\(e\)\{([\s\S]*?)\n\}", PAGE).group(1)
    # Visible text is exactly "turn ended · <dur>" (+reason when abnormal);
    # the wall-clock timestamp lives in the tooltip, not the divider text.
    assert 'parts=["turn ended"]' in body
    assert "parts.push(fmtTs" not in body and "d.title" in body
    # A still-running task's interval ends at the turn ts — validated by
    # Number.isFinite so a bad timestamp yields no invented duration.
    dur = re.search(r"function turnDurMs\(e,promptTs\)\{([\s\S]*?)\n\}", PAGE).group(1)
    assert "Date.parse(e.ts)" in dur and "Date.now()" not in dur
    assert "Number.isFinite(end)" in dur


def test_token_counter_helpers():
    for needle in (
        "usageNums",
        "tokCount",
        "runTok",
        "tokTitle",
        "fmtTok",
        "tokSpan",
        "liveUsage",
        '"stok"',
        '"htok"',
        "cognition.ai/inputTokens",
        "cognition.ai/cachedReadTokens",
        "cognition.ai/outputTokens",
        '"input_tokens"',
        '"inputTokens"',
        " tok",
    ):
        assert needle in PAGE
    # The sidebar signature fingerprints run_usage (preferred), legacy usage,
    # and the live usage-event snapshot so a finishing/running turn repaints.
    sig = re.search(r'const sig=([\s\S]*?)join\("\|"\)', PAGE).group(1)
    assert "t.run_usage" in sig and "t.usage" in sig and "liveUsage" in sig
    # tokSpan prefers the persisted per-run aggregate over the raw last
    # snapshot; legacy usage is only an estimate fallback.
    body = re.search(r"function tokSpan\(t,cls,live\)\{([\s\S]*?)\n\}", PAGE).group(1)
    assert "t.run_usage" in body and "runTok" in body
    assert 'quality:"estimate"' in body
    # Usage text stays static between overview polls — not joined to the tick.
    tick = re.search(r"function tickDurations\(\)\{([\s\S]*?)\n\}", PAGE).group(1)
    assert "stok" not in tick and "htok" not in tick


def test_perf_architecture_hooks():
    # Batch flush: chunk handlers mark blocks dirty, never re-render per chunk.
    assert "const dirty=new Set()" in PAGE
    assert "function flushBlocks()" in PAGE
    assert "dirty.add(" in PAGE
    msg = re.search(r"function addMsg\(e\)\{([^}]*)", PAGE).group(1)
    assert "md(" not in msg and "innerHTML" not in msg
    # Chunked replay with per-session abort token + rAF slicing: a reset for
    # one session must not cancel another session's in-flight replay.
    for needle in ("replayGen[id]", "requestAnimationFrame", "startReplay", "performance.now()", "replaying===id"):
        assert needle in PAGE
    # Per-session pane stash + bounded caches.
    for needle in (
        "function stashPane(",
        "function dropPane(",
        "function dropCache(",
        "paneLru",
        "cacheLru",
        "MAX_PANES",
        "MAX_CACHE",
        "rendered",
    ):
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dashboard_status_behavior_node():
    """Executable behavior coverage for the page's status/duration helpers.

    Runs the real inline <script> from PAGE inside a stubbed node vm context —
    no jsdom or npm packages — and exercises PROC_STATUS/statusOf, latestTask
    chronology, and the taskDur/fmtDur/durText rules end to end.
    """
    script = Path(__file__).parent / "dashboard_status_behavior.js"
    r = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(_jsdom_available() is None, reason="node/jsdom not installed")
def test_dashboard_replay_benchmark():
    """Executable check: chunked replay, batch flush, warm stash, sidebar sig.

    Runs scripts/bench_dashboard_replay.js against synthetic event streams
    under jsdom. Skips cleanly where node or jsdom is unavailable.
    """
    script = Path(__file__).parent.parent / "scripts" / "bench_dashboard_replay.js"
    env = {**os.environ, "NODE_PATH": _jsdom_available()}
    r = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=120, env=env)
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
        "source",
        "usage",
        "run_usage",
        "created_at",
        "started_at",
        "finished_at",
    }
    # task timing fields round-trip so the page can compute working durations
    assert j["tasks"][0]["started_at"] == "2026-09-12T10:00:02Z"
    assert j["tasks"][0]["finished_at"] == "2026-09-12T10:01:42Z"


def test_events_normalized(dash):
    _, base = dash
    code, body = _get(base + "/api/events?session=sess_1&offset=0")
    assert code == 200
    j = json.loads(body)
    kinds = [e["t"] for e in j["events"]]
    assert kinds == ["prompt", "think", "msg", "tool", "tool_status", "usage", "turn", "error"]
    usage = next(e for e in j["events"] if e["t"] == "usage")
    # Only the normalized consumed snapshot reaches the client — never the
    # raw provider _meta bag or stream/subagent routing keys.
    assert usage["consumed"]["total"] == 34
    assert "_meta" not in json.dumps(usage) and "stream" not in usage["consumed"]
    assert j["offset"] > 0 and j["reset"] is False
    tool = next(e for e in j["events"] if e["t"] == "tool")
    assert tool["kind"] == "execute" and "pwd" in tool["title"]
    # prompt_sent carries the dispatch origin + task id through normalize_event
    prompt = j["events"][0]
    assert prompt["src"] == "dashboard" and prompt["task"] == "t1"
    turn = next(e for e in j["events"] if e["t"] == "turn")
    assert turn["task"] == "t1" and turn["stop_reason"] == "end_turn"
    err = j["events"][-1]
    assert err["t"] == "error" and "stalled once" in err["text"]
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
    j = json.loads(body)
    assert code == 404 and j["pending"] is True and j["state"] == "queued"
    # A requeued (busy/foreign-waiting) message reports the bridge's state.
    rec = json.loads((home / "outbox" / name).read_text(encoding="utf-8"))
    rec.update({"attempts": 3, "state": "waiting_busy"})
    (home / "outbox" / name).write_text(json.dumps(rec), encoding="utf-8")
    code, body = _get(base + f"/api/send_status?name={name}")
    j = json.loads(body)
    assert code == 404 and j["pending"] is True
    assert j["state"] == "waiting_busy" and j["attempts"] == 3
    # An in-flight claim reports "delivering"; a missing file is terminal.
    (home / "outbox" / name).rename(home / "outbox" / f"{name}.4242.claim")
    code, body = _get(base + f"/api/send_status?name={name}")
    j = json.loads(body)
    assert code == 404 and j["pending"] is True and j["state"] == "delivering"
    (home / "outbox" / f"{name}.4242.claim").unlink()
    code, body = _get(base + f"/api/send_status?name={name}")
    j = json.loads(body)
    assert code == 404 and j["pending"] is False and j["state"] == "missing"
    # A done record is returned verbatim and consumed.
    (home / "outbox" / "done").mkdir(exist_ok=True)
    done_payload = {"ok": True, "state": "dispatched", "task_id": "task_1", "session_id": "sess_1"}
    (home / "outbox" / "done" / name).write_text(json.dumps(done_payload), encoding="utf-8")
    code, body = _get(base + f"/api/send_status?name={name}")
    assert code == 200 and json.loads(body) == done_payload
    assert not (home / "outbox" / "done" / name).exists()
    # The record is consumed before the response is written, so a follow-up
    # poll can never observe the same done payload twice.
    code, body = _get(base + f"/api/send_status?name={name}")
    j = json.loads(body)
    assert code == 404 and j["state"] == "missing"
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
